/** Programmatic API surface for @provena/cli. */
export const CLI_PACKAGE = "@provena/cli";

export {
  CONFIG_FILENAME,
  DEFAULT_DB_PATH,
  DEFAULT_INDEX_EXCLUDE,
  DEFAULT_INDEX_INCLUDE,
  DEFAULT_STORE_URL,
  PROVENA_DIR,
  configExists,
  configPath,
  createDefaultConfig,
  ensureGitignore,
  getGitRoot,
  provenaDir,
  readConfig,
  validateConfig,
  writeConfig,
  type ProvenaConfig,
  type ProvenaDatabase,
  type ProvenaIndex,
  type ProvenaScope,
} from "./config.js";

export {
  discoverRepo,
  enumerateFiles,
  resolveRepoRoot,
  type DiscoveredFile,
  type DiscoverOptions,
} from "./indexer/discover.js";

export { chunkTypeScriptFile } from "./indexer/chunkers/typescript.js";
export type { ChunkKind, CodeChunk } from "./indexer/types.js";