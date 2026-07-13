import { getGitRoot, loadConfig } from "../config.js";
import { canonicalMemoryAsOf, refreshRepoBrain } from "../brain/index.js";
import {
  connectedComponents,
  degreeCentrality,
  induceRepoGraphAt,
  memoryTimeline,
  neighborhood,
  pageRank,
  shortestPath,
  type RepoGraph,
  type RepoGraphNode,
} from "../graph/index.js";
import { neo4jRepositoryId, syncGraphToNeo4j } from "../storage/index.js";
import { compareText } from "../brain/utils.js";

function hasFlag(args: string[], flag: string): boolean {
  return args.includes(flag);
}

function valueAfter(args: string[], flag: string): string | undefined {
  return args.flatMap((value, index) =>
    value === flag && args[index + 1] ? [args[index + 1]!] : [],
  ).at(-1);
}

const VALUE_FLAGS = new Set(["--hops", "--memory-as-of"]);

function positional(args: string[]): string[] {
  const result: string[] = [];
  for (let index = 0; index < args.length; index += 1) {
    const value = args[index]!;
    if (VALUE_FLAGS.has(value)) {
      index += 1;
      continue;
    }
    if (!value.startsWith("-")) result.push(value);
  }
  return result;
}

function resolveNode(graph: RepoGraph, value: string): RepoGraphNode {
  const query = value.trim().toLowerCase();
  const tiers = [
    graph.nodes.filter((node) => node.id.toLowerCase() === query),
    graph.nodes.filter((node) =>
      node.type === "memory" && typeof node.metadata.eventId === "string" &&
      node.metadata.eventId.toLowerCase() === query,
    ),
    graph.nodes.filter((node) => node.type !== "symbol" && node.path?.toLowerCase() === query),
    graph.nodes.filter((node) => node.label.toLowerCase() === query),
  ];
  for (const matches of tiers) {
    if (matches.length === 1) return matches[0]!;
    if (matches.length > 1) throw new Error("graph node is ambiguous");
  }
  throw new Error("graph node not found");
}

function queryMetadata(graph: RepoGraph, memoryAsOf?: string): Record<string, unknown> {
  return {
    sourceFingerprint: graph.sourceFingerprint,
    ...(graph.schemaVersion === 2
      ? {
          memoryFingerprint: graph.memoryFingerprint,
          projectionFingerprint: graph.projectionFingerprint,
          timeSemantics: graph.timeSemantics,
        }
      : {}),
    repositoryTopology: "current",
    ...(memoryAsOf ? { memoryAsOf } : {}),
  };
}

export function printGraphHelp(): void {
  console.log("Usage: provena graph [stats|neighbors|path|components|timeline|sync] [options]");
  console.log("");
  console.log("  stats                         Show graph size and central nodes");
  console.log("  neighbors <id|path>           Show a local graph neighborhood");
  console.log("  path <from> <to>              Find the shortest repo relationship path");
  console.log("  components                    List weakly connected components");
  console.log("  timeline <memory-id>          Show complete memory lineage");
  console.log("  sync neo4j                    Project the current graph into Neo4j");
  console.log("");
  console.log("Options: --hops N, --memory-as-of YYYY-MM-DDTHH:mm:ss.sssZ, --json");
  console.log("Temporal views use current repository topology, not a historical code snapshot.");
}

