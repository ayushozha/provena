#!/usr/bin/env node
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { runConfigShow } from "./commands/config-show.js";
import { runDiscover } from "./commands/discover.js";
import { runDoctor } from "./commands/doctor.js";
import { runIndexCommand } from "./commands/index.js";
import { runInit } from "./commands/init.js";
import { runSearchCommand } from "./commands/search.js";
import { runServe } from "./commands/serve.js";
import { runWatchCommand } from "./commands/watch.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

function readVersion(): string {
  const pkgPath = join(__dirname, "..", "package.json");
  const pkg = JSON.parse(readFileSync(pkgPath, "utf8")) as { version: string };
  return pkg.version;
}

const STUB_COMMANDS = ["status", "connect", "mcp"] as const;

type StubCommand = (typeof STUB_COMMANDS)[number];

interface CommandInfo {
  name: string;
  description: string;
}

const COMMANDS: CommandInfo[] = [
  { name: "init", description: "Initialize .provena/ config in a git repo" },
  { name: "config", description: "Show or manage local config" },
  { name: "discover", description: "List indexable files (debug)" },
  { name: "index", description: "Index TS/JS repo into Provena memories" },
  { name: "watch", description: "Watch repo and run incremental index on save" },
  { name: "search", description: "Search indexed memories in the local store" },
  { name: "serve", description: "start local SQLite store (uvicorn)" },
  { name: "doctor", description: "check config and store health" },
  ...STUB_COMMANDS.map((name) => ({
    name,
    description: "not implemented yet",
  })),
];

function printHelp(): void {
  const lines = [
    "provena — governed memory plane CLI",
    "",
    "Usage:",
    "  provena <command> [options]",
    "",
    "Commands:",
    ...COMMANDS.map((c) => `  ${c.name.padEnd(10)} ${c.description}`),
    "",
    "Index options:",
    "  --dry-run    List TS/JS files without writing",
    "  --path       Limit to a repo subtree (e.g. src/auth)",
    "  --full       Re-index every file (ignore content-hash skip)",
    "",
    "Watch options:",
    "  --interval   Polling fallback (e.g. 30s) when native watch is unavailable",
    "",
    "Search options:",
    "  --limit N    Maximum hits (default 5)",
    "  --json       Machine-readable SearchResponse",
    "  --explain    Include store explain payload when available",
    "",
    "Serve options:",
    "  --detach     Run store in background; write .provena/store.pid",
    "  --stop       Stop detached store process",
    "",
    "Global options:",
    "  --version    Show package version",
    "  --help, -h   Show this help",
    "",
  ];
  console.log(lines.join("\n"));
}

function runStub(command: StubCommand): void {
  console.error(`provena ${command}: not implemented yet (see roadmap/plan/)`);
  process.exit(1);
}

function hasFlag(args: string[], ...flags: string[]): boolean {
  return flags.some((flag) => args.includes(flag));
}

function runConfigCommand(args: string[]): number {
  const sub = args[1];
  if (sub === "show" || sub === undefined) {
    if (hasFlag(args, "--help", "-h")) {
      console.log("Usage: provena config show");
      return 0;
    }
    return runConfigShow();
  }

  console.error(`Unknown config subcommand: ${sub}`);
  console.error("Usage: provena config show");
  return 1;
}

function runInitCommand(args: string[]): number {
  if (hasFlag(args, "--help", "-h")) {
    console.log("Usage: provena init [--force]");
    console.log("");
    console.log("Options:");
    console.log("  --force, -f   Overwrite existing config");
    return 0;
  }

  const force = hasFlag(args, "--force", "-f");
  return runInit({ force });
}

function main(argv: string[]): void {
  const args = argv.slice(2);

  if (args.length === 0 || args.includes("--help") || args.includes("-h")) {
    printHelp();
    process.exit(0);
  }

  if (args.includes("--version") || args[0] === "-v") {
    console.log(readVersion());
    process.exit(0);
  }

  const command = args[0];

  if (command === "init") {
    process.exit(runInitCommand(args));
  }

  if (command === "config") {
    process.exit(runConfigCommand(args));
  }

  if (command === "discover") {
    runDiscover(args.slice(1))
      .then(() => process.exit(0))
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        console.error(`provena discover: ${message}`);
        process.exit(1);
      });
    return;
  }

  if (command === "index") {
    runIndexCommand(args.slice(1))
      .then((code) => process.exit(code))
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        console.error(`provena index: ${message}`);
        process.exit(1);
      });
    return;
  }

  if (command === "watch") {
    runWatchCommand(args.slice(1))
      .then((code) => process.exit(code))
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        console.error(`provena watch: ${message}`);
        process.exit(1);
      });
    return;
  }

  if (command === "search") {
    runSearchCommand(args.slice(1))
      .then((code) => process.exit(code))
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        console.error(`provena search: ${message}`);
        process.exit(1);
      });
    return;
  }

  if (command === "serve" || command === "doctor") {
    const run = command === "serve" ? runServe : runDoctor;
    run(args.slice(1))
      .then(() => process.exit(0))
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        console.error(`provena ${command}: ${message}`);
        process.exit(1);
      });
    return;
  }

  if ((STUB_COMMANDS as readonly string[]).includes(command)) {
    runStub(command as StubCommand);
  }

  console.error(`Unknown command: ${command}`);
  printHelp();
  process.exit(1);
}

main(process.argv);