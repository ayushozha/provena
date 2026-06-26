# PLAN-12: Incremental Index via Fingerprints

## Goal

`provena index` skips unchanged files by comparing SHA-256 from discovery with
`.provena/index-state.json`, and re-indexes only added/changed/deleted files.

## Why this is its own plan

Full re-index on every run won't scale to large repos. Incremental logic touches
delete/supersede semantics and must be correct in isolation.

## Prerequisites

- PLAN-04 complete (per-file sha256)
- PLAN-09 complete (index state file format)

## Success criteria

- [x] Second `provena index` on unchanged repo completes in <5s with "0 files changed"
- [x] Editing one file re-indexes only that file (+ relation updates)
- [x] Deleted file triggers memory delete for its artifact + chunks
- [x] `--full` flag forces complete re-index
- [x] Index state tracks `{ path, sha256, memoryIds[], relationIds[] }`
- [x] Tests: modify fixture file → only that path re-indexed

## Scope

### In scope

- `cli/src/indexer/incremental.ts` — diff discovery vs state
- Delete path: `DELETE /v1/memories/{id}` for removed files' memories
- Bump hot cache version on store after batch (store already supports this)

### Out of scope

- Git diff integration (use content hash only)
- File watcher (PLAN-13)

## Implementation

### Steps

1. Load previous state; compute `added`, `changed`, `removed` sets.
2. For `removed`: delete associated memories by ID from state.
3. For `changed`: delete old memories then re-emit (simpler than patch).
4. For `added`: normal emit path.
5. Update state atomically (write temp file + rename).
6. Log incremental stats in `last-index.json`.

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/indexer/incremental.ts` | create |
| `cli/src/indexer/run.ts` | integrate diff |
| `cli/tests/incremental.test.mjs` | create |

## Verification

```powershell
provena index
provena index   # expect 0 changed
# edit one file
provena index   # expect 1 changed
provena index --full
node cli/tests/incremental.test.mjs
```

## Handoff to next plan

PLAN-13 watch mode calls incremental index on file events.

---
## Completion
- **Completed**: 2026-06-23
- **PR**: (pending)
- **Verified by**: `cd cli && npm test` — `incremental.test.mjs` green with full suite
- **Notes**: Default `provena index` skips unchanged sha256; `--full` re-processes all discovered files; removed paths delete store memories via `DELETE /v1/memories/{id}`.