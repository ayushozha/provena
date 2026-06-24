# @provena/cli

Index TypeScript and JavaScript repos into the Provena governed memory plane and
search them from the terminal.

**Indexer MVP (plans 01–10):** `init` → `serve` → `index` → `search` for TS/JS.

## npm install (your repo)

```powershell
npm install -D @provena/cli
```

You still need the Python store once — see [docs/QUICKSTART.md](../docs/QUICKSTART.md)
(`PROVENA_STORE_ROOT` → clone of this repo + `pip install -e .`).

```powershell
npx provena init
npx provena serve --detach
npx provena index
npx provena search "authentication"
```

**Publish:** tag `cli-v0.1.0` (patch: `cli-v0.1.1`, …). Requires `NPM_TOKEN` in GitHub
secrets. First public version is always **`0.1.0`**.

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

Built output lives under `dist/`.

## Versioning

`@provena/cli` uses **full semver** (`MAJOR.MINOR.PATCH`).

### Rules (mandatory)

1. **Stay on `0.1.x` for all routine releases.** Bump **patch** only:
   `0.1.0` → `0.1.1` → `0.1.2`. Never use two-segment versions (`0.1`, `0.2`).
2. **`0.2.0` and higher minors are major releases only.** Do not move to `0.2.x`
   for a feature slice, docs pass, or housekeeping PR. A minor bump requires an
   explicit major-release decision (product + changelog), and updating
   `cli/tests/smoke.mjs` to allow the new minor line.
3. **First npm publish starts fresh at `0.1.0`.** The public package does not
   inherit monorepo-internal version history; deployment begins at `0.1.0` on npm.

| Release kind | Version | Allowed? |
|--------------|---------|----------|
| First npm publish / Indexer MVP | `0.1.0` | Yes — package start |
| Fix, test, docs, small feature | `0.1.1`, `0.1.2`, … | Yes — patch only |
| Major product milestone | `0.2.0` | Only with explicit major-release approval |
| Casual minor bump | `0.1.x` → `0.2.0` | **No** |

- Current: `0.1.0` (`package.json`)
- npm git tags: `cli-v0.1.0`, `cli-v0.1.1`, …
- CLI version is independent of the Python store / server version