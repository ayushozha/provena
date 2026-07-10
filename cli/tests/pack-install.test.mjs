/**
 * Simulates a fresh consumer: npm pack → install tarball → run --version.
 * Does not hit the npm registry (plan 26 pre-publish gate).
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  appendFileSync,
  existsSync,
  lstatSync,
  mkdtempSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const cliRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const npmCli =
  process.env.npm_execpath ??
  join(dirname(process.execPath), "node_modules", "npm", "bin", "npm-cli.js");

function run(command, args, cwd) {
  return spawnSync(command === "npm" ? process.execPath : command, command === "npm" ? [npmCli, ...args] : args, {
    cwd,
    encoding: "utf8",
    shell: false,
  });
}

function processAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function waitForExit(pids, attempts = 20) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (![...pids].some(processAlive)) return true;
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 250);
  }
  return ![...pids].some(processAlive);
}

const pack = run("npm", ["pack", "--silent"], cliRoot);
assert.equal(pack.status, 0, pack.stderr || pack.stdout);

const tarball = readdirSync(cliRoot).find((f) => f.endsWith(".tgz"));
assert.ok(tarball, "npm pack did not create a tarball");

const scratch = mkdtempSync(join(tmpdir(), "provena-pack-install-"));
const cliBin = join(scratch, "node_modules", "@provena", "cli", "dist", "cli.js");
const daemonPids = new Set();
let testError;
try {
  run("npm", ["init", "-y"], scratch);
  assert.equal(run("git", ["init", "--quiet"], scratch).status, 0);
  writeFileSync(join(scratch, ".gitignore"), "node_modules/\ndist/\n", "utf8");
  mkdirSync(join(scratch, "src"), { recursive: true });
  writeFileSync(
    join(scratch, "src", "index.ts"),
    "export function authenticate(user: string) { return Boolean(user); }\n",
    "utf8",
  );
  const consumerPackage = JSON.parse(readFileSync(join(scratch, "package.json"), "utf8"));
  consumerPackage.description = "Packed-install repo brain fixture";
  consumerPackage.scripts = { test: "node --test", build: "tsc" };
  writeFileSync(join(scratch, "package.json"), `${JSON.stringify(consumerPackage, null, 2)}\n`);

  const install = run("npm", ["install", join(cliRoot, tarball)], scratch);
  assert.equal(install.status, 0, install.stderr || install.stdout);

  const pkgJson = JSON.parse(
    readFileSync(join(scratch, "package.json"), "utf8"),
  );
  assert.ok(
    pkgJson.dependencies?.["@provena/cli"] || pkgJson.devDependencies?.["@provena/cli"],
    "tarball install did not add @provena/cli",
  );

  const versionRun = spawnSync(process.execPath, [cliBin, "--version"], {
    cwd: scratch,
    encoding: "utf8",
  });
  assert.equal(versionRun.status, 0, versionRun.stderr);
  assert.match(versionRun.stdout.trim(), /^0\.1\.\d+$/);

  const helpRun = spawnSync(process.execPath, [cliBin, "--help"], {
    cwd: scratch,
    encoding: "utf8",
  });
  assert.equal(helpRun.status, 0, helpRun.stderr);
  for (const cmd of ["init", "refresh", "context", "remember", "graph", "mcp"]) {
    assert.ok(helpRun.stdout.includes(cmd), `help missing ${cmd}`);
  }

  const initRun = run(process.execPath, [cliBin, "init", "--no-daemon"], scratch);
  assert.equal(initRun.status, 0, initRun.stderr || initRun.stdout);
  assert.match(initRun.stdout, /Ready:/);
  for (const path of [
    ".provena/config.json",
    ".provena/repo.brain.md",
    ".provena/repo.map.json",
    ".provena/graph.json",
    ".provena/manifest.json",
    ".provena/memory/events.jsonl",
    ".provena/views/decisions.md",
    ".provena/agent-instructions.md",
    ".provena/runtime/runtime.mjs",
    "AGENTS.md",
    "CLAUDE.md",
    ".cursor/rules/provena-memory.mdc",
    ".cursor/mcp.json",
    ".codex/config.toml",
    ".mcp.json",
  ]) {
    assert.ok(existsSync(join(scratch, ...path.split("/"))), `init missing ${path}`);
  }
  const ignore = readFileSync(join(scratch, ".gitignore"), "utf8");
  assert.match(ignore, /^node_modules\/$/m, "preserves consumer ignore rules");
  assert.match(ignore, /^\.provena\/\*$/m, "unknown and local .provena state is ignored");
  assert.match(ignore, /^!\.provena\/agent-instructions\.md$/m);
  assert.doesNotMatch(ignore, /^\.provena\/$/m, "tracked brain is not hidden");
  for (const durable of [
    ".provena/config.json",
    ".provena/repo.brain.md",
    ".provena/repo.map.json",
    ".provena/graph.json",
    ".provena/manifest.json",
    ".provena/memory/events.jsonl",
    ".provena/schema/memory-event.schema.json",
    ".provena/views/decisions.md",
    ".provena/agent-instructions.md",
  ]) {
    assert.notEqual(
      run("git", ["check-ignore", "--quiet", durable], scratch).status,
      0,
      `${durable} must remain trackable`,
    );
  }

  const statusRun = run(process.execPath, [cliBin, "status", "--json"], scratch);
  assert.equal(statusRun.status, 0, statusRun.stderr);
  const status = JSON.parse(statusRun.stdout);
  assert.equal(status.current, true);
  assert.equal(status.integrations.portableRuntime, true);
  assert.equal(status.integrations.agents, true);
  assert.equal(status.integrations.mcp, true);

  appendFileSync(join(scratch, ".provena", "memory", "events.jsonl"), "\n", "utf8");
  const staleMemoryStatus = run(process.execPath, [cliBin, "status", "--json"], scratch);
  assert.equal(staleMemoryStatus.status, 0, staleMemoryStatus.stderr);
  assert.equal(
    JSON.parse(staleMemoryStatus.stdout).current,
    false,
    "ledger changes must make derived views and manifest stale",
  );
  assert.equal(run(process.execPath, [cliBin, "refresh", "--quiet"], scratch).status, 0);

  const contextRun = run(
    process.execPath,
    [cliBin, "context", "authentication", "--json", "--max-tokens", "256"],
    scratch,
  );
  assert.equal(contextRun.status, 0, contextRun.stderr);
  assert.ok(
    JSON.parse(contextRun.stdout).items.some((item) => item.citations.some((citation) => citation.path === "src/index.ts")),
    "context packet cites the relevant source file",
  );

  const rememberRun = run(
    process.execPath,
    [
      cliBin,
      "remember",
      "decision",
      "Keep authentication explicit",
      "--body",
      "Authentication behavior stays explicit and source-cited.",
      "--source",
      "src/index.ts:1",
      "--tag",
      "architecture",
      "--authority",
      "human",
    ],
    scratch,
  );
  assert.equal(rememberRun.status, 0, rememberRun.stderr);
  assert.match(readFileSync(join(scratch, ".provena", "views", "decisions.md"), "utf8"), /Keep authentication explicit/);

  const graphRun = run(process.execPath, [cliBin, "graph", "stats", "--json"], scratch);
  assert.equal(graphRun.status, 0, graphRun.stderr);
  const graphStats = JSON.parse(graphRun.stdout);
  assert.ok(graphStats.nodes > 0);
  assert.ok(graphStats.edges > 0);

  const sessionRun = run(
    process.execPath,
    [cliBin, "session", "start", "authentication", "--agent", "codex", "--quiet"],
    scratch,
  );
  assert.equal(sessionRun.status, 0, sessionRun.stderr);

  const firstRefresh = run(process.execPath, [cliBin, "refresh", "--json"], scratch);
  assert.equal(firstRefresh.status, 0, firstRefresh.stderr);
  const secondRefresh = run(process.execPath, [cliBin, "refresh", "--json"], scratch);
  assert.equal(secondRefresh.status, 0, secondRefresh.stderr);
  assert.deepEqual(JSON.parse(secondRefresh.stdout).written, [], "refresh is byte-stable when inputs do not change");
  const harnessRun = run(process.execPath, [cliBin, "harness", "verify", "--json"], scratch);
  assert.equal(harnessRun.status, 0, harnessRun.stderr || harnessRun.stdout);
  assert.equal(JSON.parse(harnessRun.stdout).passed, true);

  const persistedPackage = join(scratch, ".provena", "runtime", "node_modules", "@provena", "cli");
  assert.equal(lstatSync(persistedPackage).isSymbolicLink(), false, "portable runtime owns a package copy");
  const offlineInit = spawnSync(process.execPath, [cliBin, "init", "--no-daemon"], {
    cwd: scratch,
    encoding: "utf8",
    env: {
      ...process.env,
      PROVENA_NPM_REGISTRY: "http://127.0.0.1:1",
      npm_execpath: join(scratch, "missing-npm-cli.js"),
    },
  });
  assert.equal(offlineInit.status, 0, offlineInit.stderr || offlineInit.stdout);
  assert.match(offlineInit.stdout, /runtime: current/, "verified runtime reuse must need no npm or registry");

  const daemonStart = run(process.execPath, [cliBin, "daemon", "start", "--interval", "10s"], scratch);
  assert.equal(daemonStart.status, 0, daemonStart.stderr || daemonStart.stdout);
  const daemonBeforeRepair = JSON.parse(
    readFileSync(join(scratch, ".provena", "daemon.pid"), "utf8"),
  );
  assert.ok(Number.isSafeInteger(daemonBeforeRepair.pid) && daemonBeforeRepair.pid > 0);
  daemonPids.add(daemonBeforeRepair.pid);
  const persistedBrainModule = join(persistedPackage, "dist", "brain", "index.js");
  writeFileSync(
    persistedBrainModule,
    `${readFileSync(persistedBrainModule, "utf8")}\n// injected runtime tamper\n`,
    "utf8",
  );
  const repairRuntime = run(process.execPath, [cliBin, "mcp", "install"], scratch);
  assert.equal(repairRuntime.status, 0, repairRuntime.stderr || repairRuntime.stdout);
  assert.doesNotMatch(
    readFileSync(persistedBrainModule, "utf8"),
    /injected runtime tamper/,
    "runtime integrity mismatch must trigger a clean offline reinstall",
  );
  const daemonAfterRepair = JSON.parse(
    readFileSync(join(scratch, ".provena", "daemon.pid"), "utf8"),
  );
  assert.ok(Number.isSafeInteger(daemonAfterRepair.pid) && daemonAfterRepair.pid > 0);
  daemonPids.add(daemonAfterRepair.pid);
  assert.notEqual(
    daemonAfterRepair.pid,
    daemonBeforeRepair.pid,
    "a live daemon must restart onto the repaired runtime",
  );
  assert.equal(daemonAfterRepair.intervalMs, 10_000, "runtime upgrade preserves cadence");
  assert.equal(
    processAlive(daemonBeforeRepair.pid),
    false,
    "runtime repair must stop the prior daemon process",
  );
  daemonPids.delete(daemonBeforeRepair.pid);
  assert.equal(run(process.execPath, [cliBin, "daemon", "status"], scratch).status, 0);
  assert.equal(run(process.execPath, [cliBin, "daemon", "stop"], scratch).status, 0);
  assert.equal(waitForExit(new Set([daemonAfterRepair.pid])), true, "daemon must exit after stop");
  assert.equal(run(process.execPath, [cliBin, "daemon", "status"], scratch).status, 1);
  daemonPids.delete(daemonAfterRepair.pid);
  renameSync(join(scratch, "node_modules", "@provena", "cli"), join(scratch, "node_modules", "@provena", "cli-disabled"));
  const portableRun = run(
    process.execPath,
    [join(scratch, ".provena", "runtime", "runtime.mjs"), "refresh", "--quiet"],
    scratch,
  );
  assert.equal(portableRun.status, 0, portableRun.stderr || portableRun.stdout);
} catch (error) {
  testError = error;
}

const cleanupErrors = [];
try {
  const daemonState = join(scratch, ".provena", "daemon.pid");
  if (existsSync(daemonState)) {
    const pid = JSON.parse(readFileSync(daemonState, "utf8")).pid;
    if (!Number.isSafeInteger(pid) || pid <= 0) {
      throw new Error(`packed-install daemon state has invalid pid: ${String(pid)}`);
    }
    daemonPids.add(pid);
  }
  if (existsSync(cliBin) && [...daemonPids].some(processAlive)) {
    spawnSync(process.execPath, [cliBin, "daemon", "stop"], {
      cwd: scratch,
      encoding: "utf8",
      shell: false,
      timeout: 5_000,
    });
  }
  if (!waitForExit(daemonPids)) {
    throw new Error(`packed-install daemon remained alive: ${[...daemonPids].filter(processAlive).join(", ")}`);
  }
  rmSync(scratch, {
    recursive: true,
    force: true,
    maxRetries: 10,
    retryDelay: 50,
  });
} catch (error) {
  cleanupErrors.push(error);
}
try {
  rmSync(join(cliRoot, tarball), { force: true });
} catch (error) {
  cleanupErrors.push(error);
}
const cleanupError = cleanupErrors.length > 1
  ? new AggregateError(cleanupErrors, "packed-install cleanup failed")
  : cleanupErrors[0];
if (testError && cleanupError) {
  throw new AggregateError([testError, cleanupError], "packed-install test and cleanup failed");
}
if (testError) throw testError;
if (cleanupError) throw cleanupError;

console.log("pack-install.test: ok");
