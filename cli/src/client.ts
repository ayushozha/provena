import { assertLoopbackStoreUrl, type ProvenaScope } from "./config.js";
import {
  CAPTURE_ABSTRACTION_LIMITS, assertCaptureAbstractionCredentialFree, captureAbstractionInputSchema, validateCaptureAbstraction,
  type CaptureAbstractionInput, type CaptureAbstractionResponse,
} from "./capture/abstraction.js";
import { canonicalJson } from "./brain/utils.js";
import { assertNoSecretMaterial } from "./security/memory.js";

/** Store API scope envelope (matches app.models.ScopeEnvelope). */
export interface ScopeEnvelope {
  tenant_id: string;
  workspace_id?: string;
  project_id?: string;
  user_id?: string;
  agent_id?: string;
  session_id?: string;
}

export interface SourceReference {
  source_type: string;
  source_id: string;
  uri?: string;
  title?: string;
  excerpt?: string;
  span_start?: number;
  span_end?: number;
  metadata?: Record<string, unknown>;
}

export type MemoryKind =
  | "fact"
  | "episode"
  | "artifact"
  | "decision"
  | "relation"
  | "preference"
  | "instruction"
  | "metric"
  | "stakeholder"
  | "source"
  | "workflow"
  | "mistake"
  | "handoff"
  | "invariant";

export interface MemoryCreate {
  memory_id?: string;
  kind: MemoryKind;
  scope: ScopeEnvelope;
  content: string;
  title?: string;
  summary?: string;
  entity_keys?: string[];
  tags?: string[];
  metadata?: Record<string, unknown>;
  source_references?: SourceReference[];
}

export interface MemoryRecord {
  memory_id: string;
  fingerprint: string;
  kind: MemoryKind;
  title: string | null;
  content: string;
  entity_keys: string[];
  tags: string[];
  source_references: SourceReference[];
}

export interface MemoryWriteResult {
  created: boolean;
  memory: MemoryRecord;
}

export type RelationKind =
  | "related_to"
  | "supports"
  | "derived_from"
  | "defined_in"
  | "supersedes"
  | "conflicts_with";

export interface RelationWrite {
  from_memory_id: string;
  to_memory_id: string;
  relation: RelationKind;
  scope: ScopeEnvelope;
}

export interface RelatedMemory {
  relation: RelationKind;
  memory: MemoryRecord;
}

export interface SearchRequest {
  query: string;
  scope: ScopeEnvelope;
  limit?: number;
  tags?: string[];
  entity_keys?: string[];
  include_relations?: boolean;
}

export interface SearchResult {
  memory: MemoryRecord;
  score: number;
  reasons: string[];
  related_memories?: RelatedMemory[];
}

export interface SearchResponse {
  results: SearchResult[];
}

export interface SearchExplainCandidate {
  memory: MemoryRecord;
  score: number;
  reasons: string[];
  rejection_reasons?: string[];
  fts_rank?: number | null;
  rank?: number | null;
}

export interface SearchExplainResponse {
  query: string;
  query_terms: string[];
  candidate_strategy: string;
  total_candidates: number;
  returned: SearchExplainCandidate[];
  not_returned?: SearchExplainCandidate[];
  filtered_out?: SearchExplainCandidate[];
}

export interface PipelineWriteResult {
  created: boolean;
  memory: MemoryRecord;
}

export interface RepositoryMemorySyncRequest {
  schema_version: 1;
  scope: ScopeEnvelope;
  ledger_path: ".provena/memory/events.jsonl";
  memory_fingerprint: string;
  ledger_bytes: number;
  ledger: string;
}

export interface RepositoryMemorySyncTimings {
  validate: number;
  write: number;
  relations: number;
  total: number;
}

export interface RepositoryMemorySyncResponse {
  schema_version: 1;
  repository_id: string;
  ledger_fingerprint: string;
  events_fingerprint: string;
  received_events: number;
  created_memories: number;
  unchanged_memories: number;
  repaired_memories: number;
  suppressed_events: number;
  status_updates: number;
  created_relations: number;
  repaired_relations: number;
  unchanged_relations: number;
  suppressed_relations: number;
  checkpoint_updated: boolean;
  no_op: boolean;
  duration_ms: number;
  timings_ms: RepositoryMemorySyncTimings;
}

