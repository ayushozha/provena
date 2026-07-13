import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { ProvenaClient } from "../client.js";
import {
  INDEX_ERRORS_LOG,
  LAST_INDEX_FILENAME,
  provenaDir,
  type ProvenaConfig,
} from "../config.js";
import { chunkTypeScriptFile } from "./chunkers/typescript.js";
import { discoverRepo, resolveRepoRoot, type DiscoveredFile } from "./discover.js";
import { emitMemories, loadIndexState } from "./emit.js";
import { upsertEntitiesFromChunks } from "./entities.js";
import {
  computeIndexDiff,
  deleteFileMemories,
  filesToIndex,
  incrementalCountsFromDiff,
  prepareFileReplacementState,
  replaceFileInIndexState,
  removeIndexedFile,
  saveIndexStateAtomic,
} from "./incremental.js";
import { emitRelations } from "./relations.js";

const TS_JS_LANGUAGES = new Set(["typescript", "tsx", "javascript", "jsx"]);
const TS_JS_PATTERN = /\.(tsx?|jsx?|mtsx?|cjs)$/i;
/** Parallel workers corrupt index-state without shared in-memory state; keep at 1 by default. */
const DEFAULT_CONCURRENCY = 1;

export interface RunIndexOptions {
  dryRun?: boolean;
  pathPrefix?: string;
  concurrency?: number;
  cwd?: string;
  /** When true, re-process every discovered file (still removes deleted paths). */
  full?: boolean;
}

export interface IndexSummary {
  startedAt: string;
  finishedAt: string;
  filesDiscovered: number;
  filesIndexed: number;
  filesFailed: number;
  filesAdded: number;
  filesChanged: number;
  filesRemoved: number;
  filesUnchanged: number;
  memoriesCreated: number;
  memoriesSkipped: number;
  memoriesDeleted: number;
  relationsCreated: number;
  entitiesUpserted: number;
  dryRun: boolean;
  fullReindex: boolean;
  pathPrefix?: string;
}

export interface RunIndexResult {
  exitCode: number;
  summary: IndexSummary;
}

interface MutableStats {
  filesIndexed: number;
  filesFailed: number;
  memoriesCreated: number;
  memoriesSkipped: number;
  memoriesDeleted: number;
  relationsCreated: number;
  entitiesUpserted: number;
}

function isTsJsFile(file: DiscoveredFile): boolean {
  if (file.language && TS_JS_LANGUAGES.has(file.language)) {
    return true;
  }
  return TS_JS_PATTERN.test(file.path);
}

function normalizePathPrefix(pathPrefix: string): string {
  return pathPrefix.replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/$/, "");
}

function filterByPathPrefix(
  files: DiscoveredFile[],
  pathPrefix?: string,
): DiscoveredFile[] {
  if (!pathPrefix) {
    return files;
  }
  const norm = normalizePathPrefix(pathPrefix);
  return files.filter(
    (file) => file.path === norm || file.path.startsWith(`${norm}/`),
  );
}

function logIndexError(projectRoot: string, relativePath: string, error: unknown): void {
  const dir = provenaDir(projectRoot);
  mkdirSync(dir, { recursive: true });
  const logPath = join(dir, INDEX_ERRORS_LOG);
  const message = error instanceof Error ? error.message : String(error);
  appendFileSync(
    logPath,
    `${new Date().toISOString()}\t${relativePath}\t${message}\n`,
    "utf8",
  );
}

function writeLastIndexSummary(projectRoot: string, summary: IndexSummary): void {
  const dir = provenaDir(projectRoot);
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, LAST_INDEX_FILENAME),
    `${JSON.stringify(summary, null, 2)}\n`,
    "utf8",
  );
}

function printProgress(
  filesDone: number,
  filesTotal: number,
  stats: MutableStats,
): void {
  const memories = stats.memoriesCreated + stats.memoriesSkipped;
  process.stdout.write(
    `\rIndexed ${filesDone}/${filesTotal} files, ${memories} memories, ${stats.relationsCreated} relations`,
  );
}

/** PLAN-21: register trigger phrases from exported symbols (stub). */
async function registerTriggerPhrases(): Promise<void> {
  return undefined;
}

