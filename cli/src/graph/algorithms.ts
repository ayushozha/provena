import type {
  DegreeScore,
  GraphDirection,
  RepoGraph,
  RepoGraphEdge,
} from "./types.js";
import { compareText } from "../brain/utils.js";

interface Adjacency {
  incoming: Map<string, RepoGraphEdge[]>;
  outgoing: Map<string, RepoGraphEdge[]>;
}

function adjacency(graph: RepoGraph): Adjacency {
  const known = new Set(graph.nodes.map((node) => node.id));
  const incoming = new Map<string, RepoGraphEdge[]>();
  const outgoing = new Map<string, RepoGraphEdge[]>();
  for (const id of known) {
    incoming.set(id, []);
    outgoing.set(id, []);
  }
  for (const edge of graph.edges) {
    if (!known.has(edge.from) || !known.has(edge.to)) continue;
    outgoing.get(edge.from)?.push(edge);
    incoming.get(edge.to)?.push(edge);
  }
  for (const list of [...incoming.values(), ...outgoing.values()]) {
    list.sort((a, b) => compareText(a.id, b.id));
  }
  return { incoming, outgoing };
}

function neighborsFor(
  id: string,
  links: Adjacency,
  direction: GraphDirection,
): Array<{ id: string; edge: RepoGraphEdge }> {
  const result: Array<{ id: string; edge: RepoGraphEdge }> = [];
  if (direction === "out" || direction === "both") {
    for (const edge of links.outgoing.get(id) ?? []) result.push({ id: edge.to, edge });
  }
  if (direction === "in" || direction === "both") {
    for (const edge of links.incoming.get(id) ?? []) result.push({ id: edge.from, edge });
  }
  return result.sort((a, b) => compareText(a.id, b.id) || compareText(a.edge.id, b.edge.id));
}

export interface PageRankOptions {
  damping?: number;
  maxIterations?: number;
  tolerance?: number;
}

export function pageRank(
  graph: RepoGraph,
  options: PageRankOptions = {},
): Record<string, number> {
  const ids = graph.nodes.map((node) => node.id).sort(compareText);
  if (ids.length === 0) return {};
  const damping = options.damping ?? 0.85;
  const maxIterations = options.maxIterations ?? 100;
  const tolerance = options.tolerance ?? 1e-10;
  if (!(damping > 0 && damping < 1)) throw new Error("PageRank damping must be between 0 and 1");
  if (!Number.isInteger(maxIterations) || maxIterations < 1) throw new Error("PageRank maxIterations must be a positive integer");
  if (!(tolerance > 0)) throw new Error("PageRank tolerance must be positive");
  const links = adjacency(graph);
  const count = ids.length;
  let scores = Object.fromEntries(ids.map((id) => [id, 1 / count])) as Record<string, number>;

  for (let iteration = 0; iteration < maxIterations; iteration += 1) {
    const outgoingWeights = new Map(
      ids.map((id) => [
        id,
        (links.outgoing.get(id) ?? []).reduce(
          (sum, edge) => sum + Math.max(0, edge.weight),
          0,
        ),
      ]),
    );
    const dangling = ids
      .filter((id) => (outgoingWeights.get(id) ?? 0) === 0)
      .reduce((sum, id) => sum + (scores[id] ?? 0), 0);
    const next: Record<string, number> = {};
    let delta = 0;
    for (const id of ids) {
      let inbound = 0;
      for (const incoming of links.incoming.get(id) ?? []) {
        const totalWeight = outgoingWeights.get(incoming.from) ?? 0;
        if (totalWeight > 0) {
          inbound += (scores[incoming.from] ?? 0) * (Math.max(0, incoming.weight) / totalWeight);
        }
      }
      next[id] = (1 - damping) / count + damping * (inbound + dangling / count);
      delta += Math.abs(next[id] - (scores[id] ?? 0));
    }
    scores = next;
    if (delta <= tolerance) break;
  }
  const total = Object.values(scores).reduce((sum, score) => sum + score, 0) || 1;
  return Object.fromEntries(ids.map((id) => [id, scores[id] / total]));
}

