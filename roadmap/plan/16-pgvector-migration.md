# PLAN-16: pgvector Migration

## Goal

Add `pgvector` extension and vector KNN search to `PostgresStore`, matching
sqlite-vec behavior on SQLite with automatic fallback to linear scan when index missing.

## Why this is its own plan

Vector search is a distinct migration + query path. Shipping it separately keeps
PLAN-15 CRUD reviewable without vector complexity.

## Prerequisites

- PLAN-15 complete (`PostgresStore` baseline)

## Success criteria

- [ ] Migration enables `CREATE EXTENSION vector`
- [ ] `memories.embedding` column type `vector(N)` where N = config dimensions
- [ ] KNN query uses `<=>` cosine distance with IVFFlat or HNSW index
- [ ] Hybrid search merges FTS rank + vector score (same weighting as SQLite path)
- [ ] `tests/test_postgres_vector.py` passes on Postgres with pgvector
- [ ] Intelligence embeddings write compatible vectors to Postgres store

## Scope

### In scope

- `storage/migrations/002_pgvector.sql`
- Update `PostgresStore.search_memories()` hybrid path
- Index creation guarded: `CREATE INDEX IF NOT EXISTS ... USING hnsw`

### Out of scope

- Redis embedding cache (PLAN-19)
- Re-embedding all existing SQLite memories automatically

## Implementation

### Steps

1. Add migration runner support for numbered migrations (if not exists).
2. Alter table: add `embedding vector(1536)` nullable; backfill from `embedding_json`.
3. Port vector scoring logic from `app/store.py` sqlite-vec path.
4. Feature flag: `PROVENA_PGVECTOR_ENABLED=true` default on when extension detected.
5. Document dimension mismatch handling (truncate/pad/reject).

## Files to create or modify

| Path | Action |
|------|--------|
| `storage/migrations/002_pgvector.sql` | create |
| `app/store_postgres.py` | vector search |
| `tests/test_postgres_vector.py` | create |
| `DEPLOYMENT.md` | pgvector requirements |

## Verification

```powershell
$env:PROVENA_DATABASE_URL = "postgresql://.../provena_test?sslmode=require"
.venv\Scripts\python.exe -m pytest tests/test_postgres_vector.py -q
.venv\Scripts\python.exe -m pytest tests/test_vector_index.py -q  # sqlite parity reference
```

## Handoff to next plan

PLAN-17 lets CLI point at Postgres-backed store for team-wide repo memory.