import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { refreshRepoBrain } from "../brain/index.js";
import { getGitRoot, loadConfig } from "../config.js";
import { renderContextPacketMarkdown, type ContextPacket } from "../context/index.js";
import {
  compileMaintenanceTaskContext,
  maintenancePlanView,
  type MaintenancePlanView,
} from "../maintenance/index.js";
import { assertSafeRepoPath } from "../security/paths.js";

const DEFAULT_PLAN_LIMIT = 32;
const MAX_PLAN_LIMIT = 256;
const DEFAULT_CONTEXT_TOKENS = 1_500;
const MIN_CONTEXT_TOKENS = 64;
const MAX_CONTEXT_TOKENS = 100_000;
const MAINTENANCE_TASK_ID_PATTERN = /^maintenance-task:[a-f0-9]{20}$/;

interface PlanArguments {
  json: boolean;
  limit: number;
}

interface ContextArguments {
  json: boolean;
  maxTokens: number;
  taskId: string;
}

function boundedPositiveInteger(
  value: string | undefined,
  minimum: number,
  maximum: number,
  error: string,
): number {
  if (value === undefined || !/^[1-9]\d*$/.test(value)) throw new Error(error);
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new Error(error);
  }
  return parsed;
}

function parsePlanArguments(args: string[]): PlanArguments {
  let json = false;
  let limit = DEFAULT_PLAN_LIMIT;
  let hasLimit = false;
  for (let index = 0; index < args.length; index += 1) {
    const value = args[index]!;
    if (value === "--json") {
      if (json) throw new Error("invalid maintain plan arguments");
      json = true;
      continue;
    }
    if (value === "--limit") {
      if (hasLimit) throw new Error("invalid maintain plan arguments");
      hasLimit = true;
      limit = boundedPositiveInteger(
        args[index + 1],
        1,
        MAX_PLAN_LIMIT,
        "invalid maintain plan arguments",
      );
      index += 1;
      continue;
    }
    throw new Error("invalid maintain plan arguments");
  }
  return { json, limit };
}

function parseContextArguments(args: string[]): ContextArguments {
  let json = false;
  let maxTokens = DEFAULT_CONTEXT_TOKENS;
  let hasMaxTokens = false;
  let taskId: string | undefined;
  for (let index = 0; index < args.length; index += 1) {
    const value = args[index]!;
    if (value === "--json") {
      if (json) throw new Error("invalid maintain context arguments");
      json = true;
      continue;
    }
    if (value === "--max-tokens") {
      if (hasMaxTokens) throw new Error("invalid maintain context arguments");
      hasMaxTokens = true;
      maxTokens = boundedPositiveInteger(
        args[index + 1],
        MIN_CONTEXT_TOKENS,
        MAX_CONTEXT_TOKENS,
        "invalid maintain context arguments",
      );
      index += 1;
      continue;
    }
    if (taskId === undefined && MAINTENANCE_TASK_ID_PATTERN.test(value)) {
      taskId = value;
      continue;
    }
    throw new Error("invalid maintain context arguments");
  }
  if (taskId === undefined) throw new Error("invalid maintain context arguments");
  return { json, maxTokens, taskId };
}

function renderMaintenancePlanView(view: MaintenancePlanView): string {
  const lines = [
    "Provena maintenance proposals",
    `Plan fingerprint: ${view.planFingerprint}`,
    `Tasks: ${view.returnedTasks} of ${view.totalTasks}`,
  ];
  if (view.tasks.length === 0) lines.push("No maintenance tasks are currently proposed.");
  for (const task of view.tasks) {
    lines.push(`- ${task.id} (${task.kind})`);
    if (task.memoryIds.length > 0) lines.push(`  memories: ${task.memoryIds.join(", ")}`);
    if (task.paths.length > 0) lines.push(`  paths: ${task.paths.join(", ")}`);
  }
  return `${lines.join("\n")}\n`;
}

function writeContextOutput(
  repoRoot: string,
  packet: ContextPacket,
  json: boolean,
): string {
  const rendered = json
    ? `${JSON.stringify(packet, null, 2)}\n`
    : renderContextPacketMarkdown(packet);
  const contextDir = join(repoRoot, ".provena", "context");
  assertSafeRepoPath(repoRoot, contextDir);
  mkdirSync(contextDir, { recursive: true });
  const outputPath = join(contextDir, json ? "latest.json" : "latest.md");
  assertSafeRepoPath(repoRoot, outputPath);
  writeFileSync(outputPath, rendered, "utf8");
  return rendered;
}

export function printMaintainHelp(): void {
  console.log("Usage:");
  console.log("  provena maintain plan [--limit N] [--json]");
  console.log("  provena maintain context <task-id> [--max-tokens N] [--json]");
  console.log("");
  console.log("List deterministic review proposals or compile one cited task packet.");
  console.log("Commands do not spawn agents or approve/apply proposals.");
  console.log("Each CLI command performs one normal refresh, which may append source-grounded observations.");
}

export async function runMaintainCommand(
  args: string[],
  cwd = process.cwd(),
): Promise<number> {
  const subcommand = args[0];
  if (
    subcommand === undefined ||
    subcommand === "--help" ||
    subcommand === "-h" ||
    ((subcommand === "plan" || subcommand === "context") &&
      args.length === 2 &&
      (args[1] === "--help" || args[1] === "-h"))
  ) {
    printMaintainHelp();
    return 0;
  }

  if (subcommand === "plan") {
    const parsed = parsePlanArguments(args.slice(1));
    const repoRoot = getGitRoot(cwd);
    loadConfig(repoRoot);
    const refreshed = await refreshRepoBrain(repoRoot);
    const view = maintenancePlanView(refreshed.maintenancePlan, parsed.limit);
    process.stdout.write(
      parsed.json ? `${JSON.stringify(view, null, 2)}\n` : renderMaintenancePlanView(view),
    );
    return 0;
  }

  if (subcommand === "context") {
    const parsed = parseContextArguments(args.slice(1));
    const repoRoot = getGitRoot(cwd);
    loadConfig(repoRoot);
    const refreshed = await refreshRepoBrain(repoRoot);
    const packet = compileMaintenanceTaskContext(
      refreshed.map,
      refreshed.graph,
      refreshed.memory,
      refreshed.maintenancePlan,
      parsed.taskId,
      { maxTokens: parsed.maxTokens },
    );
    process.stdout.write(writeContextOutput(repoRoot, packet, parsed.json));
    return 0;
  }

  throw new Error("invalid maintain command");
}
