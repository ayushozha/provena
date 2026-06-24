import { dirname, join, normalize } from "node:path";

const TRY_EXTENSIONS = [
  "",
  ".ts",
  ".tsx",
  ".mts",
  ".cts",
  ".js",
  ".jsx",
  ".mjs",
  ".cjs",
];

const INDEX_SUFFIXES = [
  "/index.ts",
  "/index.tsx",
  "/index.js",
  "/index.jsx",
  "/index.mjs",
];

/** Extract the module specifier from a chunker import label. */
export function parseImportSpecifier(label: string): string | null {
  const trimmed = label.trim();
  const fromMatch = trimmed.match(/\bfrom\s+["']([^"']+)["']\s*$/);
  if (fromMatch) {
    return fromMatch[1] ?? null;
  }
  const bareMatch = trimmed.match(/^["']([^"']+)["']$/);
  if (bareMatch) {
    return bareMatch[1] ?? null;
  }
  return null;
}

export function isRelativeImport(moduleSpecifier: string): boolean {
  return (
    moduleSpecifier.startsWith("./") || moduleSpecifier.startsWith("../")
  );
}

function toPosixPath(path: string): string {
  return path.split("\\").join("/");
}

function normalizeRepoPath(path: string): string {
  return toPosixPath(normalize(path)).replace(/^\.\//, "");
}

/** Resolve a TypeScript import specifier to a repo-relative POSIX file path. */
export function resolveImportSpecifier(
  importerRelativePath: string,
  moduleSpecifier: string,
  indexedFiles: Iterable<string>,
): string | null {
  if (!isRelativeImport(moduleSpecifier)) {
    return null;
  }

  const indexed = new Set(
    [...indexedFiles].map((filePath) => normalizeRepoPath(filePath)),
  );
  const importerDir = dirname(normalizeRepoPath(importerRelativePath));
  const joined = normalizeRepoPath(join(importerDir, moduleSpecifier));

  const candidates: string[] = [];
  for (const ext of TRY_EXTENSIONS) {
    candidates.push(`${joined}${ext}`);
  }
  for (const suffix of INDEX_SUFFIXES) {
    candidates.push(`${joined}${suffix}`);
  }

  for (const candidate of candidates) {
    if (indexed.has(candidate)) {
      return candidate;
    }
  }

  return null;
}

/** Map import labels on a chunk to resolvable indexed file paths. */
export function resolveChunkImports(
  importerRelativePath: string,
  importLabels: string[],
  indexedFiles: Iterable<string>,
): string[] {
  const resolved = new Set<string>();
  for (const label of importLabels) {
    const specifier = parseImportSpecifier(label);
    if (!specifier) {
      continue;
    }
    const target = resolveImportSpecifier(
      importerRelativePath,
      specifier,
      indexedFiles,
    );
    if (target) {
      resolved.add(target);
    }
  }
  return [...resolved].sort((a, b) => a.localeCompare(b));
}