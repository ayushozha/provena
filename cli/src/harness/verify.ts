import { createHash } from "node:crypto";
import {
  closeSync,
  existsSync,
  fstatSync,
  openSync,
  readSync,
} from "node:fs";
import { isAbsolute, join } from "node:path";
import {
  readRepoBrainArtifacts,
  refreshRepoBrain,
  MAX_STORED_ARTIFACT_BYTES,
  MAX_STORED_GENERATION_BYTES,
  REPO_BRAIN_MANAGED_ARTIFACT_PATHS,
  REPO_BRAIN_PATH,
  REPO_MANIFEST_PATH,
  type RepoBrainManifest,
} from "../brain/index.js";
import { buildContextPacket } from "../context/index.js";
import { normalizeRepoPath } from "../brain/utils.js";
import { assertSafeRepoPath } from "../security/paths.js";
import { inspectRepoBrainArtifactIntegrity } from "./integrity.js";
import {
  MAINTENANCE_PLAN_PATH,
  MAX_MAINTENANCE_PLAN_BYTES,
} from "../maintenance/plan.js";

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

function validManifestArtifacts(
  repoRoot: string,
  manifest: RepoBrainManifest,
): boolean {
  try {
    if (!Array.isArray(manifest.artifacts)) return false;
    const expected = [...REPO_BRAIN_MANAGED_ARTIFACT_PATHS].sort();
    const paths = manifest.artifacts.map((artifact) => artifact.path).sort();
    if (
      paths.length !== expected.length ||
      new Set(paths).size !== paths.length ||
      paths.some((path, index) => path !== expected[index])
    ) {
      return false;
    }
    let declaredBytes = 0;
    const validDeclarations = manifest.artifacts.every((artifact) => {
      const maxBytes = artifact.path === MAINTENANCE_PLAN_PATH
        ? MAX_MAINTENANCE_PLAN_BYTES
        : MAX_STORED_ARTIFACT_BYTES;
      if (
        !Number.isSafeInteger(artifact.bytes) ||
        artifact.bytes < 0 ||
        artifact.bytes > maxBytes ||
        !/^[a-f0-9]{64}$/.test(artifact.sha256)
      ) {
        return false;
      }
      declaredBytes += artifact.bytes;
      if (
        !Number.isSafeInteger(declaredBytes) ||
        declaredBytes > MAX_STORED_GENERATION_BYTES
      ) {
        return false;
      }
      const content = readCappedArtifact(repoRoot, artifact.path);
      return content.byteLength === artifact.bytes && sha256(content) === artifact.sha256;
    });
    return validDeclarations;
  } catch {
    return false;
  }
}

/** Stat before reading so a hostile checked-in artifact cannot bypass the cap. */
function readCappedArtifact(repoRoot: string, artifactPath: string): Buffer {
  const relativePath = normalizeRepoPath(repoRoot, artifactPath);
  if (relativePath === ".") throw new Error("managed artifact path must identify a file");
  const path = join(repoRoot, ...relativePath.split("/"));
  assertSafeRepoPath(repoRoot, path);
  const handle = openSync(path, "r");
  try {
    const before = fstatSync(handle);
    const maxBytes = artifactPath === MAINTENANCE_PLAN_PATH
      ? MAX_MAINTENANCE_PLAN_BYTES
      : MAX_STORED_ARTIFACT_BYTES;
    if (!before.isFile() || before.size > maxBytes) {
      throw new Error("managed artifact exceeds the safety cap or is not a file");
    }
    const content = Buffer.alloc(before.size);
    let offset = 0;
    while (offset < content.byteLength) {
      const bytesRead = readSync(
        handle,
        content,
        offset,
        content.byteLength - offset,
        offset,
      );
      if (bytesRead === 0) {
        throw new Error("managed artifact changed while it was being inspected");
      }
      offset += bytesRead;
    }
    const extra = Buffer.alloc(1);
    if (readSync(handle, extra, 0, 1, content.byteLength) !== 0) {
      throw new Error("managed artifact changed while it was being inspected");
    }
    const after = fstatSync(handle);
    if (after.size !== before.size) {
      throw new Error("managed artifact changed while it was being inspected");
    }
    return content;
  } finally {
    closeSync(handle);
  }
}

