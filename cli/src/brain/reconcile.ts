import { lstat } from "node:fs/promises";
import { join } from "node:path";
import {
  extendMemoryLedgerSnapshot,
  prepareMemoryEvent,
  type MemoryLedgerSnapshot,
} from "./events.js";
import type {
  MemoryEvent,
  NewMemoryEvent,
  RepoCommand,
  RepoMap,
  RepoPackage,
} from "./types.js";
import {
  canonicalJson,
  compareText,
  normalizeRepoPath,
  sha256,
  stableId,
} from "./utils.js";
import { assertSafeRepoPath } from "../security/paths.js";
import { assertNoSecretMaterial } from "../security/memory.js";

export const REPO_MAP_MEMORY_GENERATOR = "provena.repo-map";
export const REPO_MAP_MEMORY_GENERATOR_VERSION = 1 as const;
export const REPO_MAP_MEMORY_TAG = "provena:managed:repo-map";
export const REPO_MAP_MEMORY_DATA_KEY = "provenaManagedMemory";

export type RepoMemoryAction = "ADD" | "SUPERSEDE" | "RETRACT";

export interface RepoMemoryReconciliation {
  candidates: number;
  added: number;
  noops: number;
  superseded: number;
  retracted: number;
  deferred: number;
  conflicts: number;
  durationMs: number;
}

export interface ReconcileRepoMapMemoryOptions {
  now?: () => Date;
  clock?: () => number;
}

export interface ReconcileRepoMapMemoryResult {
  memory: MemoryLedgerSnapshot;
  appended: MemoryEvent[];
  reconciliation: RepoMemoryReconciliation;
}

type PackageDescriptor = {
  type: "package";
  manifestPath: string;
  packagePath: string;
  name: string;
  ecosystem: RepoPackage["ecosystem"];
  dependencies: string[];
};

type CommandDescriptor = {
  type: "command";
  manifestPath: string;
  cwd: string;
  name: string;
  command: string;
};

type CandidateDescriptor = PackageDescriptor | CommandDescriptor;

interface Candidate {
  logicalKey: string;
  fingerprint: string;
  descriptor: CandidateDescriptor;
  sourceBlob: string;
}

interface ManagedMetadata {
  generator: typeof REPO_MAP_MEMORY_GENERATOR;
  version: typeof REPO_MAP_MEMORY_GENERATOR_VERSION;
  logicalKey: string;
  candidateFingerprint: string;
  action: RepoMemoryAction;
  predecessorId: string | null;
  candidate: CandidateDescriptor;
}

interface ManagedRecord {
  event: MemoryEvent;
  metadata: ManagedMetadata;
  ledgerIndex: number;
}

interface ManagedLineage {
  head: ManagedRecord;
  protectedByHigherAuthority: boolean;
}

const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const METADATA_KEYS = [
  "action",
  "candidate",
  "candidateFingerprint",
  "generator",
  "logicalKey",
  "predecessorId",
  "version",
] as const;
const PACKAGE_DESCRIPTOR_KEYS = [
  "dependencies",
  "ecosystem",
  "manifestPath",
  "name",
  "packagePath",
  "type",
] as const;
const COMMAND_DESCRIPTOR_KEYS = [
  "command",
  "cwd",
  "manifestPath",
  "name",
  "type",
] as const;

function structuralError(contract: string): never {
  throw new Error(`invalid managed repo-map memory lineage (${contract})`);
}

function candidateError(contract: string): never {
  throw new Error(`invalid repo-map memory candidate (${contract})`);
}

function sameKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort(compareText);
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function normalizedText(value: unknown, contract: string): string {
  if (typeof value !== "string") candidateError(contract);
  const normalized = value.normalize("NFC").replace(/\r\n?/g, "\n").trim();
  if (!normalized) candidateError(contract);
  return normalized;
}

