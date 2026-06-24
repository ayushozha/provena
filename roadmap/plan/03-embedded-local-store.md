# PLAN-03: Embedded Local Store

## Goal

`provena init` (or a new `provena serve`) boots the standalone Python store
against `.provena/provena.db`, runs migrations, and exposes `/healthz` on the
configured port — so the CLI has a live backend before indexing exists.

## Why this is its own plan

Indexing and search need a running store. Bundling "start store + migrate +
health check" as one vertical slice unblocks all data-plane plans.

## Prerequisites

- PLAN-02 complete (`.provena/config.json` exists)

## Success criteria

- [ ] First `provena init` creates SQLite file via store migration on first request
- [ ] `provena serve` starts uvicorn on `config.store_url` port (default 18092)
- [ ] `provena serve --detach` backgrounds process and writes `.provena/store.pid`
- [ ] `provena serve --stop` kills detached process cleanly
- [ ] `curl http://127.0.0.1:18092/healthz` returns `status: ok`
- [ ] `PROVENA_DB_PATH` env overrides config path when set

## Scope

### In scope

- `cli/src/commands/serve.ts` — spawn `python -m uvicorn app.main:app`
- Resolve Python: prefer repo `.venv`, then `uv run`, then `python`
- Pass `PROVENA_DB_PATH=<absolute path to .provena/provena.db>`
- PID file + port-in-use detection

### Out of scope

- Docker / polyglot compose
- Postgres backend

## Implementation

### Steps

1. Add `serve` subcommand to CLI.
2. On serve:
   - Load config from cwd `.provena/config.json`
   - Ensure parent dir exists for DB path
   - Spawn uvicorn with cwd = repo root (where `app/` lives) OR document that
     Provena must be installed as dependency — **decision**: for monorepo dev,
     resolve store code relative to Provena package install path; document both modes.
3. Implement `--detach` using `child_process.spawn` detached + PID file.
4. Implement `--stop` reading PID file.
5. Add `provena doctor` that checks config + healthz (same plan, small).

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/commands/serve.ts` | create |
| `cli/src/commands/doctor.ts` | create |
| `cli/src/process.ts` | create (spawn helpers) |
| `cli/src/cli.ts` | wire commands |
| `app/config.py` | ensure `PROVENA_DB_PATH` respected (verify only) |

## Verification

```powershell
cd <test-repo-with-provena-init>
provena serve --detach
provena doctor
curl http://127.0.0.1:18092/healthz
provena serve --stop
```

From Provena repo itself (dogfood):

```powershell
cd C:\Users\ayush\Desktop\YC\provena
$env:PROVENA_DB_PATH = ".\.provena\test.db"
.venv\Scripts\python.exe -m uvicorn app.main:app --port 18092
curl http://127.0.0.1:18092/healthz
```

## Handoff to next plan

PLAN-09 (`provena index`) assumes store is reachable at `config.store_url`.
Document in each plan: run `provena serve --detach` before index.