/** Reject an oversized multi-file generation before allocating artifact buffers. */
function assertStoredGenerationBudget(repoRoot: string): void {
  let totalBytes = 0;
  for (const artifactPath of [
    ...REPO_BRAIN_MANAGED_ARTIFACT_PATHS,
    REPO_MANIFEST_PATH,
  ]) {
    const relativePath = normalizeRepoPath(repoRoot, artifactPath);
    const path = join(repoRoot, ...relativePath.split("/"));
    assertSafeRepoPath(repoRoot, path);
    const handle = openSync(path, "r");
    try {
      const info = fstatSync(handle);
      const maxBytes = artifactPath === MAINTENANCE_PLAN_PATH
        ? MAX_MAINTENANCE_PLAN_BYTES
        : MAX_STORED_ARTIFACT_BYTES;
      if (!info.isFile() || info.size > maxBytes) {
        throw new Error("managed artifact exceeds the safety cap or is not a file");
      }
      totalBytes += info.size;
      if (
        !Number.isSafeInteger(totalBytes) ||
        totalBytes > MAX_STORED_GENERATION_BYTES
      ) {
        throw new Error("managed artifact generation exceeds the safety cap");
      }
    } finally {
      closeSync(handle);
    }
  }
}

function failedStoredReport(): HarnessReport {
  return {
    schemaVersion: 1,
    passed: false,
    sourceFingerprint: "",
    checks: [{
      name: "stored-artifact-parse",
      status: "fail",
      detail: "stored repo brain artifacts are missing, oversized, malformed, or inconsistent",
    }],
  };
}

/** Inspect the committed generation without refreshing or repairing it first. */
export async function verifyStoredRepoMemory(repoRoot: string): Promise<HarnessReport> {
  const checks: HarnessCheck[] = [];
  try {
    assertStoredGenerationBudget(repoRoot);
    for (const path of [...REPO_BRAIN_MANAGED_ARTIFACT_PATHS, REPO_MANIFEST_PATH]) {
      readCappedArtifact(repoRoot, path);
    }
    const { map, graph, maintenancePlan, manifest, memory } =
      await readRepoBrainArtifacts(repoRoot);
    const integrity = inspectRepoBrainArtifactIntegrity(
      map,
      graph,
      manifest,
      memory,
      maintenancePlan,
    );
    checks.push(...integrity.checks.map((item) => ({
      name: item.name,
      status: item.passed ? "pass" as const : "fail" as const,
      detail: item.detail,
    })));
    check(
      checks,
      "stored-manifest-integrity",
      validManifestArtifacts(repoRoot, manifest),
      "the exact managed artifact set has valid hashes and byte counts before refresh",
      "the managed artifact set, hash, or byte count is invalid before refresh",
    );
    return {
      schemaVersion: 1,
      passed: checks.every((item) => item.status !== "fail"),
      sourceFingerprint: map.sourceFingerprint,
      checks,
    };
  } catch {
    return failedStoredReport();
  }
}

export async function verifyRepoMemory(repoRoot: string): Promise<HarnessReport> {
  const stored = await verifyStoredRepoMemory(repoRoot);
  if (!stored.passed) return stored;
  const first = await refreshRepoBrain(repoRoot);
  const second = await refreshRepoBrain(repoRoot);
  const checks: HarnessCheck[] = [...stored.checks];
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
  const validArtifacts = validManifestArtifacts(repoRoot, manifest);
  check(checks, "manifest-integrity", validArtifacts, "all artifact hashes and byte counts match", "manifest artifact hash mismatch");

  const brainBytes = readCappedArtifact(repoRoot, REPO_BRAIN_PATH).byteLength;
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
