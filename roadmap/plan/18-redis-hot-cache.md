# PLAN-18: Redis Hot Cache

## Goal

Implement `RedisHotCache` satisfying `MemoryHotCache` protocol so search responses
and tenant cache versions survive process restarts and work across multiple store
workers.

## Why this is its own plan

Redis integration is pluggable by design (`app/hot_cache.py` documents this).
One adapter, wired via config, with tests — independent of embedding cache.

## Prerequisites

- PLAN-03 complete (store with `InMemoryHotCache` reference)
- Redis instance available for tests (Docker or local)

## Success criteria

- [ ] `RedisHotCache` implements `get_search`, `set_search`, `bump_search_version`
- [ ] `PROVENA_REDIS_URL=redis://localhost:6379/0` enables Redis backend
- [ ] `backend_name` returns `"redis"`
- [ ] Tenant version bump invalidates prior keys (same semantics as in-memory)
- [ ] TTL honored via Redis `SETEX`
- [ ] Store integration test: two searches, bump version, second miss cache
- [ ] Falls back to `InMemoryHotCache` when Redis URL unset

## Scope

### In scope

- `app/redis_cache.py`
- `app/config.py` — `redis_url` optional
- `app/store.py` — inject cache from factory
- Use `redis` Py package (official)

### Out of scope

- Embedding cache (PLAN-19)
- Redis as job queue (future)

## Implementation

### Steps

1. Key format: `provena:search:{tenant_id}:v{version}:{cache_key_hash}`.
2. Version key: `provena:search_version:{tenant_id}` INCR on bump.
3. JSON serialize search response dict.
4. Connection pool; handle Redis down → log warning, fall back in-memory.
5. Unit tests with fakeredis if available, else Docker redis in CI.

## Files to create or modify

| Path | Action |
|------|--------|
| `app/redis_cache.py` | create |
| `app/store_factory.py` | wire cache selection |
| `app/config.py` | redis_url |
| `pyproject.toml` | add `redis` |
| `tests/test_redis_cache.py` | create |

## Verification

```powershell
docker run -d -p 6379:6379 redis:7-alpine
$env:PROVENA_REDIS_URL = "redis://localhost:6379/0"
$env:PROVENA_DB_PATH = ".\.provena\test.db"
.venv\Scripts\python.exe -m pytest tests/test_redis_cache.py -q
```

## Handoff to next plan

PLAN-20 adds Redis service to docker-compose for polyglot deployments.
PLAN-19 adds second Redis keyspace for embeddings.