function normalizedPath(repoRoot: string, value: unknown, contract: string): string {
  if (typeof value !== "string" || value.length === 0) candidateError(contract);
  let path: string;
  try {
    path = normalizeRepoPath(repoRoot, value);
  } catch {
    candidateError(contract);
  }
  if (path === "." && contract.includes("manifest")) candidateError(contract);
  return path;
}

function packageLogicalKey(manifestPath: string): string {
  return stableId("repo-map-package", manifestPath);
}

function commandLogicalKey(descriptor: Pick<CommandDescriptor, "manifestPath" | "cwd" | "name">): string {
  return stableId("repo-map-command", descriptor.manifestPath, descriptor.cwd, descriptor.name);
}

function logicalKeyFor(descriptor: CandidateDescriptor): string {
  return descriptor.type === "package"
    ? packageLogicalKey(descriptor.manifestPath)
    : commandLogicalKey(descriptor);
}

function candidateFingerprint(descriptor: CandidateDescriptor): string {
  const canonical = canonicalJson(descriptor);
  // Validate before lineage outcome branching so a protected/no-op candidate
  // cannot leak credential-like source data into derived repo artifacts.
  assertNoSecretMaterial(canonical);
  return sha256(canonical);
}

function deterministicEventId(
  logicalKey: string,
  action: RepoMemoryAction,
  fingerprint: string,
  predecessorId: string | null,
): string {
  return `memory:repo-map:${sha256(canonicalJson({
    action,
    fingerprint,
    generator: REPO_MAP_MEMORY_GENERATOR,
    logicalKey,
    predecessorId,
    version: REPO_MAP_MEMORY_GENERATOR_VERSION,
  })).slice(0, 40)}`;
}

function packageDescriptor(repoRoot: string, pkg: RepoPackage): PackageDescriptor {
  const dependencies = [
    ...new Set(pkg.dependencies.map((dependency) => normalizedText(dependency, "package-dependency"))),
  ].sort(compareText);
  const ecosystem = pkg.ecosystem;
  if (!["node", "python", "go", "rust", "make"].includes(ecosystem)) {
    candidateError("package-ecosystem");
  }
  return {
    type: "package",
    manifestPath: normalizedPath(repoRoot, pkg.manifestPath, "package-manifest"),
    packagePath: normalizedPath(repoRoot, pkg.path, "package-path"),
    name: normalizedText(pkg.name, "package-name"),
    ecosystem,
    dependencies,
  };
}

function commandDescriptor(repoRoot: string, command: RepoCommand): CommandDescriptor {
  return {
    type: "command",
    manifestPath: normalizedPath(repoRoot, command.source, "command-manifest"),
    cwd: normalizedPath(repoRoot, command.cwd, "command-cwd"),
    name: normalizedText(command.name, "command-name"),
    command: normalizedText(command.command, "command-invocation"),
  };
}

function addCandidate(candidates: Map<string, Candidate>, candidate: Candidate): void {
  const previous = candidates.get(candidate.logicalKey);
  if (previous && canonicalJson(previous.descriptor) !== canonicalJson(candidate.descriptor)) {
    candidateError("logical-key-collision");
  }
  if (previous) candidateError("duplicate-logical-key");
  candidates.set(candidate.logicalKey, candidate);
}

