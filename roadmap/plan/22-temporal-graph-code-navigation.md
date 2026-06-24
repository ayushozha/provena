# PLAN-22: Temporal Graph Code Navigation

## Goal

Expose `provena graph <query>` CLI that calls `POST /v1/memories/graph/temporal`
seeded from a symbol or natural language query — returning related code memories
as a navigable graph (imports, callers, file siblings).

## Why this is its own plan

Graph navigation is a distinct UX on top of existing store API — validates the
knowledge graph story without changing indexer or storage.

## Prerequisites

- PLAN-07 complete (relations)
- PLAN-08 complete (entities)
- PLAN-10 complete (search CLI patterns)

## Success criteria

- [ ] `provena graph "AuthService"` returns nodes + edges as text tree or `--json`
- [ ] Seeds from entity registry match when query equals canonical name
- [ ] Falls back to search top hit as seed when no exact entity match
- [ ] `--depth 2` controls traversal (store API already supports depth)
- [ ] Output includes file:line from `source_references`
- [ ] MCP tool or doc link for same API (optional note in plan output)

## Scope

### In scope

- `cli/src/commands/graph.ts`
- Uses existing `TemporalGraphRequest` / `TemporalGraphResponse` models
- Pretty-print: node title, kind, edges labeled by relation type

### Out of scope

- Web graph visualization UI
- Neo4j export

## Implementation

### Steps

1. Resolve query → seed memory IDs via entity registry search or memory search.
2. POST temporal graph with scope from config.
3. Render adjacency list grouped by relation kind.
4. Add `provena graph --from-file src/auth.ts` seeding from file artifact memory.

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/commands/graph.ts` | create |
| `cli/src/cli.ts` | wire `graph` |
| `cli/tests/graph.test.mjs` | create |

## Verification

```powershell
provena index
provena graph "ProvenaStore"
provena graph --from-file app/store.py --depth 1 --json
node cli/tests/graph.test.mjs
```

## Handoff to next plan

Agents use graph + search together for codebase understanding.