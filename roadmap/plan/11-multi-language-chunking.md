# PLAN-11: Multi-Language Chunking

## Goal

Extend the indexer beyond TypeScript to Python, Go, and Rust using tree-sitter
grammars — same `CodeChunk` contract, language-specific chunker modules.

## Why this is its own plan

Each grammar has edge cases; delivering all languages in one plan creates an
untestable mega-diff. One plan, four chunkers, one integration point.

## Prerequisites

- PLAN-05 complete (chunker pattern established)
- PLAN-09 complete (`provena index` orchestration exists)

## Success criteria

- [ ] `chunkPythonFile`, `chunkGoFile`, `chunkRustFile` return valid `CodeChunk[]`
- [ ] `provena index` auto-selects chunker by `DiscoveredFile.language`
- [ ] Provena dogfood repo indexes `.py`, `.go`, `.rs` files (not only TS)
- [ ] Tests per language with fixture files in `cli/fixtures/`
- [ ] Import extraction works for Python `import`/`from` and Go `import` blocks

## Scope

### In scope

- `cli/src/indexer/chunkers/python.ts`
- `cli/src/indexer/chunkers/go.ts`
- `cli/src/indexer/chunkers/rust.ts`
- `cli/src/indexer/chunkers/index.ts` — dispatcher

### Out of scope

- Markdown/docs chunking (future `provena index --docs`)
- Java, C#

## Implementation

### Steps

1. Add grammars: `tree-sitter-python`, `tree-sitter-go`, `tree-sitter-rust`.
2. Mirror TS chunker structure; adapt node types per language.
3. Update `discover.ts` language map if needed.
4. Update `relations.ts` import resolver for Python/Go module paths (best-effort).
5. Run full index on Provena repo; verify memory count includes non-TS files.

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/indexer/chunkers/python.ts` | create |
| `cli/src/indexer/chunkers/go.ts` | create |
| `cli/src/indexer/chunkers/rust.ts` | create |
| `cli/src/indexer/chunkers/index.ts` | create |
| `cli/fixtures/sample.py`, `sample.go`, `sample.rs` | create |
| `cli/tests/chunk-multilang.test.mjs` | create |

## Verification

```powershell
cd cli && npm run build
node tests/chunk-multilang.test.mjs
cd C:\Users\ayush\Desktop\YC\provena
provena index
# verify app/store.py and control-plane/*.go appear in search results
provena search "ProvenaStore"
```

## Handoff to next plan

PLAN-12 incremental index benefits from all languages sharing hash in index state.