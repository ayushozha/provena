import { spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readlinkSync,
  readdirSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { assertSafeRepoPath } from "../security/paths.js";

export interface RuntimeInstallResult {
  runtimeRoot: string;
  runnerPath: string;
  installed: boolean;
  reused: boolean;
}

const NPM_COMMAND_TIMEOUT_MS = 5 * 60 * 1_000;

function packageRoot(): string {
  // dist/integrations/runtime.js -> package root
  return resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
}

function packageVersion(root: string): string {
  const value = JSON.parse(readFileSync(join(root, "package.json"), "utf8")) as {
    version?: unknown;
  };
  if (typeof value.version !== "string" || !/^\d+\.\d+\.\d+/.test(value.version)) {
    throw new Error("cannot determine @provena/cli package version");
  }
  return value.version;
}

function runtimeSource(version: string): string {
  return `#!/usr/bin/env node
import { appendFileSync, existsSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { refreshRepoBrain } from "./node_modules/@provena/cli/dist/brain/index.js";
import { assertSafeRepoPath } from "./node_modules/@provena/cli/dist/security/paths.js";

const VERSION = ${JSON.stringify(version)};
const args = process.argv.slice(2);
const command = args[0] ?? "refresh";
const quiet = args.includes("--quiet");
const repoRoot = resolve(process.cwd());

async function refresh() {
  const result = await refreshRepoBrain(repoRoot);
  if (!quiet) console.log(\`Provena \${VERSION}: refreshed \${result.map.files.length} files\`);
}

if (command === "refresh") {
  await refresh();
} else if (command === "daemon") {
  const intervalAt = args.indexOf("--interval-ms");
  const interval = intervalAt >= 0 ? Number(args[intervalAt + 1]) : 900000;
  const tokenAt = args.indexOf("--token");
  const token = tokenAt >= 0 ? args[tokenAt + 1] : "";
  if (!Number.isFinite(interval) || interval < 10000) throw new Error("interval must be at least 10000ms");
  if (!token || token.length < 16) throw new Error("daemon token is required");
  const pidPath = join(repoRoot, ".provena", "daemon.pid");
  const heartbeatPath = join(repoRoot, ".provena", "daemon.heartbeat");
  const logPath = join(repoRoot, ".provena", "daemon.log");
  const stopPath = join(repoRoot, ".provena", "daemon.stop");
  const runner = resolve(process.argv[1]);
  const safe = (path) => assertSafeRepoPath(repoRoot, path);
  for (const path of [pidPath, heartbeatPath, logPath, stopPath, runner]) safe(path);
  const readJson = (path) => {
    safe(path);
    if (!existsSync(path)) return null;
    try { return JSON.parse(readFileSync(path, "utf8")); } catch { return null; }
  };
  const writeJson = (path, value) => {
    safe(path);
    const temporary = \`\${path}.provena-\${process.pid}.tmp\`;
    safe(temporary);
    try {
      writeFileSync(temporary, \`\${JSON.stringify(value)}\\n\`, { encoding: "utf8", mode: 0o600 });
      safe(temporary);
      safe(path);
      renameSync(temporary, path);
    } finally {
      safe(temporary);
      rmSync(temporary, { force: true });
    }
  };
  const state = {
    schemaVersion: 1,
    pid: process.pid,
    token,
    runner,
    startedAt: new Date().toISOString(),
    intervalMs: interval,
  };
  const existingOwner = readJson(pidPath);
  if (
    existingOwner &&
    (existingOwner.pid !== process.pid || existingOwner.token !== token)
  ) {
    process.exit(0);
  }
  const heartbeat = () => writeJson(heartbeatPath, {
    schemaVersion: 1,
    pid: process.pid,
    token,
    heartbeatAt: new Date().toISOString(),
  });
  writeJson(pidPath, state);
  heartbeat();
  const tick = async () => {
    try {
      await refresh();
      safe(logPath);
      appendFileSync(logPath, \`\${new Date().toISOString()} refreshed\\n\`, "utf8");
    } catch (error) {
      try {
        safe(logPath);
        appendFileSync(logPath, \`\${new Date().toISOString()} error \${error instanceof Error ? error.message : String(error)}\\n\`, "utf8");
      } catch {}
    }
  };
  const removeIfOwned = (path) => {
    const value = readJson(path);
    if (value?.token === token && value?.pid === process.pid) {
      safe(path);
      rmSync(path, { force: true });
    }
  };
  process.once("exit", () => {
    removeIfOwned(pidPath);
    removeIfOwned(heartbeatPath);
    const stop = readJson(stopPath);
    if (stop?.token === token) {
      safe(stopPath);
      rmSync(stopPath, { force: true });
    }
  });
  process.once("SIGINT", () => process.exit(0));
  process.once("SIGTERM", () => process.exit(0));
  const stopIfRequested = () => {
    const stop = readJson(stopPath);
    if (stop?.token === token && stop?.pid === process.pid) process.exit(0);
  };
  setInterval(heartbeat, 2000);
  stopIfRequested();
  setInterval(stopIfRequested, 1000);
  await tick();
  let ticking = false;
  setInterval(async () => {
    if (ticking) return;
    ticking = true;
    try { await tick(); } finally { ticking = false; }
  }, interval);
} else {
  throw new Error(\`unknown portable runtime command: \${command}\`);
}
`;
}

function writeAtomic(path: string, content: string): void {
  const temporary = `${path}.provena-${process.pid}.tmp`;
  try {
    writeFileSync(temporary, content, { encoding: "utf8", mode: 0o700 });
    renameSync(temporary, path);
  } finally {
    rmSync(temporary, { force: true });
  }
}

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

export function runtimeTreeSha256(root: string): string {
  const resolvedRoot = resolve(root);
  const hash = createHash("sha256");
  const visit = (directory: string, prefix: string): void => {
    const entries = readdirSync(directory, { withFileTypes: true })
      .sort((left, right) => compareText(left.name, right.name));
    for (const entry of entries) {
      const relativePath = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (relativePath === "integrity.json") continue;
      const absolutePath = join(directory, entry.name);
      const info = lstatSync(absolutePath);
      if (info.isSymbolicLink()) {
        const target = readlinkSync(absolutePath);
        const resolvedTarget = resolve(directory, target);
        const relativeTarget = relative(resolvedRoot, resolvedTarget);
        if (
          relativeTarget === ".." ||
          relativeTarget.startsWith(`..${sep}`) ||
          isAbsolute(relativeTarget)
        ) {
          throw new Error(`portable runtime symlink escapes its root: ${relativePath}`);
        }
        hash.update(`L\0${relativePath}\0${target}\0`);
      } else if (info.isDirectory()) {
        hash.update(`D\0${relativePath}\0`);
        visit(absolutePath, relativePath);
      } else if (info.isFile()) {
        hash.update(`F\0${relativePath}\0`);
        hash.update(readFileSync(absolutePath));
        hash.update("\0");
      }
    }
  };
  visit(root, "");
  return hash.digest("hex");
}

function canReuseRuntime(
  repoRoot: string,
  runtimeRoot: string,
  runnerPath: string,
  wantedRunner: string,
  version: string,
): boolean {
  if (!existsSync(runtimeRoot)) return false;
  for (const path of [runtimeRoot, runnerPath, join(runtimeRoot, "integrity.json")]) {
    assertSafeRepoPath(repoRoot, path);
  }
  try {
    if (!lstatSync(runtimeRoot).isDirectory() || lstatSync(runtimeRoot).isSymbolicLink()) {
      return false;
    }
    const installedRoot = join(runtimeRoot, "node_modules", "@provena", "cli");
    assertSafeRepoPath(repoRoot, installedRoot);
    if (lstatSync(installedRoot).isSymbolicLink()) return false;
    const integrity = JSON.parse(
      readFileSync(join(runtimeRoot, "integrity.json"), "utf8"),
    ) as Record<string, unknown>;
    return (
      integrity.schemaVersion === 2 &&
      integrity.packageVersion === version &&
      typeof integrity.runtimeTreeSha256 === "string" &&
      packageVersion(installedRoot) === version &&
      readFileSync(runnerPath, "utf8") === wantedRunner &&
      runtimeTreeSha256(runtimeRoot) === integrity.runtimeTreeSha256
    );
  } catch {
    return false;
  }
}

/**
 * Persist the package that invoked `npx provena init` inside ignored local
 * state. Hooks can then refresh without depending on an npm cache or network.
 * Dependency lifecycle scripts are disabled because refresh only needs the
 * package's JavaScript repo-brain path.
 */
export function installPortableRuntime(repoRoot: string): RuntimeInstallResult {
  const sourceRoot = packageRoot();
  const version = packageVersion(sourceRoot);
  const runtimeRoot = join(repoRoot, ".provena", "runtime");
  const runnerPath = join(runtimeRoot, "runtime.mjs");
  const wanted = runtimeSource(version);
  const token = randomUUID();
  const cacheRoot = join(repoRoot, ".provena", "cache");
  const stageRoot = join(cacheRoot, `runtime-stage-${token}`);
  const packRoot = join(cacheRoot, `runtime-pack-${token}`);
  const backupRoot = join(cacheRoot, `runtime-backup-${token}`);
  for (const path of [cacheRoot, stageRoot, packRoot, backupRoot, runtimeRoot]) {
    assertSafeRepoPath(repoRoot, path);
  }
  if (canReuseRuntime(repoRoot, runtimeRoot, runnerPath, wanted, version)) {
    return { runtimeRoot, runnerPath, installed: false, reused: true };
  }
  mkdirSync(cacheRoot, { recursive: true });
  mkdirSync(stageRoot);
  mkdirSync(packRoot);
  const npmCliCandidates = [
    process.env.npm_execpath,
    join(dirname(process.execPath), "node_modules", "npm", "bin", "npm-cli.js"),
  ].filter((value): value is string => Boolean(value));
  const npmCli = npmCliCandidates.find((value) => existsSync(value));
  if (process.platform === "win32" && !npmCli) {
    throw new Error("portable runtime install requires npm-cli.js beside Node.js");
  }
  const executable = npmCli ? process.execPath : "npm";
  const invokeNpm = (args: string[], cwd: string) =>
    spawnSync(executable, npmCli ? [npmCli, ...args] : args, {
      cwd,
      encoding: "utf8",
      shell: false,
      timeout: NPM_COMMAND_TIMEOUT_MS,
    });
  let backedUp = false;
  try {
    const pack = invokeNpm([
      "pack",
      sourceRoot,
      "--silent",
      "--ignore-scripts",
      "--pack-destination",
      packRoot,
    ], sourceRoot);
    if (pack.status !== 0) {
      const detail = pack.error?.message ?? pack.stderr ?? pack.stdout ?? "unknown npm failure";
      throw new Error(`portable runtime pack failed: ${detail.trim()}`);
    }
    const tarball = readdirSync(packRoot).find((name) => name.endsWith(".tgz"));
    if (!tarball) throw new Error("portable runtime pack produced no tarball");
    const tarballPath = join(packRoot, tarball);
    const install = invokeNpm([
      "install",
      "--prefix",
      stageRoot,
      "--no-save",
      "--package-lock=false",
      "--ignore-scripts",
      "--no-audit",
      "--no-fund",
      "--offline",
      "--registry",
      process.env.PROVENA_NPM_REGISTRY ?? "https://registry.npmjs.org/",
      tarballPath,
    ], stageRoot);
    if (install.status !== 0) {
      const detail = install.error?.message ?? install.stderr ?? install.stdout ?? "unknown npm failure";
      throw new Error(`portable runtime install failed: ${detail.trim()}`);
    }
    const installedRoot = join(stageRoot, "node_modules", "@provena", "cli");
    if (lstatSync(installedRoot).isSymbolicLink()) {
      throw new Error("portable runtime package unexpectedly installed as a symlink");
    }
    if (packageVersion(installedRoot) !== version) {
      throw new Error("portable runtime package version mismatch");
    }
    writeAtomic(join(stageRoot, "runtime.mjs"), wanted);
    const runtimeTreeHash = runtimeTreeSha256(stageRoot);
    writeFileSync(
      join(stageRoot, "integrity.json"),
      `${JSON.stringify({
        schemaVersion: 2,
        packageVersion: version,
        packageTarballSha256: createHash("sha256")
          .update(readFileSync(tarballPath))
          .digest("hex"),
        runtimeTreeSha256: runtimeTreeHash,
      }, null, 2)}\n`,
      { encoding: "utf8", mode: 0o600 },
    );
    rmSync(packRoot, { recursive: true, force: true });
    if (existsSync(runtimeRoot)) {
      renameSync(runtimeRoot, backupRoot);
      backedUp = true;
    }
    renameSync(stageRoot, runtimeRoot);
    if (backedUp) rmSync(backupRoot, { recursive: true, force: true });
  } finally {
    rmSync(packRoot, { recursive: true, force: true });
    rmSync(stageRoot, { recursive: true, force: true });
    if (backedUp && !existsSync(runtimeRoot) && existsSync(backupRoot)) {
      renameSync(backupRoot, runtimeRoot);
    } else {
      rmSync(backupRoot, { recursive: true, force: true });
    }
  }
  return { runtimeRoot, runnerPath, installed: true, reused: false };
}
