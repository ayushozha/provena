import { randomUUID } from "node:crypto";
import { mkdir, open, readFile, stat } from "node:fs/promises";
import { dirname, isAbsolute, join } from "node:path";
import {
  MEMORY_KINDS,
  MEMORY_SUBJECT_TYPES,
  type MemoryEvent,
  type MemoryEventRecord,
  type MemorySource,
  type NewMemoryEvent,
} from "./types.js";
import { canonicalJson, compareText, normalizeRepoPath, sha256, stableId } from "./utils.js";
import { assertNoSecretMaterial } from "../security/memory.js";
import { withRepoMemoryLock } from "./lock.js";
import { assertSafeRepoPath } from "../security/paths.js";

export const MEMORY_LEDGER_PATH = ".provena/memory/events.jsonl";
const MAX_TITLE_CHARACTERS = 512;
const MAX_BODY_CHARACTERS = 50_000;
const MAX_STRUCTURED_DATA_CHARACTERS = 100_000;
const MAX_LIST_ENTRIES = 256;
const MAX_LEDGER_BYTES = 64 * 1024 * 1024;

export interface AppendMemoryOptions {
  now?: () => Date;
}

export interface MemoryLedgerSnapshot {
  events: MemoryEvent[];
  memoryFingerprint: string;
  bytes: number;
}

function requiredText(value: string, field: string, maxCharacters = MAX_BODY_CHARACTERS): string {
  if (typeof value !== "string") throw new Error(`${field} must be a string`);
  const trimmed = value.trim();
  if (!trimmed) throw new Error(`${field} must not be empty`);
  if (trimmed.length > maxCharacters) {
    throw new Error(`${field} must not exceed ${maxCharacters} characters`);
  }
  return trimmed;
}

function optionalText(
  value: string | undefined,
  field: string,
  maxCharacters = 2_048,
): string | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== "string") throw new Error(`${field} must be a string`);
  const trimmed = value.replace(/\s+/g, " ").trim();
  if (!trimmed) return undefined;
  if (trimmed.length > maxCharacters) {
    throw new Error(`${field} must not exceed ${maxCharacters} characters`);
  }
  return trimmed;
}

function weight(value: number | undefined, fallback: number, field: string): number {
  const result = value ?? fallback;
  if (!Number.isFinite(result) || result < 0 || result > 1) {
    throw new Error(`${field} must be between 0 and 1`);
  }
  return result;
}

function timestamp(value: string, field: string): string {
  if (typeof value !== "string") throw new Error(`${field} must be an ISO timestamp`);
  const millis = Date.parse(value);
  if (!Number.isFinite(millis)) throw new Error(`${field} must be an ISO timestamp`);
  const canonical = new Date(millis).toISOString();
  if (canonical !== value) throw new Error(`${field} must be a canonical UTC ISO timestamp`);
  return canonical;
}

function cleanStrings(values: string[] | undefined): string[] {
  if (values !== undefined && !Array.isArray(values)) throw new Error("memory list fields must be arrays");
  if ((values?.length ?? 0) > MAX_LIST_ENTRIES) {
    throw new Error(`memory list fields must not exceed ${MAX_LIST_ENTRIES} entries`);
  }
  const cleaned = (values ?? []).map((value) => {
    if (typeof value !== "string") throw new Error("memory list entries must be strings");
    return value.replace(/\s+/g, " ").trim();
  });
  return [...new Set(cleaned.filter(Boolean))].sort(
    compareText,
  );
}

function cleanAppliesTo(repoRoot: string, values: string[] | undefined): string[] {
  return cleanStrings(values).map((value) => {
    if (isAbsolute(value)) throw new Error("appliesTo paths must be repository-relative");
    return normalizeRepoPath(repoRoot, value);
  }).sort(compareText);
}

