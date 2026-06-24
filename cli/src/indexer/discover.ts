import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  createReadStream,
  existsSync,
  readFileSync,
  readdirSync,
  statSync,
} from "node:fs";
import { basename, join, relative, resolve, sep } from "node:path";
import fg from "fast-glob";
import ignore, { type Ignore } from "ignore";
import type { ProvenaConfig } from "../config.js";

export interface DiscoveredFile {
  path: string;
  absolutePath: string;
  sha256: string;
  sizeBytes: number;
  language: string | null;
}

export interface DiscoverOptions {
  repoRoot?: string;
  cwd?: string;
  warn?: (message: string) => void;
}

const MAX_FILE_BYTES = 512 * 1024;
const SNIFF_BYTES = 8192;

const LANGUAGE_BY_EXT: Record<string, string> = {
  ".ts": "typescript",
  ".tsx": "tsx",
  ".js": "javascript",
  ".jsx": "jsx",
  ".py": "python",
  ".go": "go",
  ".rs": "rust",
  ".md": "markdown",
};

const BINARY_EXTENSIONS = new Set([
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".bmp",
  ".ico",
  ".webp",
  ".pdf",
  ".zip",
  ".gz",
  ".tar",
  ".7z",
  ".rar",
  ".exe",
  ".dll",
  ".so",
  ".dylib",
  ".woff",
  ".woff2",
  ".ttf",
  ".eot",
  ".mp3",
  ".mp4",
  ".avi",
  ".mov",
  ".wasm",
  ".bin",
  ".db",
  ".sqlite",
  ".pyc",
  ".class",
  ".o",
  ".a",
]);

function toPosixPath(filePath: string): string {
  return filePath.split(sep).join("/");
}

export function resolveRepoRoot(cwd: string = process.cwd()): string {
  const result = spawnSync("git", ["rev-parse", "--show-toplevel"], {
    cwd,
    encoding: "utf8",
  });
  if (result.status === 0) {
    return result.stdout.trim();
  }
  return cwd;
}

function gitIsAvailable(repoRoot: string): boolean {
  const result = spawnSync("git", ["rev-parse", "--show-toplevel"], {
    cwd: repoRoot,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    return false;
  }
  return resolve(result.stdout.trim()) === resolve(repoRoot);
}

function collectGitignoreFiles(repoRoot: string): string[] {
  const files: string[] = [];
  const queue = [repoRoot];

  while (queue.length > 0) {
    const dir = queue.pop();
    if (!dir) {
      continue;
    }

    const gitignorePath = join(dir, ".gitignore");
    if (existsSync(gitignorePath)) {
      files.push(gitignorePath);
    }

    let entries: string[];
    try {
      entries = readdirSync(dir, { withFileTypes: true })
        .filter((entry) => entry.isDirectory() && entry.name !== ".git")
        .map((entry) => join(dir, entry.name));
    } catch {
      continue;
    }

    for (const entry of entries) {
      queue.push(entry);
    }
  }

  return files;
}

export function buildRepoIgnoreMatcher(
  repoRoot: string,
): (relativePath: string) => boolean {
  const ig = buildIgnoreFilter(repoRoot);
  return (relativePath: string) => ig.ignores(toPosixPath(relativePath));
}

function buildIgnoreFilter(repoRoot: string): Ignore {
  const ig = ignore();
  for (const gitignorePath of collectGitignoreFiles(repoRoot)) {
    const relDir = toPosixPath(relative(repoRoot, join(gitignorePath, "..")) || ".");
    const prefix = relDir === "." ? "" : `${relDir}/`;
    const content = readFileSync(gitignorePath, "utf8");
    for (const line of content.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) {
        continue;
      }
      ig.add(prefix + trimmed);
    }
  }
  return ig;
}

function filterGitIgnored(repoRoot: string, relativePaths: string[]): string[] {
  if (relativePaths.length === 0) {
    return relativePaths;
  }

  if (!gitIsAvailable(repoRoot)) {
    const ig = buildIgnoreFilter(repoRoot);
    return relativePaths.filter((path) => !ig.ignores(path));
  }

  const input = Buffer.from(`${relativePaths.join("\0")}\0`, "utf8");
  const result = spawnSync("git", ["check-ignore", "--stdin", "-z"], {
    cwd: repoRoot,
    input,
    encoding: "buffer",
    maxBuffer: 64 * 1024 * 1024,
  });

  if (result.error || (result.status !== 0 && result.status !== 1)) {
    const ig = buildIgnoreFilter(repoRoot);
    return relativePaths.filter((path) => !ig.ignores(path));
  }

  const ignored = new Set<string>();
  if (result.stdout.length > 0) {
    for (const path of result.stdout.toString("utf8").split("\0").filter(Boolean)) {
      ignored.add(toPosixPath(path));
    }
  }

  return relativePaths.filter((path) => !ignored.has(path));
}

