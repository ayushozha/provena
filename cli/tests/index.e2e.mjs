import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import {
  cpSync,
  existsSync,
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

function initProjectFromFixture(projectRoot, storeUrl) {
  cpSync(fixtureRepo, projectRoot, { recursive: true });
  mkdirSync(join(projectRoot, ".provena"), { recursive: true });
  const config = {
    version: 1,
    backend: "sqlite",
    database: { path: ".provena/provena.db" },
    store_url: storeUrl,
    scope: {
      tenant_id: "index-e2e",
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
  const projectRoot = mkdtempSync(join(tmpdir(), "provena-index-e2e-"));
  const dbPath = join(projectRoot, ".provena", "provena.db");
  const port = await findFreePort();
  const storeUrl = `http://127.0.0.1:${port}`;
  const config = initProjectFromFixture(projectRoot, storeUrl);

  const child = await startStore(port, dbPath);
  let exitCode = 0;

  try {
    const healthy = await waitForHealth(storeUrl, child);
    assert.equal(healthy, true, "store /healthz did not become ready");

    const dryRun = await runIndex(config, projectRoot, {
      dryRun: true,
      cwd: projectRoot,
    });
    assert.equal(dryRun.exitCode, 0, "dry-run should succeed");
    assert.ok(dryRun.summary.filesDiscovered >= 3, "expected TS fixture files");

    const pathScoped = await runIndex(config, projectRoot, {
      dryRun: true,
      pathPrefix: "src/auth.ts",
      cwd: projectRoot,
    });
    assert.equal(pathScoped.summary.filesDiscovered, 1, "--path should scope to one file");

    const indexed = await runIndex(config, projectRoot, { cwd: projectRoot });
    assert.equal(indexed.exitCode, 0, "index run should succeed");
    assert.ok(indexed.summary.filesIndexed >= 3, "expected indexed files");
    assert.ok(indexed.summary.memoriesCreated > 0, "expected created memories");
    assert.ok(indexed.summary.relationsCreated > 0, "expected relations");

    assert.ok(
      existsSync(join(projectRoot, ".provena", "index-state.json")),
      "index-state.json missing",
    );
    const indexState = JSON.parse(
      readFileSync(join(projectRoot, ".provena", "index-state.json"), "utf8"),
    );
    assert.ok(indexState.files["src/auth.ts"], "auth.ts should be indexed");

    assert.ok(
      existsSync(join(projectRoot, ".provena", "last-index.json")),
      "last-index.json missing",
    );
    const lastIndex = JSON.parse(
      readFileSync(join(projectRoot, ".provena", "last-index.json"), "utf8"),
    );
    assert.equal(lastIndex.filesIndexed, indexed.summary.filesIndexed);

    const client = new ProvenaClient({ storeUrl });
    const search = await client.searchMemories({
      query: "authenticate",
      scope: {
        tenant_id: config.scope.tenant_id,
        project_id: config.scope.project_id,
      },
      limit: 10,
    });
    const hit = search.results.find((result) =>
      result.memory.content.includes("authenticate"),
    );
    assert.ok(hit, "search should return authenticate chunk after index");

    console.log("index.e2e: ok");
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
    process.exit(exitCode);
  }
}

await main();