function cleanSources(repoRoot: string, sources: MemorySource[] | undefined): MemorySource[] {
  if (sources !== undefined && !Array.isArray(sources)) throw new Error("sources must be an array");
  if ((sources?.length ?? 0) > MAX_LIST_ENTRIES) {
    throw new Error(`sources must not exceed ${MAX_LIST_ENTRIES} entries`);
  }
  return (sources ?? [])
    .map((source) => {
      if (!source || typeof source !== "object" || typeof source.path !== "string") {
        throw new Error("each memory source requires a repository-relative path");
      }
      if (!source.path.trim() || isAbsolute(source.path)) {
        throw new Error("memory source paths must be repository-relative");
      }
      const path = normalizeRepoPath(repoRoot, source.path);
      if (path === ".") throw new Error("memory source path must identify a file");
      if (source.startLine !== undefined && (!Number.isInteger(source.startLine) || source.startLine < 1)) {
        throw new Error("memory source startLine must be a positive integer");
      }
      if (source.endLine !== undefined && (!Number.isInteger(source.endLine) || source.endLine < 1)) {
        throw new Error("memory source endLine must be a positive integer");
      }
      if (
        source.startLine !== undefined &&
        source.endLine !== undefined &&
        source.endLine < source.startLine
      ) {
        throw new Error("memory source endLine must not precede startLine");
      }
      const symbol = optionalText(source.symbol, "memory source symbol");
      const blob = optionalText(source.blob, "memory source blob");
      const commit = optionalText(source.commit, "memory source commit");
      return {
        path,
        ...(symbol ? { symbol } : {}),
        ...(source.startLine !== undefined ? { startLine: source.startLine } : {}),
        ...(source.endLine !== undefined ? { endLine: source.endLine } : {}),
        ...(blob ? { blob } : {}),
        ...(commit ? { commit } : {}),
      };
    })
    .sort(
      (a, b) =>
        compareText(a.path, b.path) ||
        (a.startLine ?? 0) - (b.startLine ?? 0) ||
        compareText(a.symbol ?? "", b.symbol ?? ""),
    );
}

function validateExplicitClaims(input: NewMemoryEvent): void {
  const hasRationale = Object.prototype.hasOwnProperty.call(
    input.structuredData ?? {},
    "rationale",
  );
  if (
    (input.kind === "decision" || input.kind === "preference" || hasRationale) &&
    input.provenance.method !== "explicit"
  ) {
    throw new Error(
      `${input.kind} and rationale memories must be explicitly recorded, never inferred`,
    );
  }
  if (
    (input.kind === "decision" || input.kind === "preference") &&
    input.authority !== "human" &&
    input.authority !== "agent"
  ) {
    throw new Error(`${input.kind} memories require human or agent authority`);
  }
}