export class RepositoryMemorySyncTransportError extends Error {
  constructor(path: string, cause: unknown, credential?: string) {
    const detail = cause instanceof Error ? cause.message : String(cause);
    // Do not retain the original error as `cause`: fetch implementations can
    // include request headers or URLs in it, which would make a bearer token
    // visible through structured error inspection even when `message` is safe.
    super(`POST ${path} transport failed: ${redactCredential(detail, credential)}`);
    this.name = "RepositoryMemorySyncTransportError";
  }
}

export interface ProvenaClientOptions {
  storeUrl: string;
  intelligenceUrl?: string;
  /** Optional bearer credential supplied at runtime; never persisted by the client. */
  apiKey?: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

const DEFAULT_SEARCH_LIMIT = 5;
class CaptureAbstractionError extends Error {}

export class ProvenaClient {
  private readonly baseUrl: string;
  private readonly intelligenceUrl: string | undefined;
  private readonly apiKey: string | undefined;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;

  constructor(options: ProvenaClientOptions) {
    this.baseUrl = options.storeUrl.replace(/\/$/, "");
    this.intelligenceUrl = options.intelligenceUrl?.replace(/\/$/, "");
    this.apiKey = options.apiKey;
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.timeoutMs = options.timeoutMs ?? 30_000;
  }

  private scopedHeaders(scope: ScopeEnvelope): Record<string, string> {
    return {
      "X-Provena-Tenant-Id": scope.tenant_id,
      "X-Provena-Principal-Id": "provena-cli",
      "X-Provena-Role": "editor",
      ...(this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : {}),
    };
  }

  /** Explicit review-only abstraction; credentials and remote response errors never enter local drafts. */
  async abstractCapturedProcedure(input: CaptureAbstractionInput, scope: ScopeEnvelope): Promise<CaptureAbstractionResponse> {
    if (!this.intelligenceUrl) throw new Error("capture abstraction requires config.intelligence_url");
    try { assertLoopbackStoreUrl(this.intelligenceUrl); }
    catch { throw new Error("capture abstraction intelligence URL must be an authorized HTTP(S) origin without credentials"); }
    const parsed = captureAbstractionInputSchema.safeParse(input);
    if (!parsed.success) throw new Error("invalid capture abstraction input");
    const body = canonicalJson(parsed.data).slice(0, -1);
    assertNoSecretMaterial(body);
    assertCaptureAbstractionCredentialFree(parsed.data, this.apiKey);
    if (Buffer.byteLength(body) > CAPTURE_ABSTRACTION_LIMITS.requestBytes) throw new Error("capture abstraction request exceeds its byte limit; select fewer calls");
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const deadline = new Promise<never>((_, reject) => {
      timer = setTimeout(() => { controller.abort(); reject(new CaptureAbstractionError("capture abstraction deadline exceeded")); }, this.timeoutMs);
    });
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      const response = await Promise.race([this.fetchImpl(`${this.intelligenceUrl}/v1/procedures/abstract`, {
        method: "POST", headers: { "Content-Type": "application/json", ...this.scopedHeaders(scope) },
        body, redirect: "error", signal: controller.signal,
      }), deadline]);
      if (response.redirected || response.status >= 300 && response.status < 400) throw new CaptureAbstractionError("capture abstraction redirects are not accepted");
      if (!response.ok) throw new CaptureAbstractionError(`capture abstraction request failed (HTTP ${response.status})`);
      if ((response.headers.get("content-encoding") ?? "identity").toLowerCase() !== "identity") throw new CaptureAbstractionError("capture abstraction compressed responses are not accepted");
      if (!/^application\/(?:json|[A-Za-z0-9!#$&^_.+-]+\+json)(?:\s*;|$)/i.test(response.headers.get("content-type") ?? "")) throw new CaptureAbstractionError("capture abstraction requires a JSON response");
      const declared = Number(response.headers.get("content-length"));
      if (Number.isFinite(declared) && declared > CAPTURE_ABSTRACTION_LIMITS.responseBytes) throw new CaptureAbstractionError("capture abstraction response exceeds its byte limit");
      if (!response.body) throw new CaptureAbstractionError("capture abstraction response has no body");
      reader = response.body.getReader();
      let size = 0;
      const chunks: Uint8Array[] = [];
      for (;;) {
        const item = await Promise.race([reader.read(), deadline]);
        if (item.done) break;
        size += item.value.byteLength;
        if (size > CAPTURE_ABSTRACTION_LIMITS.responseBytes) throw new CaptureAbstractionError("capture abstraction response exceeds its byte limit");
        chunks.push(item.value);
      }
      let value: unknown;
      try {
        const text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks, size));
        value = JSON.parse(text);
      } catch { throw new CaptureAbstractionError("invalid capture abstraction JSON response"); }
      try {
        const result = validateCaptureAbstraction(parsed.data, value);
        assertCaptureAbstractionCredentialFree(result, this.apiKey);
        return result;
      }
      catch { throw new CaptureAbstractionError("invalid capture abstraction grounded response"); }
    } catch (error) {
      // Never retain a cause or echo URLs, headers, service bodies, or credentials.
      if (error instanceof CaptureAbstractionError) throw new Error(error.message);
      throw new Error("capture abstraction unavailable or rejected; no draft was written");
    } finally {
      clearTimeout(timer!);
      controller.abort();
      void reader?.cancel().catch(() => {});
    }
  }

