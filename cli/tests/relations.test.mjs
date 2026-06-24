import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { ProvenaClient } from "../dist/client.js";
import { chunkTypeScriptFile } from "../dist/indexer/chunkers/typescript.js";
import { emitMemories } from "../dist/indexer/emit.js";
import {
  emitRelations,
  implementationPathForTest,
} from "../dist/indexer/relations.js";
import {
  parseImportSpecifier,
  resolveImportSpecifier,
} from "../dist/indexer/resolve-import.js";
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
    runner.command === "uv" ||
    runner.command === "python" ||
    runner.command === "python3";

  const child = spawn(runner.command, args, {
    cwd: runner.storeRoot,
    env: { ...process.env, PROVENA_DB_PATH: dbPath },
    stdio: ["ignore", "pipe", "pipe"],
    shell: useShell,
  });

  child.stderr?.on("data", (chunk) => {
    const line = chunk.toString();
    if (line.includes("ERROR") || line.includes("Traceback")) {
      process.stderr.write(line);
    }
  });

  return child;
}

function listFixtureFiles(rootDir) {
  const files = [];
  function walk(dir) {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry);
      const stat = statSync(full);
      if (stat.isDirectory()) {
        walk(full);
      } else if (/\.tsx?$/.test(entry)) {
        files.push(full);
      }
    }
  }
  walk(rootDir);
  return files.sort((a, b) => a.localeCompare(b));
}

function runResolveImportUnitTests() {
  assert.equal(parseImportSpecifier('User from "./types"'), "./types");
  assert.equal(parseImportSpecifier('"./types"'), "./types");
  assert.equal(parseImportSpecifier('createHash from "node:crypto"'), "node:crypto");

  const indexed = new Set([
    "src/types.ts",
    "src/auth.ts",
    "src/auth.test.ts",
  ]);

  assert.equal(
    resolveImportSpecifier("src/auth.ts", "./types", indexed),
    "src/types.ts",
  );
  assert.equal(
    resolveImportSpecifier("src/auth.test.ts", "./auth", indexed),
    "src/auth.ts",
  );
  assert.equal(
    resolveImportSpecifier("src/auth.ts", "node:crypto", indexed),
    null,
  );

  assert.equal(
    implementationPathForTest("src/auth.test.ts"),
    "src/auth.ts",
  );
  assert.equal(implementationPathForTest("src/auth.ts"), null);

  console.log("relations.test: resolve-import unit ok");
}

