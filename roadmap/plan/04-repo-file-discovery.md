# PLAN-04: Repo File Discovery

## Goal

Build a gitignore-aware file enumerator the indexer will call: given a repo root
and include/exclude globs from config, return the list of indexable file paths
with stable relative paths and content hashes.

## Why this is its own plan

Correct file discovery is foundational — wrong ignores mean leaked secrets or
missing code. Isolating it allows thorough testing without chunking or API calls.

## Prerequisites

- PLAN-02 complete (config `index.include` / `index.exclude`)

## Success criteria

- [ ] `enumerateFiles(repoRoot, config)` returns sorted relative paths
- [ ] Respects `.gitignore` (use `ignore` npm package or parse via `git check-ignore`)
- [ ] Applies config exclude globs on top of gitignore
- [ ] Skips binary files (extension blocklist + null-byte sniff)
- [ ] Returns `{ path, absolutePath, sha256, sizeBytes, language }` per file
- [ ] Unit tests cover: node_modules skipped, `src/foo.ts` included, `.env` skipped

## Scope

### In scope

- `cli/src/indexer/discover.ts`
- Language detection by extension map (ts, tsx, js, jsx, py, go, rs, md)
- Max file size cap (e.g. 512 KB) with skip + warn

### Out of scope

- Parsing file contents
- Writing to store

## Implementation

### Steps

1. Load gitignore from repo root; use `git check-ignore -v` for accuracy on Windows.
2. Walk with `fast-glob` respecting ignore rules.
3. Compute SHA-256 per file for incremental index (used in PLAN-12).
4. Export `discoverRepo(config): DiscoveredFile[]`.
5. Add CLI debug command: `provena discover --json` prints file list (dev aid).

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/indexer/discover.ts` | create |
| `cli/src/commands/discover.ts` | create (debug) |
| `cli/package.json` | add `fast-glob`, `ignore` deps |
| `cli/tests/discover.test.mjs` | create with fixture repo |

## Verification

```powershell
cd cli && npm run build
node tests/discover.test.mjs
cd C:\Users\ayush\Desktop\YC\provena
node cli/dist/cli.js discover --json | Select-Object -First 5
# confirm node_modules absent, app/main.py present
```

## Handoff to next plan

PLAN-05 consumes `DiscoveredFile[]` for TS/JS files only initially.
PLAN-12 reuses `sha256` for incremental indexing.