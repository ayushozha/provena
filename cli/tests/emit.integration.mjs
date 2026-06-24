import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { emitMemories } from "../dist/indexer/emit.js";
import { ProvenaClient } from "../dist/client.js";
import {
  findStoreRoot,
  resolvePythonRunner,
} from "../dist/process.js";

const cliRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(cliRoot, "..");
const fixturePath = join(cliRoot, "fixtures", "emit-sample.ts");
const fixtureSource = readFileSync(fixturePath, "utf8");

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

function buildChunks(relativePath) {
  const functionBody = `export function authenticate(user: string): boolean {
  return user.length > 0;
}`;

  return [
    {
      filePath: relativePath,
      language: "typescript",
      kind: "module",
      name: null,
      startLine: 1,
      endLine: 5,
      content: fixtureSource,
      imports: [],
      exported: false,
    },
    {
      filePath: relativePath,
      language: "typescript",
      kind: "function",
      name: "authenticate",
      startLine: 2,
      endLine: 4,
      content: functionBody,
      exported: true,
    },
  ];
}

async function main() {
  const projectRoot = mkdtempSync(join(tmpdir(), "provena-emit-int-"));
  const dbPath = join(projectRoot, ".provena", "provena.db");
  const port = await findFreePort();
  const storeUrl = `http://127.0.0.1:${port}`;

  const config = {
    version: 1,
    backend: "sqlite",
    database: { path: ".provena/provena.db" },
    store_url: storeUrl,
    scope: {
      tenant_id: "emit-test",
      project_id: "emit-test",
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

    const relativePath = "fixtures/emit-sample.ts";
    const absolutePath = fixturePath;
    const chunks = buildChunks(relativePath);

    const emitResult = await emitMemories(
      chunks,
      { path: relativePath, absolutePath, sha256: "test-sha" },
      config.scope,
      { storeUrl, projectRoot, repoRoot: cliRoot },
    );

    assert.ok(emitResult.memoryIds.length >= 2, "expected artifact + fact IDs");
    assert.ok(emitResult.created >= 2, "expected new memories");

    const indexState = JSON.parse(
      readFileSync(join(projectRoot, ".provena", "index-state.json"), "utf8"),
    );
    assert.ok(indexState.files[relativePath], "index-state missing file entry");
    assert.deepEqual(
      indexState.files[relativePath].memoryIds,
      emitResult.memoryIds,
    );

    const client = new ProvenaClient({ storeUrl });
    const search = await client.searchMemories({
      query: "function authenticate",
      scope: {
        tenant_id: config.scope.tenant_id,
        project_id: config.scope.project_id,
      },
      limit: 10,
    });

    const hit = search.results.find((result) =>
      result.memory.content.includes("authenticate"),
    );
    assert.ok(hit, "search did not return authenticate chunk content");
    assert.equal(hit.memory.kind, "fact");
    assert.equal(hit.memory.title, `${relativePath}::authenticate`);

    const rerun = await emitMemories(
      chunks,
      { path: relativePath, absolutePath, sha256: "test-sha" },
      config.scope,
      { storeUrl, projectRoot, repoRoot: cliRoot },
    );
    assert.equal(rerun.skipped, emitResult.memoryIds.length, "dedup should skip");
    assert.equal(rerun.created, 0, "dedup should not create duplicates");

    console.log("emit.integration: ok");
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