async function runIntegrationTests() {
  const projectRoot = mkdtempSync(join(tmpdir(), "provena-relations-int-"));
  const dbPath = join(projectRoot, ".provena", "provena.db");
  const port = await findFreePort();
  const storeUrl = `http://127.0.0.1:${port}`;

  const config = {
    version: 1,
    backend: "sqlite",
    database: { path: ".provena/provena.db" },
    store_url: storeUrl,
    scope: {
      tenant_id: "relations-test",
      project_id: "relations-test",
    },
    index: {
      include: ["**/*"],
      exclude: ["node_modules/**"],
    },
  };

  mkdirSync(join(projectRoot, ".provena"), { recursive: true });
  writeFileSync(
    join(projectRoot, ".provena", "config.json"),
    `${JSON.stringify(config, null, 2)}\n`,
    "utf8",
  );

  const child = await startStore(port, dbPath);
  let exitCode = 0;

  try {
    const healthy = await waitForHealth(storeUrl, child);
    assert.equal(
      healthy,
      true,
      `store /healthz did not become ready (exit=${child.exitCode})`,
    );

    const client = new ProvenaClient({ storeUrl });
    const emitOpts = {
      storeUrl,
      projectRoot,
      repoRoot: fixtureRepo,
      client,
    };
    const relationOpts = emitOpts;

    const indexedFiles = [];
    for (const absolutePath of listFixtureFiles(fixtureRepo)) {
      const relativePath = relative(fixtureRepo, absolutePath)
        .split("\\")
        .join("/");
      const source = readFileSync(absolutePath, "utf8");
      const chunks = await chunkTypeScriptFile(relativePath, source);
      indexedFiles.push({
        relativePath,
        absolutePath,
        chunks,
      });
      await emitMemories(
        chunks,
        { path: relativePath, absolutePath, sha256: `sha-${relativePath}` },
        config.scope,
        emitOpts,
      );
    }

    for (const file of indexedFiles) {
      await emitRelations(
        file.chunks,
        {
          path: file.relativePath,
          absolutePath: file.absolutePath,
          sha256: `sha-${file.relativePath}`,
        },
        config.scope,
        relationOpts,
      );
    }

    const indexState = JSON.parse(
      readFileSync(join(projectRoot, ".provena", "index-state.json"), "utf8"),
    );

    const authArtifactId = indexState.files["src/auth.ts"].artifactMemoryId;
    assert.ok(authArtifactId, "auth artifact memory id missing");

    const authFnId = indexState.chunks["src/auth.ts::authenticate"];
    const userStoreId = indexState.chunks["src/auth.ts::UserStore"];
    const findByIdId = indexState.chunks["src/auth.ts::findById"];
    assert.ok(authFnId, "authenticate chunk memory missing");
    assert.ok(userStoreId, "UserStore chunk memory missing");
    assert.ok(findByIdId, "findById chunk memory missing");

    assert.ok(
      indexState.relations[`${authFnId}|defined_in|${authArtifactId}`],
      "defined_in fingerprint missing for authenticate",
    );
    assert.ok(
      indexState.relations[`${findByIdId}|related_to|${userStoreId}`],
      "related_to fingerprint missing for method -> class",
    );

    const importChunkId = indexState.chunks["src/auth.ts::import"];
    const typesArtifactId = indexState.files["src/types.ts"].artifactMemoryId;
    if (importChunkId && typesArtifactId) {
      assert.ok(
        indexState.relations[`${importChunkId}|derived_from|${typesArtifactId}`],
        "derived_from fingerprint missing for import -> types",
      );
    }

    const testFnId = indexState.chunks["src/auth.test.ts::authenticate"];
    if (testFnId && authFnId) {
      assert.ok(
        indexState.relations[`${testFnId}|supports|${authFnId}`],
        "supports fingerprint missing for test -> impl",
      );
    }

    const firstRunRelationCount = Object.keys(indexState.relations).length;

    for (const file of indexedFiles) {
      const rerun = await emitRelations(
        file.chunks,
        {
          path: file.relativePath,
          absolutePath: file.absolutePath,
          sha256: `sha-${file.relativePath}`,
        },
        config.scope,
        relationOpts,
      );
      assert.equal(
        rerun.created,
        0,
        `re-run created relations for ${file.relativePath}`,
      );
    }

    const indexStateAfter = JSON.parse(
      readFileSync(join(projectRoot, ".provena", "index-state.json"), "utf8"),
    );
    assert.equal(
      Object.keys(indexStateAfter.relations).length,
      firstRunRelationCount,
      "relation count changed on re-run",
    );

    const search = await client.searchMemories({
      query: "auth.ts",
      scope: {
        tenant_id: config.scope.tenant_id,
        project_id: config.scope.project_id,
      },
      entity_keys: ["file:src/auth.ts"],
      include_relations: true,
      limit: 10,
    });

    const fileHit = search.results.find(
      (result) => result.memory.memory_id === authArtifactId,
    );
    assert.ok(fileHit, "search did not return auth file artifact");

    const relatedIds = new Set(
      (fileHit.related_memories ?? []).map((related) => related.memory.memory_id),
    );
    assert.ok(
      relatedIds.has(authFnId),
      "file artifact graph query missing authenticate child",
    );
    const definedInEdge = (fileHit.related_memories ?? []).find(
      (related) =>
        related.memory.memory_id === authFnId &&
        related.relation === "defined_in",
    );
    assert.ok(definedInEdge, "authenticate relation kind should be defined_in");

    console.log("relations.test: integration ok");
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
      } else {
        child.kill("SIGTERM");
      }
      setTimeout(resolvePromise, 3000);
    });
    try {
      rmSync(projectRoot, { recursive: true, force: true });
    } catch {
      // Windows may keep SQLite handles briefly after kill
    }
    if (exitCode !== 0) {
      process.exit(exitCode);
    }
  }
}

runResolveImportUnitTests();
await runIntegrationTests();