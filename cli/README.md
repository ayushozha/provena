# @provena/cli

[![npm release](https://img.shields.io/badge/npm-first%20release%20pending-lightgrey)](../docs/PUBLISH_CLI.md)
[![Node.js](https://img.shields.io/badge/node-%3E%3D18.14.1-43853d)](./package.json)
[![CLI CI](https://github.com/ayushozha/provena/actions/workflows/ci-cli.yml/badge.svg)](https://github.com/ayushozha/provena/actions/workflows/ci-cli.yml)

Install a living, cited repository brain for coding agents. The portable path
requires Node.js only; Python and external databases are optional extensions.

## Install

The package is not yet published to npm. From a Provena source checkout, build
and install the tarball into the target Git repository. Production dependencies
are bundled so the portable runtime is carried by the archive rather than
resolved later from the registry:

```powershell
Push-Location C:\path\to\provena\cli
npm ci
npm pack
$ProvenaTarball = (Resolve-Path .\provena-cli-0.1.0.tgz)
Pop-Location

Set-Location C:\path\to\your-repository
npm install --save-dev $ProvenaTarball
npx provena init
```

The current dry-run package is about 10 MB compressed and 78 MB unpacked with
127 bundled production packages. Treat `npm pack --dry-run --json` as the
authoritative footprint for a release candidate.

After the first public release:

```powershell
npm install --save-dev @provena/cli
npx provena init
# or:
npx @provena/cli init
```

The command generates the tracked brain/map/graph/ledger/views and installs an
ignored persistent runtime, agent instructions, project MCP configs, safe Git
refresh hooks, and the fixed-cadence daemon. Use these opt-outs when needed:

```text
--no-agents  --no-hooks  --no-mcp  --no-runtime  --no-daemon
```

Hooks, MCP, and the daemon invoke the persisted runtime, so `--no-runtime`
requires `--no-hooks --no-mcp --no-daemon` as well.

The installer preserves existing instruction/config content through managed
blocks and will not edit a global or out-of-repository Git hooks path.

## Everyday workflow

```powershell
npx provena session start "fix the login redirect" --agent codex
npx provena context "fix the login redirect" --path src/auth/session.ts
npx provena remember mistake "Refresh token race" `
  --authority human `
  --body "Do not rotate the same refresh token in concurrent requests." `
  --source src/auth/session.ts:80-112 `
  --trigger refresh-token
npx provena refresh
npx provena checkpoint --summary "Redirect fixed; auth tests pass" --authority human
npx provena harness verify
```

## Commands

| Command | Description |
|---|---|
| `init` | Complete one-click repository installation |
| `refresh`, `index` | Regenerate deterministic repo memory artifacts |
| `context`, `search` | Build a cited task packet |
| `remember` | Append a typed explicit memory event |
| `checkpoint` | Append a handoff with current Git state |
| `session start` | Refresh and emit boot context for an agent session |
| `status` | Show freshness and integration health |
| `graph stats\|neighbors\|path\|components` | Explore the local repo graph |
| `graph sync neo4j` | Synchronize the graph into Neo4j |
| `mcp install\|serve` | Repair configs or run the stdio MCP server |
| `agents install` | Repair managed agent instructions |
| `daemon start\|stop\|status` | Control fixed-cadence refresh |
| `harness verify` | Verify hashes, determinism, graph, citations, and budgets |
| `index --store`, `search --store` | Use the optional governed-memory HTTP store |
| `serve`, `doctor`, `watch` | Operate the optional Python local store |

Run `npx provena <command> --help` for command options.

## Durable files

Commit these files:

```text
.provena/config.json
.provena/repo.brain.md
.provena/repo.map.json
.provena/graph.json
.provena/manifest.json
.provena/schema/memory-event.schema.json
.provena/memory/events.jsonl
.provena/views/*.md
.provena/agent-instructions.md
```

Cache, context packets, runtime dependencies, daemon state, and local databases
are selectively ignored. Provena never adds a blanket `.provena/` ignore.

The tracked `.provena/config.json` holds repository identity, tenant/project
scope, optional-store URL/database path, and scan include/exclude globs.
Managed blocks or entries may also appear in `AGENTS.md`, `CLAUDE.md`,
`.github/copilot-instructions.md`,
`.cursor/rules/provena.mdc`, `.mcp.json`, `.codex/config.toml`,
`.vscode/mcp.json`, and eligible repo-local Git hooks. Existing conflicting MCP
entries are preserved and reported instead of overwritten.

## MCP

`init` writes project configs for Claude-compatible `.mcp.json`, Cursor, Codex,
and VS Code. The stdio server provides brain/map/graph/manifest/memory resources
and context, refresh, remember, graph-neighborhood, and graph-path tools.

The Git-tracked ledger and local MCP accept only `public` and `internal`
memories. `confidential` and `restricted` writes are rejected; use the governed
store for sensitive material. Likely credential-like content is rejected by a
best-effort detector, not a substitute for a secret scanner or access control.

## Neo4j

```powershell
$env:PROVENA_NEO4J_URI = "neo4j+s://example.databases.neo4j.io"
$env:PROVENA_NEO4J_USERNAME = "neo4j"
$env:PROVENA_NEO4J_PASSWORD = "<secret-manager-value>"
$env:PROVENA_NEO4J_DATABASE = "neo4j" # optional
npx provena graph sync neo4j
```

The adapter uses the official driver, composite repo/node identity, batched
transactional projection, fingerprint-based stale cleanup, and no credentials
in tracked configuration. It mirrors the current code graph only; temporal
memory/event history and incremental Neo4j CDC remain roadmap.

## Optional governed store

The repo brain does not require Python. Teams that need the full HTTP memory
plane can run the repository's Python store and use the legacy semantic index:

```powershell
$env:PROVENA_STORE_ROOT = "C:\path\to\provena"
npx provena serve --detach
npx provena index --store
npx provena search --store "authentication" --limit 10
```

The portable path uses stdio MCP and opens no network port. The CLI-managed
store defaults to `127.0.0.1:18092`.

The store supports SQLite FTS plus sqlite-vec KNN when available (otherwise a
linear cosine scan). PostgreSQL uses `tsvector` FTS and currently performs the
same linear vector fallback; pgvector KNN remains roadmap. See
[`docs/QUICKSTART.md`](../docs/QUICKSTART.md) and
[`docs/REPO_MEMORY_ARCHITECTURE.md`](../docs/REPO_MEMORY_ARCHITECTURE.md).

## Programmatic API

```ts
import {
  appendMemoryEvent,
  buildContextPacket,
  pageRank,
  readMemoryLedgerSnapshot,
  refreshRepoBrain,
  syncGraphToNeo4j,
  verifyRepoMemory,
} from "@provena/cli";
```

For snapshot attestation, pass `buildContextPacket` the `memory` snapshot from
`refreshRepoBrain` or `readMemoryLedgerSnapshot`. Its `memoryFingerprint` is the
SHA-256 of the exact raw ledger bytes, so it matches manifest and session
metadata produced from the same snapshot, even when blank lines or line endings
differ. The backward-compatible
`MemoryEvent[]` input reconstructs canonical JSONL and cannot preserve those
otherwise invisible byte differences.

## Test gate

```powershell
cd cli
npm ci
npm test
```

The suite builds TypeScript and runs brain/event/schema tests, graph algorithms,
context budgets, agent/MCP integration, Neo4j projection, security, packed npm
installation into a fresh repo, optional-store E2E, incremental rollback,
watch/search, and concurrency regressions.

## Versioning

The first public package is `0.1.0`. Routine releases remain on `0.1.x` and
bump patch only. `0.2.0` requires an explicitly approved major product release.
Versions always use full `MAJOR.MINOR.PATCH` form.

## Clone, upgrade, and removal

The committed brain does not include the machine-local runtime or client/hook
state. After cloning, run `npx provena init` to rehydrate those integrations.
After updating the package, run `npx provena init` again to repair managed
blocks and refresh artifacts. To remove the installation today, stop the
daemon, remove the Provena-managed blocks/config entries/hooks, uninstall the
package, and delete `.provena/` only after exporting any ledger events you need.
First-class `upgrade` and `uninstall` commands are not implemented yet.

## License

No license file is currently checked in. Treat the package source as all rights
reserved until an explicit license is added.
