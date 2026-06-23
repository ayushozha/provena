# PLAN-24: Index Status and Health

## Goal

`provena status` prints operator-friendly summary: backend, store health, index
freshness, memory/entity/relation counts, last index time, and stale file warnings.

## Why this is its own plan

Observability for local/team installs is a complete UX feature — uses multiple
data sources (config, healthz, index state, store overview API).

## Prerequisites

- PLAN-09 complete (last-index.json)
- PLAN-03 complete (doctor patterns)
- PLAN-17 optional (postgres status)

## Success criteria

- [ ] `provena status` human-readable output in <2s
- [ ] Shows: backend (sqlite/postgres), store URL, health, redis (if configured)
- [ ] Shows: files indexed, memories, relations, entities, last index timestamp
- [ ] Shows: git dirty file count not yet re-indexed (compare working tree hash vs state)
- [ ] `--json` for scripting
- [ ] Exit 1 if store unhealthy; exit 0 with warnings if index stale >7 days

## Scope

### In scope

- `cli/src/commands/status.ts`
- Call store `GET` architecture overview if endpoint exists, else aggregate search/count APIs
- Read `.provena/last-index.json` and `index-state.json`

### Out of scope

- Web dashboard
- Datadog integration

## Implementation

### Steps

1. Reuse `doctor` health checks.
2. Add store endpoint if missing: `GET /v1/admin/overview` returning counts — or use existing `MemoryArchitectureOverview` route if present in store.
3. Compute stale: `git status --porcelain` paths not matching index state hashes.
4. Format table output.

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/commands/status.ts` | create |
| `app/main.py` | add/verify overview route |
| `cli/src/cli.ts` | wire `status` |

## Verification

```powershell
provena serve --detach
provena index
provena status
provena status --json
# stop store, provena status → exit 1
```

## Handoff to next plan

PLAN-26 quickstart references `provena status` as verification step.