function deriveCandidates(
  repoRoot: string,
  map: RepoMap,
): {
  candidates: Map<string, Candidate>;
  files: Map<string, { sha256: string }>;
  validManifests: Set<string>;
} {
  if (
    !map.scan ||
    typeof map.scan.complete !== "boolean" ||
    !Array.isArray(map.scan.warnings) ||
    map.scan.warnings.some((warning) => typeof warning !== "string")
  ) {
    candidateError("scan-diagnostics");
  }
  const files = new Map<string, { sha256: string }>();
  for (const file of map.files) {
    const path = normalizedPath(repoRoot, file.path, "file-path");
    if (files.has(path)) candidateError("duplicate-file-path");
    if (!SHA256_PATTERN.test(file.sha256)) candidateError("source-attestation");
    files.set(path, { sha256: file.sha256 });
  }

  const candidates = new Map<string, Candidate>();
  const validManifests = new Set<string>();
  for (const pkg of map.packages) {
    const descriptor = packageDescriptor(repoRoot, pkg);
    const source = files.get(descriptor.manifestPath);
    if (!source) candidateError("package-source-attestation");
    validManifests.add(descriptor.manifestPath);
    const logicalKey = logicalKeyFor(descriptor);
    addCandidate(candidates, {
      logicalKey,
      fingerprint: candidateFingerprint(descriptor),
      descriptor,
      sourceBlob: source.sha256,
    });
  }

  for (const command of map.commands) {
    const descriptor = commandDescriptor(repoRoot, command);
    const source = files.get(descriptor.manifestPath);
    if (!source || !validManifests.has(descriptor.manifestPath)) {
      candidateError("command-source-attestation");
    }
    const logicalKey = logicalKeyFor(descriptor);
    addCandidate(candidates, {
      logicalKey,
      fingerprint: candidateFingerprint(descriptor),
      descriptor,
      sourceBlob: source.sha256,
    });
  }
  return { candidates, files, validManifests };
}

function isCandidateDescriptor(value: unknown): value is CandidateDescriptor {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const descriptor = value as Record<string, unknown>;
  if (descriptor.type === "package") {
    return (
      sameKeys(descriptor, PACKAGE_DESCRIPTOR_KEYS) &&
      typeof descriptor.manifestPath === "string" &&
      typeof descriptor.packagePath === "string" &&
      typeof descriptor.name === "string" &&
      ["node", "python", "go", "rust", "make"].includes(String(descriptor.ecosystem)) &&
      Array.isArray(descriptor.dependencies) &&
      descriptor.dependencies.every((item) => typeof item === "string")
    );
  }
  return (
    descriptor.type === "command" &&
    sameKeys(descriptor, COMMAND_DESCRIPTOR_KEYS) &&
    typeof descriptor.manifestPath === "string" &&
    typeof descriptor.cwd === "string" &&
    typeof descriptor.name === "string" &&
    typeof descriptor.command === "string"
  );
}

function normalizeStoredDescriptor(
  repoRoot: string,
  descriptor: CandidateDescriptor,
): CandidateDescriptor {
  try {
    return descriptor.type === "package"
      ? packageDescriptor(repoRoot, {
          id: "managed-validation",
          path: descriptor.packagePath,
          manifestPath: descriptor.manifestPath,
          name: descriptor.name,
          ecosystem: descriptor.ecosystem,
          dependencies: descriptor.dependencies,
          commandIds: [],
        })
      : commandDescriptor(repoRoot, {
          id: "managed-validation",
          source: descriptor.manifestPath,
          cwd: descriptor.cwd,
          name: descriptor.name,
          command: descriptor.command,
        });
  } catch {
    structuralError("metadata-normalization");
  }
}

function parseManagedMetadata(event: MemoryEvent): ManagedMetadata | null {
  const structured = event.structuredData;
  const hasMetadata = Object.prototype.hasOwnProperty.call(structured, REPO_MAP_MEMORY_DATA_KEY);
  const hasTag = event.tags.includes(REPO_MAP_MEMORY_TAG);
  if (!hasMetadata && !hasTag) return null;
  if (!hasMetadata || !hasTag || Object.keys(structured).length !== 1) {
    structuralError("reserved-marker");
  }
  const unknown = structured[REPO_MAP_MEMORY_DATA_KEY];
  if (!unknown || typeof unknown !== "object" || Array.isArray(unknown)) {
    structuralError("metadata-shape");
  }
  const metadata = unknown as Record<string, unknown>;
  if (!sameKeys(metadata, METADATA_KEYS)) structuralError("metadata-fields");
  if (
    metadata.generator !== REPO_MAP_MEMORY_GENERATOR ||
    metadata.version !== REPO_MAP_MEMORY_GENERATOR_VERSION ||
    typeof metadata.logicalKey !== "string" ||
    typeof metadata.candidateFingerprint !== "string" ||
    !["ADD", "SUPERSEDE", "RETRACT"].includes(String(metadata.action)) ||
    !(metadata.predecessorId === null || typeof metadata.predecessorId === "string") ||
    !isCandidateDescriptor(metadata.candidate)
  ) {
    structuralError("metadata-values");
  }
  return metadata as unknown as ManagedMetadata;
}

