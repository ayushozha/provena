import neo4j, { type Driver } from "neo4j-driver";
import type { RepoGraph, RepoGraphNode } from "../graph/types.js";

export interface Neo4jConfig {
  uri: string;
  username: string;
  password: string;
  database?: string;
}

export interface Neo4jSyncResult {
  repoId: string;
  sourceFingerprint: string;
  nodes: number;
  edges: number;
}

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

function nodeRecord(node: RepoGraphNode): Record<string, unknown> {
  return {
    id: node.id,
    type: node.type,
    label: node.label,
    path: node.path ?? null,
    metadataJson: JSON.stringify(node.metadata),
  };
}

interface CypherRunner {
  run(query: string, parameters?: Record<string, unknown>): Promise<unknown>;
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
): Promise<void> {
  for (const batch of batches(graph.nodes.map(nodeRecord))) {
    await session.run(
      `UNWIND $nodes AS item
       MERGE (node:ProvenaNode {repo_id: $repoId, id: item.id})
       SET node.type = item.type,
           node.label = item.label,
           node.path = item.path,
           node.metadata_json = item.metadataJson,
           node.source_fingerprint = $fingerprint`,
      { nodes: batch, repoId, fingerprint: graph.sourceFingerprint },
    );
  }
  for (const batch of batches(graph.edges)) {
    await session.run(
      `UNWIND $edges AS item
       MATCH (source:ProvenaNode {repo_id: $repoId, id: item.from})
       MATCH (target:ProvenaNode {repo_id: $repoId, id: item.to})
       MERGE (source)-[edge:PROVENA_RELATION {repo_id: $repoId, id: item.id}]->(target)
       SET edge.type = item.type,
           edge.weight = item.weight,
           edge.source_fingerprint = $fingerprint`,
      { edges: batch, repoId, fingerprint: graph.sourceFingerprint },
    );
  }
  await session.run(
    `MATCH (:ProvenaNode {repo_id: $repoId})-[edge:PROVENA_RELATION {repo_id: $repoId}]->()
     WHERE edge.source_fingerprint IS NULL OR edge.source_fingerprint <> $fingerprint
     DELETE edge`,
    { repoId, fingerprint: graph.sourceFingerprint },
  );
  await session.run(
    `MATCH (node:ProvenaNode {repo_id: $repoId})
     WHERE node.source_fingerprint IS NULL OR node.source_fingerprint <> $fingerprint
     DETACH DELETE node`,
    { repoId, fingerprint: graph.sourceFingerprint },
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
        await projectGraph(transaction, graph, normalizedRepoId);
      });
    } finally {
      await session.close();
    }
  } finally {
    if (!suppliedDriver) await driver.close();
  }
  return {
    repoId: normalizedRepoId,
    sourceFingerprint: graph.sourceFingerprint,
    nodes: graph.nodes.length,
    edges: graph.edges.length,
  };
}
