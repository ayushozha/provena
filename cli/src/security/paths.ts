import { existsSync, lstatSync, realpathSync } from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";

function contained(root: string, target: string): boolean {
  const rel = relative(root, target);
  return rel === "" || (!rel.startsWith(`..${sep}`) && rel !== ".." && !isAbsolute(rel));
}
/**
 * Validate a repository mutation path component-by-component. Existing
 * symlinks/junctions are rejected so managed writes and recursive cleanup
 * cannot be redirected outside the checkout.
 */
export function assertSafeRepoPath(repoRoot: string, target: string): void {
  const root = resolve(repoRoot);
  const destination = resolve(target);
  if (!contained(root, destination)) {
    throw new Error(`refusing repository write outside ${root}: ${destination}`);
  }

  const realRoot = realpathSync(root);
  const rel = relative(root, destination);
  let cursor = root;
  for (const segment of rel.split(/[\\/]+/).filter(Boolean)) {
    cursor = resolve(cursor, segment);
    if (!existsSync(cursor)) continue;
    const info = lstatSync(cursor);
    if (info.isSymbolicLink()) {
      throw new Error(`refusing repository write through symlink/junction: ${cursor}`);
    }
    const actual = realpathSync(cursor);
    if (!contained(realRoot, actual)) {
      throw new Error(`repository path escapes through a reparse point: ${cursor}`);
    }
  }
}