function titleAndBody(descriptor: CandidateDescriptor, action: RepoMemoryAction): {
  title: string;
  body: string;
} {
  const removed = action === "RETRACT";
  if (descriptor.type === "package") {
    return {
      title: `${removed ? "Removed" : "Observed"} package: ${descriptor.name}`,
      body: removed
        ? `The repository map no longer observes the ${descriptor.ecosystem} package declared by ${descriptor.manifestPath}.`
        : `The repository map observes ${descriptor.name} as a ${descriptor.ecosystem} package at ${descriptor.packagePath} with ${descriptor.dependencies.length} declared dependency names.`,
    };
  }
  return {
    title: `${removed ? "Removed" : "Observed"} command: ${descriptor.name}`,
    body: removed
      ? `The repository map no longer observes command ${descriptor.name} from ${descriptor.manifestPath}.`
      : `Run ${descriptor.command} from ${descriptor.cwd}; it is declared by ${descriptor.manifestPath}.`,
  };
}

function eventInput(
  metadata: ManagedMetadata,
  sourceBlob: string,
): NewMemoryEvent {
  const descriptor = metadata.candidate;
  const text = titleAndBody(descriptor, metadata.action);
  return {
    id: deterministicEventId(
      metadata.logicalKey,
      metadata.action,
      metadata.candidateFingerprint,
      metadata.predecessorId,
    ),
    kind: descriptor.type === "package" ? "fact" : "workflow",
    subjectType: descriptor.type === "package" ? "repo" : "command",
    title: text.title,
    body: text.body,
    structuredData: { [REPO_MAP_MEMORY_DATA_KEY]: metadata },
    status: metadata.action === "RETRACT" ? "retracted" : "active",
    appliesTo: [descriptor.manifestPath],
    sources: [{ path: descriptor.manifestPath, blob: sourceBlob }],
    provenance: { actor: REPO_MAP_MEMORY_GENERATOR, method: "observed" },
    authority: "tool",
    confidence: 1,
    importance: descriptor.type === "command" ? 0.7 : 0.6,
    sensitivity: "internal",
    supersedes: metadata.predecessorId ? [metadata.predecessorId] : [],
    tags: [REPO_MAP_MEMORY_TAG],
    triggers: [],
  };
}

function comparablePayload(event: MemoryEvent): Omit<MemoryEvent, "schemaVersion" | "id" | "createdAt" | "updatedAt"> {
  const { schemaVersion: _schemaVersion, id: _id, createdAt: _createdAt, updatedAt: _updatedAt, ...payload } = event;
  return payload;
}

function expectedComparable(input: NewMemoryEvent): ReturnType<typeof comparablePayload> {
  return {
    kind: input.kind,
    subjectType: input.subjectType,
    title: input.title,
    body: input.body,
    structuredData: input.structuredData ?? {},
    status: input.status ?? "active",
    appliesTo: input.appliesTo ?? [],
    sources: input.sources ?? [],
    provenance: input.provenance,
    authority: input.authority,
    confidence: input.confidence ?? 1,
    importance: input.importance ?? 0.5,
    sensitivity: input.sensitivity ?? "internal",
    supersedes: input.supersedes ?? [],
    tags: input.tags ?? [],
    triggers: input.triggers ?? [],
  };
}

