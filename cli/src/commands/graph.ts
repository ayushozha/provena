import { getGitRoot, loadConfig } from "../config.js";
import { refreshRepoBrain } from "../brain/index.js";
import {
  connectedComponents,
  degreeCentrality,
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
  const at = args.indexOf(flag);
  return at >= 0 ? args[at + 1] : undefined;
}

function resolveNode(graph: RepoGraph, value: string): RepoGraphNode {
  const query = value.trim().toLowerCase();
  const exact = graph.nodes.filter(
    (node) =>
      node.id.toLowerCase() === query ||
      node.path?.toLowerCase() === query ||
      node.label.toLowerCase() === query,
  );
  if (exact.length === 1) return exact[0]!;
  if (exact.length > 1) {
    throw new Error(
      `ambiguous graph node ${value}: ${exact.map((node) => node.id).join(", ")}`,
    );
  }
  throw new Error(`graph node not found: ${value}`);
}

export function printGraphHelp(): void {
  console.log("Usage: provena graph [stats|neighbors|path|components|sync] [options]");
  console.log("");
  console.log("  stats                         Show graph size and central nodes");
  console.log("  neighbors <id|path>           Show a local graph neighborhood");
  console.log("  path <from> <to>              Find the shortest repo relationship path");
  console.log("  components                    List weakly connected components");
  console.log("  sync neo4j                    Project the current graph into Neo4j");
  console.log("");
  console.log("Options: --hops N, --json");
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
  const { graph } = await refreshRepoBrain(repoRoot);
  const command = args[0] ?? "stats";
  const json = hasFlag(args, "--json");

  if (command === "sync") {
    if (args[1] !== "neo4j") {
      throw new Error("graph sync currently supports the `neo4j` adapter");
    }
    const { config } = loadConfig(repoRoot);
    if (!config.repository_id) {
      throw new Error("Neo4j sync requires a stable repository_id; rerun `provena init --force` to upgrade this checkout");
    }
    const result = await syncGraphToNeo4j(
      graph,
      neo4jRepositoryId(config.scope.tenant_id, config.repository_id),
    );
    console.log(
      json
        ? JSON.stringify(result, null, 2)
        : `Synced ${result.nodes} nodes and ${result.edges} edges to Neo4j for ${result.repoId}.`,
    );
    return 0;
  }

  if (command === "neighbors") {
    const value = args[1];
    if (!value) throw new Error("graph neighbors requires a node ID, path, or label");
    const node = resolveNode(graph, value);
    const rawHops = valueAfter(args, "--hops") ?? "1";
    const hops = Number(rawHops);
    if (!Number.isInteger(hops) || hops < 0 || hops > 8) {
      throw new Error("--hops must be an integer between 0 and 8");
    }
    const found = neighborhood(graph, node.id, hops);
    const result = {
      root: node,
      nodes: graph.nodes.filter((item) => found.nodeIds.includes(item.id)),
      edges: graph.edges.filter((edge) => found.edgeIds.includes(edge.id)),
    };
    console.log(JSON.stringify(result, null, 2));
    return 0;
  }

  if (command === "path") {
    if (!args[1] || !args[2]) throw new Error("graph path requires <from> and <to>");
    const from = resolveNode(graph, args[1]);
    const to = resolveNode(graph, args[2]);
    const path = shortestPath(graph, from.id, to.id);
    const nodes = path?.map((id) => graph.nodes.find((node) => node.id === id)) ?? [];
    console.log(
      json
        ? JSON.stringify({ from, to, path: nodes }, null, 2)
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
    console.log(JSON.stringify(result, null, 2));
    return 0;
  }

  if (command !== "stats") {
    throw new Error(`unknown graph subcommand: ${command}`);
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
    sourceFingerprint: graph.sourceFingerprint,
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
