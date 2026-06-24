# PLAN-02: Local Init and Config

## Goal

Implement `provena init` so running it inside any git repo creates a
`.provena/` directory with config, gitignore entry, and empty SQLite path —
without starting services or indexing yet.

## Why this is its own plan

Establishes the on-disk contract every other local feature depends on:
where the DB lives, how scope IDs are derived, and what gets gitignored.

## Prerequisites

- PLAN-01 complete (`@provena/cli` binary exists)

## Success criteria

- [ ] `provena init` creates `.provena/config.json`
- [ ] `provena init` creates `.provena/` and sets `database.path` to `.provena/provena.db`
- [ ] `provena init` appends `.provena/` to repo `.gitignore` if not present
- [ ] `provena init` is idempotent (second run does not clobber config)
- [ ] Config derives `tenant_id` from repo folder name and `project_id` from git root
- [ ] `provena init --force` overwrites config with confirmation flag

## Scope

### In scope

- `cli/src/commands/init.ts`
- `cli/src/config.ts` — read/write/validate `ProvenaConfig` schema
- JSON schema: `backend: "sqlite"`, `store_url`, `scope`, `database.path`

### Out of scope

- Creating the SQLite file (PLAN-03)
- Indexing files

## Implementation

### Steps

1. Define `ProvenaConfig` TypeScript interface:
   ```json
   {
     "version": 1,
     "backend": "sqlite",
     "database": { "path": ".provena/provena.db" },
     "store_url": "http://127.0.0.1:18092",
     "scope": {
       "tenant_id": "<repo-name>",
       "project_id": "<git-root-basename>"
     },
     "index": {
       "include": ["**/*"],
       "exclude": ["node_modules/**", ".git/**", "dist/**", "build/**"]
     }
   }
   ```
2. `init` command:
   - Detect git root via `git rev-parse --show-toplevel` (fallback: cwd)
   - Write config
   - Patch `.gitignore`
   - Print next step: `provena index`
3. Add `provena config show` stub that pretty-prints config (bonus, same plan).
4. Unit test config round-trip in `cli/tests/config.test.mjs`.

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/config.ts` | create |
| `cli/src/commands/init.ts` | create |
| `cli/src/cli.ts` | wire `init` command |
| `cli/tests/config.test.mjs` | create |

## Verification

```powershell
cd cli && npm run build
cd <temp-empty-git-repo>
npx ts-node ../cli/dist/cli.js init
Test-Path .provena/config.json
Select-String -Path .gitignore -Pattern "\.provena/"
npx ts-node ../cli/dist/cli.js init   # idempotent, exit 0
node ../cli/tests/config.test.mjs
```

## Handoff to next plan

PLAN-03 reads `config.database.path` and `config.store_url` to bootstrap the
SQLite store and optional local daemon.