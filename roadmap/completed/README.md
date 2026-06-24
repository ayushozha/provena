# Completed Plans

Finished vertical slices live here with the original plan content plus a
**Completion** block at the bottom.

## Indexer MVP (plans 01–10) — complete

| Plan | Focus |
|------|-------|
| 01 | CLI package foundation (`@provena/cli`, build, smoke) |
| 02 | `provena init` + `.provena/` config |
| 03 | `provena serve` local SQLite store |
| 04 | Gitignore-aware file discovery |
| 05 | Tree-sitter TS/JS chunking |
| 06 | Chunk → memory emission |
| 07 | Code graph relations |
| 08 | Entity registry for symbols |
| 09 | `provena index` end-to-end |
| 10 | `provena search` from terminal |

**Verify:** `cd cli && npm test`

**Next:** `roadmap/plan/11-multi-language-chunking.md` and beyond, or plan 26 for npm publish.

## Server-side plans also landed (not moved here yet)

Plans 15 (Postgres store) and 18 (Redis hot cache) are implemented on `main` but
still listed under `roadmap/plan/` until their housekeeping PR.