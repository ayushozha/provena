# PLAN-06: Memory Emission from Chunks

## Goal

Convert `CodeChunk` objects into Provena memory write payloads and POST them to
the store API — one `artifact` per file plus `fact` memories per semantic chunk.

## Why this is its own plan

Separates "what to store" from "how to discover/chunk" and validates the store
API contract before relations or graph work.

## Prerequisites

- PLAN-03 complete (store running)
- PLAN-05 complete (`CodeChunk` type)

## Success criteria

- [ ] `emitMemories(chunks, fileMeta, scope)` returns created memory IDs
- [ ] File-level `artifact` memory: title = relative path, content = file summary stub
- [ ] Chunk-level `fact` memories: content = chunk source, title = `path::symbol`
- [ ] Every memory has `source_references` pointing to `file://` URI with line span
- [ ] `entity_keys` includes `symbol` and `file:<path>`
- [ ] `tags` includes `language`, `chunk_kind`, `indexed`
- [ ] Batch writes use intelligence `/v1/pipeline/write` OR store `/v1/memories` — pick one and document; prefer pipeline for embeddings
- [ ] Integration test: index one file → search returns chunk content

## Scope

### In scope

- `cli/src/indexer/emit.ts`
- HTTP client using `config.store_url` + scope from config
- Deduplication: skip if fingerprint exists (query store or track locally in `.provena/index-state.json`)

### Out of scope

- Relations between chunks (PLAN-07)
- Full repo index command (PLAN-09)

## Implementation

### Steps

1. Build `MemoryCreate` payload per chunk with `ScopeEnvelope` from config.
2. `source_references`:
   ```json
   {
     "source_type": "file",
     "source_id": "src/auth.ts",
     "uri": "file:///abs/path/src/auth.ts",
     "title": "src/auth.ts",
     "excerpt": "<first 200 chars>",
     "span_start": 42,
     "span_end": 88
   }
   ```
3. For file artifact, content = directory + exports list (from module chunk).
4. Write via `POST /v1/memories` for simplicity in v1; upgrade to pipeline in PLAN-09 if embeddings missing.
5. Persist `filePath → memory_id[]` in index state file for relations plan.

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/indexer/emit.ts` | create |
| `cli/src/client.ts` | create (thin fetch wrapper) |
| `.provena/index-state.json` | schema documented in PLAN-02 config README |
| `cli/tests/emit.integration.mjs` | create (needs running store) |

## Verification

```powershell
provena serve --detach
cd cli && node tests/emit.integration.mjs
# manual: POST one chunk, search "function authenticate"
```

## Handoff to next plan

PLAN-07 reads `index-state.json` memory IDs to create `memory_relations`.
PLAN-09 orchestrates discover → chunk → emit for all files.