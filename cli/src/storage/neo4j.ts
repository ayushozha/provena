import neo4j, { type Driver } from "neo4j-driver";
import {
  REPO_GRAPH_PROJECTION_NAMESPACE,
  REPO_GRAPH_PROJECTION_VERSION,
  repoGraphProjectionFingerprint,
} from "../graph/index.js";
import type { RepoGraph, RepoGraphNode } from "../graph/types.js";
import { assertNoSecretMaterial } from "../security/memory.js";
import { sha256 } from "../brain/utils.js";

export interface Neo4jConfig {
  uri: string;
  username: string;
  password: string;
  database?: string;
}

export interface Neo4jSyncResult {
  repoId: string;
  sourceFingerprint: string;
  memoryFingerprint: string;
  projectionFingerprint: string;
  nodes: number;
  edges: number;
}

/** Graph v1 had no memory projection; its deterministic compatibility value is the empty-ledger hash. */
const LEGACY_MEMORY_FINGERPRINT = sha256("");

const ALLOWED_SCHEMES = new Set([
  "bolt:",
  "bolt+s:",
  "bolt+ssc:",
  "neo4j:",
  "neo4j+s:",
  "neo4j+ssc:",
]);
const ENCRYPTED_SCHEMES = new Set(["bolt+s:", "bolt+ssc:", "neo4j+s:", "neo4j+ssc:"]);

function isLoopbackHost(hostname: string): boolean {
  const value = hostname.toLowerCase().replace(/^\[|\]$/g, "");
  return value === "localhost" || value === "127.0.0.1" || value === "::1";
}

function validateNeo4jUri(uri: string, allowInsecure = false): URL {
  let parsed: URL;
  try {
    parsed = new URL(uri);
  } catch {
    throw new Error("PROVENA_NEO4J_URI must be a valid Neo4j or Bolt URI");
  }
  if (!ALLOWED_SCHEMES.has(parsed.protocol)) {
    throw new Error(
      "PROVENA_NEO4J_URI must use neo4j, neo4j+s, bolt, or a +ssc variant",
    );
  }
  if (parsed.username || parsed.password) {
    throw new Error(
      "PROVENA_NEO4J_URI must not contain credentials; use PROVENA_NEO4J_USERNAME and PROVENA_NEO4J_PASSWORD",
    );
  }
  if (
    !ENCRYPTED_SCHEMES.has(parsed.protocol) &&
    !isLoopbackHost(parsed.hostname) &&
    !allowInsecure
  ) {
    throw new Error(
      "remote Neo4j connections require an encrypted +s or +ssc URI; set PROVENA_NEO4J_ALLOW_INSECURE=1 only for an explicitly accepted private-network risk",
    );
  }
  return parsed;
}

export function neo4jRepositoryId(tenantId: string, repositoryId: string): string {
  const tenant = tenantId.trim();
  const repository = repositoryId.trim();
  if (!tenant || !repository) throw new Error("tenantId and repositoryId must not be empty");
  // JSON tuple encoding is injective even when either identifier contains the
  // delimiter characters allowed by the config schema.
  return `v1:${JSON.stringify([tenant, repository])}`;
}

function requiredEnv(
  env: NodeJS.ProcessEnv,
  name: string,
): string {
  const value = env[name]?.trim();
  if (!value) throw new Error(`${name} is required for Neo4j sync`);
  return value;
}

export function neo4jConfigFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): Neo4jConfig {
  const uri = requiredEnv(env, "PROVENA_NEO4J_URI");
  validateNeo4jUri(uri, env.PROVENA_NEO4J_ALLOW_INSECURE === "1");
  return {
    uri,
    username: requiredEnv(env, "PROVENA_NEO4J_USERNAME"),
    password: requiredEnv(env, "PROVENA_NEO4J_PASSWORD"),
    ...(env.PROVENA_NEO4J_DATABASE?.trim()
      ? { database: env.PROVENA_NEO4J_DATABASE.trim() }
      : {}),
  };
}