  async createMemory(payload: MemoryCreate): Promise<MemoryWriteResult> {
    // Generated code memories already have deterministic semantic boundaries.
    // Sending them through fact extraction can split one indexed chunk into
    // several records and destroy the source-identity contract.
    const generatedFingerprint = payload.metadata?.provena_generated_fingerprint;
    const isGeneratedMemory =
      typeof generatedFingerprint === "string" && /^[0-9a-f]{64}$/.test(generatedFingerprint);
    if (this.intelligenceUrl && !isGeneratedMemory) {
      return this.pipelineWrite(payload);
    }
    return this.postJson<MemoryWriteResult>("/v1/memories", payload);
  }

  async syncRepositoryMemoryEvents(
    repositoryId: string,
    payload: RepositoryMemorySyncRequest,
    expectedEventCount: number,
    expectedRelationCount: number,
  ): Promise<RepositoryMemorySyncResponse> {
    const path = `/v1/repositories/${encodeURIComponent(repositoryId)}/memory-events/sync`;
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...this.scopedHeaders(payload.scope),
        },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (error) {
      throw new RepositoryMemorySyncTransportError(path, error, this.apiKey);
    }
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(
        `POST ${path} failed (${response.status}): ${redactCredential(detail.slice(0, 500), this.apiKey)}`,
      );
    }
    let body: unknown;
    try {
      body = await response.json();
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      throw new Error(`invalid repository memory sync response: invalid JSON (${detail})`);
    }
    return validateRepositoryMemorySyncResponse(
      body,
      repositoryId,
      payload.memory_fingerprint,
      expectedEventCount,
      expectedRelationCount,
    );
  }

  /** Write via intelligence pipeline for embeddings (PLAN-09). */
  async pipelineWrite(payload: MemoryCreate): Promise<MemoryWriteResult> {
    const base = this.intelligenceUrl ?? this.baseUrl;
    const response = await this.fetchImpl(`${base}/v1/pipeline/write`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...this.scopedHeaders(payload.scope),
      },
      body: JSON.stringify({
        kind: payload.kind,
        scope: payload.scope,
        content: payload.content,
        title: payload.title ?? "",
        summary: payload.summary ?? "",
        entity_keys: payload.entity_keys ?? [],
        tags: payload.tags ?? [],
        metadata: payload.metadata ?? {},
        source_references: payload.source_references ?? [],
      }),
      signal: AbortSignal.timeout(this.timeoutMs),
    });

    if (!response.ok) {
      const detail = await response.text();
      throw new Error(
        `POST /v1/pipeline/write failed (${response.status}): ${redactCredential(detail.slice(0, 500), this.apiKey)}`,
      );
    }

    const body = (await response.json()) as {
      created?: boolean;
      memory?: MemoryRecord;
    };
    if (!body.memory?.memory_id) {
      throw new Error("pipeline write response missing memory");
    }
    return { created: body.created ?? true, memory: body.memory };
  }

  async searchMemories(payload: SearchRequest): Promise<SearchResponse> {
    return this.postJson<SearchResponse>("/v1/memories/search", payload);
  }

  /** Search via intelligence pipeline when `intelligenceUrl` is configured (PLAN-10). */
  async pipelineSearch(payload: SearchRequest): Promise<SearchResponse> {
    const base = this.intelligenceUrl ?? this.baseUrl;
    const response = await this.fetchImpl(`${base}/v1/pipeline/search`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...this.scopedHeaders(payload.scope),
      },
      body: JSON.stringify({
        query: payload.query,
        scope: payload.scope,
        limit: payload.limit ?? DEFAULT_SEARCH_LIMIT,
        tags: payload.tags ?? [],
        entity_keys: payload.entity_keys ?? [],
        include_relations: payload.include_relations ?? false,
      }),
      signal: AbortSignal.timeout(this.timeoutMs),
    });

    if (!response.ok) {
      const detail = await response.text();
      throw new Error(
        `POST /v1/pipeline/search failed (${response.status}): ${redactCredential(detail.slice(0, 500), this.apiKey)}`,
      );
    }

    const body = (await response.json()) as SearchResponse;
    return normalizeSearchResponse(body);
  }

  /** Route to pipeline or direct store search per config (PLAN-10). */
  async search(payload: SearchRequest): Promise<SearchResponse> {
    if (this.intelligenceUrl) {
      return this.pipelineSearch(payload);
    }
    return this.searchMemories(payload);
  }

  /**
   * Optional explain payload from the store admin endpoint.
   * Returns null when the endpoint is not exposed (404).
   */
  async explainSearch(payload: SearchRequest): Promise<SearchExplainResponse | null> {
    const response = await this.fetchImpl(
      `${this.baseUrl}/v1/admin/memories/search/explain`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(this.timeoutMs),
      },
    );

    if (response.status === 404) {
      return null;
    }

    if (!response.ok) {
      const detail = await response.text();
      throw new Error(
        `POST /v1/admin/memories/search/explain failed (${response.status}): ${detail.slice(0, 500)}`,
      );
    }

    return (await response.json()) as SearchExplainResponse;
  }

  async createRelation(payload: RelationWrite): Promise<void> {
    await this.postJsonNoBody("/v1/memories/relations", payload);
  }

  async upsertEntitiesBatch(payload: {
    scope: ScopeEnvelope;
    entities: Array<{
      canonical_name: string;
      entity_type: string;
      aliases: string[];
    }>;
  }): Promise<{ upserted: number }> {
    return this.postJson<{ upserted: number }>("/v1/admin/entities/batch", payload);
  }

  async getMemory(memoryId: string): Promise<MemoryRecord> {
    const response = await this.fetchImpl(
      `${this.baseUrl}/v1/memories/${encodeURIComponent(memoryId)}`,
      { signal: AbortSignal.timeout(this.timeoutMs) },
    );
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(
        `GET /v1/memories/${memoryId} failed (${response.status}): ${detail.slice(0, 500)}`,
      );
    }
    return (await response.json()) as MemoryRecord;
  }

  async deleteMemory(memoryId: string, hardDelete = false): Promise<boolean> {
    const query = hardDelete ? "?hard_delete=true" : "";
    const response = await this.fetchImpl(
      `${this.baseUrl}/v1/memories/${encodeURIComponent(memoryId)}${query}`,
      {
        method: "DELETE",
        signal: AbortSignal.timeout(this.timeoutMs),
      },
    );
    if (response.status === 404) {
      return false;
    }
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(
        `DELETE /v1/memories/${memoryId} failed (${response.status}): ${detail.slice(0, 500)}`,
      );
    }
    const body = (await response.json()) as { deleted?: boolean };
    return body.deleted === true;
  }

  async healthz(): Promise<boolean> {
    try {
      const response = await this.fetchImpl(`${this.baseUrl}/healthz`, {
        signal: AbortSignal.timeout(this.timeoutMs),
      });
      if (!response.ok) {
        return false;
      }
      const body = (await response.json()) as { status?: string };
      return body.status === "ok";
    } catch {
      return false;
    }
  }

  private async postJson<T>(path: string, body: unknown): Promise<T> {
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeoutMs),
    });

    if (!response.ok) {
      const detail = await response.text();
      throw new Error(
        `POST ${path} failed (${response.status}): ${detail.slice(0, 500)}`,
      );
    }

    return (await response.json()) as T;
  }

  private async postJsonNoBody(path: string, body: unknown): Promise<void> {
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeoutMs),
    });

    if (!response.ok) {
      const detail = await response.text();
      throw new Error(
        `POST ${path} failed (${response.status}): ${detail.slice(0, 500)}`,
      );
    }
  }
}

