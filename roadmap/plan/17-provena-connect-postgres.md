# PLAN-17: Provena Connect (Postgres)

## Goal

`provena connect <database-url>` switches a repo from local SQLite to a shared
PostgreSQL backend — updating config and verifying connectivity — enabling team-wide
code memory on your VPS Postgres.

## Why this is its own plan

Backend switching is a CLI + config + doctor concern separate from implementing
the Postgres driver itself.

## Prerequisites

- PLAN-15 and PLAN-16 complete (Postgres + pgvector work)
- PLAN-02 complete (config file)

## Success criteria

- [ ] `provena connect postgresql://user:pass@host:5432/db?sslmode=require` updates config
- [ ] Sets `backend: "postgres"`, `database.url`, clears `database.path`
- [ ] `provena doctor` pings Postgres and reports extension status (pgvector yes/no)
- [ ] `provena index` writes to Postgres when connected
- [ ] `provena connect --local` reverts to SQLite defaults
- [ ] Secrets not echoed in logs; URL stored with optional `{{ENV_VAR}}` indirection

## Scope

### In scope

- `cli/src/commands/connect.ts`
- Config schema v2 fields for `database.url`
- Doctor checks: `SELECT 1`, `SELECT extname FROM pg_extension WHERE extname='vector'`

### Out of scope

- Managed cloud provisioning
- Per-tenant database auto-creation (manual `CREATE DATABASE` documented)

## Implementation

### Steps

1. Extend `ProvenaConfig` with `database.url` optional field.
2. `connect` validates URL format, runs doctor probe, writes config.
3. Update `cli/src/client.ts` — store URL may differ from SQLite local serve.
4. Document creating DB: `CREATE DATABASE provena_myrepo;` on shared VPS.
5. Index command: when `backend=postgres`, skip `provena serve` requirement.

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/commands/connect.ts` | create |
| `cli/src/config.ts` | schema v2 |
| `cli/src/commands/doctor.ts` | postgres probes |
| `roadmap/plan/02-local-init-and-config.md` | cross-link only if needed |

## Verification

```powershell
provena connect "postgresql://admin:***@postgresql.ayushojha.com:5432/provena_dev?sslmode=require"
provena doctor
provena index --path app
provena search "memory store"
provena connect --local
```

## Handoff to next plan

Team repos use Postgres; local solo devs stay on SQLite — same CLI commands.