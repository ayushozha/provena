export const REPO_BRAIN_SCHEMA_VERSION = 1 as const;

export type RepoFileKind =
  | "source"
  | "test"
  | "documentation"
  | "configuration"
  | "manifest"
  | "migration"
  | "asset"
  | "other";

export interface RepoFile {
  id: string;
  path: string;
  kind: RepoFileKind;
  language: string | null;
  sizeBytes: number;
  sha256: string;
  imports: string[];
}

export interface RepoDirectory {
  id: string;
  path: string;
  fileCount: number;
}

export interface RepoSymbol {
  id: string;
  path: string;
  name: string;
  kind: string;
  line: number;
  exported: boolean;
}

export interface RepoCommand {
  id: string;
  name: string;
  command: string;
  cwd: string;
  source: string;
}

export interface RepoEnvironmentVariable {
  id: string;
  name: string;
  sources: string[];
}

export interface RepoPackage {
  id: string;
  path: string;
  manifestPath: string;
  name: string;
  ecosystem: "node" | "python" | "go" | "rust" | "make";
  dependencies: string[];
  commandIds: string[];
}

export interface RepoScanDiagnostics {
  /** False when caps, read failures, or malformed supported manifests made the map partial. */
  complete: boolean;
  /** Bounded repo-relative reasons that made this scan incomplete. */
  warnings: string[];
}

export interface RepoMap {
  schemaVersion: typeof REPO_BRAIN_SCHEMA_VERSION;
  repository: {
    name: string;
    description: string | null;
  };
  sourceFingerprint: string;
  scan: RepoScanDiagnostics;
  languages: string[];
  directories: RepoDirectory[];
  files: RepoFile[];
  symbols: RepoSymbol[];
  packages: RepoPackage[];
  commands: RepoCommand[];
  environmentVariables: RepoEnvironmentVariable[];
}

export const MEMORY_KINDS = [
  "fact",
  "decision",
  "workflow",
  "mistake",
  "preference",
  "handoff",
  "invariant",
] as const;

export type MemoryKind = (typeof MEMORY_KINDS)[number];

export const MEMORY_SUBJECT_TYPES = [
  "repo",
  "file",
  "symbol",
  "command",
  "test",
  "api",
  "architecture",
  "task",
] as const;

export type MemorySubjectType = (typeof MEMORY_SUBJECT_TYPES)[number];
export type MemoryAuthority = "human" | "agent" | "tool" | "system";
export type MemorySensitivity =
  | "public"
  | "internal"
  | "confidential"
  | "restricted";
export type MemoryStatus = "active" | "superseded" | "retracted";

export interface MemorySource {
  path: string;
  symbol?: string;
  startLine?: number;
  endLine?: number;
  blob?: string;
  commit?: string;
}

export interface MemoryProvenance {
  actor: string;
  method: "explicit" | "observed" | "imported";
  agent?: string;
  sessionId?: string;
  command?: string;
}

export interface MemoryEvent {
  schemaVersion: 1;
  id: string;
  kind: MemoryKind;
  subjectType: MemorySubjectType;
  title: string;
  body: string;
  structuredData: Record<string, unknown>;
  status: MemoryStatus;
  appliesTo: string[];
  sources: MemorySource[];
  provenance: MemoryProvenance;
  authority: MemoryAuthority;
  confidence: number;
  importance: number;
  sensitivity: MemorySensitivity;
  createdAt: string;
  updatedAt: string;
  supersedes: string[];
  tags: string[];
  triggers: string[];
}

/** Stable on-disk JSONL representation shared across language runtimes. */
export interface MemoryEventRecord {
  schema_version: 1;
  id: string;
  kind: MemoryKind;
  subject_type: MemorySubjectType;
  title: string;
  body: string;
  structured_data: Record<string, unknown>;
  status: MemoryStatus;
  applies_to: string[];
  sources: Array<{
    path: string;
    symbol?: string;
    start_line?: number;
    end_line?: number;
    blob?: string;
    commit?: string;
  }>;
  provenance: {
    actor: string;
    method: "explicit" | "observed" | "imported";
    agent?: string;
    session_id?: string;
    command?: string;
  };
  authority: MemoryAuthority;
  confidence: number;
  importance: number;
  sensitivity: MemorySensitivity;
  created_at: string;
  updated_at: string;
  supersedes: string[];
  tags: string[];
  triggers: string[];
}

export interface NewMemoryEvent {
  id?: string;
  kind: MemoryKind;
  subjectType: MemorySubjectType;
  title: string;
  body: string;
  structuredData?: Record<string, unknown>;
  status?: MemoryStatus;
  appliesTo?: string[];
  sources?: MemorySource[];
  provenance: MemoryProvenance;
  authority: MemoryAuthority;
  confidence?: number;
  importance?: number;
  sensitivity?: MemorySensitivity;
  createdAt?: string;
  updatedAt?: string;
  supersedes?: string[];
  tags?: string[];
  triggers?: string[];
}

export interface RepoBrainManifest {
  schemaVersion: 1;
  sourceFingerprint: string;
  memoryFingerprint: string;
  artifacts: Array<{
    path: string;
    sha256: string;
    bytes: number;
  }>;
}
