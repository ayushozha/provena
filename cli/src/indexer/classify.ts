import { basename } from "node:path";
import type { DiscoveredFile } from "./discover.js";

/**
 * Role a file plays in the repo. Drives brain prioritization: high-signal roles
 * (entrypoint, config) are surfaced in repo.brain.md; low-signal ones (generated,
 * vendor) are downweighted so the bootloader stays small. (PLAN claude A1.)
 */
export type FileRole =
  | "entrypoint"
  | "config"
  | "test"
  | "doc"
  | "generated"
  | "vendor"
  | "source";

export interface ClassifiedFile {
  path: string;
  role: FileRole;
  /** 0..1 signal score; higher = more worth an agent's attention. */
  importance: number;
  language: string | null;
  sizeBytes: number;
}

export interface ClassifyOptions {
  /** path -> number of other files importing it (import fan-in), if known. */
  fanIn?: Map<string, number>;
}

const CONFIG_BASENAMES = new Set([
  "package.json",
  "tsconfig.json",
  "pyproject.toml",
  "setup.py",
  "setup.cfg",
  "requirements.txt",
  "go.mod",
  "cargo.toml",
  "dockerfile",
  "docker-compose.yml",
  "docker-compose.yaml",
  "makefile",
  "justfile",
  ".eslintrc",
  ".eslintrc.json",
  ".eslintrc.js",
  ".prettierrc",
  ".gitignore",
  ".env.example",
  "vite.config.ts",
  "vitest.config.ts",
  "jest.config.js",
  "ruff.toml",
  ".ruff.toml",
]);

const CONFIG_EXTENSIONS = new Set([
  ".toml",
  ".ini",
  ".cfg",
  ".yaml",
  ".yml",
  ".conf",
]);

const DOC_EXTENSIONS = new Set([".md", ".mdx", ".rst", ".txt", ".adoc"]);

const ENTRYPOINT_BASENAMES = new Set([
  "main.py",
  "__main__.py",
  "manage.py",
  "app.py",
  "wsgi.py",
  "asgi.py",
  "main.go",
  "main.rs",
  "index.ts",
  "index.js",
  "main.ts",
  "main.js",
  "cli.ts",
  "cli.js",
  "server.ts",
  "server.js",
]);

const IMPORTANCE_BY_ROLE: Record<FileRole, number> = {
  entrypoint: 0.9,
  config: 0.6,
  source: 0.5,
  doc: 0.35,
  test: 0.25,
  generated: 0.1,
  vendor: 0.05,
};

function segments(path: string): string[] {
  return path.split("/").filter(Boolean);
}

function hasSegment(path: string, ...names: string[]): boolean {
  const set = new Set(names);
  return segments(path).some((s) => set.has(s.toLowerCase()));
}

export function classifyRole(path: string): FileRole {
  const lower = path.toLowerCase();
  const name = basename(lower);
  const ext = name.includes(".") ? name.slice(name.lastIndexOf(".")) : "";

  if (hasSegment(lower, "node_modules", "vendor", "third_party", ".venv", "venv")) {
    return "vendor";
  }
  if (hasSegment(lower, "dist", "build", "out", "generated", "__pycache__", "target") || name.endsWith(".min.js") || name.endsWith(".map")) {
    return "generated";
  }
  if (
    hasSegment(lower, "test", "tests", "__tests__", "spec", "e2e") ||
    name.includes(".test.") ||
    name.includes(".spec.") ||
    name.endsWith("_test.go") ||
    name.startsWith("test_")
  ) {
    return "test";
  }
  if (DOC_EXTENSIONS.has(ext) || hasSegment(lower, "docs")) {
    return "doc";
  }
  if (CONFIG_BASENAMES.has(name) || CONFIG_EXTENSIONS.has(ext) || name.startsWith(".env")) {
    return "config";
  }
  if (ENTRYPOINT_BASENAMES.has(name) || /(^|\/)cmd\/[^/]+\/main\.go$/.test(lower)) {
    return "entrypoint";
  }
  return "source";
}

/** Shallower paths and higher import fan-in raise importance; clamp to [0,1]. */
export function classifyFile(file: DiscoveredFile, options: ClassifyOptions = {}): ClassifiedFile {
  const role = classifyRole(file.path);
  let score = IMPORTANCE_BY_ROLE[role];

  const depth = segments(file.path).length;
  score += Math.max(0, 0.15 - (depth - 1) * 0.03); // shallow bonus

  const fan = options.fanIn?.get(file.path);
  if (fan && fan > 0) {
    score += Math.min(0.3, fan * 0.03); // reused modules matter
  }

  // Very large source files are usually generated/low-signal; nudge down.
  if (role === "source" && file.sizeBytes > 200_000) {
    score -= 0.1;
  }

  return {
    path: file.path,
    role,
    importance: Math.max(0, Math.min(1, Number(score.toFixed(3)))),
    language: file.language,
    sizeBytes: file.sizeBytes,
  };
}

export function classifyFiles(files: DiscoveredFile[], options: ClassifyOptions = {}): ClassifiedFile[] {
  return files.map((file) => classifyFile(file, options));
}
