import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { ClassifiedFile } from "../indexer/classify.js";

/**
 * Language-agnostic repo intelligence for the brain: package managers, the
 * canonical test/build/run/deploy commands, entrypoints, services, env vars,
 * and config files — detected from manifests + file patterns. (PLAN claude A2.)
 */
export interface RepoCommands {
  test?: string;
  build?: string;
  run?: string;
  deploy?: string;
}

export interface RepoIntelligence {
  packageManagers: string[];
  languages: string[];
  commands: RepoCommands;
  entrypoints: string[];
  services: string[];
  envVars: string[];
  configFiles: string[];
}

/** Cap env-var source scanning so large repos stay fast. */
const MAX_ENV_SCAN_FILES = 400;

function readFileSafe(root: string, relPath: string): string | null {
  try {
    return readFileSync(join(root, relPath), "utf8");
  } catch {
    return null;
  }
}

function parseJsonSafe<T = unknown>(text: string | null): T | null {
  if (!text) return null;
  try {
    return JSON.parse(text) as T;
  } catch {
    return null;
  }
}

function detectPackageManagers(root: string): string[] {
  const managers: string[] = [];
  if (existsSync(join(root, "package.json"))) {
    if (existsSync(join(root, "pnpm-lock.yaml"))) managers.push("pnpm");
    else if (existsSync(join(root, "yarn.lock"))) managers.push("yarn");
    else if (existsSync(join(root, "bun.lockb"))) managers.push("bun");
    else managers.push("npm");
  }
  if (existsSync(join(root, "pyproject.toml")) || existsSync(join(root, "requirements.txt"))) {
    if (existsSync(join(root, "uv.lock"))) managers.push("uv");
    else if (existsSync(join(root, "poetry.lock"))) managers.push("poetry");
    else managers.push("pip");
  }
  if (existsSync(join(root, "go.mod"))) managers.push("go modules");
  if (existsSync(join(root, "Cargo.toml"))) managers.push("cargo");
  return managers;
}

function detectCommands(root: string): RepoCommands {
  const commands: RepoCommands = {};

  const pkg = parseJsonSafe<{ scripts?: Record<string, string> }>(readFileSafe(root, "package.json"));
  const scripts = pkg?.scripts ?? {};
  const pm = existsSync(join(root, "pnpm-lock.yaml"))
    ? "pnpm"
    : existsSync(join(root, "yarn.lock"))
      ? "yarn"
      : "npm";
  if (scripts.test) commands.test = `${pm} test`;
  if (scripts.build) commands.build = `${pm} run build`;
  if (scripts.dev || scripts.start) commands.run = `${pm} run ${scripts.dev ? "dev" : "start"}`;
  if (scripts.deploy) commands.deploy = `${pm} run deploy`;

  // Makefile targets fill any remaining slots.
  const makefile = readFileSafe(root, "Makefile") ?? readFileSafe(root, "makefile");
  if (makefile) {
    const targets = new Set(
      makefile
        .split(/\r?\n/)
        .map((line) => /^([A-Za-z0-9_-]+):/.exec(line)?.[1])
        .filter((t): t is string => Boolean(t)),
    );
    for (const t of ["test", "build", "run", "deploy"] as const) {
      if (!commands[t] && targets.has(t)) commands[t] = `make ${t}`;
    }
  }

  // Language defaults for still-empty slots.
  if (existsSync(join(root, "pyproject.toml")) || existsSync(join(root, "requirements.txt"))) {
    commands.test ??= "pytest";
  }
  if (existsSync(join(root, "go.mod"))) {
    commands.test ??= "go test ./...";
    commands.build ??= "go build ./...";
  }
  if (existsSync(join(root, "Cargo.toml"))) {
    commands.test ??= "cargo test";
    commands.build ??= "cargo build";
    commands.run ??= "cargo run";
  }
  return commands;
}

const SERVICE_MARKERS = ["package.json", "pyproject.toml", "go.mod", "Cargo.toml", "Dockerfile"];

function detectServices(root: string, files: ClassifiedFile[]): string[] {
  const topDirs = new Set<string>();
  for (const file of files) {
    const first = file.path.split("/")[0];
    if (first && file.path.includes("/")) topDirs.add(first);
  }
  const services: string[] = [];
  for (const dir of topDirs) {
    if (SERVICE_MARKERS.some((marker) => existsSync(join(root, dir, marker)))) {
      services.push(dir);
    }
  }
  return services.sort();
}

const ENV_PATTERNS: RegExp[] = [
  /process\.env\.([A-Z][A-Z0-9_]{2,})/g,
  /process\.env\[["']([A-Z][A-Z0-9_]{2,})["']\]/g,
  /os\.environ(?:\.get)?\[?\(?["']([A-Z][A-Z0-9_]{2,})["']/g,
  /os\.getenv\(["']([A-Z][A-Z0-9_]{2,})["']/g,
  /std::env::var\(["']([A-Z][A-Z0-9_]{2,})["']/g,
];

function detectEnvVars(root: string, files: ClassifiedFile[]): string[] {
  const vars = new Set<string>();

  // Prefer the declared surface: .env.example keys.
  const example = readFileSafe(root, ".env.example");
  if (example) {
    for (const line of example.split(/\r?\n/)) {
      const key = /^\s*([A-Z][A-Z0-9_]{2,})\s*=/.exec(line)?.[1];
      if (key) vars.add(key);
    }
  }

  // Then scan the highest-signal source files (bounded) for usages.
  const scanTargets = files
    .filter((f) => f.role === "source" || f.role === "entrypoint" || f.role === "config")
    .sort((a, b) => b.importance - a.importance)
    .slice(0, MAX_ENV_SCAN_FILES);

  for (const file of scanTargets) {
    const text = readFileSafe(root, file.path);
    if (!text) continue;
    for (const pattern of ENV_PATTERNS) {
      pattern.lastIndex = 0;
      let match: RegExpExecArray | null;
      while ((match = pattern.exec(text)) !== null) {
        if (match[1]) vars.add(match[1]);
      }
    }
  }
  return [...vars].sort();
}

export function detectRepoIntelligence(root: string, files: ClassifiedFile[]): RepoIntelligence {
  const languages = [
    ...new Set(files.map((f) => f.language).filter((l): l is string => Boolean(l))),
  ].sort();

  const entrypoints = files
    .filter((f) => f.role === "entrypoint")
    .sort((a, b) => b.importance - a.importance)
    .map((f) => f.path);

  const configFiles = files
    .filter((f) => f.role === "config")
    .map((f) => f.path)
    .sort();

  return {
    packageManagers: detectPackageManagers(root),
    languages,
    commands: detectCommands(root),
    entrypoints,
    services: detectServices(root, files),
    envVars: detectEnvVars(root, files),
    configFiles,
  };
}
