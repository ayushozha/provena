import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { getGitRoot, loadConfig } from "../config.js";
import {
  refreshRepoBrain,
  REPO_BRAIN_PATH,
} from "../brain/index.js";
import { buildContextPacket, renderContextPacketMarkdown } from "../context/index.js";
import { assertSafeRepoPath } from "../security/paths.js";

function valueAfter(args: string[], flag: string): string | undefined {
  const at = args.indexOf(flag);
  return at >= 0 ? args[at + 1] : undefined;
}

function queryFrom(args: string[]): string {
  const values: string[] = [];
  for (let index = 0; index < args.length; index += 1) {
    const value = args[index]!;
    if (["--agent", "--max-tokens"].includes(value)) {
      index += 1;
      continue;
    }
    if (value !== "start" && !value.startsWith("-")) values.push(value);
  }
  return values.join(" ");
}

export async function runSessionCommand(
  args: string[],
  cwd = process.cwd(),
): Promise<number> {
  if (args.includes("--help") || args.includes("-h")) {
    console.log('Usage: provena session start ["<task>"] [--agent codex] [--json]');
    return 0;
  }
  if (args[0] && args[0] !== "start" && !args[0].startsWith("-")) {
    throw new Error(`unknown session subcommand: ${args[0]}`);
  }
  const repoRoot = getGitRoot(cwd);
  loadConfig(repoRoot);
  const refreshed = await refreshRepoBrain(repoRoot);
  const rawTokens = valueAfter(args, "--max-tokens");
  const maxTokens = rawTokens === undefined ? 2_500 : Number(rawTokens);
  if (!Number.isInteger(maxTokens) || maxTokens < 64) {
    throw new Error("--max-tokens must be an integer of at least 64");
  }
  const packet = buildContextPacket(refreshed.map, refreshed.graph, refreshed.memory, {
    query: queryFrom(args),
    maxTokens,
  });
  const session = {
    schemaVersion: 1,
    id: randomUUID(),
    agent: valueAfter(args, "--agent") ?? "unknown",
    startedAt: new Date().toISOString(),
    sourceFingerprint: refreshed.map.sourceFingerprint,
    memoryFingerprint: packet.memoryFingerprint,
    query: packet.query,
  };
  const sessionDir = join(repoRoot, ".provena", "cache", "sessions");
  assertSafeRepoPath(repoRoot, sessionDir);
  mkdirSync(sessionDir, { recursive: true });
  const sessionPath = join(sessionDir, `${session.id}.json`);
  assertSafeRepoPath(repoRoot, sessionPath);
  writeFileSync(sessionPath, `${JSON.stringify(session, null, 2)}\n`, "utf8");

  if (args.includes("--quiet")) return 0;
  if (args.includes("--json")) {
    console.log(JSON.stringify({ session, packet }, null, 2));
  } else {
    const brain = readFileSync(join(repoRoot, ...REPO_BRAIN_PATH.split("/")), "utf8");
    process.stdout.write(`${brain.trimEnd()}\n\n${renderContextPacketMarkdown(packet)}`);
  }
  return 0;
}
