import { execSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { basename, join } from "node:path";

export const PROVENA_DIR = ".provena";
export const CONFIG_FILENAME = "config.json";
export const DEFAULT_DB_PATH = ".provena/provena.db";
export const DEFAULT_STORE_URL = "http://127.0.0.1:18092";
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
  backend: "sqlite";
  database: ProvenaDatabase;
  store_url: string;
  scope: ProvenaScope;
  index: ProvenaIndex;
}

export function provenaDir(projectRoot: string): string {
  return join(projectRoot, PROVENA_DIR);
}

export function configPath(projectRoot: string): string {
  return join(provenaDir(projectRoot), CONFIG_FILENAME);
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
  if (obj.backend !== "sqlite") {
    throw new Error('config.backend must be "sqlite"');
  }
  if (!isNonEmptyString(obj.store_url)) {
    throw new Error("config.store_url must be a non-empty string");
  }

  const database = obj.database;
  if (typeof database !== "object" || database === null) {
    throw new Error("config.database must be an object");
  }
  const dbPath = (database as Record<string, unknown>).path;
  if (!isNonEmptyString(dbPath)) {
    throw new Error("config.database.path must be a non-empty string");
  }

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
    backend: "sqlite",
    database: { path: dbPath },
    store_url: obj.store_url,
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
  return existsSync(configPath(projectRoot));
}

export function readConfig(projectRoot: string): ProvenaConfig {
  const raw = readFileSync(configPath(projectRoot), "utf8");
  return validateConfig(JSON.parse(raw));
}

export function writeConfig(projectRoot: string, config: ProvenaConfig): void {
  const validated = validateConfig(config);
  const dir = provenaDir(projectRoot);
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    configPath(projectRoot),
    `${JSON.stringify(validated, null, 2)}\n`,
    "utf8",
  );
}

const GITIGNORE_ENTRY = ".provena/";

export function ensureGitignore(projectRoot: string): boolean {
  const gitignorePath = join(projectRoot, ".gitignore");
  const existing = existsSync(gitignorePath)
    ? readFileSync(gitignorePath, "utf8")
    : "";

  const lines = existing.split(/\r?\n/);
  const hasEntry = lines.some(
    (line) => line.trim() === GITIGNORE_ENTRY || line.trim() === ".provena",
  );
  if (hasEntry) {
    return false;
  }

  const suffix = existing.length === 0 || existing.endsWith("\n") ? "" : "\n";
  const addition = `${suffix}${GITIGNORE_ENTRY}\n`;
  writeFileSync(gitignorePath, existing + addition, "utf8");
  return true;
}