export function scopeEnvelopeFromConfig(scope: ProvenaScope): ScopeEnvelope {
  return {
    tenant_id: scope.tenant_id,
    project_id: scope.project_id,
  };
}

export function readStoreApiKey(environmentVariable: string): string | undefined {
  return process.env[environmentVariable]?.trim() || undefined;
}

function validateRepositoryMemorySyncResponse(
  value: unknown,
  repositoryId: string,
  ledgerFingerprint: string,
  expectedEventCount: number,
  expectedRelationCount: number,
): RepositoryMemorySyncResponse {
  const invalid = (detail: string): never => {
    throw new Error(`invalid repository memory sync response: ${detail}`);
  };
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return invalid("expected an object");
  }
  const body = value as Record<string, unknown>;
  if (body.schema_version !== 1) invalid("schema_version must be 1");
  if (body.repository_id !== repositoryId) invalid("repository_id does not match the request");
  if (body.ledger_fingerprint !== ledgerFingerprint) {
    invalid("ledger_fingerprint does not match the exact local ledger");
  }
  if (typeof body.events_fingerprint !== "string" || !/^[0-9a-f]{64}$/.test(body.events_fingerprint)) {
    invalid("events_fingerprint must be a lowercase SHA-256");
  }

  const counters = [
    "received_events",
    "created_memories",
    "unchanged_memories",
    "repaired_memories",
    "suppressed_events",
    "status_updates",
    "created_relations",
    "repaired_relations",
    "unchanged_relations",
    "suppressed_relations",
  ] as const;
  for (const field of counters) {
    const current = body[field];
    if (typeof current !== "number" || !Number.isSafeInteger(current) || current < 0) {
      invalid(`${field} must be a non-negative safe integer`);
    }
  }
  if (body.received_events !== expectedEventCount) {
    invalid("received_events does not match the validated local event count");
  }
  if (
    (body.created_memories as number) +
      (body.unchanged_memories as number) +
      (body.repaired_memories as number) +
      (body.suppressed_events as number) !==
    body.received_events
  ) {
    invalid("memory projection counters must equal received_events");
  }
  if (
    (body.created_relations as number) +
      (body.repaired_relations as number) +
      (body.unchanged_relations as number) +
      (body.suppressed_relations as number) !==
    expectedRelationCount
  ) {
    invalid("relation projection counters must equal the validated local relation count");
  }
  if (typeof body.checkpoint_updated !== "boolean" || typeof body.no_op !== "boolean") {
    invalid("checkpoint_updated and no_op must be booleans");
  }
  if (body.checkpoint_updated === body.no_op) {
    invalid("checkpoint_updated must be the inverse of no_op");
  }
  if (
    body.no_op &&
    (body.created_memories !== 0 ||
      body.repaired_memories !== 0 ||
      body.status_updates !== 0 ||
      body.created_relations !== 0 ||
      body.repaired_relations !== 0)
  ) {
    invalid("a no-op response cannot report projection mutations");
  }

  if (typeof body.duration_ms !== "number" || !Number.isFinite(body.duration_ms) || body.duration_ms < 0) {
    invalid("duration_ms must be a non-negative finite number");
  }
  if (typeof body.timings_ms !== "object" || body.timings_ms === null || Array.isArray(body.timings_ms)) {
    return invalid("timings_ms must be an object");
  }
  const timings = body.timings_ms as Record<string, unknown>;
  for (const field of ["validate", "write", "relations", "total"] as const) {
    const current = timings[field];
    if (typeof current !== "number" || !Number.isFinite(current) || current < 0) {
      invalid(`timings_ms.${field} must be a non-negative finite number`);
    }
  }
  if (body.duration_ms !== timings.total) {
    invalid("duration_ms must equal timings_ms.total");
  }
  return {
    schema_version: 1,
    repository_id: repositoryId,
    ledger_fingerprint: ledgerFingerprint,
    events_fingerprint: body.events_fingerprint as string,
    received_events: body.received_events as number,
    created_memories: body.created_memories as number,
    unchanged_memories: body.unchanged_memories as number,
    repaired_memories: body.repaired_memories as number,
    suppressed_events: body.suppressed_events as number,
    status_updates: body.status_updates as number,
    created_relations: body.created_relations as number,
    repaired_relations: body.repaired_relations as number,
    unchanged_relations: body.unchanged_relations as number,
    suppressed_relations: body.suppressed_relations as number,
    checkpoint_updated: body.checkpoint_updated as boolean,
    no_op: body.no_op as boolean,
    duration_ms: body.duration_ms as number,
    timings_ms: {
      validate: timings.validate as number,
      write: timings.write as number,
      relations: timings.relations as number,
      total: timings.total as number,
    },
  };
}

function redactCredential(detail: string, credential: string | undefined): string {
  return credential ? detail.split(credential).join("[REDACTED]") : detail;
}

function normalizeSearchResponse(body: SearchResponse): SearchResponse {
  return {
    results: (body.results ?? []).map((item) => ({
      memory: item.memory,
      score: item.score,
      reasons: item.reasons ?? [],
      related_memories: item.related_memories,
    })),
  };
}
