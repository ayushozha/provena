import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { join, resolve } from "node:path";
import { getGitRoot } from "../config.js";
import { isProcessRunning } from "../process.js";
import { assertSafeRepoPath } from "../security/paths.js";

const DEFAULT_INTERVAL_MS = 15 * 60 * 1000;

interface DaemonState {
  schemaVersion: 1;
  pid: number;
  token: string;
  runner: string;
  startedAt: string;
  intervalMs: number;
}

interface DaemonHeartbeat {
  schemaVersion: 1;
  pid: number;
  token: string;
  heartbeatAt: string;
}

interface DaemonStartOwner {
  schemaVersion: 1;
  pid: number;
  token: string;
  startedAt: string;
}

export interface DaemonStatus {
  running: boolean;
  pid?: number;
  token?: string;
  intervalMs?: number;
}

function parseDuration(value: string): number {
  const match = /^(\d+)(ms|s|m|h)?$/.exec(value.trim());
  if (!match) throw new Error(`invalid duration: ${value}`);
  const amount = Number(match[1]);
  const unit = (match[2] ?? "ms") as "ms" | "s" | "m" | "h";
  const multiplier: Record<typeof unit, number> = {
    ms: 1,
    s: 1_000,
    m: 60_000,
    h: 3_600_000,
  };
  const duration = amount * multiplier[unit];
  if (!Number.isSafeInteger(duration) || duration < 10_000) {
    throw new Error("daemon interval must be at least 10s");
  }
  return duration;
}

function valueAfter(args: string[], flag: string): string | undefined {
  const at = args.indexOf(flag);
  return at >= 0 ? args[at + 1] : undefined;
}

function daemonPaths(repoRoot: string) {
  const root = join(repoRoot, ".provena");
  return {
    state: join(root, "daemon.pid"),
    heartbeat: join(root, "daemon.heartbeat"),
    stop: join(root, "daemon.stop"),
    startLock: join(root, "daemon.start.lock"),
    runner: join(root, "runtime", "runtime.mjs"),
  };
}

function withDaemonStartLock<T>(
  repoRoot: string,
  lockPath: string,
  action: () => T,
): T {
  const ownerPath = join(lockPath, "owner.json");
  const token = randomUUID();
  const deadline = Date.now() + 10_000;
  while (true) {
    assertSafeRepoPath(repoRoot, lockPath);
    try {
      mkdirSync(lockPath);
      writeJsonAtomic(repoRoot, ownerPath, {
        schemaVersion: 1,
        pid: process.pid,
        token,
        startedAt: new Date().toISOString(),
      } satisfies DaemonStartOwner);
      break;
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code !== "EEXIST") throw error;
      const owner = readJson<DaemonStartOwner>(repoRoot, ownerPath);
      let ageMs: number;
      try {
        ageMs = Date.now() - statSync(lockPath).mtimeMs;
      } catch (statError) {
        if ((statError as NodeJS.ErrnoException).code === "ENOENT") continue;
        throw statError;
      }
      if (!owner && ageMs < 2_000) {
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 25);
        continue;
      }
      if (!owner || !isProcessRunning(owner.pid)) {
        const quarantine = `${lockPath}.stale-${randomUUID()}`;
        assertSafeRepoPath(repoRoot, lockPath);
        assertSafeRepoPath(repoRoot, quarantine);
        try {
          renameSync(lockPath, quarantine);
          rmSync(quarantine, { recursive: true, force: true });
        } catch (reclaimError) {
          if ((reclaimError as NodeJS.ErrnoException).code !== "ENOENT") throw reclaimError;
        }
        continue;
      }
      if (Date.now() >= deadline) {
        throw new Error(`timed out waiting for daemon startup owned by pid ${owner.pid}`);
      }
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 25);
    }
  }
  try {
    return action();
  } finally {
    const owner = readJson<DaemonStartOwner>(repoRoot, ownerPath);
    if (owner?.pid === process.pid && owner.token === token) {
      assertSafeRepoPath(repoRoot, lockPath);
      rmSync(lockPath, { recursive: true, force: true });
    }
  }
}

function readJson<T>(repoRoot: string, path: string): T | null {
  assertSafeRepoPath(repoRoot, path);
  if (!existsSync(path)) return null;
  try {
    return JSON.parse(readFileSync(path, "utf8")) as T;
  } catch {
    return null;
  }
}

function validState(value: DaemonState | null): value is DaemonState {
  return Boolean(
    value &&
      value.schemaVersion === 1 &&
      Number.isSafeInteger(value.pid) &&
      value.pid > 0 &&
      typeof value.token === "string" &&
      value.token.length >= 16 &&
      typeof value.runner === "string" &&
      Number.isFinite(Date.parse(value.startedAt)) &&
      Number.isSafeInteger(value.intervalMs) &&
      value.intervalMs >= 10_000,
  );
}

function validHeartbeat(value: DaemonHeartbeat | null): value is DaemonHeartbeat {
  return Boolean(
    value &&
      value.schemaVersion === 1 &&
      Number.isSafeInteger(value.pid) &&
      value.pid > 0 &&
      typeof value.token === "string" &&
      Number.isFinite(Date.parse(value.heartbeatAt)),
  );
}

function removeSafe(repoRoot: string, path: string): void {
  assertSafeRepoPath(repoRoot, path);
  rmSync(path, { force: true });
}

