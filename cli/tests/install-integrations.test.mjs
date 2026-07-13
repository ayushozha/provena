import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { installGitHooks, installedMcpClients, installMcpConfigs } from "../dist/index.js";

const root = mkdtempSync(join(tmpdir(), "provena-integrations-"));
try {
  assert.equal(spawnSync("git", ["init", "--quiet"], { cwd: root }).status, 0);
  assert.equal(
    spawnSync("git", ["config", "--local", "core.hooksPath", ".githooks"], {
      cwd: root,
    }).status,
    0,
  );
  writeFileSync(
    join(root, ".mcp.json"),
    `${JSON.stringify({ mcpServers: { existing: { command: "existing" } } }, null, 2)}\n`,
  );
  mkdirSync(join(root, ".codex"), { recursive: true });
  writeFileSync(join(root, ".codex", "config.toml"), "model_verbosity = \"low\"\n");

  const firstMcp = installMcpConfigs(root);
  assert.equal(firstMcp.length, 4);
  const claude = JSON.parse(readFileSync(join(root, ".mcp.json"), "utf8"));
  assert.equal(claude.mcpServers.existing.command, "existing");
  assert.equal(claude.mcpServers.provena.command, "node");
  assert.deepEqual(installMcpConfigs(root).map((item) => item.action), [
    "unchanged",
    "unchanged",
    "unchanged",
    "unchanged",
  ]);
  assert.match(readFileSync(join(root, ".codex", "config.toml"), "utf8"), /model_verbosity/);
  assert.deepEqual(installedMcpClients(root).sort(), ["claude", "codex", "cursor", "vscode"]);

  writeFileSync(
    join(root, ".mcp.json"),
    `${JSON.stringify({ mcpServers: { provena: { command: "user-owned" } } }, null, 2)}\n`,
  );
  writeFileSync(
    join(root, ".codex", "config.toml"),
    '[mcp_servers.provena]\ncommand = "user-owned"\n',
  );
  writeFileSync(join(root, ".vscode", "mcp.json"), "{\n  // user JSONC\n}\n");
  const collisions = installMcpConfigs(root);
  assert.equal(collisions.find((item) => item.client === "claude")?.action, "skipped");
  assert.equal(collisions.find((item) => item.client === "codex")?.action, "skipped");
  assert.equal(collisions.find((item) => item.client === "vscode")?.action, "skipped");
  assert.match(readFileSync(join(root, ".mcp.json"), "utf8"), /user-owned/);
  assert.match(readFileSync(join(root, ".codex", "config.toml"), "utf8"), /user-owned/);

  writeFileSync(join(root, ".cursor", "mcp.json"), '{"mcpServers":[]}\n');
  writeFileSync(join(root, ".vscode", "mcp.json"), "[]\n");
  const invalidShapes = installMcpConfigs(root);
  assert.equal(invalidShapes.find((item) => item.client === "cursor")?.action, "skipped");
  assert.equal(invalidShapes.find((item) => item.client === "vscode")?.action, "skipped");
  assert.equal(readFileSync(join(root, ".cursor", "mcp.json"), "utf8"), '{"mcpServers":[]}\n');
  assert.equal(readFileSync(join(root, ".vscode", "mcp.json"), "utf8"), "[]\n");
  assert.deepEqual(installedMcpClients(root), []);

  const hooksDir = ".githooks";
  mkdirSync(join(root, hooksDir), { recursive: true });
  mkdirSync(join(root, ".provena", "runtime"), { recursive: true });
  writeFileSync(
    join(root, ".provena", "runtime", "runtime.mjs"),
    'import { writeFileSync } from "node:fs"; writeFileSync(".hook-ran", process.argv[2] ?? "none");\n',
  );
  const postCommit = join(root, hooksDir, "post-commit");
  writeFileSync(postCommit, "#!/bin/sh\nexit 0\n", "utf8");
  const postMerge = join(root, hooksDir, "post-merge");
  const nodeHook = '#!/usr/bin/env node\nconsole.log("user hook");\n';
  writeFileSync(postMerge, nodeHook, "utf8");
  const hooks = installGitHooks(root);
  assert.equal(hooks.length, 3);
  const installed = readFileSync(postCommit, "utf8");
  assert.match(installed, /exit 0/);
  assert.match(installed, /provena repo-memory hook/);
  assert.match(installed, /\.provena\/runtime\/runtime\.mjs/);
  assert.equal(installed.match(/>>> provena repo-memory hook/g)?.length, 1);
  assert.ok(existsSync(join(root, hooksDir, "post-checkout")));
  assert.equal(hooks.find((item) => item.hook === "post-merge")?.action, "skipped");
  assert.equal(readFileSync(postMerge, "utf8"), nodeHook, "non-shell hooks are preserved byte-for-byte");
  const secondHooks = installGitHooks(root);
  assert.ok(secondHooks.filter((item) => item.hook !== "post-merge").every((item) => item.action === "unchanged"));
  assert.equal(secondHooks.find((item) => item.hook === "post-merge")?.action, "skipped");
  const hookRun = spawnSync("git", ["hook", "run", "post-commit"], {
    cwd: root,
    encoding: "utf8",
  });
  assert.equal(hookRun.status, 0, hookRun.stderr || hookRun.stdout);
  assert.equal(readFileSync(join(root, ".hook-ran"), "utf8"), "refresh");
} finally {
  rmSync(root, { recursive: true, force: true });
}

console.log("install-integrations.test: ok");