function validateManagedEvent(
  repoRoot: string,
  event: MemoryEvent,
  metadata: ManagedMetadata,
): void {
  const normalizedCandidate = normalizeStoredDescriptor(repoRoot, metadata.candidate);
  if (canonicalJson(metadata.candidate) !== canonicalJson(normalizedCandidate)) {
    structuralError("metadata-normalization");
  }
  if (
    event.tags.length !== 1 ||
    event.tags[0] !== REPO_MAP_MEMORY_TAG ||
    metadata.logicalKey !== logicalKeyFor(normalizedCandidate) ||
    metadata.candidateFingerprint !== candidateFingerprint(normalizedCandidate) ||
    !SHA256_PATTERN.test(event.sources[0]?.blob ?? "") ||
    event.sources.length !== 1 ||
    event.sources[0]?.path !== metadata.candidate.manifestPath ||
    event.id !== deterministicEventId(
      metadata.logicalKey,
      metadata.action,
      metadata.candidateFingerprint,
      metadata.predecessorId,
    )
  ) {
    structuralError("payload-fingerprint");
  }
  const expected = expectedComparable(eventInput(metadata, event.sources[0].blob!));
  if (canonicalJson(comparablePayload(event)) !== canonicalJson(expected)) {
    structuralError("payload-contract");
  }
}

function managedLineages(
  repoRoot: string,
  events: readonly MemoryEvent[],
): Map<string, ManagedLineage> {
  const eventsById = new Map<string, MemoryEvent>();
  for (const event of events) {
    if (eventsById.has(event.id)) structuralError("duplicate-id");
    eventsById.set(event.id, event);
  }
  const records = new Map<string, ManagedRecord[]>();
  events.forEach((event, ledgerIndex) => {
    const metadata = parseManagedMetadata(event);
    if (!metadata) return;
    validateManagedEvent(repoRoot, event, metadata);
    const group = records.get(metadata.logicalKey) ?? [];
    group.push({ event, metadata, ledgerIndex });
    records.set(metadata.logicalKey, group);
  });

  const globallySuperseded = new Set(events.flatMap((event) => event.supersedes));
  const managedLogicalKeyById = new Map<string, string>();
  for (const [logicalKey, group] of records) {
    for (const record of group) managedLogicalKeyById.set(record.event.id, logicalKey);
  }
  const protectedLineages = new Set<string>();
  const unmanagedToolLineages = new Set<string>();
  const ancestryWork: Array<{ id: string; kind: 1 | 2 }> = [];
  for (const event of events) {
    if (globallySuperseded.has(event.id) || managedLogicalKeyById.has(event.id)) continue;
    const kind = event.authority === "tool" ? 1 : 2;
    for (const predecessorId of event.supersedes) {
      ancestryWork.push({ id: predecessorId, kind });
    }
  }
  const visitedAncestry = new Map<string, number>();
  while (ancestryWork.length > 0) {
    const current = ancestryWork.pop()!;
    const mask = visitedAncestry.get(current.id) ?? 0;
    if ((mask & current.kind) !== 0) continue;
    visitedAncestry.set(current.id, mask | current.kind);
    const managedKey = managedLogicalKeyById.get(current.id);
    if (managedKey) {
      (current.kind === 1 ? unmanagedToolLineages : protectedLineages).add(managedKey);
      // All older records behind a valid managed event belong to the same
      // linear lineage, so stop instead of retaining transitive key sets.
      continue;
    }
    const event = eventsById.get(current.id);
    if (!event) continue;
    for (const predecessorId of event.supersedes) {
      ancestryWork.push({ id: predecessorId, kind: current.kind });
    }
  }
  const lineages = new Map<string, ManagedLineage>();
  for (const [logicalKey, group] of records) {
    const byId = new Map(group.map((record) => [record.event.id, record]));
    const children = new Map<string, ManagedRecord[]>();
    const roots: ManagedRecord[] = [];
    for (const record of group) {
      const predecessorId = record.metadata.predecessorId;
      if (predecessorId === null) {
        if (record.metadata.action !== "ADD" || record.event.supersedes.length !== 0) {
          structuralError("root-action");
        }
        roots.push(record);
        continue;
      }
      if (record.metadata.action === "ADD") structuralError("successor-action");
      const predecessor = byId.get(predecessorId);
      if (!predecessor || predecessor.ledgerIndex >= record.ledgerIndex) {
        structuralError("orphan-predecessor");
      }
      if (
        record.event.supersedes.length !== 1 ||
        record.event.supersedes[0] !== predecessorId ||
        Date.parse(record.event.createdAt) < Date.parse(predecessor.event.createdAt)
      ) {
        structuralError("predecessor-contract");
      }
      if (
        record.metadata.action === "RETRACT" &&
        (predecessor.metadata.action === "RETRACT" ||
          canonicalJson(record.metadata.candidate) !== canonicalJson(predecessor.metadata.candidate))
      ) {
        structuralError("retraction-contract");
      }
      if (
        record.metadata.action === "SUPERSEDE" &&
        predecessor.metadata.action !== "RETRACT" &&
        record.metadata.candidateFingerprint === predecessor.metadata.candidateFingerprint
      ) {
        structuralError("redundant-successor");
      }
      const successors = children.get(predecessorId) ?? [];
      successors.push(record);
      children.set(predecessorId, successors);
    }
    if (roots.length !== 1 || [...children.values()].some((items) => items.length !== 1)) {
      structuralError("multiple-heads");
    }
    let head = roots[0]!;
    const visited = new Set<string>();
    while (true) {
      if (visited.has(head.event.id)) structuralError("supersession-cycle");
      visited.add(head.event.id);
      const next = children.get(head.event.id);
      if (!next) break;
      head = next[0]!;
    }
    if (visited.size !== group.length) structuralError("disconnected-lineage");

    if (unmanagedToolLineages.has(logicalKey)) {
      structuralError("unmanaged-tool-descendant");
    }
    lineages.set(logicalKey, {
      head,
      protectedByHigherAuthority: protectedLineages.has(logicalKey),
    });
  }
  return lineages;
}