function batches<T>(values: T[], size = 500): T[][] {
  const result: T[][] = [];
  for (let index = 0; index < values.length; index += size) {
    result.push(values.slice(index, index + size));
  }
  return result;
}

const MEMORY_METADATA_KEYS = [
  "eventId",
  "title",
  "kind",
  "subjectType",
  "declaredStatus",
  "authority",
  "confidence",
  "importance",
  "sensitivity",
  "validFrom",
  "validTo",
] as const;
const EMPTY_MEMORY_PROPERTIES = Object.fromEntries(
  MEMORY_METADATA_KEYS.map((key) => [key, null]),
);

function memoryProperties(node: RepoGraphNode): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const key of MEMORY_METADATA_KEYS) {
    const value = node.metadata[key];
    const valid = key === "validTo"
      ? value === null || typeof value === "string"
      : key === "confidence" || key === "importance"
        ? typeof value === "number" && Number.isFinite(value)
        : typeof value === "string";
    if (!valid) {
      throw new Error("memory graph metadata contains an unsupported value");
    }
    result[key] = value;
  }
  return result;
}

function nodeRecord(node: RepoGraphNode): Record<string, unknown> {
  const memory = node.type === "memory"
    ? memoryProperties(node)
    : EMPTY_MEMORY_PROPERTIES;
  return {
    id: node.id,
    type: node.type,
    label: node.label,
    path: node.path ?? null,
    metadataJson: node.type === "memory" ? null : JSON.stringify(node.metadata),
    memory,
  };
}

function graphMemoryFingerprint(graph: RepoGraph): string {
  return graph.schemaVersion === 2
    ? graph.memoryFingerprint
    : LEGACY_MEMORY_FINGERPRINT;
}

interface CypherRunner {
  run(query: string, parameters?: Record<string, unknown>): Promise<unknown>;
}

interface ProjectionData {
  nodes: Array<Record<string, unknown>>;
  edges: Array<Record<string, unknown>>;
}

function projectionData(graph: RepoGraph): ProjectionData {
  return {
    nodes: graph.nodes.map(nodeRecord),
    edges: graph.edges.map((edge) => ({
      id: edge.id,
      from: edge.from,
      to: edge.to,
      type: edge.type,
      weight: edge.weight,
      effectiveAt: edge.effectiveAt ?? null,
    })),
  };
}

async function ensureSchema(session: CypherRunner): Promise<void> {
  await session.run(
    "CREATE CONSTRAINT provena_node_identity IF NOT EXISTS " +
      "FOR (node:ProvenaNode) REQUIRE (node.repo_id, node.id) IS UNIQUE",
  );
  await session.run(
    "CREATE INDEX provena_node_path IF NOT EXISTS " +
      "FOR (node:ProvenaNode) ON (node.repo_id, node.path)",
  );
}

