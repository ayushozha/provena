import { mkdirSync } from "node:fs";
import {
  configExists,
  createDefaultConfig,
  ensureGitignore,
  getGitRoot,
  provenaDir,
  readConfig,
  writeConfig,
} from "../config.js";
import { refreshRepoBrain } from "../brain/index.js";
import { readDaemonStatus, runDaemonCommand, waitForDaemonStop } from "./daemon.js";
import { installAgentInstructions } from "../integrations/agents.js";
import { installGitHooks } from "../integrations/git-hooks.js";
import { installMcpConfigs } from "../integrations/mcp-config.js";
import { installPortableRuntime } from "../integrations/runtime.js";
import { assertSafeRepoPath } from "../security/paths.js";

export interface InitOptions {
  cwd?: string;
  force?: boolean;
  agents?: boolean;
  hooks?: boolean;
  mcp?: boolean;
  runtime?: boolean;
  daemon?: boolean;
}

export async function runInit(options: InitOptions = {}): Promise<number> {
  if (
    options.runtime === false &&
    (options.hooks !== false || options.mcp !== false || options.daemon !== false)
  ) {
    throw new Error(
      "--no-runtime requires --no-hooks --no-mcp --no-daemon because those integrations execute the persisted runtime",
    );
  }
  const cwd = options.cwd ?? process.cwd();
  const gitRoot = getGitRoot(cwd);
  assertSafeRepoPath(gitRoot, provenaDir(gitRoot));
  assertSafeRepoPath(gitRoot, `${gitRoot}/.gitignore`);
  const force = options.force ?? false;
  const exists = configExists(gitRoot);
  let existingConfig: ReturnType<typeof readConfig> | undefined;
  if (exists) {
    try {
      existingConfig = readConfig(gitRoot);
    } catch (error) {
      if (!force) {
        throw new Error(
          `existing .provena/config.json is invalid; repair it or rerun with --force: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
    }
  }

  mkdirSync(provenaDir(gitRoot), { recursive: true });

  if (existingConfig && !existingConfig.repository_id) {
    existingConfig = {
      ...existingConfig,
      repository_id: createDefaultConfig({ cwd, gitRoot }).repository_id,
    };
    writeConfig(gitRoot, existingConfig);
    console.log("  assigned stable repository identity");
  }

  if (existingConfig) {
    console.log("Provena already initialized (.provena/config.json exists).");
    if (force) {
      console.log("  config preserved; managed integrations will be repaired (--force).");
    }
  } else {
    const config = createDefaultConfig({ cwd, gitRoot });
    writeConfig(gitRoot, config);
    if (exists && force) {
      console.log("Provena config overwritten (--force).");
    } else {
      console.log("Provena initialized.");
    }
    console.log(`  config: ${provenaDir(gitRoot)}/config.json`);
    console.log(`  database: ${config.database.path}`);
  }

  if (ensureGitignore(gitRoot)) {
    console.log("  updated selective .provena local-state ignores");
  }

  const refreshed = await refreshRepoBrain(gitRoot, {
    warn: (message) => console.error(`provena init: ${message}`),
  });
  console.log(
    `  brain: ${refreshed.map.files.length} files, ${refreshed.map.symbols.length} symbols, ` +
      `${refreshed.graph.nodes.length} graph nodes`,
  );

  if (options.agents !== false) {
    const agents = installAgentInstructions(gitRoot);
    console.log(`  agents: ${agents.length} managed boot surfaces`);
  }

  const needsRuntime =
    options.runtime !== false &&
    (options.hooks !== false || options.mcp !== false || options.daemon !== false);
  const daemonBeforeRuntime = needsRuntime
    ? readDaemonStatus(gitRoot)
    : { running: false, intervalMs: undefined };
  let runtimeChanged = false;
  if (needsRuntime) {
    const runtime = installPortableRuntime(gitRoot);
    runtimeChanged = !runtime.reused;
    console.log(`  runtime: ${runtime.reused ? "current" : "installed"}`);
    if (runtimeChanged && daemonBeforeRuntime.running) {
      runDaemonCommand(["stop"], gitRoot);
      if (!waitForDaemonStop(gitRoot)) {
        throw new Error("existing Provena daemon did not stop during runtime upgrade");
      }
    }
  }
  if (options.hooks !== false && needsRuntime) {
    const hooks = installGitHooks(gitRoot);
    const installed = hooks.filter((hook) => hook.action !== "skipped");
    console.log(`  hooks: ${installed.length} Git lifecycle hooks`);
    const skipped = hooks.find((hook) => hook.action === "skipped");
    if (skipped?.reason) console.log(`    skipped: ${skipped.reason}`);
  }
  if (options.mcp !== false && needsRuntime) {
    const configs = installMcpConfigs(gitRoot);
    console.log(`  mcp: ${configs.filter((item) => item.action !== "skipped").length} project client configs`);
    for (const skipped of configs.filter((item) => item.action === "skipped")) {
      console.log(`    skipped ${skipped.client}: ${skipped.reason}`);
    }
  }
  // Agent and MCP installers add tracked repository surfaces. Fold them into
  // the map before reporting the one-click installation ready.
  await refreshRepoBrain(gitRoot);
  if (options.daemon !== false && needsRuntime) {
    const interval = daemonBeforeRuntime.intervalMs;
    runDaemonCommand(
      interval ? ["start", "--interval", `${interval}ms`] : ["start"],
      gitRoot,
    );
  }

  console.log("");
  console.log("Ready: agents can read .provena/repo.brain.md or run `provena context`.");

  return 0;
}
