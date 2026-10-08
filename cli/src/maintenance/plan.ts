import { posix } from "node:path";
import {
  activeMemoryEvents,
  assertMemoryLedgerSnapshotAttestation,
  type MemoryLedgerSnapshot,
} from "../brain/events.js";
import { repoMapSourceFingerprint } from "../brain/detect.js";
import type { MemoryEvent, RepoMap } from "../brain/types.js";
import {
  canonicalJson,
  compareText,
  sha256,
  stableId,
} from "../brain/utils.js";
import { assertNoSecretMaterial } from "../security/memory.js";
import {
  MAINTENANCE_ISSUE_KINDS,
  type MaintenanceCompilation,
  type MaintenanceCompilerDiagnostics,
  type MaintenanceIssue,
  type MaintenanceIssueKind,
  type MaintenancePlan,
  type MaintenancePlanSummary,
  type MaintenancePlanView,
} from "./types.js";

export const MAINTENANCE_PLAN_PATH = ".provena/maintenance.plan.json";
export const MAINTENANCE_PLAN_SCHEMA_VERSION = 1 as const;
export const MAINTENANCE_PLANNER_NAMESPACE = "provena.maintenance" as const;
export const MAINTENANCE_PLANNER_VERSION = 1 as const;
export const MAX_MAINTENANCE_ISSUES = 256;
export const MAX_MAINTENANCE_TASKS = 256;
export const MAX_MAINTENANCE_RECORD_IDS = 32;
export const MAX_MAINTENANCE_RECORD_PATHS = 32;
export const MAX_MAINTENANCE_PLAN_BYTES = 512 * 1024;

const HASH_PATTERN = /^[a-f0-9]{64}$/;
const MEMORY_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$/;
const PLAN_INPUT_ERROR =
  "maintenance inputs do not describe one attested repository generation";
const PLAN_ATTESTATION_ERROR =
  "maintenance plan does not match the attested repository generation";
const PLAN_VIEW_ERROR = "maintenance plan view request is invalid";

interface IssueCandidate {
  kind: MaintenanceIssueKind;
  memoryIds: string[];
  paths: string[];
}

