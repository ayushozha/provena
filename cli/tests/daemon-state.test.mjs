import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { readDaemonStatus, runDaemonCommand } from "../dist/commands/daemon.js";

const root = mkdtempSync(join(tmpdir(), "provena-daemon-state-"));
const cliEntry = resolve(dirname(fileURLToPath(import.meta.url)), "..", "dist", "cli.js");
const runCli = (args) => new Promise((resolvePromise) => {
  const child = spawn(process.execPath, [cliEntry, ...args], {
    cwd: root,
    stdio: "ignore",
  });
  child.once("exit", (code) => resolvePromise(code));
});
let spawnedPid;
try {
  assert.equal(spawnSync("git", ["init", "--quiet"], { cwd: root }).status, 0);
  const runtimeRoot = join(root, ".provena", "runtime");
  mkdirSync(runtimeRoot, { recursive: true });
  const runner = join(runtimeRoot, "runtime.mjs");
  writeFileSync(
    runner,
    'import { appendFileSync, existsSync, readFileSync } from "node:fs"; import { join } from "node:path"; const token = process.argv[process.argv.indexOf("--token") + 1]; appendFileSync(join(process.cwd(), ".provena", "daemon-starts.log"), `${process.pid}\\n`); const stop = join(process.cwd(), ".provena", "daemon.stop"); setInterval(() => { if (!existsSync(stop)) return; try { const value = JSON.parse(readFileSync(stop, "utf8")); if (value.token === token && value.pid === process.pid) process.exit(0); } catch {} }, 50);\n',
    "utf8",
  );
  const staleToken = "stale-token-000000000000";
  writeFileSync(
    join(root, ".provena", "daemon.pid"),
    `${JSON.stringify({
      schemaVersion: 1,
      pid: process.pid,
      token: staleToken,
      runner: resolve(runner),
      startedAt: "2000-01-01T00:00:00.000Z",
      intervalMs: 10_000,
    })}\n`,
  );
  writeFileSync(
    join(root, ".provena", "daemon.heartbeat"),
    `${JSON.stringify({
      schemaVersion: 1,
      pid: process.pid,
      token: staleToken,
      heartbeatAt: "2000-01-01T00:00:00.000Z",
    })}\n`,
  );

  assert.equal(
    readDaemonStatus(root).running,
    true,
    "a matching live owner remains authoritative during a long refresh even when its heartbeat timestamp is old",
  );
  writeFileSync(
    join(root, ".provena", "daemon.heartbeat"),
    `${JSON.stringify({
      schemaVersion: 1,
      pid: process.pid,
      token: "different-process-token",
      heartbeatAt: "2000-01-01T00:00:00.000Z",
    })}\n`,
  );
  assert.deepEqual(readDaemonStatus(root), { running: false });
  assert.deepEqual(
    await Promise.all([
      runCli(["daemon", "start", "--interval", "10s"]),
      runCli(["daemon", "start", "--interval", "10s"]),
    ]),
    [0, 0],
  );
  for (
    let attempt = 0;
    attempt < 40 && !existsSync(join(root, ".provena", "daemon-starts.log"));
    attempt += 1
  ) {
    await delay(25);
  }
  const starts = readFileSync(join(root, ".provena", "daemon-starts.log"), "utf8")
    .trim()
    .split(/\r?\n/)
    .filter(Boolean);
  assert.equal(starts.length, 1, "concurrent starts must create exactly one daemon process");
  const current = JSON.parse(readFileSync(join(root, ".provena", "daemon.pid"), "utf8"));
  spawnedPid = current.pid;
  assert.notEqual(current.token, staleToken);
  assert.notEqual(current.pid, process.pid);
  assert.equal(readDaemonStatus(root).running, true);

  assert.equal(runDaemonCommand(["stop"], root), 0);
  const stop = JSON.parse(readFileSync(join(root, ".provena", "daemon.stop"), "utf8"));
  assert.equal(stop.token, current.token, "stop requests must be scoped to the daemon token");
  assert.equal(stop.pid, current.pid);
  for (let attempt = 0; attempt < 40 && readDaemonStatus(root).running; attempt += 1) {
    await delay(50);
  }
  assert.equal(readDaemonStatus(root).running, false);
} finally {
  if (spawnedPid) {
    try {
      process.kill(spawnedPid);
    } catch {}
  }
  rmSync(root, {
    recursive: true,
    force: true,
    maxRetries: 20,
    retryDelay: 100,
  });
}

console.log("daemon-state.test: ok");
