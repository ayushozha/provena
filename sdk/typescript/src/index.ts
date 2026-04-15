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

export interface MemoryCreate {
  kind: string;
  scope: ScopeEnvelope;
  content: string;
  title?: string;
  summary?: string;
  entity_keys?: string[];
  tags?: string[];
  metadata?: Record<string, unknown>;
  importance?: number;
  confidence?: number;
  strength?: number;
  valid_from?: string;
  valid_to?: string;
  source_references?: SourceReference[];
  supersedes_memory_id?: string;
}

export interface SearchRequest {
  query: string;
  scope: ScopeEnvelope;
  kinds?: string[];
  tags?: string[];
  entity_keys?: string[];
  include_relations?: boolean;
  include_deleted?: boolean;
  limit?: number;
}

async function request<T>(
  baseUrl: string,
  path: string,
  headers?: HeadersInit,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(new URL(path, baseUrl), {
    headers: {
      "content-type": "application/json",
      ...(headers ?? {}),
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Provena request failed (${response.status}): ${body}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export interface ClientOptions {
  apiKey?: string;
  headers?: HeadersInit;
}

export class ProvenaClient {
  constructor(
    private readonly baseUrl: string,
    private readonly options: ClientOptions = {},
  ) {}

  private requestHeaders(): HeadersInit {
    return {
      ...(this.options.apiKey
        ? { authorization: `Bearer ${this.options.apiKey}` }
        : {}),
      ...(this.options.headers ?? {}),
    };
  }

  health(): Promise<{ service: string; environment: string; status: string }> {
    return request(this.baseUrl, "/healthz", this.requestHeaders(), {
      method: "GET",
    });
  }

  createMemory(payload: MemoryCreate): Promise<any> {
    return request(this.baseUrl, "/v1/memories", {
      ...this.requestHeaders(),
    }, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  getMemory(memoryId: string): Promise<any> {
    return request(this.baseUrl, `/v1/memories/${memoryId}`, this.requestHeaders(), {
      method: "GET",
    });
  }

  searchMemories(payload: SearchRequest): Promise<any> {
    return request(this.baseUrl, "/v1/memories/search", this.requestHeaders(), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  createRelation(payload: {
    from_memory_id: string;
    to_memory_id: string;
    relation: string;
    scope: ScopeEnvelope;
  }): Promise<void> {
    return request(this.baseUrl, "/v1/memories/relations", this.requestHeaders(), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  deleteMemory(memoryId: string, hardDelete = false): Promise<any> {
    const suffix = hardDelete ? "?hard_delete=true" : "";
    return request(this.baseUrl, `/v1/memories/${memoryId}${suffix}`, this.requestHeaders(), {
      method: "DELETE",
    });
  }

  eraseScope(payload: {
    tenant_id: string;
    workspace_id?: string;
    project_id?: string;
    user_id?: string;
  }): Promise<any> {
    return request(this.baseUrl, "/v1/admin/erase", this.requestHeaders(), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }
}
