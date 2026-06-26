import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import {
  appendFileSync,
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
    appendFileSync(authPath, "\n// incremental-test-edit\n", "utf8");

    const third = await runIndex(config, projectRoot, { cwd: projectRoot });
    assert.equal(third.exitCode, 0, "single-file change index should succeed");
    assert.equal(third.summary.filesChanged, 1, "one changed file expected");
    assert.equal(third.summary.filesIndexed, 1, "only changed file should be re-indexed");

    const stateAfterEdit = JSON.parse(
      readFileSync(join(projectRoot, ".provena", "index-state.json"), "utf8"),
    );
    assert.ok(stateAfterEdit.files["src/auth.ts"]?.sha256, "auth.ts sha256 should be tracked");

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