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

export {
  REPO_BRAIN_PATH,
  MEMORY_EVENT_SCHEMA_PATH,
  MEMORY_EVENT_JSON_SCHEMA,
  REPO_GRAPH_PATH,
  REPO_MANIFEST_PATH,
  REPO_MAP_PATH,
  MEMORY_LEDGER_PATH,
  activeMemoryEvents,
  appendMemoryEvent,
  readMemoryEvents,
  memoryEventToRecord,
  refreshRepoBrain,
  scanRepo,
  type AppendMemoryOptions,
  type MemoryAuthority as RepoMemoryAuthority,
  type MemoryEvent as RepoMemoryEvent,
  type MemoryEventRecord as RepoMemoryEventRecord,
  type MemoryKind as RepoMemoryKind,
  type MemoryProvenance as RepoMemoryProvenance,
  type MemorySensitivity as RepoMemorySensitivity,
  type MemorySource as RepoMemorySource,
  type MemoryStatus as RepoMemoryStatus,
  type MemorySubjectType as RepoMemorySubjectType,
  type NewMemoryEvent,
  type RefreshRepoBrainOptions,
  type RefreshRepoBrainResult,
  type RepoBrainManifest,
  type RepoCommand,
  type RepoDirectory,
  type RepoEnvironmentVariable,
  type RepoFile,
  type RepoFileKind,
  type RepoMap,
  type RepoPackage,
  type RepoSymbol,
  type ScanRepoOptions,
} from "./brain/index.js";

export {
  buildRepoGraph,
  connectedComponents,
  degreeCentrality,
  neighborhood,
  pageRank,
  shortestPath,
  type DegreeScore,
  type GraphDirection,
  type Neighborhood,
  type PageRankOptions,
  type RepoGraph,
  type RepoGraphEdge,
  type RepoGraphEdgeType,
  type RepoGraphNode,
  type RepoGraphNodeType,
} from "./graph/index.js";

export {
  buildContextPacket,
  renderContextPacketMarkdown,
  type ContextCitation,
  type ContextItem,
  type ContextPacket,
  type ContextQuery,
} from "./context/index.js";

export {
  installAgentInstructions,
  renderAgentInstructions,
  type AgentInstallResult,
} from "./integrations/agents.js";
export {
  installGitHooks,
  type GitHookInstallResult,
} from "./integrations/git-hooks.js";
export {
  installMcpConfigs,
  installedMcpClients,
  type McpConfigResult,
} from "./integrations/mcp-config.js";
export {
  installPortableRuntime,
  type RuntimeInstallResult,
} from "./integrations/runtime.js";
export {
  neo4jConfigFromEnv,
  neo4jRepositoryId,
  syncGraphToNeo4j,
  type Neo4jConfig,
  type Neo4jSyncResult,
} from "./storage/index.js";
export { assertNoSecretMaterial } from "./security/memory.js";
export {
  verifyRepoMemory,
  type HarnessCheck,
  type HarnessReport,
} from "./harness/index.js";
