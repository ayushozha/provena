import assert from "node:assert/strict";
import {
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { installAgentInstructions } from "../dist/index.js";

const root = mkdtempSync(join(tmpdir(), "provena-agents-"));
try {
  writeFileSync(join(root, "AGENTS.md"), "# Existing rules\n\nKeep this.\n", "utf8");

  const first = installAgentInstructions(root);
  assert.equal(first.length, 5);
  assert.ok(first.some((result) => result.action === "created"));

  const agents = readFileSync(join(root, "AGENTS.md"), "utf8");
  assert.match(agents, /# Existing rules/);
  assert.match(agents, /Keep this\./);
  assert.match(agents, /provena:memory-instructions:start/);
  assert.equal(
    agents.match(/provena:memory-instructions:start/g)?.length,
    1,
    "managed instructions appear once",
  );

  const cursor = readFileSync(
    join(root, ".cursor", "rules", "provena-memory.mdc"),
    "utf8",
  );
  assert.match(cursor, /alwaysApply: true/);
  assert.match(cursor, /session start --agent cursor/);

  const second = installAgentInstructions(root);
  assert.ok(second.every((result) => result.action === "unchanged"));
  assert.equal(
    readFileSync(join(root, "AGENTS.md"), "utf8"),
    agents,
    "second install is byte-for-byte idempotent",
  );
} finally {
  rmSync(root, { recursive: true, force: true });
}

console.log("agents.test: ok");
