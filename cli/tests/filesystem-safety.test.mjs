import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  installAgentInstructions,
  installGitHooks,
  installMcpConfigs,
  readConfig,
  refreshRepoBrain,
} from "../dist/index.js";
import { runStatusCommand } from "../dist/commands/status.js";

function directoryLink(target, path) {
  symlinkSync(target, path, process.platform === "win32" ? "junction" : "dir");
}

const sandbox = mkdtempSync(join(tmpdir(), "provena-safe-paths-"));
try {
  const outside = join(sandbox, "outside");
  mkdirSync(outside);

  const brainRoot = join(sandbox, "brain");
  mkdirSync(brainRoot);
  directoryLink(outside, join(brainRoot, ".provena"));
  await assert.rejects(refreshRepoBrain(brainRoot), /symlink|junction|reparse point/);
  assert.equal(existsSync(join(outside, "repo.brain.md")), false);

  const agentRoot = join(sandbox, "agents");
  mkdirSync(agentRoot);
  directoryLink(outside, join(agentRoot, ".github"));
  assert.throws(
    () => installAgentInstructions(agentRoot),
    /symlink|junction|reparse point/,
  );
  assert.equal(existsSync(join(outside, "copilot-instructions.md")), false);

  const mcpRoot = join(sandbox, "mcp");
  mkdirSync(mcpRoot);
  directoryLink(outside, join(mcpRoot, ".cursor"));
  const mcp = installMcpConfigs(mcpRoot);
  assert.equal(mcp.find((item) => item.client === "cursor")?.action, "skipped");
  assert.equal(existsSync(join(outside, "mcp.json")), false);

  const hookRoot = join(sandbox, "hooks");
  mkdirSync(hookRoot);
  assert.equal(spawnSync("git", ["init", "--quiet"], { cwd: hookRoot }).status, 0);
  assert.equal(
    spawnSync("git", ["config", "--local", "core.hooksPath", ".githooks"], {
      cwd: hookRoot,
    }).status,
    0,
  );
  directoryLink(outside, join(hookRoot, ".githooks"));
  assert.throws(() => installGitHooks(hookRoot), /symlink|junction|reparse point/);
  assert.equal(existsSync(join(outside, "post-commit")), false);

  const unsafeReadRoot = join(sandbox, "unsafe-reads");
  mkdirSync(join(unsafeReadRoot, ".provena"), { recursive: true });
  const outsideConfig = join(outside, "config.json");
  const outsideManifest = join(outside, "manifest.json");
  writeFileSync(outsideConfig, '{"version":1}\n');
  writeFileSync(outsideManifest, '{"schemaVersion":1,"artifacts":[]}\n');
  symlinkSync(outsideConfig, join(unsafeReadRoot, ".provena", "config.json"), "file");
  symlinkSync(outsideManifest, join(unsafeReadRoot, ".provena", "manifest.json"), "file");
  assert.throws(() => readConfig(unsafeReadRoot), /symlink|junction|reparse point/);
  await assert.rejects(
    runStatusCommand(["--json"], unsafeReadRoot),
    /symlink|junction|reparse point/,
  );
} catch (error) {
  if (error?.code === "EPERM") {
    console.log("filesystem-safety.test: skipped (symlink creation unavailable)");
  } else {
    throw error;
  }
} finally {
  rmSync(sandbox, { recursive: true, force: true });
}

console.log("filesystem-safety.test: ok");
