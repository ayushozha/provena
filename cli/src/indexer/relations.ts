import { relative } from "node:path";
import {
  ProvenaClient,
  scopeEnvelopeFromConfig,
  type RelationKind,
} from "../client.js";
import type { ProvenaScope } from "../config.js";
import {
  chunkSymbolKey,
  loadIndexState,
  saveIndexState,
  type IndexState,
} from "./emit.js";
import { resolveChunkImports } from "./resolve-import.js";
import type { CodeChunk, FileMeta } from "./types.js";

const SEMANTIC_CHUNK_KINDS = new Set([
  "import",
  "class",
  "function",
  "method",
  "interface",
  "type_alias",
]);

export interface EmitRelationsOptions {
  storeUrl: string;
  projectRoot: string;
  repoRoot?: string;
  client?: ProvenaClient;
}

export interface EmitRelationsResult {
  created: number;
  skipped: number;
  relationKinds: Partial<Record<RelationKind, number>>;
}

export function relationFingerprint(
  fromMemoryId: string,
  relation: RelationKind,
  toMemoryId: string,
): string {
  return `${fromMemoryId}|${relation}|${toMemoryId}`;
}

function posixRelative(repoRoot: string, absolutePath: string): string {
  return relative(repoRoot, absolutePath).split("\\").join("/");
}

function fileArtifactMemoryId(
  indexState: IndexState,
  relativePath: string,
): string | null {
  const entry = indexState.files[relativePath];
  if (!entry) {
    return null;
  }
  return entry.artifactMemoryId ?? entry.memoryIds[0] ?? null;
}

function chunkMemoryId(
  indexState: IndexState,
  relativePath: string,
  chunk: CodeChunk,
): string | null {
  return indexState.chunks[chunkSymbolKey(relativePath, chunk)] ?? null;
}

export function implementationPathForTest(testPath: string): string | null {
  if (/\.test\.tsx?$/.test(testPath)) {
    return testPath.replace(/\.test\.(ts|tsx|mts|cts)$/, ".$1");
  }
  if (/\.spec\.tsx?$/.test(testPath)) {
    return testPath.replace(/\.spec\.(ts|tsx|mts|cts)$/, ".$1");
  }
  return null;
}

interface PendingRelation {
  fromMemoryId: string;
  toMemoryId: string;
  relation: RelationKind;
}

function collectRelationsForFile(
  chunks: CodeChunk[],
  relativePath: string,
  indexState: IndexState,
): PendingRelation[] {
  const pending: PendingRelation[] = [];
  const fileArtifactId = fileArtifactMemoryId(indexState, relativePath);
  const indexedPaths = Object.keys(indexState.files);

  for (const chunk of chunks) {
    if (chunk.kind === "module") {
      continue;
    }
    if (!SEMANTIC_CHUNK_KINDS.has(chunk.kind)) {
      continue;
    }

    const chunkId = chunkMemoryId(indexState, relativePath, chunk);
    if (!chunkId) {
      continue;
    }

    if (fileArtifactId) {
      pending.push({
        fromMemoryId: chunkId,
        toMemoryId: fileArtifactId,
        relation: "defined_in",
      });
    }

    if (chunk.imports?.length) {
      const targets = resolveChunkImports(
        relativePath,
        chunk.imports,
        indexedPaths,
      );
      for (const targetPath of targets) {
        const targetArtifactId = fileArtifactMemoryId(indexState, targetPath);
        if (targetArtifactId) {
          pending.push({
            fromMemoryId: chunkId,
            toMemoryId: targetArtifactId,
            relation: "derived_from",
          });
        }
      }
    }

    if (chunk.parentSymbol) {
      const parentKey = `${relativePath}::${chunk.parentSymbol}`;
      const parentId = indexState.chunks[parentKey];
      if (parentId) {
        pending.push({
          fromMemoryId: chunkId,
          toMemoryId: parentId,
          relation: "related_to",
        });
      }
    }
  }

  const implPath = implementationPathForTest(relativePath);
  if (implPath && indexState.files[implPath]) {
    for (const chunk of chunks) {
      if (chunk.kind === "module" || !chunk.name) {
        continue;
      }
      if (!SEMANTIC_CHUNK_KINDS.has(chunk.kind)) {
        continue;
      }

      const testChunkId = chunkMemoryId(indexState, relativePath, chunk);
      const implChunkId = indexState.chunks[`${implPath}::${chunk.name}`];
      if (testChunkId && implChunkId) {
        pending.push({
          fromMemoryId: testChunkId,
          toMemoryId: implChunkId,
          relation: "supports",
        });
      }
    }
  }

  return pending;
}

/**
 * Emit typed `memory_relations` edges for one indexed file.
 *
 * Requires prior `emitMemories` so index-state has artifact + chunk memory IDs.
 */
export async function emitRelations(
  chunks: CodeChunk[],
  fileMeta: FileMeta,
  scope: ProvenaScope,
  options: EmitRelationsOptions,
): Promise<EmitRelationsResult> {
  const client = options.client ?? new ProvenaClient({ storeUrl: options.storeUrl });
  const repoRoot = options.repoRoot ?? options.projectRoot;
  const scopeEnvelope = scopeEnvelopeFromConfig(scope);
  const relativePath =
    fileMeta.path || posixRelative(repoRoot, fileMeta.absolutePath);

  const indexState = loadIndexState(options.projectRoot);
  const pending = collectRelationsForFile(chunks, relativePath, indexState);

  let created = 0;
  let skipped = 0;
  const relationKinds: Partial<Record<RelationKind, number>> = {};

  for (const edge of pending) {
    const fingerprint = relationFingerprint(
      edge.fromMemoryId,
      edge.relation,
      edge.toMemoryId,
    );
    if (indexState.relations[fingerprint]) {
      skipped += 1;
      continue;
    }

    await client.createRelation({
      from_memory_id: edge.fromMemoryId,
      to_memory_id: edge.toMemoryId,
      relation: edge.relation,
      scope: scopeEnvelope,
    });

    indexState.relations[fingerprint] = true;
    created += 1;
    relationKinds[edge.relation] = (relationKinds[edge.relation] ?? 0) + 1;
  }

  saveIndexState(options.projectRoot, indexState);

  return { created, skipped, relationKinds };
}