function buildEvent(
  repoRoot: string,
  input: NewMemoryEvent,
  now: () => Date,
): MemoryEvent {
  if (!MEMORY_KINDS.includes(input.kind)) throw new Error(`unknown memory kind: ${input.kind}`);
  if (!MEMORY_SUBJECT_TYPES.includes(input.subjectType)) {
    throw new Error(`unknown memory subject type: ${input.subjectType}`);
  }
  if (!input.provenance || typeof input.provenance !== "object") {
    throw new Error("provenance is required");
  }
  validateExplicitClaims(input);
  const current = now().toISOString();
  const createdAt = timestamp(input.createdAt ?? current, "createdAt");
  const updatedAt = timestamp(input.updatedAt ?? createdAt, "updatedAt");
  if (Date.parse(updatedAt) < Date.parse(createdAt)) {
    throw new Error("updatedAt must not precede createdAt");
  }
  const id = input.id?.trim() || stableId("memory", createdAt, randomUUID());
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$/.test(id)) {
    throw new Error("memory id contains unsupported characters");
  }
  const status = input.status ?? "active";
  if (!["active", "superseded", "retracted"].includes(status)) {
    throw new Error(`unknown memory status: ${status}`);
  }
  const sensitivity = input.sensitivity ?? "internal";
  if (!["public", "internal", "confidential", "restricted"].includes(sensitivity)) {
    throw new Error(`unknown memory sensitivity: ${sensitivity}`);
  }
  if (!["human", "agent", "tool", "system"].includes(input.authority)) {
    throw new Error(`unknown memory authority: ${input.authority}`);
  }
  const actor = requiredText(input.provenance.actor, "provenance.actor", MAX_TITLE_CHARACTERS);
  if (!["explicit", "observed", "imported"].includes(input.provenance.method)) {
    throw new Error(`unknown provenance method: ${input.provenance.method}`);
  }
  const supersedes = cleanStrings(input.supersedes);
  if (supersedes.includes(id)) throw new Error("a memory event cannot supersede itself");
  if (
    input.structuredData === null ||
    Array.isArray(input.structuredData) ||
    (input.structuredData !== undefined && typeof input.structuredData !== "object")
  ) {
    throw new Error("structuredData must be an object");
  }
  if (JSON.stringify(input.structuredData ?? {}).length > MAX_STRUCTURED_DATA_CHARACTERS) {
    throw new Error(
      `structuredData must not exceed ${MAX_STRUCTURED_DATA_CHARACTERS} serialized characters`,
    );
  }
  const agent = optionalText(input.provenance.agent, "provenance.agent");
  const sessionId = optionalText(input.provenance.sessionId, "provenance.sessionId");
  const command = optionalText(input.provenance.command, "provenance.command", 8_192);
  return {
    schemaVersion: 1,
    id,
    kind: input.kind,
    subjectType: input.subjectType,
    title: requiredText(input.title, "title", MAX_TITLE_CHARACTERS),
    body: requiredText(input.body, "body", MAX_BODY_CHARACTERS),
    structuredData: input.structuredData ?? {},
    status,
    appliesTo: cleanAppliesTo(repoRoot, input.appliesTo),
    sources: cleanSources(repoRoot, input.sources),
    provenance: {
      actor,
      method: input.provenance.method,
      ...(agent ? { agent } : {}),
      ...(sessionId ? { sessionId } : {}),
      ...(command ? { command } : {}),
    },
    authority: input.authority,
    confidence: weight(input.confidence, 1, "confidence"),
    importance: weight(input.importance, 0.5, "importance"),
    sensitivity,
    createdAt,
    updatedAt,
    supersedes,
    tags: cleanStrings(input.tags),
    triggers: cleanStrings(input.triggers),
  };
}

export function memoryEventToRecord(event: MemoryEvent): MemoryEventRecord {
  return {
    schema_version: 1,
    id: event.id,
    kind: event.kind,
    subject_type: event.subjectType,
    title: event.title,
    body: event.body,
    structured_data: event.structuredData,
    status: event.status,
    applies_to: event.appliesTo,
    sources: event.sources.map((source) => ({
      path: source.path,
      ...(source.symbol ? { symbol: source.symbol } : {}),
      ...(source.startLine !== undefined ? { start_line: source.startLine } : {}),
      ...(source.endLine !== undefined ? { end_line: source.endLine } : {}),
      ...(source.blob ? { blob: source.blob } : {}),
      ...(source.commit ? { commit: source.commit } : {}),
    })),
    provenance: {
      actor: event.provenance.actor,
      method: event.provenance.method,
      ...(event.provenance.agent ? { agent: event.provenance.agent } : {}),
      ...(event.provenance.sessionId ? { session_id: event.provenance.sessionId } : {}),
      ...(event.provenance.command ? { command: event.provenance.command } : {}),
    },
    authority: event.authority,
    confidence: event.confidence,
    importance: event.importance,
    sensitivity: event.sensitivity,
    created_at: event.createdAt,
    updated_at: event.updatedAt,
    supersedes: event.supersedes,
    tags: event.tags,
    triggers: event.triggers,
  };
}

