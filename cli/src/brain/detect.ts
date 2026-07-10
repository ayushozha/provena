import { spawnSync } from "node:child_process";
import { readFile, lstat } from "node:fs/promises";
import { basename, dirname, extname, join, resolve } from "node:path";
import fg from "fast-glob";
import ignore from "ignore";
import type {
  RepoCommand,
  RepoDirectory,
  RepoEnvironmentVariable,
  RepoFile,
  RepoFileKind,
  RepoMap,
  RepoPackage,
  RepoSymbol,
} from "./types.js";
import {
  canonicalJson,
  compareText,
  normalizeRepoPath,
  sha256,
  stableId,
  toPosixPath,
} from "./utils.js";

export interface ScanRepoOptions {
  includePatterns?: string[];
  excludePatterns?: string[];
  maxFileBytes?: number;
  maxFiles?: number;
  maxTotalBytes?: number;
  maxSymbolsPerFile?: number;
  warn?: (message: string) => void;
}

const DEFAULT_MAX_FILE_BYTES = 512 * 1024;
const DEFAULT_MAX_FILES = 100_000;
const DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024;
const DEFAULT_MAX_SYMBOLS_PER_FILE = 250;
const GENERATED_PREFIXES = [".git/", ".provena/"];
const FALLBACK_IGNORES = [
  ".git/**",
  ".provena/**",
  "node_modules/**",
  ".venv/**",
  "venv/**",
  "dist/**",
  "build/**",
  "coverage/**",
  "target/**",
  "__pycache__/**",
];
const BINARY_EXTENSIONS = new Set([
  ".7z",
  ".a",
  ".avi",
  ".bin",
  ".bmp",
  ".class",
  ".db",
  ".dll",
  ".dylib",
  ".eot",
  ".exe",
  ".gif",
  ".gz",
  ".ico",
  ".jpeg",
  ".jpg",
  ".mov",
  ".mp3",
  ".mp4",
  ".o",
  ".pdf",
  ".pem",
  ".png",
  ".pyc",
  ".rar",
  ".so",
  ".sqlite",
  ".tar",
  ".ttf",
  ".wasm",
  ".webp",
  ".woff",
  ".woff2",
  ".zip",
]);

const LANGUAGE_BY_EXTENSION: Record<string, string> = {
  ".c": "c",
  ".cc": "cpp",
  ".cpp": "cpp",
  ".cs": "csharp",
  ".css": "css",
  ".go": "go",
  ".h": "c",
  ".hpp": "cpp",
  ".html": "html",
  ".java": "java",
  ".js": "javascript",
  ".jsx": "jsx",
  ".json": "json",
  ".kt": "kotlin",
  ".kts": "kotlin",
  ".md": "markdown",
  ".mdx": "mdx",
  ".php": "php",
  ".proto": "protobuf",
  ".py": "python",
  ".rb": "ruby",
  ".rs": "rust",
  ".scss": "scss",
  ".sh": "shell",
  ".sql": "sql",
  ".swift": "swift",
  ".toml": "toml",
  ".ts": "typescript",
  ".tsx": "tsx",
  ".vue": "vue",
  ".xml": "xml",
  ".yaml": "yaml",
  ".yml": "yaml",
};

const MANIFEST_NAMES = new Set([
  "Cargo.toml",
  "Makefile",
  "go.mod",
  "package.json",
  "pyproject.toml",
  "requirements.txt",
]);

function isSecretPath(path: string): boolean {
  const parts = path.toLowerCase().split("/");
  const name = parts.at(-1) ?? "";
  const publicEnvironmentTemplate =
    name === ".env.example" ||
    name === ".env.sample" ||
    name === ".env.template" ||
    name === ".env.defaults" ||
    name === ".env.dist";
  return (
    parts.includes(".aws") ||
    parts.includes(".ssh") ||
    (name.startsWith(".env") && !publicEnvironmentTemplate) ||
    name === ".npmrc" ||
    name === "credentials.json" ||
    name === "id_rsa" ||
    name === "id_ed25519" ||
    name.endsWith(".key") ||
    name.endsWith(".p12") ||
    name.endsWith(".pfx") ||
    name.endsWith(".pem")
  );
}

