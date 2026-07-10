import { readFileSync } from "node:fs";
import { getGitRoot, loadConfig } from "../config.js";
import {
  appendMemoryEvent,
  memoryEventToRecord,
  refreshRepoBrain,
  type MemoryAuthority,
  type MemoryKind,
  type MemorySensitivity,
  type MemorySource,
  type MemorySubjectType,
} from "../brain/index.js";
import { assertNoSecretMaterial } from "../security/memory.js";

function valuesAfter(args: string[], flag: string): string[] {
  return args.flatMap((value, index) =>
    value === flag && args[index + 1] ? [args[index + 1]!] : [],
  );
}

function valueAfter(args: string[], flag: string): string | undefined {
  return valuesAfter(args, flag).at(-1);
}

function numeric(value: string | undefined, flag: string): number | undefined {
  if (value === undefined) return undefined;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`${flag} must be a number`);
  return parsed;
}

function parseSource(value: string): MemorySource {
  const match = /^(.+?)(?::(\d+)(?:-(\d+))?)?$/.exec(value.trim());
  if (!match?.[1]) throw new Error(`invalid --source value: ${value}`);
  return {
    path: match[1],
    ...(match[2] ? { startLine: Number(match[2]) } : {}),
    ...(match[3] ? { endLine: Number(match[3]) } : {}),
  };
}

export function printRememberHelp(): void {
  console.log('Usage: provena remember <kind> "<title>" --body "<memory>" [options]');
  console.log("");
  console.log("Kinds: fact, decision, workflow, mistake, preference, handoff, invariant");
  console.log("Options:");
  console.log("  --subject <type>       repo, file, symbol, command, test, api, architecture, task");
  console.log("  --source path[:line]   Repeatable source citation");
  console.log("  --applies-to path      Repeatable scope path");
  console.log("  --tag value            Repeatable tag");
  console.log("  --authority <value>    Required: human or agent");
  console.log("  --stdin                Read the body from stdin");
}

export async function runRememberCommand(
  args: string[],
  cwd = process.cwd(),
): Promise<number> {
  if (args.includes("--help") || args.includes("-h")) {
    printRememberHelp();
    return 0;
  }
  const kind = args[0] as MemoryKind | undefined;
  const title = args[1];
  if (!kind || !title) {
    printRememberHelp();
    return 1;
  }
  const body = args.includes("--stdin")
    ? readFileSync(0, "utf8")
    : valueAfter(args, "--body");
  if (!body?.trim()) throw new Error("memory body is required (--body or --stdin)");
  assertNoSecretMaterial(`${title}\n${body}`);
  const rationale = valueAfter(args, "--rationale");
  if (rationale) assertNoSecretMaterial(rationale);
  const repoRoot = getGitRoot(cwd);
  loadConfig(repoRoot);
  const authorityValue = valueAfter(args, "--authority");
  if (!authorityValue || !["human", "agent"].includes(authorityValue)) {
    throw new Error("--authority human|agent is required for explicit memory writes");
  }
  const authority = authorityValue as MemoryAuthority;
  const actor = valueAfter(args, "--actor") ?? process.env.PROVENA_ACTOR ?? "local-user";
  const event = await appendMemoryEvent(repoRoot, {
    kind,
    subjectType: (valueAfter(args, "--subject") ?? "repo") as MemorySubjectType,
    title,
    body,
    structuredData: rationale ? { rationale } : {},
    appliesTo: valuesAfter(args, "--applies-to"),
    sources: valuesAfter(args, "--source").map(parseSource),
    provenance: {
      actor,
      method: "explicit",
      ...(valueAfter(args, "--agent") ? { agent: valueAfter(args, "--agent") } : {}),
      ...(valueAfter(args, "--session")
        ? { sessionId: valueAfter(args, "--session") }
        : {}),
      command: "provena remember",
    },
    authority,
    confidence: numeric(valueAfter(args, "--confidence"), "--confidence"),
    importance: numeric(valueAfter(args, "--importance"), "--importance"),
    sensitivity: (valueAfter(args, "--sensitivity") ?? "internal") as MemorySensitivity,
    supersedes: valuesAfter(args, "--supersedes"),
    tags: valuesAfter(args, "--tag"),
    triggers: valuesAfter(args, "--trigger"),
  });
  await refreshRepoBrain(repoRoot);
  if (args.includes("--json")) console.log(JSON.stringify(memoryEventToRecord(event), null, 2));
  else console.log(`Remembered ${event.kind}: ${event.title} (${event.id})`);
  return 0;
}
