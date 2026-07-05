/** Programmatic API surface for @provena/cli. */
export const CLI_PACKAGE = "@provena/cli";

export {
  CONFIG_FILENAME,
  DEFAULT_DB_PATH,
  DEFAULT_INDEX_EXCLUDE,
  DEFAULT_INDEX_INCLUDE,
  DEFAULT_STORE_URL,
  INDEX_ERRORS_LOG,
  INDEX_STATE_FILENAME,
  LAST_INDEX_FILENAME,
  PROVENA_DIR,
  configExists,
  configPath,
  createDefaultConfig,
  ensureGitignore,
  getGitRoot,
  loadConfig,
  parseStoreUrl,
  pidFilePath,
  provenaDir,
  readConfig,
  resolveDbPath,
  validateConfig,
  writeConfig,
  type LoadedConfig,
  type ProvenaConfig,
  type ProvenaDatabase,
  type ProvenaIndex,
  type ProvenaScope,
  type StoreEndpoint,
} from "./config.js";

export {
  ProvenaClient,
  scopeEnvelopeFromConfig,
  type MemoryCreate,
  type MemoryKind,
  type MemoryRecord,
  type MemoryWriteResult,
  type ProvenaClientOptions,
  type ScopeEnvelope,
  type SearchExplainResponse,
  type SearchRequest,
  type SearchResponse,
  type SearchResult,
  type SourceReference,
} from "./client.js";

export {
  colorEnabled,
  formatLocation,
  formatSearchJson,
  formatSearchTable,
  pickExcerpt,
  spanToLineDisplay,
  truncateExcerpt,
} from "./format.js";

export {
  DEFAULT_SEARCH_LIMIT,
  parseSearchArgs,
  printSearchHelp,
  runSearchCommand,
  type SearchCommandOptions,
} from "./commands/search.js";

export {
  buildChunkFactMemory,
  buildFileArtifactMemory,
  buildSourceReference,
  chunkSymbolKey,
  emitMemories,
  emptyIndexState,
  fileUri,
  indexStatePath,
  loadIndexState,
  memoryFingerprint,
  saveIndexState,
  type EmitOptions,
  type EmitResult,
  type IndexState,
  type IndexStateFileEntry,
} from "./indexer/emit.js";

export {
  emitRelations,
  implementationPathForTest,
  relationFingerprint,
  type EmitRelationsOptions,
  type EmitRelationsResult,
  type RelationScope,
} from "./indexer/relations.js";

export {
  isRelativeImport,
  parseImportSpecifier,
  resolveChunkImports,
  resolveImportSpecifier,
} from "./indexer/resolve-import.js";

export {
  discoverRepo,
  enumerateFiles,
  resolveRepoRoot,
  type DiscoveredFile,
  type DiscoverOptions,
} from "./indexer/discover.js";

export { chunkTypeScriptFile } from "./indexer/chunkers/typescript.js";
export type { ChunkKind, CodeChunk, FileMeta } from "./indexer/types.js";

export {
  classifyFile,
  classifyFiles,
  classifyRole,
  type ClassifiedFile,
  type ClassifyOptions,
  type FileRole,
} from "./indexer/classify.js";

export { detectRepoIntelligence, type RepoCommands, type RepoIntelligence } from "./brain/detect.js";

export {
  REPO_BRAIN_FILENAME,
  REPO_MAP_FILENAME,
  brainExists,
  buildRepoMap,
  renderBrainMarkdown,
  writeBrain,
  type RepoMap,
  type TopDirectory,
  type WriteBrainResult,
} from "./brain/brain.js";

export { printBrainHelp, runBrainCommand } from "./commands/brain.js";

export {
  computeIndexDiff,
  filesToIndex,
  incrementalCountsFromDiff,
  purgeFileFromIndexState,
  removeIndexedFile,
  type IndexDiff,
  type IncrementalCounts,
} from "./indexer/incremental.js";

export { runIndex, type IndexSummary, type RunIndexOptions, type RunIndexResult } from "./indexer/run.js";

export {
  buildEntityRecords,
  upsertEntitiesFromChunks,
  type EntityRecord,
  type EntityType,
  type UpsertEntitiesOptions,
  type UpsertEntitiesResult,
} from "./indexer/entities.js";