function isGeneratedPath(path: string): boolean {
  return GENERATED_PREFIXES.some(
    (prefix) => path === prefix.slice(0, -1) || path.startsWith(prefix),
  );
}

async function fallbackCandidates(repoRoot: string): Promise<string[]> {
  const paths = await fg("**/*", {
    cwd: repoRoot,
    dot: true,
    followSymbolicLinks: false,
    onlyFiles: true,
    suppressErrors: true,
    ignore: FALLBACK_IGNORES,
  });
  const matcher = ignore();
  try {
    matcher.add(await readFile(join(repoRoot, ".gitignore"), "utf8"));
  } catch {
    // Non-git folders do not need a .gitignore.
  }
  return paths
    .map(toPosixPath)
    .filter((path) => !matcher.ignores(path))
    .sort(compareText);
}

async function candidatePaths(repoRoot: string): Promise<string[]> {
  const result = spawnSync(
    "git",
    ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
    { cwd: repoRoot, encoding: "buffer", maxBuffer: 64 * 1024 * 1024 },
  );
  if (result.status !== 0 || result.error) return fallbackCandidates(repoRoot);
  return result.stdout
    .toString("utf8")
    .split("\0")
    .filter(Boolean)
    .map(toPosixPath)
    .sort(compareText);
}

async function configuredCandidatePaths(
  repoRoot: string,
  options: ScanRepoOptions,
): Promise<string[]> {
  const candidates = await candidatePaths(repoRoot);
  if (!options.includePatterns && !options.excludePatterns) return candidates;
  const matches = await fg(
    options.includePatterns?.length ? options.includePatterns : ["**/*"],
    {
      cwd: repoRoot,
      dot: true,
      followSymbolicLinks: false,
      onlyFiles: true,
      suppressErrors: true,
      ignore: options.excludePatterns ?? [],
    },
  );
  const allowed = new Set(matches.map(toPosixPath));
  return candidates.filter((path) => allowed.has(path));
}

function languageFor(path: string): string | null {
  const name = basename(path);
  if (name === "Dockerfile") return "dockerfile";
  if (name === "Makefile") return "makefile";
  return LANGUAGE_BY_EXTENSION[extname(name).toLowerCase()] ?? null;
}

function kindFor(path: string): RepoFileKind {
  const lower = path.toLowerCase();
  const name = basename(path);
  if (MANIFEST_NAMES.has(name)) return "manifest";
  if (
    /(^|\/)(test|tests|spec|specs|__tests__)(\/|$)/.test(lower) ||
    /(?:^|\.|_)(?:test|spec)\.[^.]+$/.test(name.toLowerCase())
  ) {
    return "test";
  }
  if (/\.(?:md|mdx|rst|adoc)$/i.test(name) || /(^|\/)docs?\//.test(lower)) {
    return "documentation";
  }
  if (/\/(?:migrations?)\//.test(`/${lower}`) || /^\d+.*\.sql$/i.test(name)) {
    return "migration";
  }
  if (
    /(?:^|\/)(?:\.github|\.circleci|config)(?:\/|$)/.test(lower) ||
    /(?:^|\.)(?:config|rc)\.(?:js|cjs|mjs|ts|json|ya?ml|toml)$/i.test(name) ||
    /^(?:Dockerfile|docker-compose.*|tsconfig.*\.json)$/i.test(name) ||
    name.startsWith(".env.")
  ) {
    return "configuration";
  }
  if (/\.(?:svg|ico|webmanifest)$/i.test(name)) return "asset";
  return languageFor(path) ? "source" : "other";
}