function appendTime(options: ReconcileRepoMapMemoryOptions, predecessor: MemoryEvent | null): Date {
  const observed = (options.now ?? (() => new Date()))();
  const observedMillis = observed.getTime();
  if (!Number.isFinite(observedMillis)) structuralError("observation-clock");
  if (!predecessor) return observed;
  const minimum = Math.max(
    Date.parse(predecessor.createdAt),
    Date.parse(predecessor.updatedAt),
  );
  return new Date(Math.max(observedMillis, minimum));
}

function buildManagedEvent(
  repoRoot: string,
  candidate: Candidate,
  action: RepoMemoryAction,
  predecessor: MemoryEvent | null,
  options: ReconcileRepoMapMemoryOptions,
): MemoryEvent {
  const metadata: ManagedMetadata = {
    generator: REPO_MAP_MEMORY_GENERATOR,
    version: REPO_MAP_MEMORY_GENERATOR_VERSION,
    logicalKey: candidate.logicalKey,
    candidateFingerprint: candidate.fingerprint,
    action,
    predecessorId: predecessor?.id ?? null,
    candidate: candidate.descriptor,
  };
  const observedAt = appendTime(options, predecessor);
  return prepareMemoryEvent(repoRoot, eventInput(metadata, candidate.sourceBlob), {
    now: () => observedAt,
  });
}

async function confirmedRetractionBlob(
  repoRoot: string,
  record: ManagedRecord,
  files: ReadonlyMap<string, { sha256: string }>,
  validManifests: ReadonlySet<string>,
): Promise<string | null> {
  const manifestPath = record.metadata.candidate.manifestPath;
  const currentFile = files.get(manifestPath);
  if (currentFile) {
    return record.metadata.candidate.type === "command" && validManifests.has(manifestPath)
      ? currentFile.sha256
      : null;
  }
  const absolute = join(repoRoot, ...manifestPath.split("/"));
  assertSafeRepoPath(repoRoot, absolute);
  try {
    await lstat(absolute);
    return null;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") return null;
    return record.event.sources[0]?.blob ?? null;
  }
}

