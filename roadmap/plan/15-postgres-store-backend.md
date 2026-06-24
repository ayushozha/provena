# PLAN-15: Postgres Store Backend

## Goal

Implement `PostgresStore` with the same public methods as `ProvenaStore` so the
app can switch backends via `PROVENA_DATABASE_URL` without changing API handlers.

## Why this is its own plan

Postgres is a full second storage driver — migrations, connection pooling, SQL
dialect differences — must ship as one testable vertical slice before pgvector.

## Prerequisites

- PLAN-03 complete (SQLite store is reference implementation)
- Access to PostgreSQL instance for integration tests

## Success criteria

- [ ] `PostgresStore` passes same test suite as SQLite for CRUD + search (FTS path)
- [ ] `PROVENA_DATABASE_URL=postgresql://...` selects Postgres in `app/main.py`
- [ ] `PROVENA_DB_PATH` still selects SQLite (mutually exclusive, documented)
- [ ] Connection uses `sslmode=require` when query param present
- [ ] Migrations run on startup via `storage/migrations/001_postgres.sql`
- [ ] Tenant isolation preserved (all queries filter `tenant_id`)

## Scope

### In scope

- `app/store_postgres.py` using `psycopg` v3 or `asyncpg` — match app async style
- `storage/migrations/001_postgres.sql` adapted from SQLite schema
- Factory in `app/store_factory.py`: `create_store(settings) -> ProvenaStore | PostgresStore`
- Shared protocol/ABC if needed: `app/store_protocol.py`

### Out of scope

- pgvector KNN (PLAN-16)
- CLI `connect` command (PLAN-17)

## Implementation

### Steps

1. Extract shared SQL fragments or duplicate minimally — prefer explicit Postgres SQL.
2. Implement: create_memory, get_memory, search_memories (tsvector), relations, entities.
3. FTS: `to_tsvector` + `plainto_tsquery` replacing FTS5.
4. Embeddings: store as `JSONB` until PLAN-16 adds `vector` column.
5. Add `tests/test_postgres_store.py` with pytest mark `postgres` (skip if no URL).
6. Document env vars in `DEPLOYMENT.md`.

## Files to create or modify

| Path | Action |
|------|--------|
| `app/store_postgres.py` | create |
| `app/store_factory.py` | create |
| `storage/migrations/001_postgres.sql` | create |
| `app/config.py` | add `database_url` |
| `app/main.py` | use factory |
| `pyproject.toml` | add `psycopg[binary]` |
| `tests/test_postgres_store.py` | create |

## Verification

```powershell
$env:PROVENA_DATABASE_URL = "postgresql://admin:***@postgresql.ayushojha.com:5432/provena_test?sslmode=require"
.venv\Scripts\python.exe -m pytest tests/test_postgres_store.py -q
.venv\Scripts\python.exe -m pytest tests/test_main.py -q
```

## Handoff to next plan

PLAN-16 adds pgvector column + KNN search path on Postgres.
PLAN-17 wires CLI to remote Postgres URL.