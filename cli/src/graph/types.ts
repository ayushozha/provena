export type RepoGraphNodeType =
  | "repository"
  | "directory"
  | "file"
  | "symbol"
  | "package"
  | "command"
  | "environment"
  | "dependency";

export type RepoGraphEdgeType =
  | "contains"
  | "defines"
  | "declares"
  | "depends_on"
  | "uses"
  | "imports";

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
}

export interface RepoGraph {
  schemaVersion: 1;
  sourceFingerprint: string;
  nodes: RepoGraphNode[];
  edges: RepoGraphEdge[];
}

export interface DegreeScore {
  in: number;
  out: number;
  total: number;
}

export type GraphDirection = "in" | "out" | "both";
