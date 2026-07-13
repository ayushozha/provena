import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, stat, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { withRepoMemoryLock } from "../dist/brain/lock.js";

const root = await mkdtemp(join(tmpdir(), "provena-lock-"));
const lockPath = join(root, ".provena", "cache", "locks", "repo-memory.lock");
try {
  let active = 0;
  let maximum = 0;
  await Promise.all(
    Array.from({ length: 8 }, (_, index) =>
      withRepoMemoryLock(root, async () => {
        active += 1;
        maximum = Math.max(maximum, active);
        await delay(15);
        active -= 1;
        return index;
      }),
    ),
  );
  assert.equal(maximum, 1, "repository memory mutations must be serialized");

  const lockModuleUrl = new URL("../dist/brain/lock.js", import.meta.url).href;
  const forcedExit = spawnSync(
    process.execPath,
    [
      "--input-type=module",
      "--eval",
      `import { withRepoMemoryLock } from ${JSON.stringify(lockModuleUrl)};
await withRepoMemoryLock(${JSON.stringify(root)}, async () => process.exit(0));`,
    ],
    { encoding: "utf8" },
  );
  assert.equal(forcedExit.status, 0, forcedExit.stderr || forcedExit.stdout);
  await assert.rejects(
    stat(lockPath),
    (error) => error?.code === "ENOENT",
    "forced process exit must synchronously release an owned repo-memory lock",
  );
  await withRepoMemoryLock(root, async () => undefined);

  await mkdir(lockPath, { recursive: true });
  await writeFile(
    join(lockPath, "owner.json"),
    `${JSON.stringify({
      token: "live-owner",
      pid: process.pid,
      acquiredAt: "2000-01-01T00:00:00.000Z",
    })}\n`,
  );
  let entered = false;
  const waiting = withRepoMemoryLock(root, async () => {
    entered = true;
  });
  await delay(150);
  assert.equal(entered, false, "an old but live owner must never be evicted");
  await rm(lockPath, { recursive: true, force: true });
  await waiting;
  assert.equal(entered, true);

  await mkdir(join(lockPath, "reclaim.claim"), { recursive: true });
  await writeFile(
    join(lockPath, "owner.json"),
    `${JSON.stringify({
      token: "dead-owner",
      pid: 2_147_483_647,
      acquiredAt: "2000-01-01T00:00:00.000Z",
    })}\n`,
  );
  const old = new Date("2000-01-01T00:00:00.000Z");
  await utimes(join(lockPath, "reclaim.claim"), old, old);
  await withRepoMemoryLock(root, async () => {
    const owner = JSON.parse(await readFile(join(lockPath, "owner.json"), "utf8"));
    assert.notEqual(owner.token, "dead-owner");
  });
} finally {
  await rm(root, { recursive: true, force: true });
}

console.log("repo-brain-lock.test: ok");