function extractEnvironmentVariables(
  path: string,
  text: string,
  language: string | null,
): string[] {
  const names = new Set<string>();
  if (/^\.env\.(?:example|sample|template|defaults|dist)$/i.test(basename(path))) {
    for (const line of text.split(/\r?\n/)) {
      const name = /^\s*([A-Z][A-Z0-9_]{1,127})\s*=/.exec(line)?.[1];
      if (name) names.add(name);
    }
  }
  const patterns: RegExp[] = [];
  if (["javascript", "jsx", "typescript", "tsx"].includes(language ?? "")) {
    patterns.push(
      /\bprocess\.env\.([A-Z][A-Z0-9_]{1,127})\b/g,
      /\bprocess\.env\[["']([A-Z][A-Z0-9_]{1,127})["']\]/g,
    );
  } else if (language === "python") {
    patterns.push(
      /\bos\.getenv\(["']([A-Z][A-Z0-9_]{1,127})["']/g,
      /\bos\.environ(?:\.get)?\(?\[?["']([A-Z][A-Z0-9_]{1,127})["']/g,
    );
  } else if (language === "rust") {
    patterns.push(/\b(?:std::)?env::var\(["']([A-Z][A-Z0-9_]{1,127})["']/g);
  } else if (language === "go") {
    patterns.push(/\bos\.(?:Getenv|LookupEnv)\(["']([A-Z][A-Z0-9_]{1,127})["']/g);
  }
  for (const pattern of patterns) {
    for (const match of text.matchAll(pattern)) if (match[1]) names.add(match[1]);
  }
  return [...names].sort(compareText);
}

function extractImports(text: string, language: string | null): string[] {
  const imports = new Set<string>();
  const patterns: RegExp[] = [];
  if (["javascript", "jsx", "typescript", "tsx"].includes(language ?? "")) {
    patterns.push(
      /\b(?:import|export)\s+(?:[^"']*?\s+from\s+)?["']([^"']+)["']/g,
      /\brequire\(\s*["']([^"']+)["']\s*\)/g,
      /\bimport\(\s*["']([^"']+)["']\s*\)/g,
    );
  } else if (language === "python") {
    patterns.push(/^\s*(?:from|import)\s+(\.*[A-Za-z_][\w.]*)/gm);
  } else if (language === "rust") {
    patterns.push(/^\s*(?:pub\s+)?use\s+([A-Za-z_][\w:]*)/gm);
  } else if (language === "go") {
    patterns.push(/^\s*import\s+(?:[A-Za-z_.]+\s+)?["`]([^"`]+)["`]/gm);
    const block = /\bimport\s*\(([^)]*)\)/gs.exec(text)?.[1] ?? "";
    for (const match of block.matchAll(/["`]([^"`]+)["`]/g)) {
      if (match[1]) imports.add(match[1]);
    }
  }
  for (const pattern of patterns) {
    for (const match of text.matchAll(pattern)) {
      if (match[1]) imports.add(match[1]);
    }
  }
  return [...imports].sort(compareText);
}

interface SymbolMatch {
  name: string;
  kind: string;
  exported: boolean;
}

function symbolOnLine(line: string, language: string | null): SymbolMatch | null {
  let match: RegExpExecArray | null;
  if (["javascript", "jsx", "typescript", "tsx"].includes(language ?? "")) {
    match = /^\s*(export\s+)?(?:default\s+)?(?:async\s+)?(function|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)/.exec(
      line,
    );
    if (match?.[3]) return { name: match[3], kind: match[2] ?? "symbol", exported: Boolean(match[1]) };
    match = /^\s*(export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>/.exec(
      line,
    );
    if (match?.[2]) return { name: match[2], kind: "function", exported: Boolean(match[1]) };
  } else if (language === "python") {
    match = /^\s*(?:async\s+)?(def|class)\s+([A-Za-z_][\w]*)/.exec(line);
    if (match?.[2]) return { name: match[2], kind: match[1] === "def" ? "function" : "class", exported: !match[2].startsWith("_") };
  } else if (language === "go") {
    match = /^\s*(func|type)\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)/.exec(line);
    if (match?.[2]) return { name: match[2], kind: match[1] === "func" ? "function" : "type", exported: /^[A-Z]/.test(match[2]) };
  } else if (language === "rust") {
    match = /^\s*(pub(?:\([^)]*\))?\s+)?(?:async\s+)?(fn|struct|enum|trait|type)\s+([A-Za-z_][\w]*)/.exec(line);
    if (match?.[3]) return { name: match[3], kind: match[2] ?? "symbol", exported: Boolean(match[1]) };
  }
  return null;
}

function extractSymbols(
  path: string,
  text: string,
  language: string | null,
  limit: number,
): RepoSymbol[] {
  const counts = new Map<string, number>();
  const symbols: RepoSymbol[] = [];
  const lines = text.split(/\r?\n/);
  for (let index = 0; index < lines.length && symbols.length < limit; index += 1) {
    const found = symbolOnLine(lines[index] ?? "", language);
    if (!found) continue;
    const key = `${found.kind}\u0000${found.name}`;
    const ordinal = (counts.get(key) ?? 0) + 1;
    counts.set(key, ordinal);
    symbols.push({
      id: stableId("symbol", path, found.kind, found.name, String(ordinal)),
      path,
      name: found.name,
      kind: found.kind,
      line: index + 1,
      exported: found.exported,
    });
  }
  return symbols;
}

function managerFor(packagePath: string, allPaths: Set<string>): string {
  let current = packagePath;
  while (true) {
    const at = (name: string) => (current === "." ? name : `${current}/${name}`);
    if (allPaths.has(at("pnpm-lock.yaml"))) return "pnpm";
    if (allPaths.has(at("yarn.lock"))) return "yarn";
    if (allPaths.has(at("bun.lock")) || allPaths.has(at("bun.lockb"))) return "bun";
    if (allPaths.has(at("package-lock.json"))) return "npm";
    if (current === ".") break;
    current = toPosixPath(dirname(current));
  }
  return "npm";
}

function commandRecord(
  cwd: string,
  name: string,
  command: string,
  source: string,
): RepoCommand {
  return {
    id: stableId("command", cwd, name, command),
    name,
    command,
    cwd,
    source,
  };
}

function nodePackage(
  file: RepoFile,
  text: string,
  allPaths: Set<string>,
): { pkg: RepoPackage; commands: RepoCommand[] } | null {
  let manifest: {
    name?: unknown;
    packageManager?: unknown;
    scripts?: unknown;
    dependencies?: unknown;
    devDependencies?: unknown;
    peerDependencies?: unknown;
    optionalDependencies?: unknown;
  };
  try {
    manifest = JSON.parse(text) as typeof manifest;
  } catch {
    return null;
  }
  const cwd = toPosixPath(dirname(file.path)) || ".";
  const declaredManager =
    typeof manifest.packageManager === "string"
      ? /^(npm|pnpm|yarn|bun)(?:@|$)/.exec(manifest.packageManager)?.[1]
      : undefined;
  const manager = declaredManager ?? managerFor(cwd, allPaths);
  const scripts =
    manifest.scripts && typeof manifest.scripts === "object"
      ? (manifest.scripts as Record<string, unknown>)
      : {};
  const commands = Object.keys(scripts)
    .filter((name) => typeof scripts[name] === "string")
    .sort(compareText)
    .map((name) => commandRecord(cwd, name, `${manager} run ${name}`, file.path));
  const dependencyGroups = [
    manifest.dependencies,
    manifest.devDependencies,
    manifest.peerDependencies,
    manifest.optionalDependencies,
  ];
  const dependencies = new Set<string>();
  for (const group of dependencyGroups) {
    if (group && typeof group === "object") {
      for (const name of Object.keys(group)) dependencies.add(name);
    }
  }
  const name = typeof manifest.name === "string"
    ? manifest.name
    : cwd === "."
      ? "repository"
      : basename(cwd);
  return {
    pkg: {
      id: stableId("package", "node", cwd, name),
      path: cwd,
      manifestPath: file.path,
      name,
      ecosystem: "node",
      dependencies: [...dependencies].sort(compareText),
      commandIds: commands.map((command) => command.id),
    },
    commands,
  };
}

function simplePackage(
  file: RepoFile,
  text: string,
): { pkg: RepoPackage; commands: RepoCommand[] } | null {
  const cwd = toPosixPath(dirname(file.path)) || ".";
  const name = basename(file.path);
  if (name === "pyproject.toml") {
    const packageName = /^name\s*=\s*["']([^"']+)["']/m.exec(text)?.[1] ?? (cwd === "." ? "repository" : basename(cwd));
    return {
      pkg: { id: stableId("package", "python", cwd, packageName), path: cwd, manifestPath: file.path, name: packageName, ecosystem: "python", dependencies: [], commandIds: [] },
      commands: [],
    };
  }
  if (name === "go.mod") {
    const packageName = /^module\s+(\S+)/m.exec(text)?.[1] ?? (cwd === "." ? "repository" : basename(cwd));
    const dependencies = [...text.matchAll(/^\s*([\w./-]+)\s+v\d/gm)].map((match) => match[1]).filter((value): value is string => Boolean(value));
    return {
      pkg: { id: stableId("package", "go", cwd, packageName), path: cwd, manifestPath: file.path, name: packageName, ecosystem: "go", dependencies: [...new Set(dependencies)].sort(), commandIds: [] },
      commands: [],
    };
  }
  if (name === "Cargo.toml") {
    const packageName = /^name\s*=\s*["']([^"']+)["']/m.exec(text)?.[1] ?? (cwd === "." ? "repository" : basename(cwd));
    const dependenciesBlock = /^\[dependencies\]\s*\r?\n([\s\S]*?)(?=^\[|$(?![\s\S]))/m.exec(text)?.[1] ?? "";
    const dependencies = [...dependenciesBlock.matchAll(/^([\w-]+)\s*=/gm)].map((match) => match[1]).filter((value): value is string => Boolean(value)).sort();
    return {
      pkg: { id: stableId("package", "rust", cwd, packageName), path: cwd, manifestPath: file.path, name: packageName, ecosystem: "rust", dependencies, commandIds: [] },
      commands: [],
    };
  }
  if (name === "Makefile") {
    const targets = [...text.matchAll(/^([A-Za-z0-9][A-Za-z0-9_.-]*):(?:\s|$)/gm)]
      .map((match) => match[1])
      .filter((value): value is string => Boolean(value) && !value.startsWith("."));
    const commands = [...new Set(targets)].sort().map((target) => commandRecord(cwd, target, `make ${target}`, file.path));
    const packageName = cwd === "." ? "repository" : basename(cwd);
    return {
      pkg: { id: stableId("package", "make", cwd, packageName), path: cwd, manifestPath: file.path, name: packageName, ecosystem: "make", dependencies: [], commandIds: commands.map((command) => command.id) },
      commands,
    };
  }
  return null;
}

function directoryRecords(files: RepoFile[]): RepoDirectory[] {
  const counts = new Map<string, number>();
  for (const file of files) {
    let current = toPosixPath(dirname(file.path));
    while (current && current !== ".") {
      counts.set(current, (counts.get(current) ?? 0) + 1);
      current = toPosixPath(dirname(current));
    }
  }
  return [...counts.entries()]
    .sort(([a], [b]) => compareText(a, b))
    .map(([path, fileCount]) => ({ id: stableId("directory", path), path, fileCount }));
}

function rootDescription(files: Map<string, string>): string | null {
  const packageJson = files.get("package.json");
  if (packageJson) {
    try {
      const description = (JSON.parse(packageJson) as { description?: unknown }).description;
      if (typeof description === "string" && description.trim()) return description.trim();
    } catch {
      // A malformed manifest is still represented in the map.
    }
  }
  const pyproject = files.get("pyproject.toml");
  return pyproject ? (/^description\s*=\s*["']([^"']+)["']/m.exec(pyproject)?.[1] ?? null) : null;
}

function gitRepositoryName(repoRoot: string): string | null {
  const result = spawnSync("git", ["config", "--get", "remote.origin.url"], {
    cwd: repoRoot,
    encoding: "utf8",
  });
  if (result.status !== 0 || !result.stdout.trim()) return null;
  const remote = result.stdout.trim().replace(/[\\/]+$/, "").replace(/\.git$/, "");
  const name = /(?:[:/])([^:/]+)$/.exec(remote)?.[1];
  return name?.trim() || null;
}

export async function scanRepo(
  repoRoot: string,
  options: ScanRepoOptions = {},
): Promise<RepoMap> {
  const root = resolve(repoRoot);
  const warn = options.warn ?? (() => undefined);
  const maxFileBytes = options.maxFileBytes ?? DEFAULT_MAX_FILE_BYTES;
  const maxFiles = options.maxFiles ?? DEFAULT_MAX_FILES;
  const maxTotalBytes = options.maxTotalBytes ?? DEFAULT_MAX_TOTAL_BYTES;
  const maxSymbolsPerFile = options.maxSymbolsPerFile ?? DEFAULT_MAX_SYMBOLS_PER_FILE;
  const paths = await configuredCandidatePaths(root, options);
  const files: RepoFile[] = [];
  const symbols: RepoSymbol[] = [];
  const textByPath = new Map<string, string>();
  let totalBytes = 0;

  for (const candidate of paths) {
    if (files.length >= maxFiles) {
      warn(`stopping scan: reached ${maxFiles} file cap`);
      break;
    }
    const path = normalizeRepoPath(root, candidate);
    if (path === "." || isGeneratedPath(path) || isSecretPath(path) || BINARY_EXTENSIONS.has(extname(path).toLowerCase())) continue;
    const absolutePath = join(root, ...path.split("/"));
    let info;
    try {
      info = await lstat(absolutePath);
    } catch {
      continue;
    }
    if (!info.isFile() || info.isSymbolicLink()) continue;
    if (info.size > maxFileBytes) {
      warn(`skipping ${path}: exceeds ${maxFileBytes} byte cap`);
      continue;
    }
    let content: Buffer;
    try {
      content = await readFile(absolutePath);
    } catch {
      continue;
    }
    if (content.subarray(0, 8192).includes(0)) continue;
    const text = content.toString("utf8").replace(/\r\n?/g, "\n");
    const canonicalContent = Buffer.from(text, "utf8");
    if (totalBytes + canonicalContent.byteLength > maxTotalBytes) {
      warn(`stopping scan before ${path}: reached ${maxTotalBytes} total byte cap`);
      break;
    }
    totalBytes += canonicalContent.byteLength;
    const language = languageFor(path);
    const file: RepoFile = {
      id: stableId("file", path),
      path,
      kind: kindFor(path),
      language,
      sizeBytes: canonicalContent.byteLength,
      sha256: sha256(canonicalContent),
      imports: extractImports(text, language),
    };
    files.push(file);
    textByPath.set(path, text);
    symbols.push(...extractSymbols(path, text, language, maxSymbolsPerFile));
  }

  files.sort((a, b) => compareText(a.path, b.path));
  symbols.sort((a, b) => compareText(a.path, b.path) || a.line - b.line || compareText(a.name, b.name));
  const allPaths = new Set(files.map((file) => file.path));
  const packages: RepoPackage[] = [];
  const commandsById = new Map<string, RepoCommand>();
  const environmentSources = new Map<string, Set<string>>();
  for (const file of files) {
    const text = textByPath.get(file.path) ?? "";
    for (const name of extractEnvironmentVariables(file.path, text, file.language)) {
      const sources = environmentSources.get(name) ?? new Set<string>();
      sources.add(file.path);
      environmentSources.set(name, sources);
    }
  }
  for (const file of files.filter((item) => item.kind === "manifest")) {
    const text = textByPath.get(file.path) ?? "";
    const detected = basename(file.path) === "package.json"
      ? nodePackage(file, text, allPaths)
      : simplePackage(file, text);
    if (!detected) continue;
    packages.push(detected.pkg);
    for (const command of detected.commands) commandsById.set(command.id, command);
  }
  packages.sort((a, b) => compareText(a.path, b.path) || compareText(a.ecosystem, b.ecosystem) || compareText(a.name, b.name));
  const commands = [...commandsById.values()].sort((a, b) => compareText(a.cwd, b.cwd) || compareText(a.name, b.name));
  const environmentVariables: RepoEnvironmentVariable[] = [...environmentSources.entries()]
    .sort(([a], [b]) => compareText(a, b))
    .map(([name, sources]) => ({
      id: stableId("environment", name),
      name,
      sources: [...sources].sort(compareText),
    }));
  const languages = [...new Set(files.map((file) => file.language).filter((value): value is string => Boolean(value)))].sort();
  const packageName = packages.find(
    (pkg) => pkg.path === "." && pkg.name !== "repository",
  )?.name;
  const repositoryName = packageName || gitRepositoryName(root) || basename(root);
  const withoutFingerprint: Omit<RepoMap, "sourceFingerprint"> = {
    schemaVersion: 1,
    repository: { name: repositoryName, description: rootDescription(textByPath) },
    languages,
    directories: directoryRecords(files),
    files,
    symbols,
    packages,
    commands,
    environmentVariables,
  };
  return {
    ...withoutFingerprint,
    sourceFingerprint: sha256(canonicalJson(withoutFingerprint)),
  };
}
