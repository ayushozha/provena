import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { getGitRoot, loadConfig } from "../config.js";
import { refreshRepoBrain } from "../brain/index.js";
import { assertSafeRepoPath } from "../security/paths.js";
import {
  buildContextPacket,
  renderContextPacketMarkdown,
  type ContextQuery,
} from "../context/index.js";

const VALUE_FLAGS = new Set([
  "--path",
  "--symbol",
  "--command",
  "--max-items",
  "--limit",
  "--max-chars",
  "--max-tokens",
  "--graph-hops",
]);

function valuesAfter(args: string[], flag: string): string[] {
  return args.flatMap((value, index) =>
    value === flag && args[index + 1] ? [args[index + 1]!] : [],
  );
}

function numberAfter(args: string[], flag: string): number | undefined {
  const value = valuesAfter(args, flag).at(-1);
  if (value === undefined) return undefined;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`${flag} must be a number`);
  return parsed;
}

function positional(args: string[]): string[] {
  const result: string[] = [];
  for (let index = 0; index < args.length; index += 1) {
    const value = args[index]!;
    if (VALUE_FLAGS.has(value)) {
      index += 1;
      continue;
    }
    if (!value.startsWith("-")) result.push(value);
  }
  return result;
}

export function printContextHelp(): void {
  console.log('Usage: provena context "<task>" [options]');
  console.log("");
  console.log("Build a cited task packet from exact paths, symbols, commands,");
  console.log("durable memories, lexical matches, and graph proximity.");
  console.log("");
  console.log("Options: --path, --symbol, --command, --max-tokens, --json");
}

export async function runContextCommand(
  args: string[],
  cwd = process.cwd(),
): Promise<number> {
  if (args.includes("--help") || args.includes("-h")) {
    printContextHelp();
    return 0;
  }
  const repoRoot = getGitRoot(cwd);
  loadConfig(repoRoot);
  const refreshed = await refreshRepoBrain(repoRoot);
  const input: ContextQuery = {
    query: positional(args).join(" "),
    paths: valuesAfter(args, "--path"),
    symbols: valuesAfter(args, "--symbol"),
    commands: valuesAfter(args, "--command"),
    maxItems: numberAfter(args, "--max-items") ?? numberAfter(args, "--limit"),
    maxCharacters: numberAfter(args, "--max-chars"),
    maxTokens: numberAfter(args, "--max-tokens"),
    graphHops: numberAfter(args, "--graph-hops"),
    includeSensitive: false,
  };
  const packet = buildContextPacket(
    refreshed.map,
    refreshed.graph,
    refreshed.memory,
    input,
  );
  const markdown = renderContextPacketMarkdown(packet);
  const contextDir = join(repoRoot, ".provena", "context");
  assertSafeRepoPath(repoRoot, contextDir);
  mkdirSync(contextDir, { recursive: true });
  const outputPath = join(contextDir, args.includes("--json") ? "latest.json" : "latest.md");
  assertSafeRepoPath(repoRoot, outputPath);
  writeFileSync(
    outputPath,
    args.includes("--json") ? `${JSON.stringify(packet, null, 2)}\n` : markdown,
    "utf8",
  );
  process.stdout.write(
    args.includes("--json") ? `${JSON.stringify(packet, null, 2)}\n` : markdown,
  );
  return 0;
}
