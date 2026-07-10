import { spawnSync } from "node:child_process";
import { getGitRoot, loadConfig } from "../config.js";
import {
  appendMemoryEvent,
  memoryEventToRecord,
  refreshRepoBrain,
} from "../brain/index.js";

function git(repoRoot: string, args: string[]): string {
  const result = spawnSync("git", args, {
    cwd: repoRoot,
    encoding: "utf8",
    shell: false,
  });
  return result.status === 0 ? result.stdout.trim() : "";
}

function gitRaw(repoRoot: string, args: string[]): string {
  const result = spawnSync("git", args, {
    cwd: repoRoot,
    encoding: "utf8",
    shell: false,
  });
  return result.status === 0 ? result.stdout : "";
}

function valueAfter(args: string[], flag: string): string | undefined {
  const at = args.indexOf(flag);
  return at >= 0 ? args[at + 1] : undefined;
}

function valuesAfter(args: string[], flag: string): string[] {
  return args.flatMap((value, index) =>
    value === flag && args[index + 1] ? [args[index + 1]!] : [],
  );
}

function changedPaths(repoRoot: string): string[] {
  const paths = [
    gitRaw(repoRoot, ["diff", "--name-only", "-z"]),
    gitRaw(repoRoot, ["diff", "--cached", "--name-only", "-z"]),
    gitRaw(repoRoot, ["ls-files", "--others", "--exclude-standard", "-z"]),
  ].flatMap((value) => value.split("\0").filter(Boolean));
  return [...new Set(paths.map((path) => path.replace(/\\/g, "/")))]
    .sort((left, right) => left < right ? -1 : left > right ? 1 : 0)
    .slice(0, 200);
}

export function printCheckpointHelp(): void {
  console.log('Usage: provena checkpoint --summary "<handoff>" [--next "<step>"]');
  console.log("");
  console.log("Record an explicit, source-aware handoff and refresh the repo brain.");
  console.log("--authority human|agent is required.");
}

export async function runCheckpointCommand(
  args: string[],
  cwd = process.cwd(),
): Promise<number> {
  if (args.includes("--help") || args.includes("-h")) {
    printCheckpointHelp();
    return 0;
  }
  const repoRoot = getGitRoot(cwd);
  loadConfig(repoRoot);
  const branch = git(repoRoot, ["branch", "--show-current"]) || "detached";
  const commit = git(repoRoot, ["rev-parse", "HEAD"]) || "uncommitted";
  const dirtyFiles = changedPaths(repoRoot);
  const nextSteps = valuesAfter(args, "--next");
  const summary =
    valueAfter(args, "--summary") ??
    `Checkpoint on ${branch} at ${commit.slice(0, 12)} with ${dirtyFiles.length} changed paths.`;
  const actor = valueAfter(args, "--actor") ?? process.env.PROVENA_ACTOR ?? "local-user";
  const authority = valueAfter(args, "--authority") as "human" | "agent" | undefined;
  if (!authority || !["human", "agent"].includes(authority)) {
    throw new Error("--authority human|agent is required for checkpoints");
  }
  const event = await appendMemoryEvent(repoRoot, {
    kind: "handoff",
    subjectType: "task",
    title: valueAfter(args, "--title") ?? `Handoff from ${branch}`,
    body: summary,
    structuredData: { branch, commit, dirtyFiles, nextSteps },
    appliesTo: dirtyFiles,
    sources: valuesAfter(args, "--source").map((path) => ({ path })),
    provenance: {
      actor,
      method: "explicit",
      ...(valueAfter(args, "--agent") ? { agent: valueAfter(args, "--agent") } : {}),
      ...(valueAfter(args, "--session")
        ? { sessionId: valueAfter(args, "--session") }
        : {}),
      command: "provena checkpoint",
    },
    authority,
    confidence: 1,
    importance: 0.7,
    tags: ["checkpoint", "handoff"],
    triggers: ["session-start", "handoff"],
  });
  await refreshRepoBrain(repoRoot);
  console.log(
    args.includes("--json")
      ? JSON.stringify(memoryEventToRecord(event), null, 2)
      : `Checkpoint recorded: ${event.id}`,
  );
  return 0;
}
