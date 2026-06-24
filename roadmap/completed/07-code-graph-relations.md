# PLAN-07: Code Graph Relations

## Goal

After memories exist for files and chunks, write typed `memory_relations` edges
that form a navigable code graph: file contains symbol, symbol imports module,
symbol calls symbol (best-effort static).

## Why this is its own plan

The knowledge graph is the differentiator. Relations are a separate store API
surface with their own correctness rules — worth isolating from raw memory writes.

## Prerequisites

- PLAN-06 complete (memories + `index-state.json` with path → memory_id map)

## Success criteria

- [ ] `defined_in` edge: chunk memory → file artifact memory
- [ ] `derived_from` edge: chunk memory → imported module file artifact (when resolvable)
- [ ] `related_to` edge: co-located chunks in same class
- [ ] `supports` edge: test file chunk → implementation chunk (path heuristic: `*.test.ts` → sibling)
- [ ] Relations are idempotent (re-run does not duplicate)
- [ ] `POST /v1/memories/relations` used for all edges
- [ ] Test: graph query from file memory returns child symbol memories

## Scope

### In scope

- `cli/src/indexer/relations.ts`
- Import resolution: relative paths → repo files (TypeScript `import` specifiers)
- Relation kinds from existing `RelationKind` enum in store

### Out of scope

- Call-graph points-to analysis (future enhancement)
- Entity registry canonicalization (PLAN-08)

## Implementation

### Steps

1. Load index state: `{ files, chunks: { symbolKey → memoryId } }`.
2. For each chunk with `imports[]`, resolve to target file artifact memory_id.
3. Emit `derived_from` chunk → target file (or target module chunk if exists).
4. Emit `defined_in` chunk → parent file artifact.
5. Store relation fingerprints in index state to skip duplicates on re-run.
6. Optional: expose `provena graph --file src/auth.ts` debug CLI (lists edges).

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/indexer/relations.ts` | create |
| `cli/src/indexer/resolve-import.ts` | create |
| `cli/tests/relations.test.mjs` | create |

## Verification

```powershell
provena serve --detach
# after indexing one fixture repo:
node cli/tests/relations.test.mjs
curl -X POST http://127.0.0.1:18092/v1/memories/search -d '{"query":"auth","scope":{...}}'
```

## Handoff to next plan

PLAN-08 registers `entity_keys` into `entity_registry` for symbol canonicalization.
PLAN-22 uses relations + temporal graph for navigation.
---
## Completion
- **Completed**: 2026-06-23
- **PR**: #27 (Indexer MVP stack on main)
- **Verified by**: `cd cli && npm test` � full Indexer MVP suite green (smoke through index e2e + search)
- **Notes**: Shipped on `main` via squashed merge #27; roadmap housekeeping in follow-up PR. TS/JS only until plan 11.