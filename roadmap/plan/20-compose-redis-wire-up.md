# PLAN-20: Docker Compose Redis Wire-up

## Goal

Add Redis to `docker-compose.yml`, wire `PROVENA_REDIS_URL` into store and
intelligence services, and document the polyglot caching stack end-to-end.

## Why this is its own plan

Infrastructure wiring is a discrete deliverable: compose file, env vars, health
checks, deployment docs — separate from Python Redis adapter code.

## Prerequisites

- PLAN-18 complete (`RedisHotCache`)
- PLAN-19 complete (embedding cache) — optional but recommended same PR window

## Success criteria

- [ ] `redis` service in docker-compose on internal network
- [ ] Store container gets `PROVENA_REDIS_URL=redis://redis:6379/0`
- [ ] Intelligence container gets same URL for embedding cache
- [ ] `docker compose up` → `provena doctor` equivalent health shows redis ok
- [ ] `DEPLOYMENT.md` updated with Redis section
- [ ] `PROVENA_REDIS_HOST_PORT` override for host debugging (default unpublished)

## Scope

### In scope

- `docker-compose.yml` redis service
- `.env.example` entries
- Healthcheck: `redis-cli ping`

### Out of scope

- Redis Cluster / Sentinel
- Managed Elasticache/terraform

## Implementation

### Steps

1. Add redis:7-alpine service with volume optional (cache ephemeral OK).
2. Pass env to `store` and `intelligence` services.
3. Update `scripts/run_e2e.py` to start redis when testing polyglot path.
4. Document when Redis is optional (standalone SQLite local dev can skip).

## Files to create or modify

| Path | Action |
|------|--------|
| `docker-compose.yml` | add redis service |
| `DEPLOYMENT.md` | Redis section |
| `.env.example` | create or update |
| `scripts/run_e2e.py` | redis dependency |

## Verification

```powershell
docker compose up -d redis store intelligence
docker compose exec store python -c "import os; print(os.environ.get('PROVENA_REDIS_URL'))"
.venv\Scripts\python.exe -m pytest tests/test_redis_cache.py -q
python scripts/run_e2e.py
```

## Handoff to next plan

Production polyglot deployments get search + embedding cache out of the box.