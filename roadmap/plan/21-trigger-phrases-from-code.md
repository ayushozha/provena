# PLAN-21: Trigger Phrases from Code

## Goal

During `provena index`, register exported function/class names as trigger phrases
on the Rust orchestration trigger index so preflight lookup finds code memories
with zero embedding cost.

## Why this is its own plan

Trigger index is a separate service (Rust :50051) with its own HTTP API — wiring
code symbols into it is an integration slice distinct from store writes.

## Prerequisites

- PLAN-09 complete (index command)
- Orchestration service runnable (polyglot or local rust binary)

## Success criteria

- [ ] Indexer POSTs trigger phrases for each exported symbol to orchestration API
- [ ] Phrase = symbol name + optional `Class.method` qualified form
- [ ] Trigger maps to chunk memory_id
- [ ] Read pipeline `_trigger_lookup` returns hits for exact symbol queries
- [ ] `config.orchestration_url` in `.provena/config.json` (default `http://127.0.0.1:50051`)
- [ ] Graceful skip when orchestration unreachable (warn, continue index)

## Scope

### In scope

- `cli/src/indexer/triggers.ts`
- Call orchestration `POST /trigger/register` (verify actual route in `orchestration/src/`)
- Index only exported/public symbols to limit noise

### Out of scope

- Fuzzy trigger matching
- Natural language triggers from comments

## Implementation

### Steps

1. Read orchestration HTTP API from `orchestration/src/main.rs`.
2. After memory emit, batch register triggers `{ phrase, memory_id, tenant_id }`.
3. Add `provena index --no-triggers` skip flag.
4. Integration test: index fixture → query orchestration lookup endpoint.

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/indexer/triggers.ts` | create |
| `cli/src/config.ts` | add `orchestration_url` |
| `cli/src/indexer/run.ts` | call trigger registration |
| `orchestration/src/` | verify/document register endpoint |

## Verification

```powershell
# start orchestration + store + intelligence per DEPLOYMENT.md
provena index --path cli/fixtures
curl -X POST http://127.0.0.1:50051/trigger/lookup -d '{"phrase":"authenticate",...}'
provena search "authenticate"
```

## Handoff to next plan

PLAN-22 combines trigger hits with temporal graph for code navigation.