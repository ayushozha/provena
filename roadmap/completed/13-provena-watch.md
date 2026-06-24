# PLAN-13: Provena Watch

## Goal

`provena watch` runs a debounced file watcher that triggers incremental index on
save — keeping repo memory fresh during active development.

## Why this is its own plan

Watch mode is a long-running process with debouncing, ignore rules, and graceful
shutdown — separate from one-shot `index`.

## Prerequisites

- PLAN-12 complete (incremental index)
- PLAN-03 complete (store reachable)

## Success criteria

- [x] `provena watch` watches repo root respecting gitignore
- [x] Debounce 500ms after last change before indexing
- [x] Only changed files re-indexed (uses PLAN-12 diff)
- [x] Ctrl+C shuts down cleanly
- [x] `--interval 30s` optional polling fallback for environments without native watch
- [x] Logs each watch-triggered index to stdout with timestamp

## Scope

### In scope

- `cli/src/commands/watch.ts`
- Use `chokidar` for cross-platform watching
- Reuse `runIndex` with incremental options

### Out of scope

- Git hook (PLAN-14)
- Intelligence pipeline hot reload

## Implementation

### Steps

1. Initialize chokidar on repo root with ignored paths from config + gitignore.
2. On `change` / `add` / `unlink`, queue path in debounce buffer.
3. Flush buffer → `runIncrementalIndex(changedPaths)`.
4. Handle watcher errors; retry once on EMFILE.

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/commands/watch.ts` | create |
| `cli/package.json` | add `chokidar` |
| `cli/src/cli.ts` | wire `watch` |

## Verification

```powershell
provena serve --detach
provena index
provena watch
# in another terminal: edit a ts file, save
# expect watch log + provena search finds new content
```

## Handoff to next plan

PLAN-14 offers git hook for users who prefer commit-time indexing over watch.

---
## Completion
- **Completed**: 2026-06-23
- **PR**: (pending)
- **Verified by**: `cd cli && npm test` — `watch.test.mjs` + full suite green
- **Notes**: chokidar watcher with gitignore + config excludes; debounced incremental `runIndex`; `--interval` polling fallback; SIGINT/SIGTERM shutdown.