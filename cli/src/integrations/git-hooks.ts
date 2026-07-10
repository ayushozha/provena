import { spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";
import { assertSafeRepoPath } from "../security/paths.js";

const START = "# >>> provena repo-memory hook >>>";
const END = "# <<< provena repo-memory hook <<<";
const HOOKS = ["post-checkout", "post-commit", "post-merge"] as const;

export interface GitHookInstallResult {
  hook: (typeof HOOKS)[number];
  path: string;
  action: "created" | "updated" | "unchanged" | "skipped";
  reason?: string;
}

function git(repoRoot: string, args: string[]): string | null {
  const result = spawnSync("git", args, {
    cwd: repoRoot,
    encoding: "utf8",
    shell: false,
  });
  return result.status === 0 ? result.stdout.trim() : null;
}

function insideRepo(repoRoot: string, path: string): boolean {
  const rel = relative(resolve(repoRoot), resolve(path));
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

function hookDirectory(repoRoot: string): { directory?: string; reason?: string } {
  const local = git(repoRoot, ["config", "--local", "--get", "core.hooksPath"]);
  const effective = git(repoRoot, ["config", "--get", "core.hooksPath"]);
  if (!local && effective) {
    return {
      reason: `global core.hooksPath is set (${effective}); refusing to modify user-global hooks`,
    };
  }
  if (local) {
    const directory = isAbsolute(local) ? local : resolve(repoRoot, local);
    return insideRepo(repoRoot, directory)
      ? { directory }
      : { reason: `repo core.hooksPath resolves outside the repository (${local})` };
  }
  const gitDirectory = git(repoRoot, ["rev-parse", "--git-dir"]);
  if (!gitDirectory) return { reason: "cannot locate Git metadata" };
  const directory = resolve(repoRoot, gitDirectory, "hooks");
  return insideRepo(repoRoot, directory)
    ? { directory }
    : { reason: "Git hooks directory resolves outside the repository" };
}

function managedBlock(): string {
  return [
    START,
    "provena_root=$(git rev-parse --show-toplevel 2>/dev/null || true)",
    "provena_runtime=\"$provena_root/.provena/runtime/runtime.mjs\"",
    "if [ -n \"$provena_root\" ] && [ -f \"$provena_runtime\" ]; then",
    "  (cd \"$provena_root\" && node \"$provena_runtime\" refresh --quiet >/dev/null 2>&1) || true",
    "fi",
    END,
  ].join("\n");
}

function writeAtomic(repoRoot: string, path: string, content: string): void {
  assertSafeRepoPath(repoRoot, path);
  const temporary = `${path}.provena-${process.pid}.tmp`;
  assertSafeRepoPath(repoRoot, temporary);
  try {
    writeFileSync(temporary, content, { encoding: "utf8", mode: 0o755 });
    assertSafeRepoPath(repoRoot, temporary);
    assertSafeRepoPath(repoRoot, path);
    renameSync(temporary, path);
    assertSafeRepoPath(repoRoot, path);
    chmodSync(path, 0o755);
  } finally {
    assertSafeRepoPath(repoRoot, temporary);
    rmSync(temporary, { force: true });
  }
}

function shellShebang(existing: string): boolean {
  const firstLine = existing.split(/\r?\n/, 1)[0] ?? "";
  return /^#!.*(?:\/(?:ba|da|k|z)?sh|\benv\s+(?:ba|da|k|z)?sh)(?:\s|$)/.test(firstLine);
}

function insertAfterShebang(existing: string, block: string): string {
  const end = existing.indexOf("\n");
  if (end < 0) return `${existing}\n\n${block}\n`;
  return `${existing.slice(0, end + 1)}\n${block}\n${existing.slice(end + 1)}`;
}

function updateHook(
  repoRoot: string,
  path: string,
): Pick<GitHookInstallResult, "action" | "reason"> {
  assertSafeRepoPath(repoRoot, path);
  const existing = existsSync(path) ? readFileSync(path, "utf8") : "";
  const start = existing.indexOf(START);
  const end = existing.indexOf(END);
  const block = managedBlock();
  let base = existing;

  if (start >= 0 || end >= 0) {
    if (start < 0 || end < start) {
      throw new Error(`malformed Provena managed hook block in ${path}`);
    }
    const before = existing.slice(0, start);
    const after = existing.slice(end + END.length);
    base = `${before.endsWith("\n\n") ? before.slice(0, -1) : before}${after.startsWith("\n") ? after.slice(1) : after}`;
  }

  if (base.length > 0 && !shellShebang(base)) {
    return {
      action: "skipped",
      reason: `existing hook is not a supported shell script: ${path}`,
    };
  }
  const next = base.length > 0
    ? insertAfterShebang(base, block)
    : `#!/bin/sh\n\n${block}\n`;

  if (next === existing) {
    assertSafeRepoPath(repoRoot, path);
    chmodSync(path, 0o755);
    return { action: "unchanged" };
  }
  writeAtomic(repoRoot, path, next);
  return { action: existing.length === 0 ? "created" : "updated" };
}

export function installGitHooks(repoRoot: string): GitHookInstallResult[] {
  const located = hookDirectory(repoRoot);
  if (!located.directory) {
    return HOOKS.map((hook) => ({
      hook,
      path: "",
      action: "skipped",
      reason: located.reason ?? "no safe hooks directory",
    }));
  }
  const directory = located.directory;
  assertSafeRepoPath(repoRoot, directory);
  mkdirSync(directory, { recursive: true });
  return HOOKS.map((hook) => {
    const path = join(directory, hook);
    const updated = updateHook(repoRoot, path);
    return { hook, path, ...updated };
  });
}