/** Hard denylist: always excluded from indexing regardless of .gitignore. */
export function isDeniedSecretPath(relativePath: string): boolean {
  const posix = toPosixPath(relativePath);
  const name = basename(posix);

  if (posix.split("/").some((segment) => segment === ".aws")) {
    return true;
  }
  if (name.startsWith(".env")) {
    return true;
  }
  if (name === ".npmrc" || name === "credentials.json") {
    return true;
  }
  const lower = name.toLowerCase();
  if (lower.endsWith(".pem") || lower.endsWith(".key")) {
    return true;
  }
  return false;
}

function hasBlockedExtension(relativePath: string): boolean {
  const name = basename(relativePath);
  const dot = name.lastIndexOf(".");
  if (dot === -1) {
    return false;
  }
  return BINARY_EXTENSIONS.has(name.slice(dot).toLowerCase());
}

function detectLanguage(relativePath: string): string | null {
  const name = basename(relativePath);
  const dot = name.lastIndexOf(".");
  if (dot === -1) {
    return null;
  }
  return LANGUAGE_BY_EXT[name.slice(dot).toLowerCase()] ?? null;
}

async function sha256File(absolutePath: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const hash = createHash("sha256");
    const stream = createReadStream(absolutePath);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("error", reject);
    stream.on("end", () => resolve(hash.digest("hex")));
  });
}

async function looksBinary(absolutePath: string, sizeBytes: number): Promise<boolean> {
  if (hasBlockedExtension(absolutePath)) {
    return true;
  }

  const sampleSize = Math.min(sizeBytes, SNIFF_BYTES);
  if (sampleSize === 0) {
    return false;
  }

  const buffer = Buffer.alloc(sampleSize);
  const stream = createReadStream(absolutePath, { start: 0, end: sampleSize - 1 });
  let offset = 0;

  await new Promise<void>((resolve, reject) => {
    stream.on("data", (chunk) => {
      const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      buf.copy(buffer, offset);
      offset += buf.length;
    });
    stream.on("error", reject);
    stream.on("end", () => resolve());
  });

  return buffer.includes(0);
}

export async function enumerateFiles(
  repoRoot: string,
  config: ProvenaConfig,
  options: DiscoverOptions = {},
): Promise<DiscoveredFile[]> {
  const warn = options.warn ?? (() => undefined);
  const include =
    config.index.include.length > 0 ? config.index.include : ["**/*"];
  const exclude = config.index.exclude;

  const absoluteMatches = await fg(include, {
    cwd: repoRoot,
    absolute: true,
    onlyFiles: true,
    dot: true,
    followSymbolicLinks: false,
    suppressErrors: true,
    ignore: exclude,
  });

  const relativeMatches = absoluteMatches
    .map((absolutePath) => toPosixPath(relative(repoRoot, absolutePath)))
    .sort((a, b) => a.localeCompare(b));

  const notGitIgnored = filterGitIgnored(repoRoot, relativeMatches);
  const discovered: DiscoveredFile[] = [];

  for (const relPath of notGitIgnored) {
    if (isDeniedSecretPath(relPath)) {
      continue;
    }
    const absolutePath = join(repoRoot, ...relPath.split("/"));

    let sizeBytes: number;
    try {
      sizeBytes = statSync(absolutePath).size;
    } catch {
      continue;
    }

    if (sizeBytes > MAX_FILE_BYTES) {
      warn(`skipping ${relPath}: exceeds ${MAX_FILE_BYTES} byte cap`);
      continue;
    }

    if (await looksBinary(absolutePath, sizeBytes)) {
      continue;
    }

    const sha256 = await sha256File(absolutePath);
    discovered.push({
      path: relPath,
      absolutePath,
      sha256,
      sizeBytes,
      language: detectLanguage(relPath),
    });
  }

  return discovered;
}

export async function discoverRepo(
  config: ProvenaConfig,
  options: DiscoverOptions = {},
): Promise<DiscoveredFile[]> {
  const cwd = options.cwd ?? process.cwd();
  const repoRoot = options.repoRoot ?? resolveRepoRoot(cwd);
  return enumerateFiles(repoRoot, config, options);
}