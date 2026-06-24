# PLAN-09: Repo Index Command

## Goal

Ship `provena index` as the end-to-end command: discover → chunk → emit memories →
write relations → upsert entities → optionally call intelligence pipeline for
embeddings — for TypeScript/JavaScript repos.

## Why this is its own plan

This is the **core user-facing milestone** for the grand vision. It composes
prior plans into one command with progress output, error handling, and summary stats.

## Prerequisites

- PLAN-03 (store running)
- PLAN-04 through PLAN-08 complete

## Success criteria

- [ ] `provena index` indexes all discovered TS/JS files in cwd repo
- [ ] Prints progress: `Indexed 142/142 files, 891 memories, 1204 relations`
- [ ] Writes `.provena/index-state.json` with file hashes and memory IDs
- [ ] `--dry-run` lists files without writing
- [ ] `--path src/auth` limits to subtree
- [ ] Failed files logged to `.provena/index-errors.log` without aborting whole run
- [ ] After index, `provena search "authentication"` returns relevant chunk (manual smoke)
- [ ] Uses intelligence `POST /v1/pipeline/write` when `config.intelligence_url` set

## Scope

### In scope

- `cli/src/commands/index.ts`
- Orchestration pipeline in `cli/src/indexer/run.ts`
- Concurrency limit (e.g. 4 files at a time)
- Summary JSON written to `.provena/last-index.json`

### Out of scope

- Python/Go/Rust files (PLAN-11)
- Incremental mode (PLAN-12)

## Implementation

### Steps

1. `runIndex(config, options)`:
   - `discover` → filter TS/JS → for each file: read → chunk → emit → relations → entities
   - Progress bar via `cli-progress` or simple stdout counter
2. After all files, optional batch embedding refresh if pipeline URL configured.
3. Register trigger phrases from exported symbol names (defer full logic to PLAN-21; stub call OK).
4. Exit code 0 if >0 files indexed; 1 if store unreachable.

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/commands/index.ts` | create |
| `cli/src/indexer/run.ts` | create |
| `cli/src/cli.ts` | wire `index` |
| `cli/tests/index.e2e.mjs` | create |

## Verification

```powershell
cd C:\Users\ayush\Desktop\YC\provena
provena init
provena serve --detach
provena index --dry-run
provena index
provena search "connector scheduler"
type .provena\last-index.json
node cli/tests/index.e2e.mjs
```

## Handoff to next plan

PLAN-10 exposes search CLI on top of indexed data.
PLAN-12 adds fingerprint skip for unchanged files.
---
## Completion
- **Completed**: 2026-06-23
- **PR**: #27 (Indexer MVP stack on main)
- **Verified by**: `cd cli && npm test` � full Indexer MVP suite green (smoke through index e2e + search)
- **Notes**: Shipped on `main` via squashed merge #27; roadmap housekeeping in follow-up PR. TS/JS only until plan 11.