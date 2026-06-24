import type { ProvenaScope } from "./config.js";

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
  | "source";

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

export interface ProvenaClientOptions {
  storeUrl: string;
  intelligenceUrl?: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

const DEFAULT_SEARCH_LIMIT = 5;

export class ProvenaClient {
  private readonly baseUrl: string;
  private readonly intelligenceUrl: string | undefined;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;

  constructor(options: ProvenaClientOptions) {
    this.baseUrl = options.storeUrl.replace(/\/$/, "");
    this.intelligenceUrl = options.intelligenceUrl?.replace(/\/$/, "");
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.timeoutMs = options.timeoutMs ?? 30_000;
  }

  async createMemory(payload: MemoryCreate): Promise<MemoryWriteResult> {
    if (this.intelligenceUrl) {
      return this.pipelineWrite(payload);
    }
    return this.postJson<MemoryWriteResult>("/v1/memories", payload);
  }

  /** Write via intelligence pipeline for embeddings (PLAN-09). */
  async pipelineWrite(payload: MemoryCreate): Promise<MemoryWriteResult> {
    const base = this.intelligenceUrl ?? this.baseUrl;
    const response = await this.fetchImpl(`${base}/v1/pipeline/write`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Provena-Role": "editor",
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
        `POST /v1/pipeline/write failed (${response.status}): ${detail.slice(0, 500)}`,
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
        "X-Provena-Role": "editor",
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
        `POST /v1/pipeline/search failed (${response.status}): ${detail.slice(0, 500)}`,
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