export function degreeCentrality(graph: RepoGraph): Record<string, DegreeScore> {
  const ids = graph.nodes.map((node) => node.id).sort(compareText);
  const links = adjacency(graph);
  const denominator = Math.max(1, ids.length - 1);
  return Object.fromEntries(
    ids.map((id) => {
      const incoming = new Set((links.incoming.get(id) ?? []).map((edge) => edge.from)).size;
      const outgoing = new Set((links.outgoing.get(id) ?? []).map((edge) => edge.to)).size;
      const total = new Set([
        ...(links.incoming.get(id) ?? []).map((edge) => edge.from),
        ...(links.outgoing.get(id) ?? []).map((edge) => edge.to),
      ]).size;
      return [id, { in: incoming / denominator, out: outgoing / denominator, total: total / denominator }];
    }),
  );
}

export function connectedComponents(graph: RepoGraph): string[][] {
  const ids = graph.nodes.map((node) => node.id).sort(compareText);
  const links = adjacency(graph);
  const visited = new Set<string>();
  const components: string[][] = [];
  for (const start of ids) {
    if (visited.has(start)) continue;
    const queue = [start];
    visited.add(start);
    const component: string[] = [];
    while (queue.length > 0) {
      const id = queue.shift();
      if (!id) continue;
      component.push(id);
      for (const neighbor of neighborsFor(id, links, "both")) {
        if (visited.has(neighbor.id)) continue;
        visited.add(neighbor.id);
        queue.push(neighbor.id);
      }
    }
    components.push(component.sort(compareText));
  }
  return components.sort(
    (a, b) => compareText(a[0] ?? "", b[0] ?? "") || a.length - b.length,
  );
}

export function shortestPath(
  graph: RepoGraph,
  from: string,
  to: string,
  direction: GraphDirection = "both",
): string[] | null {
  const known = new Set(graph.nodes.map((node) => node.id));
  if (!known.has(from) || !known.has(to)) return null;
  if (from === to) return [from];
  const links = adjacency(graph);
  const queue = [from];
  const previous = new Map<string, string>();
  const visited = new Set([from]);
  while (queue.length > 0) {
    const id = queue.shift();
    if (!id) continue;
    for (const neighbor of neighborsFor(id, links, direction)) {
      if (visited.has(neighbor.id)) continue;
      previous.set(neighbor.id, id);
      if (neighbor.id === to) {
        const path = [to];
        let cursor = to;
        while (cursor !== from) {
          const parent = previous.get(cursor);
          if (!parent) return null;
          path.push(parent);
          cursor = parent;
        }
        return path.reverse();
      }
      visited.add(neighbor.id);
      queue.push(neighbor.id);
    }
  }
  return null;
}

export interface Neighborhood {
  nodeIds: string[];
  edgeIds: string[];
}

export function neighborhood(
  graph: RepoGraph,
  start: string,
  depth = 1,
  direction: GraphDirection = "both",
): Neighborhood {
  if (!Number.isInteger(depth) || depth < 0) throw new Error("neighborhood depth must be a non-negative integer");
  if (!graph.nodes.some((node) => node.id === start)) return { nodeIds: [], edgeIds: [] };
  const links = adjacency(graph);
  const visited = new Set([start]);
  const edges = new Set<string>();
  let frontier = [start];
  for (let level = 0; level < depth && frontier.length > 0; level += 1) {
    const next = new Set<string>();
    for (const id of frontier.sort(compareText)) {
      for (const neighbor of neighborsFor(id, links, direction)) {
        edges.add(neighbor.edge.id);
        if (!visited.has(neighbor.id)) next.add(neighbor.id);
      }
    }
    for (const id of next) visited.add(id);
    frontier = [...next];
  }
  return {
    nodeIds: [...visited].sort(compareText),
    edgeIds: [...edges].sort(compareText),
  };
}
