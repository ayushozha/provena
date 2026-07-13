#!/usr/bin/env node
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { runAgentsCommand } from "./commands/agents.js";
import { runCheckpointCommand } from "./commands/checkpoint.js";
import { runConfigShow } from "./commands/config-show.js";
import { runContextCommand } from "./commands/context.js";
import { runDaemonCommand } from "./commands/daemon.js";
import { runDiscover } from "./commands/discover.js";
import { runDoctor } from "./commands/doctor.js";
import { runGraphCommand } from "./commands/graph.js";
import { runHarnessCommand } from "./commands/harness.js";
import { runIndexCommand } from "./commands/index.js";
import { runInit } from "./commands/init.js";
import { runMaintainCommand } from "./commands/maintain.js";
import { runMcpCommand } from "./commands/mcp.js";
import { runRefreshCommand } from "./commands/refresh.js";
import { runRememberCommand } from "./commands/remember.js";
import { runSearchCommand } from "./commands/search.js";
import { runServe } from "./commands/serve.js";
import { runSessionCommand } from "./commands/session.js";
import { runStatusCommand } from "./commands/status.js";
import { runSyncCommand } from "./commands/sync.js";
import { runWatchCommand } from "./commands/watch.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

function readVersion(): string {
  const pkgPath = join(__dirname, "..", "package.json");
  const pkg = JSON.parse(readFileSync(pkgPath, "utf8")) as { version: string };
  return pkg.version;
}

const COMMANDS = [
  ["init", "One-click install: brain, graph, runtime, hooks, agents, and MCP"],
  ["refresh", "Regenerate the brain, map, graph, maintenance plan, and memory views"],
  ["context", "Build a compact cited task-context packet"],
  ["maintain", "List deterministic review proposals or compile one task packet"],
  ["remember", "Append an explicit durable memory to the repo ledger"],
  ["checkpoint", "Record a handoff and refresh the brain"],
  ["status", "Show freshness, memory, daemon, and integration health"],
  ["session", "Start an agent session against the current repo brain"],
  ["graph", "Query graph algorithms or synchronize Neo4j"],
  ["harness", "Verify memory determinism, graph integrity, and context quality"],
  ["agents", "Install managed instructions for coding agents"],
  ["mcp", "Install or serve the project-scoped MCP integration"],
  ["daemon", "Manage fixed-cadence background refresh"],
  ["sync", "Replicate the canonical repo ledger into governed storage"],
  ["index", "Alias for local refresh; use --store for legacy store indexing"],
  ["search", "Alias for local context search; use --store for legacy search"],
  ["watch", "Watch and incrementally index the optional local store"],
  ["serve", "Start the optional Python SQLite/Postgres memory store"],
  ["doctor", "Check optional store config and health"],
  ["discover", "List files discoverable by the legacy semantic indexer"],
  ["config", "Show local configuration"],
] as const;

function printHelp(): void {
  console.log(
    [
      "provena — a living repository memory for software agents",
      "",
      "Usage:",
      "  provena <command> [options]",
      "",
      "Commands:",
      ...COMMANDS.map(([name, description]) => `  ${name.padEnd(11)} ${description}`),
      "",
      "Global options:",
      "  --version    Show package version",
      "  --help, -h   Show this help",
      "",
    ].join("\n"),
  );
}

function hasFlag(args: string[], ...flags: string[]): boolean {
  return flags.some((flag) => args.includes(flag));
}

function withoutFlag(args: string[], flag: string): string[] {
  return args.filter((value) => value !== flag);
}

async function runInitCommand(args: string[]): Promise<number> {
  if (hasFlag(args, "--help", "-h")) {
    console.log("Usage: provena init [options]");
    console.log("");
    console.log("  --force          Regenerate config and all managed surfaces");
    console.log("  --no-agents      Do not install AGENTS/Claude/Cursor/Copilot instructions");
    console.log("  --no-hooks       Do not install Git lifecycle refresh hooks");
    console.log("  --no-mcp         Do not install project MCP configs");
    console.log("  --no-runtime     Do not persist runtime (requires --no-hooks --no-mcp --no-daemon)");
    console.log("  --no-daemon      Do not start the 15-minute refresh daemon");
    return 0;
  }
  return runInit({
    force: hasFlag(args, "--force", "-f"),
    agents: !hasFlag(args, "--no-agents"),
    hooks: !hasFlag(args, "--no-hooks"),
    mcp: !hasFlag(args, "--no-mcp"),
    runtime: !hasFlag(args, "--no-runtime"),
    daemon: !hasFlag(args, "--no-daemon"),
  });
}

function runConfigCommand(args: string[]): number {
  const subcommand = args[0];
  if (subcommand === undefined || subcommand === "show") return runConfigShow();
  console.error(`Unknown config subcommand: ${subcommand}`);
  return 1;
}

async function dispatch(command: string, args: string[]): Promise<number> {
  switch (command) {
    case "init":
      return runInitCommand(args);
    case "refresh":
    case "brain":
      return runRefreshCommand(args);
    case "context":
      return runContextCommand(args);
    case "maintain":
      return runMaintainCommand(args);
    case "remember":
      return runRememberCommand(args);
    case "checkpoint":
      return runCheckpointCommand(args);
    case "status":
      return runStatusCommand(args);
    case "session":
      return runSessionCommand(args);
    case "graph":
      return runGraphCommand(args);
    case "harness":
      return runHarnessCommand(args);
    case "agents":
      return runAgentsCommand(args);
    case "mcp":
      return runMcpCommand(args);
    case "daemon":
      return runDaemonCommand(args);
    case "sync":
      return runSyncCommand(args);
    case "index":
      return hasFlag(args, "--store")
        ? runIndexCommand(withoutFlag(args, "--store"))
        : runRefreshCommand(args);
    case "search":
      return hasFlag(args, "--store")
        ? runSearchCommand(withoutFlag(args, "--store"))
        : runContextCommand(args);
    case "watch":
      return runWatchCommand(args);
    case "serve":
      await runServe(args);
      return 0;
    case "doctor":
      await runDoctor(args);
      return 0;
    case "discover":
      await runDiscover(args);
      return 0;
    case "config":
      return runConfigCommand(args);
    case "connect":
      console.error("provena connect moved to the optional enterprise control plane");
      return 1;
    default:
      console.error(`Unknown command: ${command}`);
      printHelp();
      return 1;
  }
}

async function main(argv: string[]): Promise<number> {
  const args = argv.slice(2);
  if (args.length === 0 || hasFlag(args.slice(0, 1), "--help", "-h")) {
    printHelp();
    return 0;
  }
  if (hasFlag(args.slice(0, 1), "--version", "-v")) {
    console.log(readVersion());
    return 0;
  }
  return dispatch(args[0]!, args.slice(1));
}

main(process.argv)
  .then((code) => process.exit(code))
  .catch((error: unknown) => {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`provena: ${message}`);
    if (process.env.PROVENA_DEBUG === "1" && error instanceof Error && error.stack) {
      console.error(error.stack);
    }
    process.exit(1);
  });