function recordToInput(record: MemoryEventRecord): NewMemoryEvent {
  return {
    id: record.id,
    kind: record.kind,
    subjectType: record.subject_type,
    title: record.title,
    body: record.body,
    structuredData: record.structured_data,
    status: record.status,
    appliesTo: record.applies_to,
    sources: record.sources.map((source) => ({
      path: source.path,
      ...(source.symbol ? { symbol: source.symbol } : {}),
      ...(source.start_line !== undefined ? { startLine: source.start_line } : {}),
      ...(source.end_line !== undefined ? { endLine: source.end_line } : {}),
      ...(source.blob ? { blob: source.blob } : {}),
      ...(source.commit ? { commit: source.commit } : {}),
    })),
    provenance: {
      actor: record.provenance.actor,
      method: record.provenance.method,
      ...(record.provenance.agent ? { agent: record.provenance.agent } : {}),
      ...(record.provenance.session_id ? { sessionId: record.provenance.session_id } : {}),
      ...(record.provenance.command ? { command: record.provenance.command } : {}),
    },
    authority: record.authority,
    confidence: record.confidence,
    importance: record.importance,
    sensitivity: record.sensitivity,
    createdAt: record.created_at,
    updatedAt: record.updated_at,
    supersedes: record.supersedes,
    tags: record.tags,
    triggers: record.triggers,
  };
}

function isMemoryEventRecord(value: unknown): value is MemoryEventRecord {
  if (!value || typeof value !== "object") return false;
  const event = value as Partial<MemoryEventRecord>;
  return (
    event.schema_version === 1 &&
    typeof event.id === "string" &&
    MEMORY_KINDS.includes(event.kind as MemoryEventRecord["kind"]) &&
    MEMORY_SUBJECT_TYPES.includes(event.subject_type as MemoryEventRecord["subject_type"]) &&
    typeof event.title === "string" &&
    typeof event.body === "string" &&
    Array.isArray(event.sources) &&
    Array.isArray(event.applies_to) &&
    Array.isArray(event.supersedes) &&
    Array.isArray(event.tags) &&
    Array.isArray(event.triggers) &&
    Boolean(event.provenance) &&
    typeof event.provenance === "object" &&
    typeof event.provenance.actor === "string" &&
    typeof event.provenance.method === "string" &&
    typeof event.authority === "string" &&
    typeof event.confidence === "number" &&
    typeof event.importance === "number" &&
    typeof event.sensitivity === "string" &&
    typeof event.structured_data === "object" &&
    event.structured_data !== null &&
    !Array.isArray(event.structured_data) &&
    typeof event.created_at === "string" &&
    typeof event.updated_at === "string"
  );
}

export async function readMemoryLedgerSnapshot(
  repoRoot: string,
): Promise<MemoryLedgerSnapshot> {
  const ledgerPath = join(repoRoot, ...MEMORY_LEDGER_PATH.split("/"));
  assertSafeRepoPath(repoRoot, ledgerPath);
  let ledger: Buffer;
  try {
    const info = await stat(ledgerPath);
    if (info.size > MAX_LEDGER_BYTES) {
      throw new Error(`${MEMORY_LEDGER_PATH} exceeds the ${MAX_LEDGER_BYTES} byte safety cap`);
    }
    ledger = await readFile(ledgerPath);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    ledger = Buffer.alloc(0);
  }
  if (ledger.byteLength > MAX_LEDGER_BYTES) {
    throw new Error(`${MEMORY_LEDGER_PATH} exceeds the ${MAX_LEDGER_BYTES} byte safety cap`);
  }
  const text = ledger.toString("utf8");
  const events: MemoryEvent[] = [];
  const ids = new Set<string>();
  for (const [index, line] of text.split(/\r?\n/).entries()) {
    if (!line.trim()) continue;
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      throw new Error(`${MEMORY_LEDGER_PATH}:${index + 1}: invalid JSON`);
    }
    if (!isMemoryEventRecord(parsed)) {
      throw new Error(`${MEMORY_LEDGER_PATH}:${index + 1}: invalid memory event`);
    }
    let validated: MemoryEvent;
    try {
      validated = buildEvent(
        repoRoot,
        recordToInput(parsed),
        () => new Date(parsed.created_at),
      );
      if (canonicalJson(memoryEventToRecord(validated)) !== canonicalJson(parsed)) {
        throw new Error("memory event is not canonical or contains unsupported fields");
      }
    } catch (error) {
      throw new Error(
        `${MEMORY_LEDGER_PATH}:${index + 1}: ${(error as Error).message}`,
      );
    }
    if (ids.has(validated.id)) {
      throw new Error(`${MEMORY_LEDGER_PATH}:${index + 1}: duplicate memory id ${validated.id}`);
    }
    ids.add(validated.id);
    events.push(validated);
  }
  return {
    events,
    memoryFingerprint: sha256(ledger),
    bytes: ledger.byteLength,
  };
}

