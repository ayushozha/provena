import { ProvenaClient } from "../client.js";
import type { DiscoveredFile } from "./discover.js";
import {
  type IndexState,
  type IndexStateFileEntry,
  saveIndexState,
} from "./emit.js";

export interface IndexDiff {
  added: DiscoveredFile[];
  changed: DiscoveredFile[];
  removed: string[];
  unchanged: DiscoveredFile[];
}

export interface IncrementalCounts {
  filesAdded: number;
  filesChanged: number;
  filesRemoved: number;
  filesUnchanged: number;
}

export interface DeleteFileMemoriesOptions {
  /** Indexed code facts are derived data, so hard deletion also removes stale edges. */
  hardDelete?: boolean;
}

function relationTouchesIds(fingerprint: string, memoryIds: Set<string>): boolean {
  const [fromMemoryId, , toMemoryId] = fingerprint.split("|");
  return memoryIds.has(fromMemoryId ?? "") || memoryIds.has(toMemoryId ?? "");
}

function pathMatchesPrefix(relativePath: string, pathPrefix?: string): boolean {
  if (!pathPrefix) {
    return true;
  }
  const norm = pathPrefix.replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/$/, "");
  return relativePath === norm || relativePath.startsWith(`${norm}/`);
}

/**
 * Compare discovery output with persisted index state (content hash only).
 */
export function computeIndexDiff(
  discovered: DiscoveredFile[],
  state: IndexState,
  options: { pathPrefix?: string } = {},
): IndexDiff {
  const scoped = options.pathPrefix
    ? discovered.filter((file) => pathMatchesPrefix(file.path, options.pathPrefix))
    : discovered;

  const discoveredByPath = new Map(scoped.map((file) => [file.path, file]));
  const statePaths = Object.keys(state.files).filter((path) =>
    pathMatchesPrefix(path, options.pathPrefix),
  );

  const added: DiscoveredFile[] = [];
  const changed: DiscoveredFile[] = [];
  const unchanged: DiscoveredFile[] = [];
  const removed: string[] = [];

  for (const file of scoped) {
    const previous = state.files[file.path];
    if (!previous) {
      added.push(file);
      continue;
    }
    if (previous.sha256 && previous.sha256 === file.sha256) {
      unchanged.push(file);
    } else {
      changed.push(file);
    }
  }

  for (const path of statePaths) {
    if (!discoveredByPath.has(path)) {
      removed.push(path);
    }
  }

  return { added, changed, removed, unchanged };
}

/** Collect memory IDs tracked for a file (artifact + chunk facts). */
export function memoryIdsForFile(entry: IndexStateFileEntry): string[] {
  return [...new Set(entry.memoryIds)];
}

/**
 * Drop file entry, chunk keys, and relation fingerprints for one path.
 */
export function purgeFileFromIndexState(state: IndexState, relativePath: string): string[] {
  const entry = state.files[relativePath];
  if (!entry) {
    return [];
  }

  const memoryIds = memoryIdsForFile(entry);
  const chunkPrefix = `${relativePath}::`;

  for (const key of Object.keys(state.chunks)) {
    if (key.startsWith(chunkPrefix)) {
      delete state.chunks[key];
    }
  }

  const memoryIdSet = new Set(memoryIds);
  for (const key of Object.keys(state.relations)) {
    if (relationTouchesIds(key, memoryIdSet)) {
      delete state.relations[key];
    }
  }

  delete state.files[relativePath];
  return memoryIds;
}

/**
 * Clear file-owned lookup and relation state in an isolated transaction copy.
 * The entry and fingerprints remain available so unchanged chunks deduplicate
 * against the still-live store memories while a replacement is being written.
 */
export function prepareFileReplacementState(
  state: IndexState,
  relativePath: string,
): string[] {
  const entry = state.files[relativePath];
  const previousIds = entry ? memoryIdsForFile(entry) : [];
  const chunkPrefix = `${relativePath}::`;

  for (const key of Object.keys(state.chunks)) {
    if (key.startsWith(chunkPrefix)) {
      delete state.chunks[key];
    }
  }

  const previousIdSet = new Set(previousIds);
  for (const key of Object.keys(state.relations)) {
    if (relationTouchesIds(key, previousIdSet)) {
      delete state.relations[key];
    }
  }
  return previousIds;
}

/** Commit one successfully indexed file from an isolated state copy. */
export function replaceFileInIndexState(
  state: IndexState,
  replacement: IndexState,
  relativePath: string,
  previousIds: string[],
): string[] {
  const nextEntry = replacement.files[relativePath];
  if (!nextEntry) {
    throw new Error(`replacement state missing ${relativePath}`);
  }

  const currentIds = new Set(nextEntry.memoryIds);
  const previousIdSet = new Set(previousIds);
  const chunkPrefix = `${relativePath}::`;

  for (const key of Object.keys(state.chunks)) {
    if (key.startsWith(chunkPrefix)) {
      delete state.chunks[key];
    }
  }
  for (const [key, memoryId] of Object.entries(replacement.chunks)) {
    if (key.startsWith(chunkPrefix)) {
      state.chunks[key] = memoryId;
    }
  }

  for (const key of Object.keys(state.relations)) {
    if (relationTouchesIds(key, previousIdSet)) {
      delete state.relations[key];
    }
  }
  for (const key of Object.keys(replacement.relations)) {
    if (relationTouchesIds(key, currentIds)) {
      state.relations[key] = true;
    }
  }

  nextEntry.fingerprints = Object.fromEntries(
    Object.entries(nextEntry.fingerprints).filter(([, memoryId]) =>
      currentIds.has(memoryId),
    ),
  );
  state.files[relativePath] = structuredClone(nextEntry);
  return previousIds.filter((memoryId) => !currentIds.has(memoryId));
}

export async function deleteFileMemories(
  client: ProvenaClient,
  memoryIds: string[],
  options: DeleteFileMemoriesOptions = {},
): Promise<number> {
  let deleted = 0;
  for (const memoryId of memoryIds) {
    if (await client.deleteMemory(memoryId, options.hardDelete ?? false)) {
      deleted += 1;
    }
  }
  return deleted;
}

function commitIndexState(target: IndexState, next: IndexState): void {
  target.version = next.version;
  target.files = next.files;
  target.chunks = next.chunks;
  target.relations = next.relations;
}

export async function removeIndexedFile(
  client: ProvenaClient,
  state: IndexState,
  relativePath: string,
  options: DeleteFileMemoriesOptions = {},
): Promise<number> {
  const next = structuredClone(state);
  const memoryIds = purgeFileFromIndexState(next, relativePath);
  if (memoryIds.length === 0) {
    return 0;
  }
  const deleted = await deleteFileMemories(client, memoryIds, options);
  commitIndexState(state, next);
  return deleted;
}

export function incrementalCountsFromDiff(diff: IndexDiff): IncrementalCounts {
  return {
    filesAdded: diff.added.length,
    filesChanged: diff.changed.length,
    filesRemoved: diff.removed.length,
    filesUnchanged: diff.unchanged.length,
  };
}

export function filesToIndex(diff: IndexDiff): DiscoveredFile[] {
  return [...diff.added, ...diff.changed];
}

export function saveIndexStateAtomic(projectRoot: string, state: IndexState): void {
  saveIndexState(projectRoot, state, { atomic: true });
}
