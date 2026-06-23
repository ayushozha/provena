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

export interface SearchRequest {
  query: string;
  scope: ScopeEnvelope;
  limit?: number;
  tags?: string[];
  entity_keys?: string[];
}

export interface SearchResult {
  memory: MemoryRecord;
  score: number;
  reasons: string[];
}

export interface SearchResponse {
  results: SearchResult[];
}

export interface ProvenaClientOptions {
  storeUrl: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export class ProvenaClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;

  constructor(options: ProvenaClientOptions) {
    this.baseUrl = options.storeUrl.replace(/\/$/, "");
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.timeoutMs = options.timeoutMs ?? 30_000;
  }

  async createMemory(payload: MemoryCreate): Promise<MemoryWriteResult> {
    return this.postJson<MemoryWriteResult>("/v1/memories", payload);
  }

  async searchMemories(payload: SearchRequest): Promise<SearchResponse> {
    return this.postJson<SearchResponse>("/v1/memories/search", payload);
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
}

export function scopeEnvelopeFromConfig(scope: ProvenaScope): ScopeEnvelope {
  return {
    tenant_id: scope.tenant_id,
    project_id: scope.project_id,
  };
}