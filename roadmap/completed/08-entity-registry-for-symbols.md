# PLAN-08: Entity Registry for Symbols

## Goal

Populate Provena's `entity_registry` table with canonical code symbols so search,
graph traversal, and `list_entities` resolve aliases consistently across files.

## Why this is its own plan

Entity registry is a distinct store capability (separate table, dedup rules).
Code symbols need conventions (`Service.Auth.login`, `file:src/auth.ts`) that
should not be mixed into chunk emission logic.

## Prerequisites

- PLAN-06 complete (memories with `entity_keys`)
- PLAN-07 complete (relations — optional but helps alias discovery)

## Success criteria

- [ ] Each indexed symbol gets `entity_registry` row: `canonical_name`, `entity_type`, `aliases_json`
- [ ] Entity types: `file`, `module`, `class`, `function`, `interface`
- [ ] Canonical name format: `<relativePath>::<symbolName>` for functions/classes
- [ ] File entities: `file:<relativePath>`
- [ ] Upsert on re-index (same canonical_name updates aliases, does not duplicate)
- [ ] Store exposes entity count in architecture overview (already exists — verify increment)
- [ ] MCP `list_entities` returns code symbols for project scope

## Scope

### In scope

- `cli/src/indexer/entities.ts`
- Direct SQLite write OR new store admin endpoint `POST /v1/admin/entities/batch`
  - **Prefer new batch endpoint** to avoid CLI holding DB credentials

### Out of scope

- Cross-repo entity linking
- NLP entity extraction from docs

## Implementation

### Steps

1. Add `POST /v1/admin/entities/batch` to `app/main.py` + `store.upsert_entities()`.
2. CLI builds entity records from chunks after emit phase.
3. Map TypeScript exported symbols to aliases (export name vs local name).
4. Wire batch upsert at end of per-file index transaction.
5. Test entity registry count matches symbol count ± files.

## Files to create or modify

| Path | Action |
|------|--------|
| `app/main.py` | add entities batch route |
| `app/store.py` | add `upsert_entities()` |
| `app/models.py` | add `EntityRegistryBatch` model |
| `cli/src/indexer/entities.ts` | create |
| `tests/test_main.py` | add entity batch test |

## Verification

```powershell
.venv\Scripts\python.exe -m pytest tests/test_main.py -q -k entity
provena serve --detach
node cli/tests/entities.integration.mjs
```

## Handoff to next plan

PLAN-09 runs full pipeline including entity upsert.
PLAN-22 temporal graph seeds from entity canonical names.
---
## Completion
- **Completed**: 2026-06-23
- **PR**: #27 (Indexer MVP stack on main)
- **Verified by**: `cd cli && npm test` � full Indexer MVP suite green (smoke through index e2e + search)
- **Notes**: Shipped on `main` via squashed merge #27; roadmap housekeeping in follow-up PR. TS/JS only until plan 11.