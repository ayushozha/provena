import { createHash } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { dirname, relative, resolve } from "node:path";
import {
  ProvenaClient,
  scopeEnvelopeFromConfig,
  type MemoryCreate,
  type ScopeEnvelope,
  type SourceReference,
} from "../client.js";
import {
  INDEX_STATE_FILENAME,
  provenaDir,
  type ProvenaScope,
} from "../config.js";
import type { CodeChunk, FileMeta } from "./types.js";

/**
 * Tracks indexed files for incremental re-index (PLAN-12) and relation wiring (PLAN-07).
 *
 * Schema:
 * ```json
 * {
 *   "version": 1,
 *   "files": {
 *     "src/auth.ts": {
 *       "sha256": "abc…",
 *       "artifactMemoryId": "mem_1",
 *       "memoryIds": ["mem_1", "mem_2"],
 *       "fingerprints": { "<sha256>": "mem_1" }
 *     }
 *   },
 *   "chunks": { "src/auth.ts::authenticate": "mem_2" },
 *   "relations": { "mem_2|defined_in|mem_1": true }
 * }
 * ```
 */
export interface IndexStateFileEntry {
  sha256?: string;
  artifactMemoryId?: string;
  memoryIds: string[];
  fingerprints: Record<string, string>;
}

export interface IndexState {
  version: 1;
  files: Record<string, IndexStateFileEntry>;
  chunks: Record<string, string>;
  relations: Record<string, true>;
}

export interface EmitOptions {
  storeUrl: string;
  projectRoot: string;
  repoRoot?: string;
  intelligenceUrl?: string;
  client?: ProvenaClient;
  /** Shared in-memory state; when set, disk is not read and save is deferred unless `persistIndexState`. */
  indexState?: IndexState;
  /** Persist index state after this file (default: true only when `indexState` is omitted). */
  persistIndexState?: boolean;
}

export interface EmitResult {
  memoryIds: string[];
  created: number;
  skipped: number;
}

const EXCERPT_MAX = 200;
const SEMANTIC_CHUNK_KINDS = new Set([
  "import",
  "class",
  "function",
  "method",
  "interface",
  "type_alias",
]);

export function chunkSymbolKey(relativePath: string, chunk: CodeChunk): string {
  const symbol = chunk.name ?? chunk.kind;
  return `${relativePath}::${symbol}`;
}

export function indexStatePath(projectRoot: string): string {
  return resolve(provenaDir(projectRoot), INDEX_STATE_FILENAME);
}

export function emptyIndexState(): IndexState {
  return { version: 1, files: {}, chunks: {}, relations: {} };
}

export function loadIndexState(projectRoot: string): IndexState {
  const path = indexStatePath(projectRoot);
  if (!existsSync(path)) {
    return emptyIndexState();
  }
  const raw = JSON.parse(readFileSync(path, "utf8")) as IndexState;
  if (raw.version !== 1 || typeof raw.files !== "object") {
    throw new Error(`invalid index state at ${path}`);
  }
  if (!raw.chunks || typeof raw.chunks !== "object") {
    raw.chunks = {};
  }
  if (!raw.relations || typeof raw.relations !== "object") {
    raw.relations = {};
  }
  return raw;
}

export function saveIndexState(projectRoot: string, state: IndexState): void {
  const path = indexStatePath(projectRoot);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, `${JSON.stringify(state, null, 2)}\n`, "utf8");
}

