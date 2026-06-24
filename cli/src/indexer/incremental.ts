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

  for (const key of Object.keys(state.relations)) {
    if (memoryIds.some((id) => key.includes(id))) {
      delete state.relations[key];
    }
  }

  delete state.files[relativePath];
  return memoryIds;
}

export async function deleteFileMemories(
  client: ProvenaClient,
  memoryIds: string[],
): Promise<number> {
  let deleted = 0;
  for (const memoryId of memoryIds) {
    try {
      if (await client.deleteMemory(memoryId)) {
        deleted += 1;
      }
    } catch {
      // Best-effort cleanup; stale IDs are dropped from local state regardless.
    }
  }
  return deleted;
}

export async function removeIndexedFile(
  client: ProvenaClient,
  state: IndexState,
  relativePath: string,
): Promise<number> {
  const memoryIds = purgeFileFromIndexState(state, relativePath);
  if (memoryIds.length === 0) {
    return 0;
  }
  return deleteFileMemories(client, memoryIds);
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