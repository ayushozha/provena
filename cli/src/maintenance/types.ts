export const MAINTENANCE_ISSUE_KINDS = [
  "evidence-gap",
  "source-not-in-map",
  "source-changed",
  "scope-not-in-map",
  "exact-content-overlap",
] as const;

export type MaintenanceIssueKind = (typeof MAINTENANCE_ISSUE_KINDS)[number];

export interface MaintenanceIssue {
  id: string;
  kind: MaintenanceIssueKind;
  memoryIds: string[];
  memoryIdsOmitted: number;
  paths: string[];
  pathsOmitted: number;
}

export interface MaintenanceTask {
  id: string;
  issueId: string;
  kind: MaintenanceIssueKind;
  memoryIds: string[];
  memoryIdsOmitted: number;
  paths: string[];
  pathsOmitted: number;
}

export interface MaintenanceCountSummary {
  total: number;
  emitted: number;
  omitted: number;
}

export interface MaintenancePlanSummary {
  activeMemoryHeads: number;
  pathChecksTotal: number;
  pathChecksDeferred: number;
  issuesTotal: number;
  issuesEmitted: number;
  issuesOmitted: number;
  tasksTotal: number;
  tasksEmitted: number;
  tasksOmitted: number;
  memoryIdsOmitted: number;
  pathsOmitted: number;
  issueKinds: Record<MaintenanceIssueKind, MaintenanceCountSummary>;
}

export interface MaintenancePlan {
  schemaVersion: 1;
  planner: {
    namespace: "provena.maintenance";
    version: 1;
  };
  sourceFingerprint: string;
  memoryFingerprint: string;
  planFingerprint: string;
  scanComplete: boolean;
  truncated: boolean;
  summary: MaintenancePlanSummary;
  issues: MaintenanceIssue[];
  tasks: MaintenanceTask[];
}

/** Test-only, non-timing evidence that compilation remains linear. */
export interface MaintenanceCompilerDiagnostics {
  eventVisits: number;
  sourceVisits: number;
  scopeVisits: number;
  overlapKeyVisits: number;
}

export interface MaintenanceCompilation {
  plan: MaintenancePlan;
  diagnostics: MaintenanceCompilerDiagnostics;
}

/** A bounded listing that retains, but never claims to replace, plan attestation. */
export interface MaintenancePlanView {
  schemaVersion: 1;
  planFingerprint: string;
  sourceFingerprint: string;
  memoryFingerprint: string;
  summary: MaintenancePlanSummary;
  returnedTasks: number;
  totalTasks: number;
  tasks: MaintenanceTask[];
}
