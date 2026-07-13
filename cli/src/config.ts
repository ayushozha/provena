import { execSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { basename, join, resolve } from "node:path";
import { assertSafeRepoPath } from "./security/paths.js";

export const PROVENA_DIR = ".provena";
export const CONFIG_FILENAME = "config.json";
/** Maps repo-relative paths to emitted memory IDs (PLAN-06/07/12). */
export const INDEX_STATE_FILENAME = "index-state.json";
/** Summary of the most recent `provena index` run (PLAN-09). */
export const LAST_INDEX_FILENAME = "last-index.json";
/** Per-file failures from indexing; run continues (PLAN-09). */
export const INDEX_ERRORS_LOG = "index-errors.log";
export const DEFAULT_DB_PATH = ".provena/provena.db";
export const DEFAULT_STORE_URL = "http://127.0.0.1:18092";
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

export function isInsecureBindAllowed(): boolean {
  return process.env.PROVENA_INSECURE_BIND === "1";
}

function toPosixPath(filePath: string): string {
  return filePath.replace(/\\/g, "/");
}

function normalizeHostname(hostname: string): string {
  const lower = hostname.toLowerCase();
  if (lower.startsWith("[") && lower.endsWith("]")) {
    return lower.slice(1, -1);
  }
  return lower;
}

export function assertLoopbackStoreUrl(storeUrl: string): void {
  if (isInsecureBindAllowed()) {
    return;
  }
  const host = normalizeHostname(new URL(storeUrl).hostname);
  if (!LOOPBACK_HOSTS.has(host)) {
    throw new Error(
      `config.store_url must use a loopback host (127.0.0.1, localhost, ::1); got ${host}`,
    );
  }
}

export function assertDatabasePathUnderProvena(dbPath: string): void {
  const posix = toPosixPath(dbPath);
  if (posix.startsWith("/") || /^[a-zA-Z]:/.test(posix)) {
    throw new Error("config.database.path must be relative and stay under .provena/");
  }

  const segments: string[] = [];
  for (const part of posix.split("/")) {
    if (!part || part === ".") {
      continue;
    }
    if (part === "..") {
      if (segments.length === 0) {
        throw new Error("config.database.path must stay under .provena/");
      }
      segments.pop();
      continue;
    }
    segments.push(part);
  }

  if (segments.length === 0 || segments[0] !== PROVENA_DIR) {
    throw new Error("config.database.path must stay under .provena/");
  }
}

export const DEFAULT_INDEX_INCLUDE = ["**/*"] as const;
export const DEFAULT_INDEX_EXCLUDE = [
  "node_modules/**",
  ".git/**",
  "dist/**",
  "build/**",
] as const;

export interface ProvenaScope {
  tenant_id: string;
  project_id: string;
}

export interface ProvenaDatabase {
  path: string;
}

export interface ProvenaIndex {
  include: string[];
  exclude: string[];
}

export interface ProvenaConfig {
  version: 1;
  repository_id?: string;
  backend: "sqlite";
  database: ProvenaDatabase;
  store_url: string;
  /** When set, memory writes use intelligence `POST /v1/pipeline/write` (PLAN-09). */
  intelligence_url?: string;
  scope: ProvenaScope;
  index: ProvenaIndex;
}

export function provenaDir(projectRoot: string): string {
  return join(projectRoot, PROVENA_DIR);
}

export function configPath(projectRoot: string): string {
  return join(provenaDir(projectRoot), CONFIG_FILENAME);
}

export function pidFilePath(projectRoot: string): string {
  return join(provenaDir(projectRoot), "store.pid");
}

export function resolveDbPath(config: ProvenaConfig, projectRoot: string): string {
  const raw = process.env.PROVENA_DB_PATH ?? config.database.path;
  return resolve(projectRoot, raw);
}

export interface StoreEndpoint {
  host: string;
  port: number;
  healthUrl: string;
}

export function parseStoreUrl(storeUrl: string): StoreEndpoint {
  const parsed = new URL(storeUrl);
  if (!parsed.hostname) {
    throw new Error(`invalid store_url host: ${storeUrl}`);
  }
  const port =
    parsed.port !== ""
      ? Number(parsed.port)
      : parsed.protocol === "https:"
        ? 443
        : 80;
  if (!Number.isFinite(port) || port <= 0) {
    throw new Error(`invalid store_url port: ${storeUrl}`);
  }
  const host = parsed.hostname;
  return {
    host,
    port,
    healthUrl: `${parsed.protocol}//${host}:${port}/healthz`,
  };
}

export interface LoadedConfig {
  config: ProvenaConfig;
  projectRoot: string;
  configFile: string;
}

export function loadConfig(cwd: string = process.cwd()): LoadedConfig {
  const projectRoot = getGitRoot(cwd);
  const configFile = configPath(projectRoot);
  if (!configExists(projectRoot)) {
    throw new Error(
      `no config at ${configFile}; run \`provena init\` in your repo first`,
    );
  }
  return { config: readConfig(projectRoot), projectRoot, configFile };
}

export function getGitRoot(cwd: string): string {
  try {
    const out = execSync("git rev-parse --show-toplevel", {
      cwd,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    });
    return out.trim();
  } catch {
    return cwd;
  }
}

export function createDefaultConfig(options: {
  cwd: string;
  gitRoot: string;
}): ProvenaConfig {
  return {
    version: 1,
    repository_id: randomUUID(),
    backend: "sqlite",
    database: { path: DEFAULT_DB_PATH },
    store_url: DEFAULT_STORE_URL,
    scope: {
      tenant_id: basename(options.cwd),
      project_id: basename(options.gitRoot),
    },
    index: {
      include: [...DEFAULT_INDEX_INCLUDE],
      exclude: [...DEFAULT_INDEX_EXCLUDE],
    },
  };
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isStringArray(value: unknown): value is string[] {
  return (
    Array.isArray(value) &&
    value.every((item) => typeof item === "string")
  );
}

export function validateConfig(value: unknown): ProvenaConfig {
  if (typeof value !== "object" || value === null) {
    throw new Error("config must be an object");
  }

  const obj = value as Record<string, unknown>;

  if (obj.version !== 1) {
    throw new Error("config.version must be 1");
  }
  let repositoryId: string | undefined;
  if (obj.repository_id !== undefined) {
    if (
      !isNonEmptyString(obj.repository_id) ||
      !/^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$/.test(obj.repository_id)
    ) {
      throw new Error("config.repository_id must be a stable identifier");
    }
    repositoryId = obj.repository_id;
  }
  if (obj.backend !== "sqlite") {
    throw new Error('config.backend must be "sqlite"');
  }
  if (!isNonEmptyString(obj.store_url)) {
    throw new Error("config.store_url must be a non-empty string");
  }
  assertLoopbackStoreUrl(obj.store_url);

  let intelligenceUrl: string | undefined;
  if (obj.intelligence_url !== undefined && obj.intelligence_url !== null) {
    if (!isNonEmptyString(obj.intelligence_url)) {
      throw new Error("config.intelligence_url must be a non-empty string when set");
    }
    intelligenceUrl = obj.intelligence_url;
  }

  const database = obj.database;
  if (typeof database !== "object" || database === null) {
    throw new Error("config.database must be an object");
  }
  const dbPath = (database as Record<string, unknown>).path;
  if (!isNonEmptyString(dbPath)) {
    throw new Error("config.database.path must be a non-empty string");
  }
  assertDatabasePathUnderProvena(dbPath);

  const scope = obj.scope;
  if (typeof scope !== "object" || scope === null) {
    throw new Error("config.scope must be an object");
  }
  const scopeObj = scope as Record<string, unknown>;
  if (!isNonEmptyString(scopeObj.tenant_id)) {
    throw new Error("config.scope.tenant_id must be a non-empty string");
  }
  if (!isNonEmptyString(scopeObj.project_id)) {
    throw new Error("config.scope.project_id must be a non-empty string");
  }

  const index = obj.index;
  if (typeof index !== "object" || index === null) {
    throw new Error("config.index must be an object");
  }
  const indexObj = index as Record<string, unknown>;
  if (!isStringArray(indexObj.include)) {
    throw new Error("config.index.include must be an array of strings");
  }
  if (!isStringArray(indexObj.exclude)) {
    throw new Error("config.index.exclude must be an array of strings");
  }

  return {
    version: 1,
    ...(repositoryId ? { repository_id: repositoryId } : {}),
    backend: "sqlite",
    database: { path: dbPath },
    store_url: obj.store_url,
    ...(intelligenceUrl ? { intelligence_url: intelligenceUrl } : {}),
    scope: {
      tenant_id: scopeObj.tenant_id,
      project_id: scopeObj.project_id,
    },
    index: {
      include: indexObj.include,
      exclude: indexObj.exclude,
    },
  };
}

export function configExists(projectRoot: string): boolean {
  const path = configPath(projectRoot);
  assertSafeRepoPath(projectRoot, path);
  return existsSync(path);
}

export function readConfig(projectRoot: string): ProvenaConfig {
  const path = configPath(projectRoot);
  assertSafeRepoPath(projectRoot, path);
  const raw = readFileSync(path, "utf8");
  return validateConfig(JSON.parse(raw));
}

export function writeConfig(projectRoot: string, config: ProvenaConfig): void {
  const validated = validateConfig(config);
  const dir = provenaDir(projectRoot);
  assertSafeRepoPath(projectRoot, dir);
  assertSafeRepoPath(projectRoot, configPath(projectRoot));
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    configPath(projectRoot),
    `${JSON.stringify(validated, null, 2)}\n`,
    "utf8",
  );
}

const GITIGNORE_START = "# >>> provena local state >>>";
const GITIGNORE_END = "# <<< provena local state <<<";
const GITIGNORE_ENTRIES = [
  "!.provena/",
  ".provena/*",
  "!.provena/config.json",
  "!.provena/repo.brain.md",
  "!.provena/agent-instructions.md",
  "!.provena/repo.map.json",
  "!.provena/graph.json",
  "!.provena/manifest.json",
  "!.provena/memory/",
  ".provena/memory/*",
  "!.provena/memory/events.jsonl",
  "!.provena/schema/",
  ".provena/schema/*",
  "!.provena/schema/memory-event.schema.json",
  "!.provena/views/",
  ".provena/views/*",
  "!.provena/views/*.md",
] as const;

function managedGitignoreBlock(): string {
  return [GITIGNORE_START, ...GITIGNORE_ENTRIES, GITIGNORE_END].join("\n");
}

export function ensureGitignore(projectRoot: string): boolean {
  const gitignorePath = join(projectRoot, ".gitignore");
  assertSafeRepoPath(projectRoot, gitignorePath);
  const existing = existsSync(gitignorePath)
    ? readFileSync(gitignorePath, "utf8")
    : "";
  const lines = existing.split(/\r?\n/);
  const start = lines.findIndex((line) => line.trim() === GITIGNORE_START);
  const end = lines.findIndex((line) => line.trim() === GITIGNORE_END);
  if ((start >= 0) !== (end >= 0) || (start >= 0 && end < start)) {
    throw new Error(`malformed Provena managed block in ${gitignorePath}`);
  }

  // Keep legacy or user-owned whole-directory ignores. The later managed
  // allowlist re-includes only Provena's durable contract, so migration cannot
  // accidentally expose arbitrary historical files under .provena/.
  const managedStart = lines.findIndex(
    (line) => line.trim() === GITIGNORE_START,
  );
  const managedEnd = lines.findIndex(
    (line) => line.trim() === GITIGNORE_END,
  );
  const block = managedGitignoreBlock().split("\n");
  let nextLines: string[];
  if (managedStart >= 0) {
    nextLines = [
      ...lines.slice(0, managedStart),
      ...block,
      ...lines.slice(managedEnd + 1),
    ];
  } else {
    while (lines.at(-1) === "") lines.pop();
    nextLines = [
      ...lines,
      ...(lines.length > 0 ? [""] : []),
      ...block,
      "",
    ];
  }

  const next = nextLines.join("\n");
  if (next === existing) {
    return false;
  }
  writeFileSync(gitignorePath, next, "utf8");
  return true;
}
