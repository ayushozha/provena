import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import {
  cpSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { ProvenaClient } from "../dist/client.js";
import { runIndex } from "../dist/indexer/run.js";
import {
  findStoreRoot,
  resolvePythonRunner,
} from "../dist/process.js";

const cliRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(cliRoot, "..");
const fixtureRepo = join(cliRoot, "fixtures", "relations-repo");

function findFreePort() {
  return new Promise((resolvePromise, rejectPromise) => {
    const server = net.createServer();
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close(() => rejectPromise(new Error("no free port")));
        return;
      }
      const { port } = address;
      server.close(() => resolvePromise(port));
    });
    server.on("error", rejectPromise);
  });
}

async function waitForHealth(storeUrl, child, attempts = 60) {
  const client = new ProvenaClient({ storeUrl, timeoutMs: 2000 });
  for (let i = 0; i < attempts; i += 1) {
    if (child.exitCode !== null) {
      return false;
    }
    if (await client.healthz()) {
      return true;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

async function startStore(port, dbPath) {
  const storeRoot = findStoreRoot(repoRoot);
  if (!storeRoot) {
    throw new Error("cannot find Python store (app/main.py)");
  }

  mkdirSync(dirname(dbPath), { recursive: true });

  const runner = resolvePythonRunner(storeRoot);
  const args = [
    ...runner.prefixArgs,
    "-m",
    "uvicorn",
    "app.main:app",
    "--host",
    "127.0.0.1",
    "--port",
    String(port),
  ];

  const useShell =
    process.platform === "win32" &&
    (runner.command === "uv" ||
      runner.command === "python" ||
      runner.command === "python3");

  const child = spawn(runner.command, args, {
    cwd: runner.storeRoot,
    env: { ...process.env, PROVENA_DB_PATH: dbPath },
    stdio: ["ignore", "pipe", "pipe"],
    shell: useShell,
    detached: process.platform !== "win32",
  });

  child.stderr?.on("data", (chunk) => {
    const line = chunk.toString();
    if (line.includes("ERROR") || line.includes("Traceback")) {
      process.stderr.write(line);
    }
  });

  return child;
}

function initProjectFromFixture(projectRoot, storeUrl) {
  cpSync(fixtureRepo, projectRoot, { recursive: true });
  mkdirSync(join(projectRoot, ".provena"), { recursive: true });
  const config = {
    version: 1,
    backend: "sqlite",
    database: { path: ".provena/provena.db" },
    store_url: storeUrl,
    scope: {
      tenant_id: "incremental-test",
      project_id: "relations-repo",
    },
    index: {
      include: ["**/*"],
      exclude: ["node_modules/**", ".git/**"],
    },
  };
  writeFileSync(
    join(projectRoot, ".provena", "config.json"),
    `${JSON.stringify(config, null, 2)}\n`,
    "utf8",
  );
  return config;
}

async function main() {
  const projectRoot = mkdtempSync(join(tmpdir(), "provena-incremental-"));
  const dbPath = join(projectRoot, ".provena", "provena.db");
  const port = await findFreePort();
  const storeUrl = `http://127.0.0.1:${port}`;
  const config = initProjectFromFixture(projectRoot, storeUrl);

  const child = await startStore(port, dbPath);
  let exitCode = 0;

  try {
    const healthy = await waitForHealth(storeUrl, child);
    assert.equal(healthy, true, "store /healthz did not become ready");

    const first = await runIndex(config, projectRoot, { cwd: projectRoot });
    assert.equal(first.exitCode, 0, "first index should succeed");
    assert.ok(first.summary.filesIndexed >= 3, "first index should process fixture files");
    assert.ok(first.summary.filesAdded >= 3, "first index should treat files as added");

    const started = Date.now();
    const second = await runIndex(config, projectRoot, { cwd: projectRoot });
    const elapsedMs = Date.now() - started;

    assert.equal(second.exitCode, 0, "unchanged re-index should succeed");
    assert.equal(second.summary.filesIndexed, 0, "unchanged re-index should skip all files");
    assert.equal(
      second.summary.filesAdded + second.summary.filesChanged + second.summary.filesRemoved,
      0,
      "unchanged re-index should report zero deltas",
    );
    assert.ok(second.summary.filesUnchanged >= 3, "unchanged files should be counted");
    assert.ok(elapsedMs < 5000, `unchanged re-index should be fast (${elapsedMs}ms)`);

    const authPath = join(projectRoot, "src", "auth.ts");
    const stateBeforeReplacement = JSON.parse(
      readFileSync(join(projectRoot, ".provena", "index-state.json"), "utf8"),
    );
    const oldAuthMemoryId = stateBeforeReplacement.chunks["src/auth.ts::authenticate"];
    assert.ok(oldAuthMemoryId, "precondition: authenticate memory should be indexed");
    writeFileSync(
      authPath,
      readFileSync(authPath, "utf8").replace('name: "tester"', 'name: "replacement-user"'),
      "utf8",
    );

    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (input, init) => {
      const url = typeof input === "string" ? input : input.url;
      if (url.endsWith("/v1/admin/entities/batch")) {
        return new Response('{"detail":"forced replacement failure"}', {
          status: 500,
          headers: { "Content-Type": "application/json" },
        });
      }
      return originalFetch(input, init);
    };
    let failedReplacement;
    try {
      failedReplacement = await runIndex(config, projectRoot, { cwd: projectRoot });
    } finally {
      globalThis.fetch = originalFetch;
    }

    assert.equal(failedReplacement.exitCode, 1, "forced replacement should fail");
    assert.equal(failedReplacement.summary.filesFailed, 1);
    const stateAfterFailure = JSON.parse(
      readFileSync(join(projectRoot, ".provena", "index-state.json"), "utf8"),
    );
    assert.equal(
      stateAfterFailure.files["src/auth.ts"].sha256,
      stateBeforeReplacement.files["src/auth.ts"].sha256,
      "failed replacement must preserve the prior local index entry",
    );
    const client = new ProvenaClient({ storeUrl });
    const oldSearch = await client.searchMemories({
      query: "tester",
      scope: {
        tenant_id: config.scope.tenant_id,
        project_id: config.scope.project_id,
      },
      limit: 10,
    });
    assert.ok(
      oldSearch.results.some((result) => result.memory.memory_id === oldAuthMemoryId),
      "failed replacement must leave the prior store memory searchable",
    );

    globalThis.fetch = async (input, init) => {
      const url = typeof input === "string" ? input : input.url;
      if (
        init?.method === "DELETE" &&
        url.includes(`/v1/memories/${encodeURIComponent(oldAuthMemoryId)}`)
      ) {
        return new Response('{"detail":"forced cleanup failure"}', {
          status: 500,
          headers: { "Content-Type": "application/json" },
        });
      }
      return originalFetch(input, init);
    };
    let failedCleanup;
    try {
      failedCleanup = await runIndex(config, projectRoot, { cwd: projectRoot });
    } finally {
      globalThis.fetch = originalFetch;
    }
    assert.equal(failedCleanup.exitCode, 1, "failed obsolete-memory cleanup should fail the file");
    assert.equal(failedCleanup.summary.filesFailed, 1);
    const stateAfterCleanupFailure = JSON.parse(
      readFileSync(join(projectRoot, ".provena", "index-state.json"), "utf8"),
    );
    assert.equal(
      stateAfterCleanupFailure.files["src/auth.ts"].sha256,
      stateBeforeReplacement.files["src/auth.ts"].sha256,
      "cleanup failure must retain the prior local index entry for retry",
    );
    const oldSearchAfterCleanupFailure = await client.searchMemories({
      query: "tester",
      scope: {
        tenant_id: config.scope.tenant_id,
        project_id: config.scope.project_id,
      },
      limit: 10,
    });
    assert.ok(
      oldSearchAfterCleanupFailure.results.some(
        (result) => result.memory.memory_id === oldAuthMemoryId,
      ),
      "cleanup failure must not forget a still-live obsolete memory",
    );

    const third = await runIndex(config, projectRoot, { cwd: projectRoot });
    assert.equal(third.exitCode, 0, "single-file change index should succeed");
    assert.equal(third.summary.filesChanged, 1, "one changed file expected");
    assert.equal(third.summary.filesIndexed, 1, "only changed file should be re-indexed");

    const stateAfterEdit = JSON.parse(
      readFileSync(join(projectRoot, ".provena", "index-state.json"), "utf8"),
    );
    assert.ok(stateAfterEdit.files["src/auth.ts"]?.sha256, "auth.ts sha256 should be tracked");
    const newAuthMemoryId = stateAfterEdit.chunks["src/auth.ts::authenticate"];
    assert.notEqual(newAuthMemoryId, oldAuthMemoryId, "changed function needs a new memory");
    const testMemoryId = stateAfterEdit.chunks["src/auth.test.ts::import"];
    const authArtifactId = stateAfterEdit.files["src/auth.ts"].artifactMemoryId;
    assert.ok(
      stateAfterEdit.relations[`${testMemoryId}|derived_from|${authArtifactId}`],
      "an unchanged test import must be rewired to the changed implementation artifact",
    );
    await assert.rejects(
      () => client.getMemory(oldAuthMemoryId),
      /failed \(404\)/,
      "obsolete generated memory should be hard-deleted after successful replacement",
    );

    writeFileSync(
      authPath,
      readFileSync(authPath, "utf8").replace("replacement-user", "Replacement-User"),
      "utf8",
    );
    const caseOnly = await runIndex(config, projectRoot, { cwd: projectRoot });
    assert.equal(caseOnly.exitCode, 0, "case-only source change should succeed");
    assert.equal(caseOnly.summary.filesChanged, 1);
    const stateAfterCaseOnly = JSON.parse(
      readFileSync(join(projectRoot, ".provena", "index-state.json"), "utf8"),
    );
    const caseChangedAuthMemoryId = stateAfterCaseOnly.chunks["src/auth.ts::authenticate"];
    assert.notEqual(
      caseChangedAuthMemoryId,
      newAuthMemoryId,
      "generated memory identity must preserve source case",
    );
    assert.ok(
      stateAfterCaseOnly.relations[
        `${testMemoryId}|derived_from|${stateAfterCaseOnly.files["src/auth.ts"].artifactMemoryId}`
      ],
      "incoming import edge must survive a case-only target update",
    );

    const typesPath = join(projectRoot, "src", "types.ts");
    writeFileSync(typesPath, `// shifted source span\n${readFileSync(typesPath, "utf8")}`, "utf8");
    globalThis.fetch = async (input, init) => {
      const url = typeof input === "string" ? input : input.url;
      const body = typeof init?.body === "string" ? init.body : "";
      if (url.endsWith("/v1/memories/relations") && body.includes('"relation":"derived_from"')) {
        return new Response('{"detail":"forced cross-file relation failure"}', {
          status: 500,
          headers: { "Content-Type": "application/json" },
        });
      }
      return originalFetch(input, init);
    };
    let failedCrossFile;
    try {
      failedCrossFile = await runIndex(config, projectRoot, { cwd: projectRoot });
    } finally {
      globalThis.fetch = originalFetch;
    }
    assert.equal(failedCrossFile.exitCode, 1, "cross-file edge failure must fail the run");
    assert.ok(failedCrossFile.summary.filesFailed >= 1);
    const stateAfterCrossFailure = JSON.parse(
      readFileSync(join(projectRoot, ".provena", "index-state.json"), "utf8"),
    );
    assert.equal(
      stateAfterCrossFailure.files["src/auth.ts"].sha256,
      "",
      "failed unchanged importer must be marked for retry",
    );

    const crossRetry = await runIndex(config, projectRoot, { cwd: projectRoot });
    assert.equal(crossRetry.exitCode, 0, "cross-file edge retry should succeed");
    const stateAfterCrossRetry = JSON.parse(
      readFileSync(join(projectRoot, ".provena", "index-state.json"), "utf8"),
    );
    const importMemoryId = stateAfterCrossRetry.chunks["src/auth.ts::import"];
    const typesArtifactId = stateAfterCrossRetry.files["src/types.ts"].artifactMemoryId;
    assert.ok(
      stateAfterCrossRetry.relations[`${importMemoryId}|derived_from|${typesArtifactId}`],
      "unchanged importer must point to the changed target after retry",
    );

    unlinkSync(join(projectRoot, "src", "types.ts"));

    const fourth = await runIndex(config, projectRoot, { cwd: projectRoot });
    assert.equal(fourth.exitCode, 0, "delete path index should succeed");
    assert.equal(fourth.summary.filesRemoved, 1, "deleted file should be removed from index");

    const stateAfterDelete = JSON.parse(
      readFileSync(join(projectRoot, ".provena", "index-state.json"), "utf8"),
    );
    assert.equal(stateAfterDelete.files["src/types.ts"], undefined, "types.ts entry should be purged");

    const fifth = await runIndex(config, projectRoot, { cwd: projectRoot, full: true });
    assert.equal(fifth.exitCode, 0, "--full re-index should succeed");
    assert.ok(fifth.summary.fullReindex, "summary should record full re-index");
    assert.ok(fifth.summary.filesIndexed >= 2, "--full should re-process remaining files");

    console.log("incremental.test: ok");
  } catch (error) {
    exitCode = 1;
    console.error(error);
  } finally {
    await new Promise((resolvePromise) => {
      if (child.exitCode !== null) {
        resolvePromise();
        return;
      }
      child.once("exit", () => resolvePromise());
      if (process.platform === "win32") {
        spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
          stdio: "ignore",
        });
      } else if (child.pid) {
        try {
          process.kill(-child.pid, "SIGTERM");
        } catch {
          child.kill("SIGTERM");
        }
      }
      setTimeout(resolvePromise, 3000);
    });
    try {
      rmSync(projectRoot, { recursive: true, force: true });
    } catch {
      // Windows may keep SQLite handles briefly after kill
    }
    process.exit(exitCode);
  }
}

await main();
