/** Programmatic API surface for @provena/cli. */
export const CLI_PACKAGE = "@provena/cli";
export * from "./procedures/index.js";
export * from "./capture/index.js";
export { runCaptureCommand } from "./commands/capture.js";
export {
  installCaptureHooks, uninstallCaptureHooks,
  type CaptureProvider, type CaptureHookConfigResult,
} from "./integrations/capture-hooks.js";
export { printProcedureHelp, runProcedureCommand } from "./commands/procedure.js";

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
  type RepositoryMemorySyncRequest,
  type RepositoryMemorySyncResponse,
  type RepositoryMemorySyncTimings,
  type ScopeEnvelope,
  type SearchExplainResponse,
  type SearchRequest,
  type SearchResponse,
  type SearchResult,
  type SourceReference,
} from "./client.js";

export { printSyncHelp, runSyncCommand } from "./commands/sync.js";
export { printMaintainHelp, runMaintainCommand } from "./commands/maintain.js";

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
  REPO_BRAIN_MANAGED_ARTIFACT_PATHS,
  MAX_STORED_ARTIFACT_BYTES,
  MAX_STORED_GENERATION_BYTES,
  MEMORY_EVENT_SCHEMA_PATH,
  MEMORY_EVENT_JSON_SCHEMA,
  REPO_GRAPH_PATH,
  REPO_MANIFEST_PATH,
  REPO_MAP_PATH,
  MEMORY_LEDGER_PATH,
  activeMemoryEvents,
  activeMemoryEventsAt,
  appendMemoryEvent,
  canonicalMemoryAsOf,
  extendMemoryLedgerSnapshot,
  prepareMemoryEvent,
  readRepoBrainArtifacts,
  readMemoryLedgerSnapshot,
  readMemoryEvents,
  memoryEventToRecord,
  reconcileRepoMapMemories,
  REPO_MAP_MEMORY_DATA_KEY,
  REPO_MAP_MEMORY_GENERATOR,
  REPO_MAP_MEMORY_GENERATOR_VERSION,
  REPO_MAP_MEMORY_TAG,
  refreshRepoBrain,
  repoMapSourceFingerprint,
  scanRepo,
  type AppendMemoryOptions,
  type MemoryLedgerSnapshot,
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
  type StoredRepoBrainArtifacts,
  type ReconcileRepoMapMemoryOptions,
  type ReconcileRepoMapMemoryResult,
  type RepoBrainManifest,
  type RepoCommand,
  type RepoDirectory,
  type RepoEnvironmentVariable,
  type RepoFile,
  type RepoFileKind,
  type RepoMap,
  type RepoMemoryAction,
  type RepoMemoryReconciliation,
  type RepoPackage,
  type RepoScanDiagnostics,
  type RepoSymbol,
  type ScanRepoOptions,
} from "./brain/index.js";

export {
  buildRepoGraph,
  buildRepoGraphWithDiagnostics,
  connectedComponents,
  deriveMemoryTemporalRecords,
  degreeCentrality,
  induceRepoGraphAt,
  MEMORY_GRAPH_NODE_PREFIX,
  memoryGraphNodeId,
  memoryTimeline,
  neighborhood,
  pageRank,
  REPO_GRAPH_PROJECTION_NAMESPACE,
  REPO_GRAPH_PROJECTION_VERSION,
  repoGraphProjectionFingerprint,
  shortestPath,
  type DegreeScore,
  type GraphDirection,
  type MemoryGraphNodeMetadata,
  type MemoryTemporalProjection,
  type MemoryTemporalRecord,
  type MemoryTimeline,
  type MemoryTimelineEntry,
  type Neighborhood,
  type PageRankOptions,
  type RepoGraph,
  type RepoGraphBuildResult,
  type RepoGraphEdge,
  type RepoGraphEdgeType,
  type RepoGraphNode,
  type RepoGraphNodeType,
  type RepoGraphV1,
  type RepoGraphV2,
  type TemporalGraphDiagnostics,
} from "./graph/index.js";

export {
  buildContextPacket,
  renderContextPacketMarkdown,
  type ContextCitation,
  type ContextItem,
  type ContextPacket,
  type ContextQuery,
} from "./context/index.js";

export * from "./maintenance/index.js";

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
  DEFAULT_REPO_MCP_HTTP_PORT,
  REPO_MCP_HTTP_BODY_LIMIT_BYTES,
  REPO_MCP_HTTP_HOST,
  runRepoMcpHttpServer,
  startRepoMcpHttpServer,
  type RepoMcpHttpLifecycleEvent,
  type RepoMcpHttpServerOptions,
  type RepoMcpHttpServerStartResult,
} from "./mcp/http.js";
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
  inspectRepoBrainArtifactIntegrity,
  verifyRepoMemory,
  verifyStoredRepoMemory,
  type ArtifactIntegrityCheck,
  type ArtifactIntegrityReport,
  type HarnessCheck,
  type HarnessReport,
} from "./harness/index.js";