async function projectGraph(
  session: CypherRunner,
  graph: RepoGraph,
  repoId: string,
  memoryFingerprint: string,
  projectionFingerprint: string,
  data: ProjectionData,
): Promise<void> {
  const projection = {
    repoId,
    sourceFingerprint: graph.sourceFingerprint,
    memoryFingerprint,
    projectionFingerprint,
    projectionNamespace: REPO_GRAPH_PROJECTION_NAMESPACE,
    projectionVersion: REPO_GRAPH_PROJECTION_VERSION,
  };
  for (const batch of batches(data.nodes)) {
    await session.run(
      `UNWIND $nodes AS item
       MERGE (node:ProvenaNode {repo_id: $repoId, id: item.id})
       SET node.type = item.type,
           node.label = item.label,
           node.path = item.path,
           node.metadata_json = item.metadataJson,
           node.event_id = item.memory.eventId,
           node.title = item.memory.title,
           node.kind = item.memory.kind,
           node.subject_type = item.memory.subjectType,
           node.declared_status = item.memory.declaredStatus,
           node.authority = item.memory.authority,
           node.confidence = item.memory.confidence,
           node.importance = item.memory.importance,
           node.sensitivity = item.memory.sensitivity,
           node.valid_from = item.memory.validFrom,
           node.valid_to = item.memory.validTo,
           node.source_fingerprint = $sourceFingerprint,
           node.memory_fingerprint = $memoryFingerprint,
           node.projection_fingerprint = $projectionFingerprint,
           node.projection_namespace = $projectionNamespace,
           node.projection_version = $projectionVersion`,
      { nodes: batch, ...projection },
    );
  }
  for (const batch of batches(data.edges)) {
    await session.run(
      `UNWIND $edges AS item
       MATCH (source:ProvenaNode {repo_id: $repoId, id: item.from})
       MATCH (target:ProvenaNode {repo_id: $repoId, id: item.to})
       MERGE (source)-[edge:PROVENA_RELATION {repo_id: $repoId, id: item.id}]->(target)
       SET edge.type = item.type,
           edge.weight = item.weight,
           edge.effective_at = item.effectiveAt,
           edge.source_fingerprint = $sourceFingerprint,
           edge.memory_fingerprint = $memoryFingerprint,
           edge.projection_fingerprint = $projectionFingerprint,
           edge.projection_namespace = $projectionNamespace,
           edge.projection_version = $projectionVersion`,
      { edges: batch, ...projection },
    );
  }
  await session.run(
    `MATCH (:ProvenaNode {repo_id: $repoId})-[edge:PROVENA_RELATION {repo_id: $repoId}]->()
     WHERE edge.projection_fingerprint IS NULL OR edge.projection_fingerprint <> $projectionFingerprint
     DELETE edge`,
    projection,
  );
  await session.run(
    `MATCH (node:ProvenaNode {repo_id: $repoId})
     WHERE node.projection_fingerprint IS NULL OR node.projection_fingerprint <> $projectionFingerprint
     DETACH DELETE node`,
    projection,
  );
}

export async function syncGraphToNeo4j(
  graph: RepoGraph,
  repoId: string,
  config: Neo4jConfig = neo4jConfigFromEnv(),
  suppliedDriver?: Driver,
): Promise<Neo4jSyncResult> {
  const normalizedRepoId = repoId.trim();
  if (!normalizedRepoId) throw new Error("repoId must not be empty");
  validateNeo4jUri(config.uri, process.env.PROVENA_NEO4J_ALLOW_INSECURE === "1");
  const memoryFingerprint = graphMemoryFingerprint(graph);
  const projectionFingerprint = repoGraphProjectionFingerprint(
    graph.sourceFingerprint,
    memoryFingerprint,
  );
  if (
    graph.schemaVersion === 2 &&
    graph.projectionFingerprint !== projectionFingerprint
  ) {
    throw new Error("graph projection fingerprint does not match its attested inputs");
  }
  const data = projectionData(graph);
  assertNoSecretMaterial(JSON.stringify({
    repoId: normalizedRepoId,
    sourceFingerprint: graph.sourceFingerprint,
    memoryFingerprint,
    data,
  }));
  try {
    const driver =
      suppliedDriver ??
      neo4j.driver(config.uri, neo4j.auth.basic(config.username, config.password));
    try {
      await driver.verifyConnectivity();
      const session = driver.session(
        config.database ? { database: config.database } : undefined,
      );
      try {
        await ensureSchema(session);
        await session.executeWrite(async (transaction) => {
          await projectGraph(
            transaction,
            graph,
            normalizedRepoId,
            memoryFingerprint,
            projectionFingerprint,
            data,
          );
        });
      } finally {
        await session.close();
      }
    } finally {
      if (!suppliedDriver) await driver.close();
    }
  } catch {
    throw new Error("Neo4j graph sync failed");
  }
  return {
    repoId: normalizedRepoId,
    sourceFingerprint: graph.sourceFingerprint,
    memoryFingerprint,
    projectionFingerprint,
    nodes: graph.nodes.length,
    edges: graph.edges.length,
  };
}