export async function readMemoryEvents(repoRoot: string): Promise<MemoryEvent[]> {
  return (await readMemoryLedgerSnapshot(repoRoot)).events;
}

export async function appendMemoryEvent(
  repoRoot: string,
  input: NewMemoryEvent,
  options: AppendMemoryOptions = {},
): Promise<MemoryEvent> {
  const event = buildEvent(repoRoot, input, options.now ?? (() => new Date()));
  const serializedEvent = canonicalJson(memoryEventToRecord(event));
  assertNoSecretMaterial(serializedEvent);
  if (["confidential", "restricted"].includes(event.sensitivity)) {
    throw new Error(
      `${event.sensitivity} memory cannot be written to the Git-tracked repo ledger; use the governed store`,
    );
  }
  return withRepoMemoryLock(repoRoot, async () => {
    const existing = await readMemoryEvents(repoRoot);
    if (existing.some((item) => item.id === event.id)) {
      throw new Error(`memory id already exists: ${event.id}`);
    }
    const authorityRank = { tool: 1, agent: 2, system: 3, human: 4 } as const;
    for (const supersededId of event.supersedes) {
      const superseded = existing.find((item) => item.id === supersededId);
      if (!superseded) {
        throw new Error(`cannot supersede unknown memory id: ${supersededId}`);
      }
      if (authorityRank[event.authority] < authorityRank[superseded.authority]) {
        throw new Error(
          `${event.authority} memory cannot supersede ${superseded.authority} memory ${supersededId}`,
        );
      }
    }
    const ledgerPath = join(repoRoot, ...MEMORY_LEDGER_PATH.split("/"));
    assertSafeRepoPath(repoRoot, dirname(ledgerPath));
    assertSafeRepoPath(repoRoot, ledgerPath);
    await mkdir(dirname(ledgerPath), { recursive: true });
    assertSafeRepoPath(repoRoot, ledgerPath);
    const currentBytes = await stat(ledgerPath)
      .then((info) => info.size)
      .catch((error: NodeJS.ErrnoException) => {
        if (error.code === "ENOENT") return 0;
        throw error;
      });
    const eventBytes = Buffer.byteLength(serializedEvent, "utf8");
    if (currentBytes + eventBytes > MAX_LEDGER_BYTES) {
      throw new Error(`${MEMORY_LEDGER_PATH} would exceed the ${MAX_LEDGER_BYTES} byte safety cap`);
    }
    const handle = await open(ledgerPath, "a");
    try {
      await handle.write(serializedEvent);
      await handle.sync();
    } finally {
      await handle.close();
    }
    return event;
  });
}

export function activeMemoryEvents(events: MemoryEvent[]): MemoryEvent[] {
  const superseded = new Set(events.flatMap((event) => event.supersedes));
  return events.filter(
    (event) => event.status === "active" && !superseded.has(event.id),
  );
}
