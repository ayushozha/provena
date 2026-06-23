# Provena Roadmap

This folder tracks work toward the grand vision:

> `npm install provena` (or `npx provena init`) in any repo → local SQLite memory of the
> entire codebase as a governed knowledge graph → optional PostgreSQL for team/prod →
> Redis for caching → hybrid RAG retrieval agents can query immediately.

## Layout

```
roadmap/
├── README.md          ← you are here
├── plan/              ← self-contained, end-to-end feature plans (not started)
└── completed/         ← finished plans moved here with completion notes
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

| # | Plan | Focus |
|---|------|-------|
| 01 | `01-cli-package-foundation.md` | Publishable `@provena/cli` npm package skeleton |
| 02 | `02-local-init-and-config.md` | `provena init` → `.provena/` layout + config |
| 03 | `03-embedded-local-store.md` | CLI spawns standalone SQLite store process |
| 04 | `04-repo-file-discovery.md` | Gitignore-aware file walker |
| 05 | `05-tree-sitter-chunking-typescript.md` | AST chunking for TS/JS |
| 06 | `06-memory-emission-from-chunks.md` | Chunks → memories + source refs |
| 07 | `07-code-graph-relations.md` | imports / defined_in / calls edges |
| 08 | `08-entity-registry-for-symbols.md` | Canonical symbol registry |
| 09 | `09-repo-index-command.md` | `provena index` end-to-end (TS/JS) |
| 10 | `10-cli-search-command.md` | `provena search` from terminal |
| 11 | `11-multi-language-chunking.md` | Python, Go, Rust chunkers |
| 12 | `12-incremental-index-fingerprints.md` | Re-index only changed files |
| 13 | `13-provena-watch.md` | File watcher + debounced re-index |
| 14 | `14-git-hook-integration.md` | Optional pre-commit index hook |
| 15 | `15-postgres-store-backend.md` | `PostgresStore` implementing store contract |
| 16 | `16-pgvector-migration.md` | Postgres schema + pgvector KNN |
| 17 | `17-provena-connect-postgres.md` | `provena connect` + env-based backend switch |
| 18 | `18-redis-hot-cache.md` | `RedisHotCache` for search responses |
| 19 | `19-redis-embedding-cache.md` | Embedding cache by content hash |
| 20 | `20-compose-redis-wire-up.md` | Redis in docker-compose polyglot stack |
| 21 | `21-trigger-phrases-from-code.md` | Register symbol names in orchestration |
| 22 | `22-temporal-graph-code-navigation.md` | Graph traversal API for code symbols |
| 23 | `23-mcp-config-generator.md` | `provena mcp` Cursor/Claude setup |
| 24 | `24-index-status-and-health.md` | `provena status` operator view |
| 25 | `25-code-recall-benchmark.md` | Eval harness for repo indexing quality |
| 26 | `26-npm-publish-and-quickstart.md` | npm publish + landing quickstart docs |

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