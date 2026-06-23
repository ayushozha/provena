import { relative } from "node:path";
import { ProvenaClient, scopeEnvelopeFromConfig } from "../client.js";
import type { ProvenaScope } from "../config.js";
import type { CodeChunk, FileMeta } from "./types.js";

export type EntityType =
  | "file"
  | "module"
  | "class"
  | "function"
  | "interface"
  | "method"
  | "type_alias";

export interface EntityRecord {
  canonical_name: string;
  entity_type: EntityType;
  aliases: string[];
}

export interface UpsertEntitiesOptions {
  storeUrl: string;
  projectRoot: string;
  repoRoot?: string;
  client?: ProvenaClient;
}

export interface UpsertEntitiesResult {
  upserted: number;
  skipped: boolean;
}

const SYMBOL_CHUNK_KINDS = new Set([
  "class",
  "function",
  "method",
  "interface",
  "type_alias",
]);

function posixRelative(repoRoot: string, absolutePath: string): string {
  return relative(repoRoot, absolutePath).split("\\").join("/");
}

function entityTypeForChunk(chunk: CodeChunk): EntityType {
  if (chunk.kind === "method") {
    return "method";
  }
  if (
    chunk.kind === "class" ||
    chunk.kind === "function" ||
    chunk.kind === "interface" ||
    chunk.kind === "type_alias"
  ) {
    return chunk.kind;
  }
  return "module";
}

/** Build entity registry rows from indexed chunks (PLAN-08/09). */
export function buildEntityRecords(
  chunks: CodeChunk[],
  fileMeta: FileMeta,
  repoRoot: string,
): EntityRecord[] {
  const relativePath =
    fileMeta.path || posixRelative(repoRoot, fileMeta.absolutePath);
  const records: EntityRecord[] = [
    {
      canonical_name: `file:${relativePath}`,
      entity_type: "file",
      aliases: [relativePath],
    },
  ];

  const moduleChunk = chunks.find((chunk) => chunk.kind === "module");
  if (moduleChunk?.name) {
    records.push({
      canonical_name: `${relativePath}::${moduleChunk.name}`,
      entity_type: "module",
      aliases: [moduleChunk.name, relativePath],
    });
  }

  for (const chunk of chunks) {
    if (!chunk.name || !SYMBOL_CHUNK_KINDS.has(chunk.kind)) {
      continue;
    }
    const canonical = `${relativePath}::${chunk.name}`;
    const aliases = [chunk.name];
    if (chunk.exported) {
      aliases.push(`export:${chunk.name}`);
    }
    if (chunk.parentSymbol) {
      aliases.push(`${chunk.parentSymbol}.${chunk.name}`);
    }
    records.push({
      canonical_name: canonical,
      entity_type: entityTypeForChunk(chunk),
      aliases: [...new Set(aliases)],
    });
  }

  return records;
}

/**
 * Upsert code symbols into `entity_registry` when the store admin batch route exists.
 * Best-effort: skips silently when the endpoint is unavailable (PLAN-08 deferred).
 */
export async function upsertEntitiesFromChunks(
  chunks: CodeChunk[],
  fileMeta: FileMeta,
  scope: ProvenaScope,
  options: UpsertEntitiesOptions,
): Promise<UpsertEntitiesResult> {
  const client = options.client ?? new ProvenaClient({ storeUrl: options.storeUrl });
  const repoRoot = options.repoRoot ?? options.projectRoot;
  const records = buildEntityRecords(chunks, fileMeta, repoRoot);
  if (records.length === 0) {
    return { upserted: 0, skipped: false };
  }

  try {
    const result = await client.upsertEntitiesBatch({
      scope: scopeEnvelopeFromConfig(scope),
      entities: records,
    });
    return { upserted: result.upserted, skipped: false };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (message.includes("(404)") || message.includes("Not Found")) {
      return { upserted: 0, skipped: true };
    }
    throw error;
  }
}