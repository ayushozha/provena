import type {
  MemoryAuthority,
  MemoryKind,
  MemorySensitivity,
  MemoryStatus,
  MemorySubjectType,
} from "../brain/types.js";

export type RepoGraphNodeType =
  | "repository"
  | "directory"
  | "file"
  | "symbol"
  | "package"
  | "command"
  | "environment"
  | "dependency"
  | "memory";

export type RepoGraphEdgeType =
  | "contains"
  | "defines"
  | "declares"
  | "depends_on"
  | "uses"
  | "imports"
  | "supersedes"
  | "cites"
  | "applies_to";

export interface RepoGraphNode {
  id: string;
  type: RepoGraphNodeType;
  label: string;
  path?: string;
  metadata: Record<string, string | number | boolean | null>;
}

export interface RepoGraphEdge {
  id: string;
  from: string;
  to: string;
  type: RepoGraphEdgeType;
  weight: number;
  effectiveAt?: string;
}

/** Legacy code-only graph artifacts remain readable until the next refresh. */
export interface RepoGraphV1 {
  schemaVersion: 1;
  sourceFingerprint: string;
  nodes: RepoGraphNode[];
  edges: RepoGraphEdge[];
}

export interface RepoGraphV2 {
  schemaVersion: 2;
  sourceFingerprint: string;
  memoryFingerprint: string;
  projectionFingerprint: string;
  timeSemantics: "event-effective-time";
  nodes: RepoGraphNode[];
  edges: RepoGraphEdge[];
}

export type RepoGraph = RepoGraphV1 | RepoGraphV2;

export interface MemoryGraphNodeMetadata {
  eventId: string;
  title: string;
  kind: MemoryKind;
  subjectType: MemorySubjectType;
  declaredStatus: MemoryStatus;
  authority: MemoryAuthority;
  confidence: number;
  importance: number;
  sensitivity: MemorySensitivity;
  validFrom: string;
  validTo: string | null;
}

export interface MemoryTemporalRecord extends MemoryGraphNodeMetadata {
  predecessors: string[];
  successors: string[];
}

export interface TemporalGraphDiagnostics {
  eventIndexVisits: number;
  supersessionReferenceVisits: number;
  successorIntervalVisits: number;
  emittedTemporalRecords: number;
}

export interface MemoryTimelineEntry extends MemoryTemporalRecord {
  id: string;
}

export interface MemoryTimeline {
  rootId: string;
  entries: MemoryTimelineEntry[];
}

export interface DegreeScore {
  in: number;
  out: number;
  total: number;
}

export type GraphDirection = "in" | "out" | "both";
