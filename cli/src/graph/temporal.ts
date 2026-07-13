import type { MemoryEvent } from "../brain/types.js";
import { canonicalMemoryAsOf } from "../brain/events.js";
import { canonicalJson, compareText, sha256 } from "../brain/utils.js";
import type {
  MemoryGraphNodeMetadata,
  MemoryTemporalRecord,
  MemoryTimeline,
  MemoryTimelineEntry,
  RepoGraph,
  RepoGraphNode,
  TemporalGraphDiagnostics,
} from "./types.js";

export const REPO_GRAPH_PROJECTION_NAMESPACE = "provena.neo4j.graph";
export const REPO_GRAPH_PROJECTION_VERSION = 2 as const;
export const MEMORY_GRAPH_NODE_PREFIX = "memory:";

export function memoryGraphNodeId(eventId: string): string {
  return `${MEMORY_GRAPH_NODE_PREFIX}${eventId}`;
}

export function repoGraphProjectionFingerprint(
  sourceFingerprint: string,
  memoryFingerprint: string,
): string {
  return sha256(canonicalJson([
    REPO_GRAPH_PROJECTION_NAMESPACE,
    REPO_GRAPH_PROJECTION_VERSION,
    sourceFingerprint,
    memoryFingerprint,
  ]));
}

export interface MemoryTemporalProjection {
  records: MemoryTemporalRecord[];
  diagnostics: TemporalGraphDiagnostics;
}

/** Derive direct reverse references and producer-effective intervals in O(V + E). */
export function deriveMemoryTemporalRecords(
  events: readonly MemoryEvent[],
): MemoryTemporalProjection {
  const diagnostics: TemporalGraphDiagnostics = {
    eventIndexVisits: 0,
    supersessionReferenceVisits: 0,
    successorIntervalVisits: 0,
    emittedTemporalRecords: 0,
  };
  const byId = new Map<string, MemoryEvent>();
  const successors = new Map<string, MemoryEvent[]>();

  for (const event of events) {
    diagnostics.eventIndexVisits += 1;
    if (byId.has(event.id)) {
      throw new Error("temporal memory graph contains a duplicate event id");
    }
    canonicalMemoryAsOf(event.createdAt);
    byId.set(event.id, event);
    successors.set(event.id, []);
  }

  for (const event of events) {
    for (const predecessorId of event.supersedes) {
      diagnostics.supersessionReferenceVisits += 1;
      if (!byId.has(predecessorId)) {
        throw new Error("temporal memory graph contains an unknown supersession reference");
      }
      successors.get(predecessorId)!.push(event);
    }
  }

  const records: MemoryTemporalRecord[] = [];
  for (const event of events) {
    const validFrom = event.createdAt;
    let earliestSuccessor: string | null = null;
    const directSuccessors = successors.get(event.id)!;
    for (const successor of directSuccessors) {
      diagnostics.successorIntervalVisits += 1;
      if (successor.createdAt < validFrom) {
        throw new Error("temporal memory graph contains an invalid effective-time transition");
      }
      if (earliestSuccessor === null || successor.createdAt < earliestSuccessor) {
        earliestSuccessor = successor.createdAt;
      }
    }
    records.push({
      eventId: event.id,
      title: event.title,
      kind: event.kind,
      subjectType: event.subjectType,
      declaredStatus: event.status,
      authority: event.authority,
      confidence: event.confidence,
      importance: event.importance,
      sensitivity: event.sensitivity,
      validFrom,
      validTo: event.status === "active" ? earliestSuccessor : validFrom,
      predecessors: [...event.supersedes],
      successors: directSuccessors.map((item) => item.id),
    });
    diagnostics.emittedTemporalRecords += 1;
  }
  return {
    records,
    diagnostics,
  };
}

function memoryMetadata(node: RepoGraphNode): MemoryGraphNodeMetadata {
  return node.metadata as unknown as MemoryGraphNodeMetadata;
}

/** Keep current code topology plus only memory heads effective at the boundary. */
export function induceRepoGraphAt(
  graph: RepoGraph,
  memoryAsOf?: string,
): RepoGraph {
  const boundary = canonicalMemoryAsOf(memoryAsOf);
  if (graph.schemaVersion === 1) return graph;

  const eligibleMemory = new Set(
    graph.nodes
      .filter((node) =>
        node.type === "memory" &&
        (boundary === undefined || memoryMetadata(node).validFrom <= boundary),
      )
      .map((node) => node.id),
  );
  const superseded = new Set(
    graph.edges
      .filter((edge) => edge.type === "supersedes" && eligibleMemory.has(edge.from))
      .map((edge) => edge.to),
  );
  const activeMemory = new Set(
    graph.nodes
      .filter((node) =>
        node.type === "memory" &&
        eligibleMemory.has(node.id) &&
        memoryMetadata(node).declaredStatus === "active" &&
        !superseded.has(node.id),
      )
      .map((node) => node.id),
  );
  const included = new Set(
    graph.nodes
      .filter((node) => node.type !== "memory" || activeMemory.has(node.id))
      .map((node) => node.id),
  );
  return {
    ...graph,
    nodes: graph.nodes.filter((node) => included.has(node.id)),
    edges: graph.edges.filter(
      (edge) => included.has(edge.from) && included.has(edge.to),
    ),
  };
}

const TIMELINE_LOOKUP_ERROR = "memory timeline target was not found or is ambiguous";

/** Return the complete connected direct-supersession lineage for one memory. */
export function memoryTimeline(graph: RepoGraph, value: string): MemoryTimeline {
  const memoryNodes = graph.nodes.filter((node) => node.type === "memory");
  const candidates = memoryNodes.filter(
    (node) => node.id === value || memoryMetadata(node).eventId === value,
  );
  if (candidates.length !== 1) throw new Error(TIMELINE_LOOKUP_ERROR);

  const predecessors = new Map<string, string[]>();
  const successors = new Map<string, string[]>();
  for (const node of memoryNodes) {
    predecessors.set(node.id, []);
    successors.set(node.id, []);
  }
  for (const edge of graph.edges) {
    if (
      edge.type !== "supersedes" ||
      !predecessors.has(edge.from) ||
      !successors.has(edge.to)
    ) {
      continue;
    }
    predecessors.get(edge.from)!.push(edge.to);
    successors.get(edge.to)!.push(edge.from);
  }
  for (const links of [...predecessors.values(), ...successors.values()]) {
    links.sort(compareText);
  }

  const rootId = candidates[0]!.id;
  const selected = new Set([rootId]);
  const queue = [rootId];
  for (let cursor = 0; cursor < queue.length; cursor += 1) {
    const id = queue[cursor]!;
    for (const related of [
      ...(predecessors.get(id) ?? []),
      ...(successors.get(id) ?? []),
    ]) {
      if (selected.has(related)) continue;
      selected.add(related);
      queue.push(related);
    }
  }

  const byId = new Map(memoryNodes.map((node) => [node.id, node]));
  const entries: MemoryTimelineEntry[] = [...selected].map((id) => {
    const node = byId.get(id)!;
    return {
      id,
      ...memoryMetadata(node),
      predecessors: predecessors.get(id) ?? [],
      successors: successors.get(id) ?? [],
    };
  }).sort(
    (left, right) =>
      compareText(left.validFrom, right.validFrom) || compareText(left.id, right.id),
  );
  return { rootId, entries };
}
