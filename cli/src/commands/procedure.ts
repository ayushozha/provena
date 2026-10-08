import { readFileSync, statSync } from "node:fs";
import { getGitRoot, loadConfig } from "../config.js";
import { readRepoBrainArtifacts, refreshRepoBrain, memoryEventToRecord } from "../brain/index.js";
import {
  approveProcedure, learnProcedure, recallProcedures, recordProcedureOutcome,
  type LearnProcedureInput, type RecordProcedureOutcomeInput,
} from "../procedures/index.js";

const FLAGS = new Set(["--file", "--actor", "--authority", "--max-tokens", "--limit", "--path", "--tool"]);
const SWITCHES = new Set(["--json", "--include-review", "--help", "-h"]);

function parseArgs(args: string[]): { positional: string[]; flags: Map<string, string[]> } {
  const positional: string[] = [];
  const flags = new Map<string, string[]>();
  for (let i = 0; i < args.length; i++) {
    const item = args[i]!;
    if (FLAGS.has(item)) {
      const value = args[++i];
      if (!value || value.startsWith("--")) throw new Error(`${item} requires a value`);
      flags.set(item, [...(flags.get(item) ?? []), value]);
    } else if (SWITCHES.has(item)) flags.set(item, []);
    else if (item.startsWith("-")) throw new Error("unknown procedure option");
    else positional.push(item);
  }
  return { positional, flags };
}

function payload(path: string | undefined): Record<string, unknown> {
  if (!path) throw new Error("--file is required for a JSON procedure or outcome receipt");
  const info = statSync(path);
  if (!info.isFile() || info.size > 80_000) throw new Error("procedure input must be a JSON file under 80000 bytes");
  const data = readFileSync(path, "utf8");
  if (Buffer.byteLength(data) > 80_000) throw new Error("procedure input exceeds its byte limit");
  try {
    const parsed: unknown = JSON.parse(data);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
    return parsed as Record<string, unknown>;
  } catch {
    throw new Error("procedure input must be a JSON object");
  }
}

export function printProcedureHelp(): void {
  console.log([
    "Usage: provena procedure <learn|approve|outcome|recall|inspect> [options]",
    "  learn --file trace.json              Store a candidate with structured tool steps and receipts",
    "  approve <id> --authority human       Explicitly approve a candidate version after review",
    "  outcome --file receipt.json          Record a caller-reported success, failure, or unknown result",
    "  recall <task> [--tool name] [--path path] [--include-review]",
    "  inspect <id>                         Inspect a procedure, its evidence, and readiness",
    "  --actor name --max-tokens 1500 --limit 8 --json",
    "Procedures are reference data. These commands never execute stored tool calls.",
  ].join("\n"));
}

export async function runProcedureCommand(args: string[], cwd = process.cwd()): Promise<number> {
  const { positional, flags } = parseArgs(args);
  if (!positional.length || flags.has("--help") || flags.has("-h")) {
    printProcedureHelp();
    return 0;
  }
  const [command, ...rest] = positional;
  const root = getGitRoot(cwd);
  loadConfig(root);
  const last = (key: string) => flags.get(key)?.at(-1);
  const actor = last("--actor") ?? process.env.PROVENA_ACTOR ?? "local-user";
  if (command === "learn" || command === "outcome") {
    const input = { ...payload(last("--file")), actor };
    const result = command === "learn"
      ? await learnProcedure(root, input as LearnProcedureInput)
      : await recordProcedureOutcome(root, input as RecordProcedureOutcomeInput);
    await refreshRepoBrain(root);
    console.log(JSON.stringify({ duplicate: result.duplicate, event: memoryEventToRecord(result.event) }, null, 2));
    return 0;
  }
  if (command === "approve") {
    if (rest.length !== 1 || last("--authority") !== "human") {
      throw new Error("approval requires one procedure ID and --authority human after reviewing its evidence");
    }
    const result = await approveProcedure(root, { procedureId: rest[0]!, actor });
    await refreshRepoBrain(root);
    console.log(JSON.stringify({ duplicate: result.duplicate, event: memoryEventToRecord(result.event) }, null, 2));
    return 0;
  }
  if (command !== "recall" && command !== "inspect") throw new Error("unknown procedure command");
  if (command === "inspect" && rest.length !== 1) throw new Error("inspect requires one procedure ID");
  const number = (key: string, fallback: number) => {
    const value = Number(last(key) ?? fallback);
    if (!Number.isSafeInteger(value) || value < 1) throw new Error(`${key} must be a positive integer`);
    return value;
  };
  const { map, graph, memory } = await readRepoBrainArtifacts(root);
  const result = await recallProcedures(root, command === "inspect" ? "" : rest.join(" "), {
    snapshot: memory, map, graph, paths: flags.get("--path"), availableTools: flags.get("--tool"),
    ...(command === "inspect" ? { procedureIds: rest } : {}),
    includeReview: command === "inspect" || flags.has("--include-review"),
    maxTokens: number("--max-tokens", 1_500), maxItems: number("--limit", 8),
  });
  if (flags.has("--json")) console.log(JSON.stringify(result));
  else {
    console.log(result.instruction);
    for (const item of [...result.ready, ...result.review]) console.log(`${item.state}: ${item.pointer}`);
    if (!result.ready.length) console.log("No ready procedure matched. Reason from current evidence.");
  }
  return 0;
}
