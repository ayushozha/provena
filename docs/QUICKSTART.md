# Provena CLI — five-minute quick start

Create a living, agent-readable repository memory with Node.js 18.14.1 or newer.
The default path does not require Python, Docker, a model API, or a database server.

## 1. Install and initialize

`@provena/cli` is not yet published. First build a tarball from a Provena source
checkout, then install it in the existing Git repository. The archive bundles
the production dependency tree used by the portable runtime:

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

After the first public package release, use either registry form:

```powershell
npm install --save-dev @provena/cli
npx provena init
# or:
npx @provena/cli init
```

Initialization creates the tracked `.provena/` brain and ledger, persists an
ignored local runtime, installs agent/MCP integrations, safely adds repo-local
Git hooks when permitted, and starts a 15-minute refresh daemon.

For a CI fixture or a repo where background work is undesirable:

```powershell
npx provena init --no-daemon --no-hooks
```

## 2. Inspect the brain

```powershell
Get-Content .provena\repo.brain.md
npx provena status
npx provena graph stats
npx provena harness verify
```

## 3. Get task context

```powershell
npx provena context "change authentication middleware" `
  --path src/auth/middleware.ts `
  --symbol authenticate `
  --max-tokens 2500
```

The packet is printed and cached under `.provena/context/`. Results combine
exact matches, lexical relevance, graph proximity, and active memories. Every
item carries a file/line/symbol or memory-event citation.

To force one or more exact eligible active memories into the highest selection
tier, repeat `--memory-id` (up to 32 times):

```powershell
npx provena context "review the authorization decision" `
  --memory-id decision-auth-boundary `
  --memory-id workflow-auth-tests
```

## 4. Review deterministic maintenance proposals

```powershell
npx provena maintain plan --limit 32
npx provena maintain plan --limit 32 --json
$MaintenanceTask = "task-id-from-the-plan"
npx provena maintain context $MaintenanceTask --max-tokens 1500
```

The canonical `.provena/maintenance.plan.json` is regenerated and attested by
every normal refresh. The plan command returns a bounded view of exact evidence
gaps, paths absent from a complete current map, and exact active-memory
overlaps. The context command compiles one cited packet and caches it only as
the existing ignored `.provena/context/latest.md` or `latest.json` output.

These are proposal-only review surfaces. They do not spawn agents, approve or
apply changes, rewrite the ledger, perform semantic consolidation/decay, or add
a scheduler. Autonomous subagent DAG execution remains roadmap.

## 5. Record durable knowledge

```powershell
npx provena remember decision "Gateway owns authorization" `
  --authority human `
  --subject architecture `
  --body "Services trust only the gateway-issued principal." `
  --rationale "This keeps authorization policy auditable in one boundary." `
  --source control-plane/auth.go:42-80 `
  --importance 0.9 `
  --tag auth
```

Other kinds are `fact`, `workflow`, `mistake`, `preference`, `handoff`, and
`invariant`. Decisions, preferences, and rationale are explicit-only. Do not
put secrets in memory; Provena rejects likely credentials and private keys as
a best-effort guard. The tracked ledger accepts only `public` and `internal`
events. Store confidential/restricted material in the governed service instead.

## 6. Start and finish an agent session

```powershell
npx provena session start "repair token refresh" --agent codex

# ...work and validate...

npx provena checkpoint `
  --authority human `
  --summary "Token refresh repaired; focused and integration tests pass" `
  --next "Run deployment smoke test"
```

Managed `AGENTS.md`, `CLAUDE.md`, Cursor, and Copilot instructions tell agents
to follow this boot/context/checkpoint protocol automatically.

## Optional Neo4j projection

```powershell
$env:PROVENA_NEO4J_URI = "neo4j+s://example.databases.neo4j.io"
$env:PROVENA_NEO4J_USERNAME = "neo4j"
$env:PROVENA_NEO4J_PASSWORD = "<secret-manager-value>"
npx provena graph sync neo4j
```

## Optional governed-memory service

The larger Python/Go/Rust platform adds shared SQLite/PostgreSQL memory,
FTS/vector retrieval, governance, queues, lifecycle controls, and connected
mode. SQLite uses sqlite-vec KNN when available and otherwise a linear cosine
scan; PostgreSQL currently uses `tsvector` FTS plus the linear vector fallback.
pgvector KNN is roadmap. None of this is needed for the repo brain.

From a Provena source checkout:

```powershell
$env:PROVENA_STORE_ROOT = "C:\path\to\provena"
npx provena serve --detach
npx provena doctor
npx provena sync store --dry-run
npx provena sync store --json
npx provena index --store
npx provena search --store "authentication" --limit 10
```

`sync store` projects the complete canonical JSONL ledger; `index --store`
remains the deeper TS/JS chunk indexer. Sync verifies the exact raw-ledger
SHA-256, commits stable event/source/relation projections and its checkpoint in
one transaction, and treats an identical replay as a no-op. It does not run
automatic extraction over already-canonical events.

The portable repo-brain path defaults to stdio and opens no port. If a local
client requires Streamable HTTP, start the same MCP surface explicitly:

```powershell
npx provena mcp serve --http
# MCP:    http://127.0.0.1:18093/mcp
# Health: http://127.0.0.1:18093/healthz
```

Use `--port <port>` for another canonical local port and Ctrl+C to stop the
foreground listener. Managed MCP configs remain stdio. This loopback endpoint
is unauthenticated, so any local process can invoke both read and mutation
tools while it runs. Both transports expose five resources and seven tools,
including the two read-only maintenance tools. It has no TLS, remote binding, permissive CORS,
SSE/sessions, daemon mode, rate limiting, or enterprise-proxy guarantees.

The separate CLI-managed optional store defaults to `127.0.0.1:18092`, and
ordinary refresh does not fail when that optional store is absent.

## Clone, upgrade, and uninstall

Commit the durable `.provena/` artifacts, including `config.json`,
`maintenance.plan.json`, and the event ledger. Runtime dependencies, context
packets, daemon state, logs, and local databases remain ignored. After cloning,
run `npx provena init` to recreate the
runtime and managed agent/MCP/hook integrations. Run it again after upgrading
the package to refresh and repair the installation.

There is no automated uninstall command yet. Stop the daemon, remove
Provena-managed instruction/config/hook blocks, uninstall the package, and only
then remove `.provena/` after preserving any ledger events you need.

## Troubleshooting

| Symptom | Resolution |
|---|---|
| `no config ... run provena init` | Initialize from the repository root |
| Git hooks show `0` installed | A global/out-of-repo hooks path is configured; session + daemon refresh still work |
| Daemon should not run | `npx provena daemon stop`; re-init later with `--no-daemon` |
| MCP client cannot find Provena | Run `npx provena mcp install`, then restart the client |
| MCP client requires an HTTP URL | Run `npx provena mcp serve --http`, use `http://127.0.0.1:18093/mcp`, and keep the foreground process running |
| Neo4j sync reports missing env | Set URI, username, and password in the process environment |
| Optional store cannot start | Set `PROVENA_STORE_ROOT` to the source checkout containing `app/main.py` |

## Next reading

- [Repo Memory Architecture](./REPO_MEMORY_ARCHITECTURE.md)
- [Enterprise Memory Plan](./ENTERPRISE_MEMORY_PLAN.md)
- [CLI reference](../cli/README.md)
- [Polyglot architecture](../ARCHITECTURE.md)
