# Provena Roadmap

This folder tracks work toward the grand vision:

> `npm install provena` (or `npx provena init`) in any repo → local SQLite memory of the
> entire codebase as a governed knowledge graph → optional PostgreSQL for team/prod →
> Redis for caching → hybrid RAG retrieval agents can query immediately.

## Status

**Indexer MVP (plans 01–10): complete** on `main` — `provena init` → `serve` →
`index` → `search` for TypeScript/JavaScript. Verify with `cd cli && npm test`.
Completed plan files live in `completed/`; npm publish and fresh-machine
quickstart remain plan 26.

## Layout

```
roadmap/
├── README.md          ← you are here
├── plan/              ← upcoming vertical slices
└── completed/         ← finished plans with completion notes
```

## How to use a plan

Each file in `plan/` is **one focus, one vertical slice, end-to-end complete**:

1. Read prerequisites — prior plans must be done first.
2. Execute every step in the plan.
3. Run all verification commands — all must pass.
4. Move the file to `completed/` and append a short completion block at the bottom.

Plans are numbered for suggested order, but only **prerequisites** are hard
dependencies. Parallel work is fine when prerequisites do not overlap.

## Plan index (suggested order)

| # | Plan | Focus | Status |
|---|------|-------|--------|
| 01–10 | `completed/01-…` through `10-…` | Indexer MVP (CLI init → search) | **Done** — see `completed/` |
| 11 | `11-multi-language-chunking.md` | Python, Go, Rust chunkers | Planned |
| 12 | `12-incremental-index-fingerprints.md` | Re-index only changed files | Planned |
| 13 | `13-provena-watch.md` | File watcher + debounced re-index | Planned |
| 14 | `14-git-hook-integration.md` | Optional pre-commit index hook | Planned |
| 15 | `15-postgres-store-backend.md` | `PostgresStore` implementing store contract | Landed (#22); housekeeping pending |
| 16 | `16-pgvector-migration.md` | Postgres schema + pgvector KNN | Planned |
| 17 | `17-provena-connect-postgres.md` | `provena connect` + env-based backend switch | Planned |
| 18 | `18-redis-hot-cache.md` | `RedisHotCache` for search responses | Landed (#31); housekeeping pending |
| 19 | `19-redis-embedding-cache.md` | Embedding cache by content hash | Planned |
| 20 | `20-compose-redis-wire-up.md` | Redis in docker-compose polyglot stack | Planned |
| 21 | `21-trigger-phrases-from-code.md` | Register symbol names in orchestration | Planned |
| 22 | `22-temporal-graph-code-navigation.md` | Graph traversal API for code symbols | Planned |
| 23 | `23-mcp-config-generator.md` | `provena mcp` Cursor/Claude setup | Planned |
| 24 | `24-index-status-and-health.md` | `provena status` operator view | Planned |
| 25 | `25-code-recall-benchmark.md` | Eval harness for repo indexing quality | Planned |
| 26 | `26-npm-publish-and-quickstart.md` | npm publish + landing quickstart docs | Planned |

## Completion template

When moving a plan to `completed/`, append:

```markdown
---
## Completion
- **Completed**: YYYY-MM-DD
- **PR**: #NNN or commit SHA
- **Verified by**: command output summary
- **Notes**: anything the next plan owner should know
```