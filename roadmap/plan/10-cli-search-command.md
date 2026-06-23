# PLAN-10: CLI Search Command

## Goal

Implement `provena search <query>` that queries the local store (via intelligence
pipeline when available) and prints ranked results with file paths, line ranges,
and scores — proving the index is useful from the terminal.

## Why this is its own plan

Search is the user-visible payoff. Separate from indexing so search UX (formatting,
pipeline vs store fallback, JSON mode) can iterate without touching the indexer.

## Prerequisites

- PLAN-09 complete (indexed repo)
- PLAN-03 complete (store running)

## Success criteria

- [ ] `provena search "auth middleware"` prints top 5 hits with path:line, score, excerpt
- [ ] `--json` outputs machine-readable `SearchResponse`
- [ ] `--limit N` respected
- [ ] Uses `POST /v1/pipeline/search` when intelligence URL configured; falls back to `POST /v1/memories/search`
- [ ] Respects scope from `.provena/config.json`
- [ ] Exit 1 with helpful message if store down or no index state found

## Scope

### In scope

- `cli/src/commands/search.ts`
- Pretty table output for terminal
- Optional `--explain` flag calling store explain endpoint if exists

### Out of scope

- Interactive TUI
- VS Code extension

## Implementation

### Steps

1. Load config; verify `.provena/index-state.json` exists (warn if missing).
2. Build `SearchRequest` with scope + query.
3. Route to pipeline or store per config.
4. Map `source_references[0].span_start/end` to line display.
5. Color output optional (disable on Windows NO_COLOR).

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/commands/search.ts` | create |
| `cli/src/format.ts` | create |
| `cli/src/cli.ts` | wire `search` |
| `cli/tests/search.test.mjs` | create |

## Verification

```powershell
provena serve --detach
provena index
provena search "ProviderWorkerNotFoundError"
provena search "vector index" --json
node cli/tests/search.test.mjs
```

## Handoff to next plan

PLAN-23 MCP config uses same search path for agents.
PLAN-25 benchmark uses search for recall measurement.