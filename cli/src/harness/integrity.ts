import {
  MEMORY_KINDS,
  MEMORY_SUBJECT_TYPES,
  type RepoBrainManifest,
  type RepoMap,
} from "../brain/types.js";
import {
  assertMemoryLedgerSnapshotAttestation,
  MEMORY_LEDGER_PATH,
  type MemoryLedgerSnapshot,
} from "../brain/events.js";
import {
  MAX_STORED_ARTIFACT_BYTES,
  MAX_STORED_GENERATION_BYTES,
  REPO_BRAIN_MANAGED_ARTIFACT_PATHS,
} from "../brain/artifacts.js";
import {
  assertMaintenancePlanAttestation,
  canonicalMaintenancePlan,
  compileMaintenancePlan,
  MAINTENANCE_PLAN_PATH,
  MAX_MAINTENANCE_PLAN_BYTES,
} from "../maintenance/plan.js";
import type { MaintenancePlan } from "../maintenance/types.js";
import { repoMapSourceFingerprint } from "../brain/detect.js";
import { canonicalJson, compareText, sha256 } from "../brain/utils.js";
import {
  buildRepoGraph,
  memoryGraphNodeId,
  repoGraphProjectionFingerprint,
  type MemoryGraphNodeMetadata,
  type RepoGraph,
  type RepoGraphEdge,
  type RepoGraphNode,
} from "../graph/index.js";

export interface ArtifactIntegrityCheck {
  name: string;
  passed: boolean;
  detail: string;
}

export interface ArtifactIntegrityReport {
  passed: boolean;
  checks: ArtifactIntegrityCheck[];
}

const HASH_PATTERN = /^[a-f0-9]{64}$/;
const MEMORY_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$/;
const NODE_TYPES = new Set([
  "repository",
  "directory",
  "file",
  "symbol",
  "package",
  "command",
  "environment",
  "dependency",
  "memory",
]);
const EDGE_TYPES = new Set([
  "contains",
  "defines",
  "declares",
  "depends_on",
  "uses",
  "imports",
  "supersedes",
  "cites",
  "applies_to",
]);
const MEMORY_METADATA_KEYS = [
  "authority",
  "confidence",
  "declaredStatus",
  "eventId",
  "importance",
  "kind",
  "sensitivity",
  "subjectType",
  "title",
  "validFrom",
  "validTo",
].sort(compareText);

