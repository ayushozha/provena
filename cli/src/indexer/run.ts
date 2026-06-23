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
import { emitMemories, loadIndexState, saveIndexState } from "./emit.js";
import { upsertEntitiesFromChunks } from "./entities.js";
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
}

export interface IndexSummary {
  startedAt: string;
  finishedAt: string;
  filesDiscovered: number;
  filesIndexed: number;
  filesFailed: number;
  memoriesCreated: number;
  memoriesSkipped: number;
  relationsCreated: number;
  entitiesUpserted: number;
  dryRun: boolean;
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
    memoriesCreated: 0,
    memoriesSkipped: 0,
    relationsCreated: 0,
    entitiesUpserted: 0,
    dryRun: options.dryRun ?? false,
    pathPrefix: options.pathPrefix,
  };
}

/**
 * End-to-end index pipeline: discover → chunk → emit → relations → entities.
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

  const stats: MutableStats = {
    filesIndexed: 0,
    filesFailed: 0,
    memoriesCreated: 0,
    memoriesSkipped: 0,
    relationsCreated: 0,
    entitiesUpserted: 0,
  };

  let filesDone = 0;
  const indexState = loadIndexState(projectRoot);
  const pipelineOpts = {
    storeUrl: config.store_url,
    intelligenceUrl: config.intelligence_url,
    projectRoot,
    repoRoot,
    client,
    indexState,
    persistIndexState: false,
  };

  const indexedFiles: Array<{
    chunks: Awaited<ReturnType<typeof chunkTypeScriptFile>>;
    fileMeta: { path: string; absolutePath: string; sha256: string };
  }> = [];

  await runPool(files, concurrency, async (file) => {
    try {
      const content = readFileSync(file.absolutePath, "utf8");
      const chunks = await chunkTypeScriptFile(file.path, content);
      const fileMeta = {
        path: file.path,
        absolutePath: file.absolutePath,
        sha256: file.sha256,
      };

      const emitResult = await emitMemories(chunks, fileMeta, config.scope, pipelineOpts);
      stats.memoriesCreated += emitResult.created;
      stats.memoriesSkipped += emitResult.skipped;

      const relationResult = await emitRelations(chunks, fileMeta, config.scope, {
        ...pipelineOpts,
        relationScope: "intra-file",
      });
      stats.relationsCreated += relationResult.created;

      const entityResult = await upsertEntitiesFromChunks(
        chunks,
        fileMeta,
        config.scope,
        { storeUrl: config.store_url, projectRoot, repoRoot, client },
      );
      stats.entitiesUpserted += entityResult.upserted;

      indexedFiles.push({ chunks, fileMeta });
      await registerTriggerPhrases();

      stats.filesIndexed += 1;
    } catch (error) {
      stats.filesFailed += 1;
      logIndexError(projectRoot, file.path, error);
      console.error(`\nprovena index: failed ${file.path}: ${error instanceof Error ? error.message : error}`);
    } finally {
      filesDone += 1;
      printProgress(filesDone, files.length, stats);
    }
  });

  process.stdout.write("\n");

  for (const { chunks, fileMeta } of indexedFiles) {
    try {
      const crossFileResult = await emitRelations(chunks, fileMeta, config.scope, {
        ...pipelineOpts,
        relationScope: "cross-file",
      });
      stats.relationsCreated += crossFileResult.created;
    } catch (error) {
      logIndexError(projectRoot, fileMeta.path, error);
      console.error(
        `\nprovena index: cross-file relations failed ${fileMeta.path}: ${error instanceof Error ? error.message : error}`,
      );
    }
  }

  saveIndexState(projectRoot, indexState);

  const summary: IndexSummary = {
    startedAt,
    finishedAt: new Date().toISOString(),
    filesDiscovered: files.length,
    filesIndexed: stats.filesIndexed,
    filesFailed: stats.filesFailed,
    memoriesCreated: stats.memoriesCreated,
    memoriesSkipped: stats.memoriesSkipped,
    relationsCreated: stats.relationsCreated,
    entitiesUpserted: stats.entitiesUpserted,
    dryRun: false,
    pathPrefix: options.pathPrefix,
  };

  writeLastIndexSummary(projectRoot, summary);

  const exitCode = stats.filesIndexed > 0 ? 0 : 1;
  return { exitCode, summary };
}