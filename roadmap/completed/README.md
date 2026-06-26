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

## npm publish (plan 26) — publish-ready

| Plan | Focus |
|------|-------|
| 26 | Quickstart, pack-install test, `publish-cli.yml`, `@provena/cli@0.1.0` |

Tag `cli-v0.1.0` after adding `NPM_TOKEN` to GitHub secrets.

## Incremental index (plan 12) — complete

| Plan | Focus |
|------|-------|
| 12 | SHA-256 diff skip, `--full`, delete-on-remove |

**Next:** `roadmap/plan/13-provena-watch.md` or plan 11 multi-language chunking.

## Server-side plans also landed (not moved here yet)

Plans 15 (Postgres store) and 18 (Redis hot cache) are implemented on `main` but
still listed under `roadmap/plan/` until their housekeeping PR.