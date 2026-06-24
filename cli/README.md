# @provena/cli

Index TypeScript and JavaScript repos into the Provena governed memory plane and
search them from the terminal.

**Indexer MVP (plans 01–10):** `init` → `serve` → `index` → `search` for TS/JS.
See [roadmap/completed/](../roadmap/completed/) for shipped plan notes; [roadmap/plan/](../roadmap/plan/) for what is next.

## Monorepo quick start

From the Provena repo root (Python 3.12+ and Node 18+):

```powershell
cd cli
npm install
npm test

# In a git repo you want to index (or this monorepo):
cd ..
python -m uvicorn app.main:app --port 8092   # or: npx provena serve --detach
cd cli
node dist/cli.js init
node dist/cli.js index
node dist/cli.js search "authentication"
```

`provena serve` spawns uvicorn against the repo-root `app/` store when run from
a checkout that contains it.

## Commands

| Command | Status |
|---------|--------|
| `init` | Initialize `.provena/config.json` in a git repo |
| `config show` | Print resolved local config |
| `discover` | List indexable TS/JS paths (debug) |
| `serve` | Start local SQLite store (`--detach`, `--stop`) |
| `doctor` | Config + store health |
| `index` | Discover → chunk → emit memories + relations (TS/JS) |
| `search` | Query indexed memories (`--json`, `--limit`, `--explain`) |
| `status`, `connect`, `mcp` | Stubs — see `roadmap/plan/` |

## Test gate

```powershell
cd cli
npm test
```

Runs the full Indexer MVP suite: smoke, config, pack, security, discovery,
chunking, emit, relations, index e2e (spawns store), search, concurrency.

Individual targets: `npm run test:discover`, `test:chunk-typescript`, `test:emit`,
`test:index-e2e`, `test:search`.

## Programmatic API

```ts
import { chunkTypeScriptFile, runIndex, ProvenaClient } from "@provena/cli";
```

Built output lives under `dist/`. Package is not published to npm yet (plan 26).