import { randomUUID } from "node:crypto";
import { readFileSync, rmSync } from "node:fs";
import { mkdir, readFile, rename, rm, rmdir, stat, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { isProcessRunning } from "../process.js";
import { assertSafeRepoPath } from "../security/paths.js";

const LOCK_PATH = ".provena/cache/locks/repo-memory.lock";
const RETRY_MS = 50;
const WAIT_MS = 30_000;
const STALE_MS = 2 * 60_000;

interface LockOwner {
  token: string;
  pid: number;
  acquiredAt: string;
}

interface ActiveLock {
  repoRoot: string;
  path: string;
  owner: LockOwner;
}

const activeLocks = new Map<string, ActiveLock>();

function releaseActiveLockSync(lock: ActiveLock): void {
  const ownerPath = join(lock.path, "owner.json");
  assertSafeRepoPath(lock.repoRoot, ownerPath);
  const current = JSON.parse(readFileSync(ownerPath, "utf8")) as Partial<LockOwner>;
  if (current.pid !== lock.owner.pid || current.token !== lock.owner.token) return;
  assertSafeRepoPath(lock.repoRoot, lock.path);
  rmSync(lock.path, { recursive: true, force: true });
  activeLocks.delete(lock.path);
}

process.once("exit", () => {
  for (const lock of activeLocks.values()) {
    try {
      releaseActiveLockSync(lock);
    } catch {
      // Exit cleanup is best effort and must never delete a lock whose current
      // token and PID could not be verified synchronously.
    }
  }
});

async function ownerAt(path: string): Promise<LockOwner | null> {
  try {
    return JSON.parse(await readFile(join(path, "owner.json"), "utf8")) as LockOwner;
  } catch {
    return null;
  }
}

async function stale(path: string): Promise<{ owner: LockOwner | null } | null> {
  const owner = await ownerAt(path);
  let acquired = owner ? Date.parse(owner.acquiredAt) : Number.NaN;
  if (!Number.isFinite(acquired)) {
    try {
      acquired = (await stat(path)).mtimeMs;
    } catch {
      return null;
    }
  }
  const age = Date.now() - acquired;
  // A slow refresh must never lose its lock merely because it exceeded a
  // wall-clock timeout. Reclaim only when the owner is confirmed dead (or no
  // owner was ever written) and the grace period has elapsed.
  return age > STALE_MS && (!owner || !isProcessRunning(owner.pid)) ? { owner } : null;
}

async function reclaimStaleLock(repoRoot: string, path: string): Promise<boolean> {
  const snapshot = await stale(path);
  if (!snapshot) return false;

  // The claim directory serializes stale-lock reapers. Without it, two
  // waiters can both classify an old lock as stale and the second can delete a
  // newly acquired replacement.
  const claimPath = join(path, "reclaim.claim");
  assertSafeRepoPath(repoRoot, claimPath);
  let claimed = false;
  for (let attempt = 0; attempt < 2 && !claimed; attempt += 1) {
    try {
      await mkdir(claimPath);
      claimed = true;
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === "ENOENT") return true;
      if (code !== "EEXIST") throw error;
      let abandoned = false;
      try {
        abandoned = Date.now() - (await stat(claimPath)).mtimeMs > STALE_MS;
      } catch (statError) {
        if ((statError as NodeJS.ErrnoException).code === "ENOENT") continue;
        throw statError;
      }
      if (!abandoned) return false;
      // The claim is deliberately an empty directory, so rmdir is an atomic
      // lease takeover: only one waiter can remove an abandoned claimant.
      assertSafeRepoPath(repoRoot, claimPath);
      try {
        await rmdir(claimPath);
      } catch (removeError) {
        if ((removeError as NodeJS.ErrnoException).code !== "ENOENT") return false;
      }
    }
  }
  if (!claimed) return false;

  try {
    const currentOwner = await ownerAt(path);
    if (snapshot.owner) {
      const current = await stale(path);
      if (!current || currentOwner?.token !== snapshot.owner.token) return false;
    } else if (currentOwner) {
      return false;
    }

    const quarantine = `${path}.stale-${randomUUID()}`;
    assertSafeRepoPath(repoRoot, quarantine);
    await rename(path, quarantine);
    assertSafeRepoPath(repoRoot, quarantine);
    await rm(quarantine, { recursive: true, force: true });
    return true;
  } finally {
    // If the lock was quarantined this path no longer exists; otherwise free
    // the claim for a future verified attempt.
    assertSafeRepoPath(repoRoot, claimPath);
    await rm(claimPath, { recursive: true, force: true }).catch(() => undefined);
  }
}

async function acquire(repoRoot: string): Promise<{ path: string; owner: LockOwner }> {
  const path = join(repoRoot, ...LOCK_PATH.split("/"));
  assertSafeRepoPath(repoRoot, dirname(path));
  assertSafeRepoPath(repoRoot, path);
  await mkdir(dirname(path), { recursive: true });
  const deadline = Date.now() + WAIT_MS;
  while (Date.now() < deadline) {
    try {
      await mkdir(path);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      if (await reclaimStaleLock(repoRoot, path)) {
        continue;
      }
      await delay(RETRY_MS);
      continue;
    }

    const owner: LockOwner = {
      token: randomUUID(),
      pid: process.pid,
      acquiredAt: new Date().toISOString(),
    };
    const ownerPath = join(path, "owner.json");
    assertSafeRepoPath(repoRoot, ownerPath);
    try {
      await writeFile(ownerPath, `${JSON.stringify(owner)}\n`, {
        encoding: "utf8",
        flag: "wx",
        mode: 0o600,
      });
      return { path, owner };
    } catch (error) {
      // A failed owner write must not strand an ownerless lock for the stale
      // grace period.
      assertSafeRepoPath(repoRoot, path);
      await rm(path, { recursive: true, force: true }).catch(() => undefined);
      throw error;
    }
  }
  throw new Error(`timed out waiting for ${LOCK_PATH}`);
}

export async function withRepoMemoryLock<T>(
  repoRoot: string,
  operation: () => Promise<T>,
): Promise<T> {
  const lock = await acquire(repoRoot);
  activeLocks.set(lock.path, { repoRoot, ...lock });
  try {
    return await operation();
  } finally {
    const current = await ownerAt(lock.path);
    if (current?.pid === lock.owner.pid && current.token === lock.owner.token) {
      assertSafeRepoPath(repoRoot, lock.path);
      await rm(lock.path, { recursive: true, force: true });
    }
    activeLocks.delete(lock.path);
  }
}