async function runPool<T>(
  items: T[],
  concurrency: number,
  worker: (item: T) => Promise<void>,
): Promise<void> {
  let cursor = 0;
  async function next(): Promise<void> {
    while (cursor < items.length) {
      const index = cursor;
      cursor += 1;
      await worker(items[index]!);
    }
  }
  const workers = Math.min(Math.max(1, concurrency), items.length || 1);
  await Promise.all(Array.from({ length: workers }, () => next()));
}

function emptySummary(
  startedAt: string,
  options: RunIndexOptions,
  filesDiscovered = 0,
): IndexSummary {
  return {
    startedAt,
    finishedAt: new Date().toISOString(),
    filesDiscovered,
    filesIndexed: 0,
    filesFailed: 0,
    filesAdded: 0,
    filesChanged: 0,
    filesRemoved: 0,
    filesUnchanged: 0,
    memoriesCreated: 0,
    memoriesSkipped: 0,
    memoriesDeleted: 0,
    relationsCreated: 0,
    entitiesUpserted: 0,
    dryRun: options.dryRun ?? false,
    fullReindex: options.full ?? false,
    pathPrefix: options.pathPrefix,
  };
}

/**
 * End-to-end index pipeline: discover → diff → chunk → emit → relations → entities.
 */
export async function runIndex(
  config: ProvenaConfig,
  projectRoot: string,
  options: RunIndexOptions = {},
): Promise<RunIndexResult> {
  const startedAt = new Date().toISOString();
  const cwd = options.cwd ?? process.cwd();
  const repoRoot = resolveRepoRoot(cwd);
  const dryRun = options.dryRun ?? false;
  const fullReindex = options.full ?? false;
  const concurrency = options.concurrency ?? DEFAULT_CONCURRENCY;

  const discovered = await discoverRepo(config, {
    cwd,
    repoRoot,
    warn: (message) => console.error(`provena index: ${message}`),
  });

  const files = filterByPathPrefix(discovered.filter(isTsJsFile), options.pathPrefix);

  if (dryRun) {
    for (const file of files) {
      console.log(file.path);
    }
    const summary = emptySummary(startedAt, options, files.length);
    summary.finishedAt = new Date().toISOString();
    return { exitCode: files.length > 0 ? 0 : 1, summary };
  }

  const client = new ProvenaClient({
    storeUrl: config.store_url,
    intelligenceUrl: config.intelligence_url,
  });

  const healthy = await client.healthz();
  if (!healthy) {
    console.error("provena index: store unreachable; run `provena serve --detach` first");
    const summary = emptySummary(startedAt, options, files.length);
    return { exitCode: 1, summary };
  }

  const indexState = loadIndexState(projectRoot);
  const diff = computeIndexDiff(files, indexState, { pathPrefix: options.pathPrefix });
  const incremental = incrementalCountsFromDiff(diff);

  const stats: MutableStats = {
    filesIndexed: 0,
    filesFailed: 0,
    memoriesCreated: 0,
    memoriesSkipped: 0,
    memoriesDeleted: 0,
    relationsCreated: 0,
    entitiesUpserted: 0,
  };

  for (const removedPath of diff.removed) {
    try {
      stats.memoriesDeleted += await removeIndexedFile(
        client,
        indexState,
        removedPath,
        { hardDelete: true },
      );
    } catch (error) {
      stats.filesFailed += 1;
      logIndexError(projectRoot, removedPath, error);
      console.error(
        `\nprovena index: failed removing ${removedPath}: ${error instanceof Error ? error.message : error}`,
      );
    }
  }

  const worklist = fullReindex ? files : filesToIndex(diff);

  let filesDone = 0;
  const pipelineOpts = {
    storeUrl: config.store_url,
    intelligenceUrl: config.intelligence_url,
    projectRoot,
    repoRoot,
    client,
    indexState,
    persistIndexState: false,
  };

  const indexedFiles = new Map<string, {
    chunks: Awaited<ReturnType<typeof chunkTypeScriptFile>>;
    fileMeta: { path: string; absolutePath: string; sha256: string };
  }>();

  await runPool(worklist, concurrency, async (file) => {
    try {
      const content = readFileSync(file.absolutePath, "utf8");
      const chunks = await chunkTypeScriptFile(file.path, content);
      const fileMeta = {
        path: file.path,
        absolutePath: file.absolutePath,
        sha256: file.sha256,
      };
      const replacementState = structuredClone(indexState);
      const previousIds = prepareFileReplacementState(
        replacementState,
        file.path,
      );
      const filePipelineOpts = {
        ...pipelineOpts,
        indexState: replacementState,
      };

      const emitResult = await emitMemories(
        chunks,
        fileMeta,
        config.scope,
        filePipelineOpts,
      );

      const relationResult = await emitRelations(chunks, fileMeta, config.scope, {
        ...filePipelineOpts,
        relationScope: "intra-file",
      });

      const entityResult = await upsertEntitiesFromChunks(
        chunks,
        fileMeta,
        config.scope,
        { storeUrl: config.store_url, projectRoot, repoRoot, client },
      );

      // Any future trigger registration must succeed before the local state
      // accepts this source hash, otherwise the next incremental run could
      // incorrectly skip unfinished work.
      await registerTriggerPhrases();

      const committedState = structuredClone(indexState);
      const obsoleteIds = replaceFileInIndexState(
        committedState,
        replacementState,
        file.path,
        previousIds,
      );
      stats.memoriesDeleted += await deleteFileMemories(
        client,
        obsoleteIds,
        { hardDelete: true },
      );
      indexState.files = committedState.files;
      indexState.chunks = committedState.chunks;
      indexState.relations = committedState.relations;
      stats.memoriesCreated += emitResult.created;
      stats.memoriesSkipped += emitResult.skipped;
      stats.relationsCreated += relationResult.created;
      stats.entitiesUpserted += entityResult.upserted;

      indexedFiles.set(file.path, { chunks, fileMeta });

      stats.filesIndexed += 1;
    } catch (error) {
      stats.filesFailed += 1;
      logIndexError(projectRoot, file.path, error);
      console.error(`\nprovena index: failed ${file.path}: ${error instanceof Error ? error.message : error}`);
    } finally {
      filesDone += 1;
      if (worklist.length > 0) {
        printProgress(filesDone, worklist.length, stats);
      }
    }
  });

  if (worklist.length > 0) {
    process.stdout.write("\n");
  }

  // Re-evaluate cross-file edges for every surviving source whenever topology
  // changed. A changed target receives new generated-memory IDs, so unchanged
  // importers and tests must be rewired as well as the changed file itself.
  const crossFileWork = worklist.length > 0 || diff.removed.length > 0 ? files : [];
  for (const file of crossFileWork) {
    try {
      const indexed = indexedFiles.get(file.path);
      const chunks = indexed?.chunks ?? await chunkTypeScriptFile(
        file.path,
        readFileSync(file.absolutePath, "utf8"),
      );
      const fileMeta = indexed?.fileMeta ?? {
        path: file.path,
        absolutePath: file.absolutePath,
        sha256: file.sha256,
      };
      const crossFileResult = await emitRelations(chunks, fileMeta, config.scope, {
        ...pipelineOpts,
        relationScope: "cross-file",
      });
      stats.relationsCreated += crossFileResult.created;
    } catch (error) {
      stats.filesFailed += 1;
      // Force a retry even when this source file itself was unchanged. The
      // store may have accepted only a prefix of its edges.
      if (indexState.files[file.path]) {
        indexState.files[file.path]!.sha256 = "";
      }
      logIndexError(projectRoot, file.path, error);
      console.error(
        `\nprovena index: cross-file relations failed ${file.path}: ${error instanceof Error ? error.message : error}`,
      );
    }
  }

  saveIndexStateAtomic(projectRoot, indexState);

  const summary: IndexSummary = {
    startedAt,
    finishedAt: new Date().toISOString(),
    filesDiscovered: files.length,
    filesIndexed: stats.filesIndexed,
    filesFailed: stats.filesFailed,
    filesAdded: incremental.filesAdded,
    filesChanged: incremental.filesChanged,
    filesRemoved: incremental.filesRemoved,
    filesUnchanged: incremental.filesUnchanged,
    memoriesCreated: stats.memoriesCreated,
    memoriesSkipped: stats.memoriesSkipped,
    memoriesDeleted: stats.memoriesDeleted,
    relationsCreated: stats.relationsCreated,
    entitiesUpserted: stats.entitiesUpserted,
    dryRun: false,
    fullReindex,
    pathPrefix: options.pathPrefix,
  };

  writeLastIndexSummary(projectRoot, summary);

  const hadWork =
    stats.filesIndexed > 0 ||
    incremental.filesRemoved > 0 ||
    incremental.filesUnchanged > 0;
  const exitCode = stats.filesFailed > 0 ? 1 : hadWork ? 0 : 1;
  return { exitCode, summary };
}
