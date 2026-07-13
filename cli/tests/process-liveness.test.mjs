import assert from "node:assert/strict";
import { isProcessRunning } from "../dist/process.js";

const originalKill = process.kill;
try {
  process.kill = () => {
    throw Object.assign(new Error("operation not permitted"), { code: "EPERM" });
  };
  assert.equal(
    isProcessRunning(2_147_483_647),
    true,
    "EPERM means the process exists but belongs to another user",
  );

  process.kill = () => {
    throw Object.assign(new Error("no such process"), { code: "ESRCH" });
  };
  assert.equal(isProcessRunning(2_147_483_647), false, "ESRCH means the process is gone");
} finally {
  process.kill = originalKill;
}

console.log("process-liveness.test: ok");
