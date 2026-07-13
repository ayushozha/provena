import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import {
  refreshRepoBrain,
  REPO_BRAIN_PATH,
  type RepoBrainManifest,
} from "../brain/index.js";
import { buildContextPacket } from "../context/index.js";
import { normalizeRepoPath } from "../brain/utils.js";
import { assertSafeRepoPath } from "../security/paths.js";

export interface HarnessCheck {
  name: string;
  status: "pass" | "warn" | "fail";
  detail: string;
}

export interface HarnessReport {
  schemaVersion: 1;
  passed: boolean;
  sourceFingerprint: string;
  checks: HarnessCheck[];
}

function sha256(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function check(
  checks: HarnessCheck[],
  name: string,
  condition: boolean,
  success: string,
  failure: string,
  failureStatus: HarnessCheck["status"] = "fail",
): void {
  checks.push({
    name,
    status: condition ? "pass" : failureStatus,
    detail: condition ? success : failure,
  });
}

export async function verifyRepoMemory(repoRoot: string): Promise<HarnessReport> {
  const first = await refreshRepoBrain(repoRoot);
  const second = await refreshRepoBrain(repoRoot);
  const checks: HarnessCheck[] = [];
  check(
    checks,
    "deterministic-refresh",
    second.written.length === 0,
    "a second refresh was byte-stable",
    `second refresh rewrote ${second.written.join(", ")}`,
  );

  const nodeIds = new Set(first.graph.nodes.map((node) => node.id));
  const uniqueNodes = nodeIds.size === first.graph.nodes.length;
  const uniqueEdges = new Set(first.graph.edges.map((edge) => edge.id)).size === first.graph.edges.length;
  const validEdges = first.graph.edges.every(
    (edge) => nodeIds.has(edge.from) && nodeIds.has(edge.to),
  );
  check(checks, "graph-node-identities", uniqueNodes, "all graph node IDs are unique", "duplicate graph node IDs found");
  check(checks, "graph-edge-identities", uniqueEdges, "all graph edge IDs are unique", "duplicate graph edge IDs found");
  check(checks, "graph-references", validEdges, "all graph edges resolve", "graph contains dangling edge endpoints");

  const relativePaths = [
    ...first.map.files.map((file) => file.path),
    ...first.map.symbols.map((symbol) => symbol.path),
    ...first.graph.nodes.flatMap((node) => (node.path ? [node.path] : [])),
  ];
  check(
    checks,
    "portable-paths",
    relativePaths.every((path) => !isAbsolute(path) && !path.replace(/\\/g, "/").startsWith("../")),
    "map and graph paths are repository-relative",
    "map or graph contains an absolute/escaping path",
  );

  const manifest: RepoBrainManifest = first.manifest;
  const validArtifacts = manifest.artifacts.every((artifact) => {
    try {
      const relativePath = normalizeRepoPath(repoRoot, artifact.path);
      if (relativePath === "." || artifact.bytes > 64 * 1024 * 1024) return false;
      const path = join(repoRoot, ...relativePath.split("/"));
      assertSafeRepoPath(repoRoot, path);
      if (!existsSync(path)) return false;
      const content = readFileSync(path);
      return content.byteLength === artifact.bytes && sha256(content) === artifact.sha256;
    } catch {
      return false;
    }
  });
  check(checks, "manifest-integrity", validArtifacts, "all artifact hashes and byte counts match", "manifest artifact hash mismatch");

  const brainBytes = Buffer.byteLength(
    readFileSync(join(repoRoot, ...REPO_BRAIN_PATH.split("/")), "utf8"),
    "utf8",
  );
  check(
    checks,
    "bootloader-budget",
    brainBytes <= 12_000,
    `repo brain is ${brainBytes} bytes`,
    `repo brain is ${brainBytes} bytes (12,000 byte limit)`,
  );

  const events = first.memory.events;
  const missingSources = events.flatMap((event) =>
    event.sources.filter((source) => !existsSync(join(repoRoot, ...source.path.split("/")))),
  );
  check(
    checks,
    "memory-source-availability",
    missingSources.length === 0,
    "all current memory source paths exist",
    `${missingSources.length} memory source paths no longer exist (historical citations may be valid)`,
    "warn",
  );

  const packet = buildContextPacket(first.map, first.graph, first.memory, {
    query: "",
    maxTokens: 512,
  });
  check(
    checks,
    "context-memory-fingerprint",
    packet.memoryFingerprint === first.manifest.memoryFingerprint,
    "context packet matches the manifest memory fingerprint",
    "context packet does not match the manifest memory fingerprint",
  );
  check(
    checks,
    "context-citations",
    packet.items.length > 0 && packet.items.every((item) => item.citations.length > 0),
    `${packet.items.length} context items all carry citations`,
    "context smoke packet was empty or uncited",
  );
  check(
    checks,
    "context-budget",
    packet.budget.estimatedTokens <= packet.budget.maxTokens,
    `context stayed within ${packet.budget.maxTokens} tokens`,
    "context exceeded its declared budget",
  );

  return {
    schemaVersion: 1,
    passed: checks.every((item) => item.status !== "fail"),
    sourceFingerprint: first.map.sourceFingerprint,
    checks,
  };
}