function normalizedPath(value: string): string {
  if (typeof value !== "string" || value.length === 0) throw new Error(PLAN_INPUT_ERROR);
  if (value.startsWith("/") || /^[A-Za-z]:[\\/]/.test(value)) {
    throw new Error(PLAN_INPUT_ERROR);
  }
  // RepoMap and ledger production already establish the platform's canonical
  // repo-relative spelling. Preserve exact Unicode, whitespace, case, and
  // backslash identity here; only collapse portable POSIX dot segments.
  const normalized = posix.normalize(value).replace(/^\.\//, "");
  if (!normalized || normalized === ".." || normalized.startsWith("../")) {
    throw new Error(PLAN_INPUT_ERROR);
  }
  return normalized;
}

function uniqueSorted(values: readonly string[]): string[] {
  return [...new Set(values)].sort(compareText);
}

function sortedMemoryIds(values: readonly string[]): string[] {
  let records = [...new Set(values)];
  const width = records.reduce((maximum, value) => Math.max(maximum, value.length), 0);
  for (let index = width - 1; index >= 0; index -= 1) {
    const buckets = Array.from({ length: 128 }, () => [] as string[]);
    for (const value of records) {
      const bucket = index < value.length ? value.charCodeAt(index) + 1 : 0;
      buckets[bucket]!.push(value);
    }
    records = buckets.flat();
  }
  return records;
}

function normalizedClaimText(value: string): string {
  if (typeof value !== "string") throw new Error(PLAN_INPUT_ERROR);
  return value.normalize("NFC").replace(/\s+/gu, " ").trim().toLowerCase();
}

function assertInputs(map: RepoMap, memory: MemoryLedgerSnapshot): void {
  try {
    assertMemoryLedgerSnapshotAttestation(memory);
    if (
      !map ||
      typeof map !== "object" ||
      map.schemaVersion !== 1 ||
      !map.scan ||
      typeof map.scan.complete !== "boolean" ||
      !Array.isArray(map.files) ||
      !Array.isArray(map.directories) ||
      !Array.isArray(map.packages) ||
      typeof map.sourceFingerprint !== "string" ||
      !HASH_PATTERN.test(map.sourceFingerprint) ||
      map.sourceFingerprint !== repoMapSourceFingerprint(map)
    ) {
      throw new Error();
    }
    const ids = new Set<string>();
    for (const event of memory.events) {
      if (
        !event ||
        typeof event !== "object" ||
        typeof event.id !== "string" ||
        !MEMORY_ID_PATTERN.test(event.id) ||
        ids.has(event.id) ||
        !Array.isArray(event.sources) ||
        !Array.isArray(event.appliesTo)
      ) {
        throw new Error();
      }
      ids.add(event.id);
    }
  } catch {
    throw new Error(PLAN_INPUT_ERROR);
  }
}

function candidateId(candidate: IssueCandidate): string {
  return stableId(
    "maintenance-issue",
    MAINTENANCE_PLANNER_NAMESPACE,
    String(MAINTENANCE_PLANNER_VERSION),
    canonicalJson([candidate.kind, candidate.memoryIds, candidate.paths]),
  );
}

/** Fixed-width hexadecimal radix ordering keeps candidate ordering O(n). */
function orderedCandidates(candidates: readonly IssueCandidate[]): IssueCandidate[] {
  const kindOrder: readonly MaintenanceIssueKind[] = [
    "evidence-gap",
    "exact-content-overlap",
    "scope-not-in-map",
    "source-not-in-map",
    "source-changed",
  ];
  const ordered: IssueCandidate[] = [];
  for (const kind of kindOrder) {
    let records = candidates
      .filter((candidate) => candidate.kind === kind)
      .map((candidate) => ({ candidate, hash: candidateId(candidate).slice(-20) }));
    for (let index = 19; index >= 0; index -= 1) {
      const buckets = Array.from(
        { length: 16 },
        () => [] as typeof records,
      );
      for (const record of records) {
        const bucket = Number.parseInt(record.hash[index]!, 16);
        buckets[bucket]!.push(record);
      }
      records = buckets.flat();
    }
    ordered.push(...records.map((record) => record.candidate));
  }
  return ordered;
}

function issueRecord(candidate: IssueCandidate): MaintenanceIssue {
  const memoryIds = sortedMemoryIds(candidate.memoryIds);
  const paths = uniqueSorted(candidate.paths);
  return {
    id: candidateId({ ...candidate, memoryIds, paths }),
    kind: candidate.kind,
    memoryIds: memoryIds.slice(0, MAX_MAINTENANCE_RECORD_IDS),
    memoryIdsOmitted: Math.max(0, memoryIds.length - MAX_MAINTENANCE_RECORD_IDS),
    paths: paths.slice(0, MAX_MAINTENANCE_RECORD_PATHS),
    pathsOmitted: Math.max(0, paths.length - MAX_MAINTENANCE_RECORD_PATHS),
  };
}

function emptyKindCounts(): MaintenancePlanSummary["issueKinds"] {
  return Object.fromEntries(
    MAINTENANCE_ISSUE_KINDS.map((kind) => [kind, { total: 0, emitted: 0, omitted: 0 }]),
  ) as MaintenancePlanSummary["issueKinds"];
}

function planFingerprintPayload(
  plan: Omit<MaintenancePlan, "planFingerprint">,
): Omit<MaintenancePlan, "planFingerprint"> {
  return plan;
}

function createPlan(
  map: RepoMap,
  memory: MemoryLedgerSnapshot,
  candidates: readonly IssueCandidate[],
  emittedCount: number,
  activeMemoryHeads: number,
  pathChecksTotal: number,
  pathChecksDeferred: number,
): MaintenancePlan {
  const issueKinds = emptyKindCounts();
  for (const candidate of candidates) issueKinds[candidate.kind].total += 1;

  const issues = candidates.slice(0, emittedCount).map(issueRecord);
  for (const issue of issues) issueKinds[issue.kind].emitted += 1;
  for (const kind of MAINTENANCE_ISSUE_KINDS) {
    issueKinds[kind].omitted = issueKinds[kind].total - issueKinds[kind].emitted;
  }
  const tasks = issues.slice(0, MAX_MAINTENANCE_TASKS).map((issue) => ({
    id: stableId("maintenance-task", issue.id),
    issueId: issue.id,
    kind: issue.kind,
    memoryIds: issue.memoryIds,
    memoryIdsOmitted: issue.memoryIdsOmitted,
    paths: issue.paths,
    pathsOmitted: issue.pathsOmitted,
  }));
  const summary: MaintenancePlanSummary = {
    activeMemoryHeads,
    pathChecksTotal,
    pathChecksDeferred,
    issuesTotal: candidates.length,
    issuesEmitted: issues.length,
    issuesOmitted: candidates.length - issues.length,
    tasksTotal: candidates.length,
    tasksEmitted: tasks.length,
    tasksOmitted: candidates.length - tasks.length,
    memoryIdsOmitted: issues.reduce((total, issue) => total + issue.memoryIdsOmitted, 0),
    pathsOmitted: issues.reduce((total, issue) => total + issue.pathsOmitted, 0),
    issueKinds,
  };
  const withoutFingerprint: Omit<MaintenancePlan, "planFingerprint"> = {
    schemaVersion: MAINTENANCE_PLAN_SCHEMA_VERSION,
    planner: {
      namespace: MAINTENANCE_PLANNER_NAMESPACE,
      version: MAINTENANCE_PLANNER_VERSION,
    },
    sourceFingerprint: map.sourceFingerprint,
    memoryFingerprint: memory.memoryFingerprint,
    scanComplete: map.scan.complete,
    truncated:
      summary.issuesOmitted > 0 ||
      summary.tasksOmitted > 0 ||
      summary.memoryIdsOmitted > 0 ||
      summary.pathsOmitted > 0,
    summary,
    issues,
    tasks,
  };
  return {
    ...withoutFingerprint,
    planFingerprint: sha256(canonicalJson(planFingerprintPayload(withoutFingerprint))),
  };
}

function overlapKey(event: MemoryEvent, appliesTo: readonly string[]): string {
  return canonicalJson([
    event.kind,
    event.subjectType,
    normalizedClaimText(event.title),
    normalizedClaimText(event.body),
    appliesTo,
  ]);
}

export function compileMaintenancePlanWithDiagnostics(
  map: RepoMap,
  memory: MemoryLedgerSnapshot,
): MaintenanceCompilation {
  assertInputs(map, memory);
  const diagnostics: MaintenanceCompilerDiagnostics = {
    eventVisits: 0,
    sourceVisits: 0,
    scopeVisits: 0,
    overlapKeyVisits: 0,
  };
  const active = activeMemoryEvents(memory.events);
  const files = new Map(map.files.map((file) => [normalizedPath(file.path), file]));
  const filePaths = new Set(files.keys());
  const scopePaths = new Set([
    ".",
    ...filePaths,
    ...map.directories.map((directory) => normalizedPath(directory.path)),
    ...map.packages.map((pkg) => normalizedPath(pkg.path)),
  ]);
  const candidates: IssueCandidate[] = [];
  const overlaps = new Map<string, { memoryIds: string[]; paths: string[] }>();
  let pathChecksTotal = 0;
  let pathChecksDeferred = 0;

  for (const event of active) {
    diagnostics.eventVisits += 1;
    const appliesTo = uniqueSorted(event.appliesTo.map(normalizedPath));
    if (event.sources.length === 0 && appliesTo.length === 0) {
      candidates.push({ kind: "evidence-gap", memoryIds: [event.id], paths: [] });
    }

    const missingSources: string[] = [];
    const changedSources: string[] = [];
    for (const source of event.sources) {
      diagnostics.sourceVisits += 1;
      pathChecksTotal += 1;
      const path = normalizedPath(source.path);
      if (!map.scan.complete) pathChecksDeferred += 1;
      else if (!filePaths.has(path)) missingSources.push(path);
      else {
        const currentHash = files.get(path)?.sha256;
        const sourceHash = source.blob?.replace(/^sha256-lf:/, "");
        // The map hashes canonical UTF-8 text with LF line endings. Raw-byte
        // SHA-256, Git object IDs, and opaque references are different formats.
        if (
          sourceHash && HASH_PATTERN.test(sourceHash) &&
          currentHash && HASH_PATTERN.test(currentHash) &&
          sourceHash !== currentHash
        ) changedSources.push(path);
      }
    }
    if (missingSources.length > 0) {
      candidates.push({
        kind: "source-not-in-map",
        memoryIds: [event.id],
        paths: uniqueSorted(missingSources),
      });
    }
    if (changedSources.length > 0) {
      candidates.push({
        kind: "source-changed",
        memoryIds: [event.id],
        paths: uniqueSorted(changedSources),
      });
    }

    const missingScopes: string[] = [];
    for (const path of appliesTo) {
      diagnostics.scopeVisits += 1;
      if (path === ".") continue;
      pathChecksTotal += 1;
      if (!map.scan.complete) pathChecksDeferred += 1;
      else if (!scopePaths.has(path)) missingScopes.push(path);
    }
    if (missingScopes.length > 0) {
      candidates.push({
        kind: "scope-not-in-map",
        memoryIds: [event.id],
        paths: uniqueSorted(missingScopes),
      });
    }

    diagnostics.overlapKeyVisits += 1;
    const key = overlapKey(event, appliesTo);
    const group = overlaps.get(key) ?? { memoryIds: [], paths: appliesTo };
    group.memoryIds.push(event.id);
    overlaps.set(key, group);
  }

  for (const group of overlaps.values()) {
    const memoryIds = sortedMemoryIds(group.memoryIds);
    if (memoryIds.length < 2) continue;
    candidates.push({
      kind: "exact-content-overlap",
      memoryIds,
      paths: group.paths,
    });
  }
  const ordered = orderedCandidates(candidates);

  let emittedCount = Math.min(
    ordered.length,
    MAX_MAINTENANCE_ISSUES,
    MAX_MAINTENANCE_TASKS,
  );
  let plan = createPlan(
    map,
    memory,
    ordered,
    emittedCount,
    active.length,
    pathChecksTotal,
    pathChecksDeferred,
  );
  while (
    Buffer.byteLength(canonicalMaintenancePlan(plan), "utf8") >
      MAX_MAINTENANCE_PLAN_BYTES &&
    emittedCount > 0
  ) {
    emittedCount -= 1;
    plan = createPlan(
      map,
      memory,
      ordered,
      emittedCount,
      active.length,
      pathChecksTotal,
      pathChecksDeferred,
    );
  }
  const serialized = canonicalMaintenancePlan(plan);
  if (Buffer.byteLength(serialized, "utf8") > MAX_MAINTENANCE_PLAN_BYTES) {
    throw new Error("maintenance plan exceeds its safety cap");
  }
  assertNoSecretMaterial(serialized);
  return { plan, diagnostics };
}

export function compileMaintenancePlan(
  map: RepoMap,
  memory: MemoryLedgerSnapshot,
): MaintenancePlan {
  return compileMaintenancePlanWithDiagnostics(map, memory).plan;
}

export function canonicalMaintenancePlan(plan: MaintenancePlan): string {
  return canonicalJson(plan, true);
}

export function assertMaintenancePlanAttestation(
  value: unknown,
  map: RepoMap,
  memory: MemoryLedgerSnapshot,
): asserts value is MaintenancePlan {
  try {
    const expected = compileMaintenancePlan(map, memory);
    if (
      !value ||
      typeof value !== "object" ||
      canonicalJson(value) !== canonicalJson(expected) ||
      Buffer.byteLength(canonicalJson(value, true), "utf8") > MAX_MAINTENANCE_PLAN_BYTES
    ) {
      throw new Error();
    }
  } catch {
    throw new Error(PLAN_ATTESTATION_ERROR);
  }
}

export function maintenancePlanView(
  plan: MaintenancePlan,
  limit: number,
): MaintenancePlanView {
  if (
    !plan ||
    typeof plan !== "object" ||
    plan.schemaVersion !== MAINTENANCE_PLAN_SCHEMA_VERSION ||
    !HASH_PATTERN.test(plan.planFingerprint) ||
    !Array.isArray(plan.tasks) ||
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > MAX_MAINTENANCE_TASKS
  ) {
    throw new Error(PLAN_VIEW_ERROR);
  }
  const tasks = plan.tasks.slice(0, limit);
  return {
    schemaVersion: 1,
    planFingerprint: plan.planFingerprint,
    sourceFingerprint: plan.sourceFingerprint,
    memoryFingerprint: plan.memoryFingerprint,
    summary: plan.summary,
    returnedTasks: tasks.length,
    totalTasks: plan.tasks.length,
    tasks,
  };
}