export async function runGraphCommand(
  args: string[],
  cwd = process.cwd(),
): Promise<number> {
  if (hasFlag(args, "--help") || hasFlag(args, "-h")) {
    printGraphHelp();
    return 0;
  }
  const repoRoot = getGitRoot(cwd);
  loadConfig(repoRoot);
  // Refresh is deterministic and makes graph queries reflect the current tree.
  const { graph: completeGraph } = await refreshRepoBrain(repoRoot);
  const operands = positional(args);
  const command = operands[0] ?? "stats";
  const json = hasFlag(args, "--json");
  const memoryAsOf = canonicalMemoryAsOf(
    hasFlag(args, "--memory-as-of") ? valueAfter(args, "--memory-as-of") ?? "" : undefined,
  );

  if (command === "sync") {
    if (operands[1] !== "neo4j") {
      throw new Error("graph sync currently supports the `neo4j` adapter");
    }
    const { config } = loadConfig(repoRoot);
    if (!config.repository_id) {
      throw new Error("Neo4j sync requires a stable repository_id; rerun `provena init --force` to upgrade this checkout");
    }
    const result = await syncGraphToNeo4j(
      completeGraph,
      neo4jRepositoryId(config.scope.tenant_id, config.repository_id),
    );
    console.log(
      json
        ? JSON.stringify(result, null, 2)
        : `Synced ${result.nodes} nodes and ${result.edges} edges to Neo4j for ${result.repoId}.`,
    );
    return 0;
  }

  if (command === "timeline") {
    if (!operands[1]) throw new Error("graph timeline requires a memory ID");
    console.log(JSON.stringify({
      ...queryMetadata(completeGraph),
      ...memoryTimeline(completeGraph, operands[1]),
    }, null, 2));
    return 0;
  }

  const graph = induceRepoGraphAt(completeGraph, memoryAsOf);
  const metadata = queryMetadata(graph, memoryAsOf);

  if (command === "neighbors") {
    const value = operands[1];
    if (!value) throw new Error("graph neighbors requires a node ID, path, or label");
    const node = resolveNode(graph, value);
    const rawHops = valueAfter(args, "--hops") ?? "1";
    const hops = Number(rawHops);
    if (!Number.isInteger(hops) || hops < 0 || hops > 8) {
      throw new Error("--hops must be an integer between 0 and 8");
    }
    const found = neighborhood(graph, node.id, hops);
    const result = {
      ...metadata,
      root: node,
      nodes: graph.nodes.filter((item) => found.nodeIds.includes(item.id)),
      edges: graph.edges.filter((edge) => found.edgeIds.includes(edge.id)),
    };
    console.log(JSON.stringify(result, null, 2));
    return 0;
  }

  if (command === "path") {
    if (!operands[1] || !operands[2]) throw new Error("graph path requires <from> and <to>");
    const from = resolveNode(graph, operands[1]);
    const to = resolveNode(graph, operands[2]);
    const path = shortestPath(graph, from.id, to.id);
    const nodes = path?.map((id) => graph.nodes.find((node) => node.id === id)) ?? [];
    console.log(
      json
        ? JSON.stringify({ ...metadata, from, to, path: nodes }, null, 2)
        : nodes.length
          ? nodes.map((node) => `${node?.type}:${node?.path ?? node?.label}`).join(" -> ")
          : "No graph path found.",
    );
    return nodes.length ? 0 : 1;
  }

  const components = connectedComponents(graph);
  if (command === "components") {
    const result = components.map((ids) => ({
      size: ids.length,
      nodes: ids.map((id) => graph.nodes.find((node) => node.id === id)),
    }));
    console.log(JSON.stringify({ ...metadata, components: result }, null, 2));
    return 0;
  }

  if (command !== "stats") {
    throw new Error("unknown graph subcommand");
  }
  const rank = pageRank(graph);
  const degree = degreeCentrality(graph);
  const top = graph.nodes
    .map((node) => ({
      id: node.id,
      type: node.type,
      label: node.label,
      path: node.path,
      pageRank: rank[node.id] ?? 0,
      degree: degree[node.id]?.total ?? 0,
    }))
    .sort((a, b) => b.pageRank - a.pageRank || b.degree - a.degree || compareText(a.id, b.id))
    .slice(0, 15);
  const stats = {
    ...metadata,
    nodes: graph.nodes.length,
    edges: graph.edges.length,
    components: components.length,
    nodeTypes: Object.fromEntries(
      [...new Set(graph.nodes.map((node) => node.type))]
        .sort()
        .map((type) => [type, graph.nodes.filter((node) => node.type === type).length]),
    ),
    top,
  };
  if (json) {
    console.log(JSON.stringify(stats, null, 2));
  } else {
    console.log(`Graph: ${stats.nodes} nodes, ${stats.edges} edges, ${stats.components} components`);
    for (const node of top.slice(0, 8)) {
      console.log(`  ${node.type.padEnd(10)} ${(node.path ?? node.label).slice(0, 70)}`);
    }
  }
  return 0;
}