function measuredDuration(start: number, end: number): number {
  const duration = end - start;
  return Number.isFinite(duration) && duration >= 0 ? duration : 0;
}

export async function reconcileRepoMapMemories(
  repoRoot: string,
  map: RepoMap,
  snapshot: MemoryLedgerSnapshot,
  options: ReconcileRepoMapMemoryOptions = {},
): Promise<ReconcileRepoMapMemoryResult> {
  const clock = options.clock ?? (() => performance.now());
  const startedAt = clock();
  const derived = deriveCandidates(repoRoot, map);
  const lineages = managedLineages(repoRoot, snapshot.events);
  const reconciliation: RepoMemoryReconciliation = {
    candidates: derived.candidates.size,
    added: 0,
    noops: 0,
    superseded: 0,
    retracted: 0,
    deferred: 0,
    conflicts: 0,
    durationMs: 0,
  };
  const appended: MemoryEvent[] = [];
  const occupiedIds = new Set(snapshot.events.map((event) => event.id));
  const keys = new Set([...derived.candidates.keys(), ...lineages.keys()]);

  for (const logicalKey of [...keys].sort(compareText)) {
    const candidate = derived.candidates.get(logicalKey);
    const lineage = lineages.get(logicalKey);
    if (candidate) {
      if (lineage?.protectedByHigherAuthority) {
        reconciliation.conflicts += 1;
        continue;
      }
      if (!lineage) {
        const event = buildManagedEvent(repoRoot, candidate, "ADD", null, options);
        if (occupiedIds.has(event.id)) structuralError("reserved-id-collision");
        occupiedIds.add(event.id);
        appended.push(event);
        reconciliation.added += 1;
        continue;
      }
      if (
        lineage.head.metadata.action !== "RETRACT" &&
        lineage.head.metadata.candidateFingerprint === candidate.fingerprint
      ) {
        reconciliation.noops += 1;
        continue;
      }
      const event = buildManagedEvent(
        repoRoot,
        candidate,
        "SUPERSEDE",
        lineage.head.event,
        options,
      );
      if (occupiedIds.has(event.id)) structuralError("reserved-id-collision");
      occupiedIds.add(event.id);
      appended.push(event);
      reconciliation.superseded += 1;
      continue;
    }

    if (
      !lineage ||
      lineage.protectedByHigherAuthority ||
      lineage.head.metadata.action === "RETRACT"
    ) {
      continue;
    }
    if (!map.scan.complete) {
      reconciliation.deferred += 1;
      continue;
    }
    const sourceBlob = await confirmedRetractionBlob(
      repoRoot,
      lineage.head,
      derived.files,
      derived.validManifests,
    );
    if (!sourceBlob) {
      reconciliation.deferred += 1;
      continue;
    }
    const prior = lineage.head.metadata;
    const tombstone: Candidate = {
      logicalKey: prior.logicalKey,
      fingerprint: prior.candidateFingerprint,
      descriptor: prior.candidate,
      sourceBlob,
    };
    const event = buildManagedEvent(
      repoRoot,
      tombstone,
      "RETRACT",
      lineage.head.event,
      options,
    );
    if (occupiedIds.has(event.id)) structuralError("reserved-id-collision");
    occupiedIds.add(event.id);
    appended.push(event);
    reconciliation.retracted += 1;
  }

  const memory = appended.length
    ? extendMemoryLedgerSnapshot(repoRoot, snapshot, appended)
    : snapshot;
  reconciliation.durationMs = measuredDuration(startedAt, clock());
  return { memory, appended, reconciliation };
}
