/** Programmatic API surface for @provena/cli. */
export const CLI_PACKAGE = "@provena/cli";

export {
  CONFIG_FILENAME,
  DEFAULT_DB_PATH,
  DEFAULT_INDEX_EXCLUDE,
  DEFAULT_INDEX_INCLUDE,
  DEFAULT_STORE_URL,
  INDEX_STATE_FILENAME,
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
  type SearchRequest,
  type SearchResponse,
  type SearchResult,
  type SourceReference,
} from "./client.js";

export {
  buildChunkFactMemory,
  buildFileArtifactMemory,
  buildSourceReference,
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

export type { CodeChunk, FileMeta } from "./indexer/types.js";