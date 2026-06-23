# PLAN-19: Redis Embedding Cache

## Goal

Cache embedding vectors by content hash in Redis so re-indexing unchanged chunks
and duplicate content across files avoids redundant embedding API calls.

## Why this is its own plan

Embedding cache is a cross-cutting intelligence-layer optimization with different
key semantics and TTL than search response cache — separate from PLAN-18.

## Prerequisites

- PLAN-18 complete (Redis client wiring exists)
- PLAN-06/09 complete (indexer generates content to embed)

## Success criteria

- [ ] `EmbeddingManager` checks Redis before calling embedding provider
- [ ] Cache key: `provena:emb:{model}:{sha256(content)}`
- [ ] Cache hit returns vector; miss calls provider and SETEX with 7-day TTL
- [ ] `PROVENA_REDIS_URL` required; no-op when unset (direct provider call)
- [ ] Second index of unchanged repo shows 0 embedding API calls (mock test)
- [ ] Vector dimension stored in cache metadata for mismatch detection

## Scope

### In scope

- `intelligence/app/embedding_cache.py`
- Hook in `intelligence/app/embeddings.py`
- Optional CLI flag `provena index --no-embed-cache` to bypass

### Out of scope

- Caching LLM chat completions
- Cross-tenant cache sharing (tenant_id prefix if needed for ACL)

## Implementation

### Steps

1. Create `EmbeddingCache` protocol + `RedisEmbeddingCache`.
2. Serialize vector as JSON array or binary float32 blob (prefer blob for size).
3. Wire into `EmbeddingManager.embed_text()`.
4. Add metrics log: `embedding_cache_hits`, `embedding_cache_misses`.
5. Test with mocked provider counting invocations.

## Files to create or modify

| Path | Action |
|------|--------|
| `intelligence/app/embedding_cache.py` | create |
| `intelligence/app/embeddings.py` | integrate cache |
| `intelligence/tests/test_embedding_cache.py` | create |

## Verification

```powershell
docker run -d -p 6379:6379 redis:7-alpine
$env:PROVENA_REDIS_URL = "redis://localhost:6379/0"
cd intelligence
..\.venv\Scripts\python.exe -m pytest tests/test_embedding_cache.py -q
```

## Handoff to next plan

PLAN-20 ensures Redis is in compose for polyglot stacks using embedding cache.