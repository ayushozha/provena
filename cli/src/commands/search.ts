import { existsSync } from "node:fs";
import {
  ProvenaClient,
  scopeEnvelopeFromConfig,
  type SearchExplainResponse,
  type SearchResponse,
} from "../client.js";
import { loadConfig } from "../config.js";
import { formatSearchJson, formatSearchTable } from "../format.js";
import { indexStatePath } from "../indexer/emit.js";

export const DEFAULT_SEARCH_LIMIT = 5;

export interface SearchCommandOptions {
  query: string;
  limit: number;
  json: boolean;
  explain: boolean;
}

function hasFlag(args: string[], ...flags: string[]): boolean {
  return flags.some((flag) => args.includes(flag));
}

function optionValue(args: string[], flag: string): string | undefined {
  const index = args.indexOf(flag);
  if (index === -1) {
    return undefined;
  }
  const value = args[index + 1];
  if (!value || value.startsWith("-")) {
    return undefined;
  }
  return value;
}

function parseLimit(raw: string | undefined): number | undefined {
  if (raw === undefined) {
    return undefined;
  }
  const limit = Number.parseInt(raw, 10);
  if (!Number.isFinite(limit) || limit <= 0) {
    return undefined;
  }
  return limit;
}

/** Parse `provena search` argv (after the `search` token). */
export function parseSearchArgs(args: string[]): SearchCommandOptions | "help" | "error" {
  if (hasFlag(args, "--help", "-h")) {
    return "help";
  }

  const positional: string[] = [];
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === "--json") {
      continue;
    }
    if (arg === "--explain") {
      continue;
    }
    if (arg === "--limit") {
      i += 1;
      continue;
    }
    if (arg.startsWith("-")) {
      return "error";
    }
    positional.push(arg);
  }

  if (positional.length === 0) {
    return "error";
  }

  const limitRaw = optionValue(args, "--limit");
  const parsedLimit = parseLimit(limitRaw);
  if (limitRaw !== undefined && parsedLimit === undefined) {
    return "error";
  }

  return {
    query: positional.join(" "),
    limit: parsedLimit ?? DEFAULT_SEARCH_LIMIT,
    json: hasFlag(args, "--json"),
    explain: hasFlag(args, "--explain"),
  };
}

export function printSearchHelp(): void {
  console.log("Usage: provena search <query> [options]");
  console.log("");
  console.log("Options:");
  console.log("  --limit <n>   Maximum hits to return (default: 5)");
  console.log("  --json        Emit machine-readable SearchResponse JSON");
  console.log("  --explain     Include store search explain payload when available");
  console.log("  --help, -h    Show this help");
}

function formatExplainSummary(explain: SearchExplainResponse): string {
  const lines: string[] = [
    "",
    `Explain: ${explain.candidate_strategy} (${explain.total_candidates} candidates)`,
  ];
  for (const candidate of explain.returned) {
    const title = candidate.memory.title ?? candidate.memory.memory_id;
    lines.push(
      `  #${candidate.rank ?? "?"} score=${candidate.score.toFixed(3)} ${title}`,
    );
    if (candidate.reasons.length > 0) {
      lines.push(`    reasons: ${candidate.reasons.join(", ")}`);
    }
  }
  if ((explain.filtered_out?.length ?? 0) > 0) {
    lines.push(`  filtered out: ${explain.filtered_out?.length}`);
  }
  return lines.join("\n");
}

export async function runSearchCommand(args: string[]): Promise<number> {
  const parsed = parseSearchArgs(args);
  if (parsed === "help") {
    printSearchHelp();
    return 0;
  }
  if (parsed === "error") {
    console.error("Usage: provena search <query> [--limit N] [--json] [--explain]");
    return 1;
  }

  const { config, projectRoot } = loadConfig();
  const statePath = indexStatePath(projectRoot);

  if (!existsSync(statePath)) {
    console.error(
      `provena search: no index at ${statePath}; run \`provena index\` first`,
    );
    return 1;
  }

  const client = new ProvenaClient({
    storeUrl: config.store_url,
    intelligenceUrl: config.intelligence_url,
  });

  const healthy = await client.healthz();
  if (!healthy) {
    console.error(
      `provena search: store not reachable at ${config.store_url}; run \`provena serve\` or \`provena doctor\``,
    );
    return 1;
  }

  const scope = scopeEnvelopeFromConfig(config.scope);
  const request = {
    query: parsed.query,
    scope,
    limit: parsed.limit,
  };

  let response: SearchResponse;
  try {
    response = await client.search(request);
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`provena search: ${message}`);
    return 1;
  }

  if (parsed.json) {
    process.stdout.write(formatSearchJson(response));
  } else {
    console.log(formatSearchTable(response.results));
    if (response.results.length === 0) {
      console.error(
        "provena search: no matches (try a different query or run `provena index`)",
      );
      return 1;
    }
  }

  if (parsed.explain) {
    const explain = await client.explainSearch(request);
    if (!explain) {
      console.error(
        "provena search: explain endpoint not available on this store (skipped)",
      );
    } else if (parsed.json) {
      process.stdout.write(`${JSON.stringify({ explain }, null, 2)}\n`);
    } else {
      console.log(formatExplainSummary(explain));
    }
  }

  return 0;
}