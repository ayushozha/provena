import { createHash, randomUUID } from "node:crypto";
import { mkdir, open, rename, rm } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import canonicalize from "canonicalize";
import { assertSafeRepoPath } from "../security/paths.js";

export function sha256(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

export function stableId(prefix: string, ...parts: string[]): string {
  return `${prefix}:${sha256(parts.join("\u0000")).slice(0, 20)}`;
}

/** Locale-independent UTF-16 code-unit ordering for persisted artifacts. */
export function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

export function toPosixPath(value: string): string {
  return value.split(sep).join("/");
}

export function normalizeRepoPath(repoRoot: string, value: string): string {
  const root = resolve(repoRoot);
  const target = isAbsolute(value) ? resolve(value) : resolve(root, value);
  const rel = toPosixPath(relative(root, target));
  if (!rel || rel === ".") return ".";
  if (rel === ".." || rel.startsWith("../") || isAbsolute(rel)) {
    throw new Error(`path must stay inside the repository: ${value}`);
  }
  return rel.replace(/^\.\//, "");
}

function sortJson(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortJson);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([a], [b]) => compareText(a, b))
      .map(([key, item]) => [key, sortJson(item)]),
  );
}

export function canonicalJson(value: unknown, pretty = false): string {
  if (pretty) return `${JSON.stringify(sortJson(value), null, 2)}\n`;
  const serialized = canonicalize(value);
  if (serialized === undefined) throw new TypeError("value is not JSON serializable");
  return `${serialized}\n`;
}

export async function writeFileAtomic(
  repoRoot: string,
  filePath: string,
  content: string | Buffer,
): Promise<void> {
  assertSafeRepoPath(repoRoot, dirname(filePath));
  assertSafeRepoPath(repoRoot, filePath);
  await mkdir(dirname(filePath), { recursive: true });
  const temporaryPath = `${filePath}.tmp-${process.pid}-${randomUUID()}`;
  assertSafeRepoPath(repoRoot, temporaryPath);
  try {
    const handle = await open(temporaryPath, "wx");
    try {
      await handle.writeFile(content);
      await handle.sync();
    } finally {
      await handle.close();
    }
    assertSafeRepoPath(repoRoot, temporaryPath);
    assertSafeRepoPath(repoRoot, filePath);
    await rename(temporaryPath, filePath);
  } catch (error) {
    assertSafeRepoPath(repoRoot, temporaryPath);
    await rm(temporaryPath, { force: true }).catch(() => undefined);
    throw error;
  }
}
