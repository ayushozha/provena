# PLAN-05: Tree-sitter Chunking (TypeScript/JavaScript)

## Goal

Parse TypeScript and JavaScript source files into semantic chunks (module,
class, function, method, interface) with line ranges and raw source text —
ready to become memories.

## Why this is its own plan

Chunk quality drives RAG quality. Tree-sitter integration is non-trivial
(native bindings, grammar WASM) and deserves isolated verification.

## Prerequisites

- PLAN-04 complete (`DiscoveredFile` with `language: "typescript" | "javascript"`)

## Success criteria

- [ ] `chunkTypeScriptFile(path, content)` returns `CodeChunk[]`
- [ ] Each chunk has: `kind`, `name`, `startLine`, `endLine`, `content`, `parentSymbol?`
- [ ] Chunks cover: imports block, exported functions, classes, interfaces
- [ ] Chunks do not overlap; union covers meaningful symbols (not every comment)
- [ ] Handles parse errors gracefully (single `artifact` fallback chunk for whole file)
- [ ] Test fixture: sample TS file → expected chunk count and names

## Scope

### In scope

- `cli/src/indexer/chunkers/typescript.ts`
- `web-tree-sitter` + `tree-sitter-typescript` grammars
- Chunk kinds: `module`, `import`, `class`, `function`, `method`, `interface`, `type_alias`

### Out of scope

- Python / Go / Rust (PLAN-11)
- Emitting memories (PLAN-06)

## Implementation

### Steps

1. Add tree-sitter WASM init once per process.
2. Walk AST; emit chunk when entering function/class/interface nodes.
3. Capture leading doc comment as `docstring` field on chunk.
4. Extract import specifiers into `imports: string[]` on module chunk.
5. Export unified `CodeChunk` interface in `cli/src/indexer/types.ts`.

### CodeChunk shape

```typescript
interface CodeChunk {
  filePath: string;
  language: string;
  kind: string;
  name: string | null;
  startLine: number;
  endLine: number;
  content: string;
  docstring?: string;
  imports?: string[];
  exported?: boolean;
}
```

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/indexer/types.ts` | create |
| `cli/src/indexer/chunkers/typescript.ts` | create |
| `cli/fixtures/sample.ts` | create test fixture |
| `cli/tests/chunk-typescript.test.mjs` | create |
| `cli/package.json` | add tree-sitter deps |

## Verification

```powershell
cd cli && npm run build
node tests/chunk-typescript.test.mjs
```

## Handoff to next plan

PLAN-06 maps `CodeChunk` → Provena `MemoryCreate` payloads.
PLAN-07 uses `imports` to build graph edges.
---
## Completion
- **Completed**: 2026-06-23
- **PR**: #27 (Indexer MVP stack on main)
- **Verified by**: `cd cli && npm test` � full Indexer MVP suite green (smoke through index e2e + search)
- **Notes**: Shipped on `main` via squashed merge #27; roadmap housekeeping in follow-up PR. TS/JS only until plan 11.