function writeJsonAtomic(repoRoot: string, path: string, value: unknown): void {
  assertSafeRepoPath(repoRoot, path);
  const temporary = `${path}.provena-${process.pid}-${randomUUID()}.tmp`;
  assertSafeRepoPath(repoRoot, temporary);
  try {
    writeFileSync(temporary, `${JSON.stringify(value)}\n`, { encoding: "utf8", mode: 0o600 });
    assertSafeRepoPath(repoRoot, temporary);
    assertSafeRepoPath(repoRoot, path);
    renameSync(temporary, path);
  } finally {
    assertSafeRepoPath(repoRoot, temporary);
    rmSync(temporary, { force: true });
  }
}

export function readDaemonStatus(repoRoot: string): DaemonStatus {
  const paths = daemonPaths(repoRoot);
  const state = readJson<DaemonState>(repoRoot, paths.state);
  const heartbeat = readJson<DaemonHeartbeat>(repoRoot, paths.heartbeat);
  const running =
    validState(state) &&
    validHeartbeat(heartbeat) &&
    resolve(state.runner) === resolve(paths.runner) &&
    heartbeat.pid === state.pid &&
    heartbeat.token === state.token &&
    isProcessRunning(state.pid);
  if (running) {
    return {
      running: true,
      pid: state.pid,
      token: state.token,
      intervalMs: state.intervalMs,
    };
  }

  // Never signal a process from untrusted or stale PID state. Removing only
  // Provena's metadata lets a healthy replacement start without risking an
  // unrelated process that reused the integer PID.
  removeSafe(repoRoot, paths.state);
  removeSafe(repoRoot, paths.heartbeat);
  return { running: false };
}

export function printDaemonHelp(): void {
  console.log("Usage: provena daemon <start|stop|status> [--interval 15m]");
  console.log("");
  console.log("Run the ignored portable runtime on a fixed refresh cadence.");
}

export function runDaemonCommand(args: string[], cwd = process.cwd()): number {
  if (args.includes("--help") || args.includes("-h")) {
    printDaemonHelp();
    return 0;
  }
  const repoRoot = getGitRoot(cwd);
  const command = args[0] ?? "status";
  const paths = daemonPaths(repoRoot);
  for (const path of Object.values(paths)) assertSafeRepoPath(repoRoot, path);
  const status = command === "start" ? { running: false } : readDaemonStatus(repoRoot);

  if (command === "status") {
    if (status.running) {
      console.log(`Provena daemon running (pid ${status.pid}).`);
      return 0;
    }
    console.log("Provena daemon is stopped.");
    return 1;
  }

  if (command === "stop") {
    if (!status.running || !status.pid || !status.token) {
      console.log("Provena daemon is already stopped.");
      return 0;
    }
    // A token-scoped cooperative request cannot stop an unrelated process or
    // a newer Provena daemon that happens to reuse the same PID.
    writeJsonAtomic(repoRoot, paths.stop, {
      schemaVersion: 1,
      pid: status.pid,
      token: status.token,
      requestedAt: new Date().toISOString(),
    });
    console.log(`Provena daemon stop requested (pid ${status.pid}).`);
    return 0;
  }

  if (command !== "start") {
    console.error(`provena daemon: unknown subcommand ${command}`);
    printDaemonHelp();
    return 1;
  }
  if (!existsSync(paths.runner)) {
    console.error("provena daemon: portable runtime missing; run `provena init --force`");
    return 1;
  }
  return withDaemonStartLock(repoRoot, paths.startLock, () => {
    const lockedStatus = readDaemonStatus(repoRoot);
    if (lockedStatus.running) {
      console.log(`Provena daemon already running (pid ${lockedStatus.pid}).`);
      return 0;
    }
    removeSafe(repoRoot, paths.stop);
    const rawInterval = valueAfter(args, "--interval");
    const interval = rawInterval ? parseDuration(rawInterval) : DEFAULT_INTERVAL_MS;
    const token = randomUUID();
    const child = spawn(
      process.execPath,
      [
        paths.runner,
        "daemon",
        "--interval-ms",
        String(interval),
        "--token",
        token,
        "--quiet",
      ],
      { cwd: repoRoot, detached: true, stdio: "ignore", windowsHide: true },
    );
    if (!child.pid) {
      console.error("provena daemon: failed to start");
      return 1;
    }
    const now = new Date().toISOString();
    try {
      writeJsonAtomic(repoRoot, paths.state, {
        schemaVersion: 1,
        pid: child.pid,
        token,
        runner: resolve(paths.runner),
        startedAt: now,
        intervalMs: interval,
      } satisfies DaemonState);
      writeJsonAtomic(repoRoot, paths.heartbeat, {
        schemaVersion: 1,
        pid: child.pid,
        token,
        heartbeatAt: now,
      } satisfies DaemonHeartbeat);
    } catch (error) {
      try {
        process.kill(child.pid);
      } catch {
        // The child may have already exited; preserve the original write error.
      }
      throw error;
    }
    child.unref();
    console.log(`Provena daemon started (pid ${child.pid}, every ${interval}ms).`);
    return 0;
  });
}

export function waitForDaemonStop(repoRoot: string, timeoutMs = 10_000): boolean {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!readDaemonStatus(repoRoot).running) return true;
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 50);
  }
  return !readDaemonStatus(repoRoot).running;
}