function object(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function finiteUnit(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function canonicalTimestamp(value: unknown): value is string {
  if (
    typeof value !== "string" ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(value)
  ) {
    return false;
  }
  const millis = Date.parse(value);
  return Number.isFinite(millis) && new Date(millis).toISOString() === value;
}

function result(
  name: string,
  passed: boolean,
  success: string,
  failure: string,
): ArtifactIntegrityCheck {
  return { name, passed, detail: passed ? success : failure };
}

function report(checks: ArtifactIntegrityCheck[]): ArtifactIntegrityReport {
  return { passed: checks.every((check) => check.passed), checks };
}

function scalarMetadata(value: unknown): value is Record<string, string | number | boolean | null> {
  return object(value) && Object.values(value).every((item) =>
    item === null ||
    typeof item === "string" ||
    typeof item === "boolean" ||
    (typeof item === "number" && Number.isFinite(item))
  );
}

function graphNode(value: unknown): value is RepoGraphNode {
  if (!object(value)) return false;
  return (
    typeof value.id === "string" &&
    typeof value.type === "string" &&
    NODE_TYPES.has(value.type) &&
    typeof value.label === "string" &&
    (value.path === undefined || typeof value.path === "string") &&
    scalarMetadata(value.metadata)
  );
}

function graphEdge(value: unknown): value is RepoGraphEdge {
  if (!object(value)) return false;
  return (
    typeof value.id === "string" &&
    typeof value.from === "string" &&
    typeof value.to === "string" &&
    typeof value.type === "string" &&
    EDGE_TYPES.has(value.type) &&
    typeof value.weight === "number" &&
    Number.isFinite(value.weight) &&
    value.weight >= 0 &&
    (value.effectiveAt === undefined || typeof value.effectiveAt === "string")
  );
}

function validRepoMap(value: unknown): value is RepoMap {
  if (!object(value) || value.schemaVersion !== 1 || !object(value.repository)) return false;
  return (
    typeof value.repository.name === "string" &&
    (value.repository.description === null || typeof value.repository.description === "string") &&
    typeof value.sourceFingerprint === "string" &&
    HASH_PATTERN.test(value.sourceFingerprint) &&
    object(value.scan) &&
    typeof value.scan.complete === "boolean" &&
    Array.isArray(value.scan.warnings) &&
    Array.isArray(value.languages) &&
    Array.isArray(value.directories) &&
    Array.isArray(value.files) &&
    Array.isArray(value.symbols) &&
    Array.isArray(value.packages) &&
    Array.isArray(value.commands) &&
    Array.isArray(value.environmentVariables)
  );
}

function validRepoGraph(value: unknown): value is RepoGraph {
  if (
    !object(value) ||
    ![1, 2].includes(value.schemaVersion as number) ||
    typeof value.sourceFingerprint !== "string" ||
    !HASH_PATTERN.test(value.sourceFingerprint) ||
    !Array.isArray(value.nodes) ||
    !value.nodes.every(graphNode) ||
    !Array.isArray(value.edges) ||
    !value.edges.every(graphEdge)
  ) {
    return false;
  }
  return value.schemaVersion === 1 || (
    typeof value.memoryFingerprint === "string" &&
    HASH_PATTERN.test(value.memoryFingerprint) &&
    typeof value.projectionFingerprint === "string" &&
    HASH_PATTERN.test(value.projectionFingerprint) &&
    value.timeSemantics === "event-effective-time"
  );
}

function validManifest(value: unknown): value is RepoBrainManifest {
  if (
    !object(value) ||
    value.schemaVersion !== 1 ||
    typeof value.sourceFingerprint !== "string" ||
    !HASH_PATTERN.test(value.sourceFingerprint) ||
    typeof value.memoryFingerprint !== "string" ||
    !HASH_PATTERN.test(value.memoryFingerprint) ||
    !Array.isArray(value.artifacts)
  ) {
    return false;
  }
  let totalBytes = 0;
  for (const artifact of value.artifacts) {
    const maxBytes = object(artifact) && artifact.path === MAINTENANCE_PLAN_PATH
      ? MAX_MAINTENANCE_PLAN_BYTES
      : MAX_STORED_ARTIFACT_BYTES;
    if (
      !object(artifact) ||
      typeof artifact.path !== "string" ||
      typeof artifact.sha256 !== "string" ||
      !HASH_PATTERN.test(artifact.sha256) ||
      !Number.isSafeInteger(artifact.bytes) ||
      (artifact.bytes as number) < 0 ||
      (artifact.bytes as number) > maxBytes
    ) {
      return false;
    }
    totalBytes += artifact.bytes as number;
    if (
      !Number.isSafeInteger(totalBytes) ||
      totalBytes > MAX_STORED_GENERATION_BYTES
    ) {
      return false;
    }
  }
  return true;
}

function validMemorySnapshot(value: unknown): value is MemoryLedgerSnapshot {
  return object(value) &&
    Array.isArray(value.events) &&
    typeof value.rawLedger === "string" &&
    typeof value.memoryFingerprint === "string" &&
    HASH_PATTERN.test(value.memoryFingerprint) &&
    Number.isSafeInteger(value.bytes) &&
    (value.bytes as number) >= 0 &&
    (value.bytes as number) <= MAX_STORED_ARTIFACT_BYTES;
}

function exactMemorySnapshot(snapshot: MemoryLedgerSnapshot): boolean {
  try {
    assertMemoryLedgerSnapshotAttestation(snapshot);
    return true;
  } catch {
    return false;
  }
}

function exactManifestArtifactSet(manifest: RepoBrainManifest): boolean {
  const expected = [...REPO_BRAIN_MANAGED_ARTIFACT_PATHS].sort(compareText);
  const actual = manifest.artifacts.map((artifact) => artifact.path).sort(compareText);
  return actual.length === expected.length &&
    new Set(actual).size === actual.length &&
    actual.every((path, index) => path === expected[index]);
}

function validMemoryNode(node: RepoGraphNode): boolean {
  if (node.type !== "memory") return true;
  const keys = Object.keys(node.metadata).sort(compareText);
  if (keys.length !== MEMORY_METADATA_KEYS.length) return false;
  if (keys.some((key, index) => key !== MEMORY_METADATA_KEYS[index])) return false;
  const metadata = node.metadata as unknown as MemoryGraphNodeMetadata;
  if (
    typeof metadata.eventId !== "string" ||
    !MEMORY_ID_PATTERN.test(metadata.eventId) ||
    node.id !== memoryGraphNodeId(metadata.eventId) ||
    typeof metadata.title !== "string" ||
    !metadata.title ||
    metadata.title.length > 512 ||
    !MEMORY_KINDS.includes(metadata.kind) ||
    !MEMORY_SUBJECT_TYPES.includes(metadata.subjectType) ||
    !["active", "superseded", "retracted"].includes(metadata.declaredStatus) ||
    !["human", "agent", "tool", "system"].includes(metadata.authority) ||
    !["public", "internal", "confidential", "restricted"].includes(metadata.sensitivity) ||
    !finiteUnit(metadata.confidence) ||
    !finiteUnit(metadata.importance) ||
    !canonicalTimestamp(metadata.validFrom) ||
    !(metadata.validTo === null || canonicalTimestamp(metadata.validTo))
  ) {
    return false;
  }
  if (metadata.validTo !== null && metadata.validTo < metadata.validFrom) return false;
  return metadata.declaredStatus === "active"
    ? metadata.validTo === null || metadata.validTo >= metadata.validFrom
    : metadata.validTo === metadata.validFrom;
}

/** Pure, non-repairing validation for parsed stored artifacts and exact ledger bytes. */
export function inspectRepoBrainArtifactIntegrity(
  mapValue: unknown,
  graphValue: unknown,
  manifestValue: unknown,
  memoryValue: unknown,
  maintenancePlanValue?: unknown,
): ArtifactIntegrityReport {
  const checks: ArtifactIntegrityCheck[] = [];
  try {
    const shapesValid = validRepoMap(mapValue) &&
      validRepoGraph(graphValue) &&
      validManifest(manifestValue) &&
      validMemorySnapshot(memoryValue);
    checks.push(result(
      "stored-artifact-shapes",
      shapesValid,
      "stored map, graph, manifest, and memory snapshot shapes are valid",
      "stored map, graph, manifest, or memory snapshot has an invalid shape",
    ));
    if (!shapesValid) return report(checks);

    const map = mapValue;
    const graph = graphValue;
    const manifest = manifestValue;
    const memory = memoryValue;
    let maintenancePlan: MaintenancePlan | null = null;
    try {
      const candidate = maintenancePlanValue ?? compileMaintenancePlan(map, memory);
      assertMaintenancePlanAttestation(candidate, map, memory);
      maintenancePlan = candidate;
    } catch {
      maintenancePlan = null;
    }
    const planEntry = manifest.artifacts.find(
      (artifact) => artifact.path === MAINTENANCE_PLAN_PATH,
    );
    const planText = maintenancePlan ? canonicalMaintenancePlan(maintenancePlan) : "";
    checks.push(result(
      "stored-maintenance-plan",
      maintenancePlan !== null &&
        Buffer.byteLength(planText, "utf8") <= MAX_MAINTENANCE_PLAN_BYTES &&
        planEntry?.sha256 === sha256(planText) &&
        planEntry.bytes === Buffer.byteLength(planText, "utf8"),
      "maintenance plan is canonical, bounded, and attested to the stored generation",
      "maintenance plan is malformed, oversized, noncanonical, or unattested",
    ));
    checks.push(result(
      "stored-manifest-artifact-set",
      exactManifestArtifactSet(manifest),
      "manifest contains the exact unique managed artifact set",
      "manifest does not contain the exact unique managed artifact set",
    ));
    const ledgerBytes = Buffer.from(memory.rawLedger, "utf8");
    checks.push(result(
      "stored-memory-snapshot",
      exactMemorySnapshot(memory) &&
        memory.bytes === ledgerBytes.byteLength &&
        memory.memoryFingerprint === sha256(ledgerBytes),
      "memory snapshot byte count and fingerprint match its exact ledger bytes",
      "memory snapshot byte count or fingerprint does not match its exact ledger bytes",
    ));

    const ledgerEntries = manifest.artifacts.filter(
      (artifact) => artifact.path === MEMORY_LEDGER_PATH,
    );
    checks.push(result(
      "stored-ledger-attestation",
      ledgerEntries.length === 1 &&
        manifest.memoryFingerprint === memory.memoryFingerprint &&
        ledgerEntries[0]!.sha256 === memory.memoryFingerprint &&
        ledgerEntries[0]!.bytes === memory.bytes,
      "manifest and ledger artifact attest the exact memory snapshot",
      "manifest or ledger artifact does not attest the exact memory snapshot",
    ));

    checks.push(result(
      "stored-source-fingerprint",
      map.sourceFingerprint === repoMapSourceFingerprint(map) &&
        graph.sourceFingerprint === map.sourceFingerprint &&
        manifest.sourceFingerprint === map.sourceFingerprint,
      "map, graph, and manifest source fingerprints agree",
      "stored map, graph, and manifest source fingerprints do not agree",
    ));

    const nodeIds = new Set(graph.nodes.map((node) => node.id));
    checks.push(result(
      "stored-graph-identities",
      nodeIds.size === graph.nodes.length &&
        new Set(graph.edges.map((edge) => edge.id)).size === graph.edges.length,
      "stored graph node and edge identities are unique",
      "stored graph contains duplicate node or edge identities",
    ));
    checks.push(result(
      "stored-graph-references",
      graph.edges.every((edge) => nodeIds.has(edge.from) && nodeIds.has(edge.to)),
      "stored graph edges resolve",
      "stored graph contains a dangling edge endpoint",
    ));

    if (graph.schemaVersion === 1) {
      let canonicalCodeGraph = false;
      try {
        const expected = buildRepoGraph(map, []);
        canonicalCodeGraph = canonicalJson(graph.nodes) === canonicalJson(expected.nodes) &&
          canonicalJson(graph.edges) === canonicalJson(expected.edges);
      } catch {
        canonicalCodeGraph = false;
      }
      checks.push(result(
        "stored-temporal-schema",
        graph.nodes.every((node) => node.type !== "memory") &&
          graph.edges.every((edge) =>
            !["supersedes", "cites", "applies_to"].includes(edge.type)),
        "legacy graph is a code-only schema-v1 artifact",
        "legacy graph contains unsupported temporal records",
      ));
      checks.push(result(
        "stored-canonical-graph",
        canonicalCodeGraph,
        "legacy graph preserves the canonical code-only projection",
        "legacy graph is not the canonical code-only projection",
      ));
      return report(checks);
    }

    checks.push(result(
      "stored-memory-fingerprint",
      graph.memoryFingerprint === manifest.memoryFingerprint &&
        graph.memoryFingerprint === memory.memoryFingerprint,
      "graph, manifest, and exact ledger memory fingerprints agree",
      "stored graph, manifest, and exact ledger memory fingerprints do not agree",
    ));
    checks.push(result(
      "stored-projection-fingerprint",
      graph.projectionFingerprint === repoGraphProjectionFingerprint(
        graph.sourceFingerprint,
        graph.memoryFingerprint,
      ),
      "graph projection fingerprint and time semantics are valid",
      "stored graph projection fingerprint or time semantics is invalid",
    ));
    checks.push(result(
      "stored-temporal-nodes",
      graph.nodes.every(validMemoryNode),
      "stored temporal memory metadata and endpoints are canonical",
      "stored graph contains malformed temporal memory metadata or endpoints",
    ));
    const nodesById = new Map(graph.nodes.map((node) => [node.id, node]));
    checks.push(result(
      "stored-temporal-edges",
      graph.edges.every((edge) => {
        const source = nodesById.get(edge.from);
        if (source?.type !== "memory") return edge.effectiveAt === undefined;
        const metadata = source.metadata as unknown as MemoryGraphNodeMetadata;
        return canonicalTimestamp(edge.effectiveAt) && edge.effectiveAt === metadata.validFrom;
      }),
      "stored temporal edge timestamps match their memory events",
      "stored graph contains a malformed temporal edge timestamp",
    ));

    let canonicalGraph = false;
    try {
      canonicalGraph = canonicalJson(graph) === canonicalJson(buildRepoGraph(map, memory));
    } catch {
      canonicalGraph = false;
    }
    checks.push(result(
      "stored-canonical-graph",
      canonicalGraph,
      "stored graph is the canonical projection of the map and exact ledger snapshot",
      "stored graph is not the canonical projection of the map and exact ledger snapshot",
    ));
    return report(checks);
  } catch {
    checks.push(result(
      "stored-integrity-evaluation",
      false,
      "stored artifact integrity evaluation completed",
      "stored artifact integrity evaluation failed safely",
    ));
    return report(checks);
  }
}
