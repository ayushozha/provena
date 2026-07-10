import { existsSync, readFileSync, statSync } from "node:fs";
import { createHash } from "node:crypto";
import { join } from "node:path";
import { getGitRoot, configExists, loadConfig } from "../config.js";
import {
  activeMemoryEvents,
  readMemoryEvents,
  scanRepo,
  MEMORY_LEDGER_PATH,
  REPO_MANIFEST_PATH,
  type RepoBrainManifest,
} from "../brain/index.js";
import { readDaemonStatus } from "./daemon.js";
import { normalizeRepoPath } from "../brain/utils.js";
import { assertSafeRepoPath } from "../security/paths.js";
import { installedMcpClients } from "../integrations/mcp-config.js";

export async function runStatusCommand(
  args: string[],
  cwd = process.cwd(),
): Promise<number> {
  const repoRoot = getGitRoot(cwd);
  const manifestPath = join(repoRoot, ...REPO_MANIFEST_PATH.split("/"));
  assertSafeRepoPath(repoRoot, manifestPath);
  if (!existsSync(manifestPath)) {
    const message = { initialized: false, repoRoot, next: "provena init" };
    console.log(args.includes("--json") ? JSON.stringify(message, null, 2) : "Provena is not initialized. Run `provena init`.");
    return 1;
  }
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as RepoBrainManifest;
  if (manifest.schemaVersion !== 1 || !Array.isArray(manifest.artifacts)) {
    throw new Error("invalid .provena/manifest.json; run `provena refresh` to repair it");
  }
  const { config } = loadConfig(repoRoot);
  const current = await scanRepo(repoRoot, {
    includePatterns: config.index.include,
    excludePatterns: config.index.exclude,
  });
  const events = await readMemoryEvents(repoRoot);
  const digest = (value: string | Buffer) => createHash("sha256").update(value).digest("hex");
  const ledgerPath = join(repoRoot, ...MEMORY_LEDGER_PATH.split("/"));
  const memoryFingerprint = digest(existsSync(ledgerPath) ? readFileSync(ledgerPath) : "");
  const artifactHashesCurrent = manifest.artifacts.every((artifact) => {
    try {
      if (
        typeof artifact.path !== "string" ||
        typeof artifact.sha256 !== "string" ||
        !Number.isSafeInteger(artifact.bytes) ||
        artifact.bytes < 0 ||
        artifact.bytes > 64 * 1024 * 1024
      ) return false;
      const relativePath = normalizeRepoPath(repoRoot, artifact.path);
      if (relativePath === ".") return false;
      const artifactPath = join(repoRoot, ...relativePath.split("/"));
      assertSafeRepoPath(repoRoot, artifactPath);
      if (!existsSync(artifactPath)) return false;
      const content = readFileSync(artifactPath);
      return content.byteLength === artifact.bytes && digest(content) === artifact.sha256;
    } catch {
      return false;
    }
  });
  const daemonStatus = readDaemonStatus(repoRoot);
  const daemonPid = daemonStatus.pid;
  const status = {
    initialized: true,
    current:
      current.sourceFingerprint === manifest.sourceFingerprint &&
      memoryFingerprint === manifest.memoryFingerprint &&
      artifactHashesCurrent,
    sourceFingerprint: manifest.sourceFingerprint,
    currentFingerprint: current.sourceFingerprint,
    files: current.files.length,
    symbols: current.symbols.length,
    memories: {
      total: events.length,
      active: activeMemoryEvents(events).length,
      fingerprint: manifest.memoryFingerprint,
      currentFingerprint: memoryFingerprint,
    },
    refreshedAt: statSync(manifestPath).mtime.toISOString(),
    daemon: daemonPid ? { running: true, pid: daemonPid } : { running: false },
    integrations: {
      config: configExists(repoRoot),
      portableRuntime: existsSync(join(repoRoot, ".provena", "runtime", "runtime.mjs")),
      agents: existsSync(join(repoRoot, ".provena", "agent-instructions.md")),
      mcp: installedMcpClients(repoRoot).length > 0,
      neo4jConfigured: Boolean(
        process.env.PROVENA_NEO4J_URI &&
          process.env.PROVENA_NEO4J_USERNAME &&
          process.env.PROVENA_NEO4J_PASSWORD,
      ),
    },
  };
  if (args.includes("--json")) {
    console.log(JSON.stringify(status, null, 2));
  } else {
    console.log(`Provena brain: ${status.current ? "current" : "stale"}`);
    console.log(`  repo: ${status.files} files, ${status.symbols} symbols`);
    console.log(`  memory: ${status.memories.active}/${status.memories.total} active`);
    console.log(`  daemon: ${daemonPid ? `running (pid ${daemonPid})` : "stopped"}`);
    console.log(
      `  integrations: agents=${status.integrations.agents ? "yes" : "no"} ` +
        `mcp=${status.integrations.mcp ? "yes" : "no"} neo4j=${status.integrations.neo4jConfigured ? "configured" : "off"}`,
    );
  }
  return 0;
}