/** Mirrors ProvenaStore._fingerprint for local deduplication. */
export function memoryFingerprint(
  scope: ScopeEnvelope,
  kind: string,
  title: string | null | undefined,
  content: string,
): string {
  const scopeJson = JSON.stringify(scopeForFingerprint(scope));
  const value = [
    scopeJson,
    kind,
    (title ?? "").trim().toLowerCase(),
    content.trim().toLowerCase(),
  ].join("|");
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function scopeForFingerprint(scope: ScopeEnvelope): ScopeEnvelope {
  const out: ScopeEnvelope = { tenant_id: scope.tenant_id };
  if (scope.workspace_id) out.workspace_id = scope.workspace_id;
  if (scope.project_id) out.project_id = scope.project_id;
  if (scope.user_id) out.user_id = scope.user_id;
  if (scope.agent_id) out.agent_id = scope.agent_id;
  if (scope.session_id) out.session_id = scope.session_id;
  return out;
}

export function fileUri(absolutePath: string): string {
  const normalized = resolve(absolutePath).replace(/\\/g, "/");
  if (/^[A-Za-z]:/.test(normalized)) {
    return `file:///${normalized}`;
  }
  return `file://${normalized}`;
}

export function buildSourceReference(
  relativePath: string,
  absolutePath: string,
  startLine: number,
  endLine: number,
  content: string,
): SourceReference {
  return {
    source_type: "file",
    source_id: relativePath,
    uri: fileUri(absolutePath),
    title: relativePath,
    excerpt: content.slice(0, EXCERPT_MAX),
    span_start: startLine,
    span_end: endLine,
  };
}

function posixRelative(repoRoot: string, absolutePath: string): string {
  return relative(repoRoot, absolutePath).split("\\").join("/");
}

function buildFileArtifactContent(
  relativePath: string,
  chunks: CodeChunk[],
): string {
  const moduleChunk = chunks.find((chunk) => chunk.kind === "module");
  const dir = relativePath.includes("/")
    ? relativePath.slice(0, relativePath.lastIndexOf("/"))
    : ".";
  const lines = [`Directory: ${dir}`, `File: ${relativePath}`];

  if (moduleChunk?.imports?.length) {
    lines.push(`Imports: ${moduleChunk.imports.join(", ")}`);
  }

  const exports = chunks
    .filter((chunk) => chunk.exported && chunk.name)
    .map((chunk) => chunk.name as string);
  if (exports.length) {
    lines.push(`Exports: ${exports.join(", ")}`);
  } else if (moduleChunk?.content) {
    lines.push("Summary: indexed TypeScript/JavaScript module");
  }

  return lines.join("\n");
}

export function buildFileArtifactMemory(
  chunks: CodeChunk[],
  fileMeta: FileMeta,
  scope: ScopeEnvelope,
  repoRoot: string,
): MemoryCreate {
  const relativePath = fileMeta.path || posixRelative(repoRoot, fileMeta.absolutePath);
  const content = buildFileArtifactContent(relativePath, chunks);

  return {
    kind: "artifact",
    scope,
    title: relativePath,
    content,
    entity_keys: [`file:${relativePath}`],
    tags: [chunks[0]?.language ?? "unknown", "module", "indexed"],
    source_references: [
      buildSourceReference(
        relativePath,
        fileMeta.absolutePath,
        1,
        chunks.reduce((max, chunk) => Math.max(max, chunk.endLine), 1),
        content,
      ),
    ],
    metadata: {
      indexed_path: relativePath,
      sha256: fileMeta.sha256,
    },
  };
}

export function buildChunkFactMemory(
  chunk: CodeChunk,
  fileMeta: FileMeta,
  scope: ScopeEnvelope,
  repoRoot: string,
): MemoryCreate {
  const relativePath = chunk.filePath || fileMeta.path || posixRelative(repoRoot, fileMeta.absolutePath);
  const symbol = chunk.name ?? chunk.kind;
  const title = `${relativePath}::${symbol}`;

  const entityKeys = [`file:${relativePath}`];
  if (chunk.name) {
    entityKeys.unshift(chunk.name);
  }

  return {
    kind: "fact",
    scope,
    title,
    content: chunk.content,
    summary: chunk.docstring,
    entity_keys: entityKeys,
    tags: [chunk.language, chunk.kind, "indexed"],
    source_references: [
      buildSourceReference(
        relativePath,
        fileMeta.absolutePath,
        chunk.startLine,
        chunk.endLine,
        chunk.content,
      ),
    ],
    metadata: {
      chunk_kind: chunk.kind,
      parent_symbol: chunk.parentSymbol,
      exported: chunk.exported ?? false,
    },
  };
}

async function writeMemory(
  client: ProvenaClient,
  payload: MemoryCreate,
  fingerprints: Record<string, string>,
): Promise<{ memoryId: string; created: boolean }> {
  const fingerprint = memoryFingerprint(
    payload.scope,
    payload.kind,
    payload.title,
    payload.content,
  );

  const existingId = fingerprints[fingerprint];
  if (existingId) {
    return { memoryId: existingId, created: false };
  }

  const result = await client.createMemory(payload);
  fingerprints[fingerprint] = result.memory.memory_id;
  return { memoryId: result.memory.memory_id, created: result.created };
}

/**
 * Convert chunks into Provena memories via `POST /v1/memories`.
 *
 * Writes one file-level `artifact` plus `fact` memories per semantic chunk.
 * Persists `filePath → memory_id[]` in `.provena/index-state.json`.
 */
export async function emitMemories(
  chunks: CodeChunk[],
  fileMeta: FileMeta,
  scope: ProvenaScope,
  options: EmitOptions,
): Promise<EmitResult> {
  if (chunks.length === 0) {
    return { memoryIds: [], created: 0, skipped: 0 };
  }

  const client =
    options.client ??
    new ProvenaClient({
      storeUrl: options.storeUrl,
      intelligenceUrl: options.intelligenceUrl,
    });
  const repoRoot = options.repoRoot ?? options.projectRoot;
  const scopeEnvelope = scopeEnvelopeFromConfig(scope);
  const relativePath =
    fileMeta.path || posixRelative(repoRoot, fileMeta.absolutePath);

  const indexState = options.indexState ?? loadIndexState(options.projectRoot);
  const persistIndexState =
    options.persistIndexState ?? options.indexState === undefined;
  const entry: IndexStateFileEntry = indexState.files[relativePath] ?? {
    sha256: fileMeta.sha256,
    memoryIds: [],
    fingerprints: {},
  };

  if (fileMeta.sha256) {
    entry.sha256 = fileMeta.sha256;
  }

  const memoryIds: string[] = [];
  let created = 0;
  let skipped = 0;

  const artifact = buildFileArtifactMemory(chunks, fileMeta, scopeEnvelope, repoRoot);
  const artifactWrite = await writeMemory(client, artifact, entry.fingerprints);
  memoryIds.push(artifactWrite.memoryId);
  entry.artifactMemoryId = artifactWrite.memoryId;
  if (artifactWrite.created) {
    created += 1;
  } else {
    skipped += 1;
  }

  for (const chunk of chunks) {
    if (chunk.kind === "module") {
      continue;
    }
    if (!SEMANTIC_CHUNK_KINDS.has(chunk.kind)) {
      continue;
    }

    const fact = buildChunkFactMemory(chunk, fileMeta, scopeEnvelope, repoRoot);
    const factWrite = await writeMemory(client, fact, entry.fingerprints);
    memoryIds.push(factWrite.memoryId);
    indexState.chunks[chunkSymbolKey(relativePath, chunk)] = factWrite.memoryId;
    if (factWrite.created) {
      created += 1;
    } else {
      skipped += 1;
    }
  }

  entry.memoryIds = [...new Set(memoryIds)];
  indexState.files[relativePath] = entry;
  if (persistIndexState) {
    saveIndexState(options.projectRoot, indexState);
  }

  return { memoryIds: entry.memoryIds, created, skipped };
}