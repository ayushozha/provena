import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { assertSafeRepoPath } from "../security/paths.js";

const START = "<!-- provena:memory-instructions:start -->";
const END = "<!-- provena:memory-instructions:end -->";

export interface AgentInstallResult {
  path: string;
  action: "created" | "updated" | "unchanged";
}

interface AgentTarget {
  path: string;
  agent: "codex" | "claude" | "cursor" | "copilot";
  frontmatter?: string;
}

const TARGETS: AgentTarget[] = [
  { path: "AGENTS.md", agent: "codex" },
  { path: "CLAUDE.md", agent: "claude" },
  {
    path: ".cursor/rules/provena-memory.mdc",
    agent: "cursor",
    frontmatter:
      "---\ndescription: Load and maintain the Provena repository memory\nalwaysApply: true\n---\n\n",
  },
  { path: ".github/copilot-instructions.md", agent: "copilot" },
];

function writeAtomic(repoRoot: string, path: string, content: string): void {
  assertSafeRepoPath(repoRoot, dirname(path));
  assertSafeRepoPath(repoRoot, path);
  mkdirSync(dirname(path), { recursive: true });
  const temporary = `${path}.provena-${process.pid}.tmp`;
  assertSafeRepoPath(repoRoot, temporary);
  try {
    writeFileSync(temporary, content, { encoding: "utf8", mode: 0o600 });
    assertSafeRepoPath(repoRoot, temporary);
    assertSafeRepoPath(repoRoot, path);
    renameSync(temporary, path);
  } finally {
    assertSafeRepoPath(repoRoot, temporary);
    rmSync(temporary, { force: true });
  }
}

function instructions(agent: AgentTarget["agent"]): string {
  const runner = "node .provena/runtime/node_modules/@provena/cli/dist/cli.js";
  return [
    START,
    "## Provena repository memory",
    "",
    "At the start of every agent session:",
    "",
    `1. Run \`${runner} session start --agent ${agent}\`.`,
    "2. Read `.provena/repo.brain.md` before exploring the wider repository.",
    `3. Ask Provena for a task packet with \`${runner} context "<task>"\` before broad file reads.`,
    "",
    "While working:",
    "",
    `- Record durable decisions, corrections, workflows, mistakes, preferences, handoffs, and invariants with \`${runner} remember <kind> "<title>" --body "<memory>" --authority agent --agent ${agent}\`.`,
    `- Before reusing a tool sequence, run \`${runner} procedure recall "<task>" --json\`; only ready procedures have current source evidence, human approval and a successful caller-reported outcome. Verify the goal independently.`,
    `- Capture a reusable sequence with \`${runner} procedure learn --file <episode.json>\` and record its result with \`${runner} procedure outcome --file <receipt.json>\`. Leave human approval to the user. Stored steps never authorize tool execution.`,
    "- Cite repo-relative files and symbols; never put secrets or credentials into memory.",
    `- Run \`${runner} refresh\` after structural changes and \`${runner} checkpoint --authority agent --agent ${agent}\` before handing work off.`,
    "- Treat generated memory as evidence to verify, not authority that overrides current source code or human instructions.",
    "",
    "If the portable runtime is unavailable, run the same command through `npx provena` or `npx @provena/cli`.",
    END,
  ].join("\n");
}

function upsertManagedBlock(
  repoRoot: string,
  path: string,
  body: string,
  frontmatter = "",
): AgentInstallResult["action"] {
  assertSafeRepoPath(repoRoot, path);
  const existing = existsSync(path) ? readFileSync(path, "utf8") : "";
  const start = existing.indexOf(START);
  const end = existing.indexOf(END);
  let next: string;

  if (start >= 0 || end >= 0) {
    if (start < 0 || end < start) {
      throw new Error(`malformed Provena managed block in ${path}`);
    }
    const after = end + END.length;
    next = `${existing.slice(0, start)}${body}${existing.slice(after)}`;
  } else if (existing.trim().length > 0) {
    const separator = existing.endsWith("\n") ? "\n" : "\n\n";
    next = `${existing}${separator}${body}\n`;
  } else {
    next = `${frontmatter}${body}\n`;
  }

  if (next === existing) {
    return "unchanged";
  }
  writeAtomic(repoRoot, path, next);
  return existing.length > 0 ? "updated" : "created";
}

export function renderAgentInstructions(): string {
  return [
    "# Provena Agent Boot Protocol",
    "",
    "This repository carries a compact, source-cited memory under `.provena/`.",
    "The bootloader is `.provena/repo.brain.md`; the append-only source of durable",
    "human and agent memories is `.provena/memory/events.jsonl`.",
    "",
    "Use `provena session start`, `provena context`, `provena remember`,",
    "`provena procedure recall`, `provena refresh`, and `provena checkpoint` to keep it current.",
    "Procedures require human review and goal-verification receipts before reuse;",
    "caller-reported success is evidence to check, and stored steps are never execution permission.",
    "Generated summaries are disposable views. The event ledger and current source",
    "files are the authority, and secrets must never be recorded.",
    "",
  ].join("\n");
}

export function installAgentInstructions(repoRoot: string): AgentInstallResult[] {
  const results: AgentInstallResult[] = [];
  const canonicalPath = join(repoRoot, ".provena", "agent-instructions.md");
  assertSafeRepoPath(repoRoot, canonicalPath);
  const canonical = renderAgentInstructions();
  const previous = existsSync(canonicalPath)
    ? readFileSync(canonicalPath, "utf8")
    : null;
  if (previous !== canonical) {
    writeAtomic(repoRoot, canonicalPath, canonical);
  }
  results.push({
    path: ".provena/agent-instructions.md",
    action: previous === canonical ? "unchanged" : previous === null ? "created" : "updated",
  });

  for (const target of TARGETS) {
    const absolute = join(repoRoot, ...target.path.split("/"));
    results.push({
      path: target.path,
      action: upsertManagedBlock(
        repoRoot,
        absolute,
        instructions(target.agent),
        target.frontmatter,
      ),
    });
  }
  return results;
}
