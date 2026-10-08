# @provena/cli

[![npm release](https://img.shields.io/badge/npm-first%20release%20pending-lightgrey)](../docs/PUBLISH_CLI.md)
[![Node.js](https://img.shields.io/badge/node-%3E%3D22.17.0-43853d)](./package.json)
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

The current dry-run package is about 11 MB compressed and 86 MB unpacked with
113 bundled production packages. Treat `npm pack --dry-run --json` as the
authoritative footprint for a release candidate.

After the first public release:

```powershell
npm install --save-dev @provena/cli
npx provena init
# or:
npx @provena/cli init
```

The command generates the tracked brain/map/graph/maintenance-plan/ledger/views
and installs an ignored persistent runtime, agent instructions, project MCP
configs, safe Git refresh hooks, and the fixed-cadence daemon. Use these opt-outs
when needed:

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
npx provena maintain plan --limit 10
$MaintenanceTask = "task-id-from-the-plan"
npx provena maintain context $MaintenanceTask --max-tokens 1500
npx provena remember mistake "Refresh token race" `
  --authority human `
  --body "Do not rotate the same refresh token in concurrent requests." `
  --source src/auth/session.ts:80-112 `
  --trigger refresh-token
npx provena refresh
npx provena checkpoint --summary "Redirect fixed; auth tests pass" --authority human
npx provena sync store --dry-run
npx provena harness verify
```

### Automatic source-grounded memory

Every normal refresh now reconciles the manifest facts already present in the
repo map into the append-only ledger. A detected package becomes an observed
`fact`; each declared invocation such as `npm run test` or `make build` becomes
an observed `workflow`. Generated events use tool authority, internal
sensitivity, the manifest's scanned SHA-256, and a reserved Provena ownership
contract. They never infer a decision, preference, or rationale.

The lifecycle is append-only: first observation adds a memory, the same
declaration is a byte-stable no-op, a changed declaration supersedes its prior
head, and a confirmed removal appends a retraction tombstone. Reappearance
continues that lineage with a new ID. Capped, unreadable, malformed Node, or
excluded manifests defer apparent removals instead of converting incomplete
scans into false deletions. Explicit human and agent memories are never managed by this
reconciler, and a higher-authority successor blocks an automatic override.
The derived repo map exposes this evidence as `scan.complete` and a bounded
`scan.warnings` list so agents can distinguish a complete observation from a
degraded one.

This first automatic extractor remembers package names/dependency names and
command invocations exposed by `repo.map.json`. The current map does not expose
dependency versions or script bodies, so version-only and script-body-only
changes are not claimed as semantic memory updates. Refresh JSON, MCP refresh,
and daemon logs report candidate, add, no-op, supersede, retract, deferred,
conflict, and elapsed-millisecond counters. The behavior remains Node-only,
offline, and shared by manual refresh, session start, Git hooks, MCP, and the
fixed-cadence daemon.

### Deterministic maintenance proposals

The same refresh generation compiles `.provena/maintenance.plan.json` from the
committed repo map and canonical active ledger heads. It proposes bounded review
tasks for memories with no evidence/scope, changed source SHA-256 hashes, source or scope paths absent from a
complete current map, and exact normalized active-memory overlaps. Incomplete
scans defer path-absence and source-change advice. The compiler performs no model call, network
request, database query, pairwise semantic comparison, or filesystem rescan.

`maintain plan` defaults to 32 returned tasks and emits a bounded view envelope;
the full artifact remains the manifest-attested plan. `maintain context`
defaults to 1500 tokens and writes only the ignored
`.provena/context/latest.md` or `latest.json` output after its one normal
refresh. Neither command spawns a process, approves/applies a proposal, or
changes the ledger beyond source-grounded events that normal refresh itself may
append. Ordinary `context` accepts up to 32 repeatable exact
`--memory-id <event-id>` selectors and retains the existing citation,
sensitivity, effective-time, graph, and budget rules. Semantic consolidation,
decay, autonomous subagent DAG execution, and automatic proposal application
remain roadmap.

## Commands

| Command | Description |
|---|---|
| `init` | Complete one-click repository installation |
| `refresh`, `index` | Regenerate artifacts, including the maintenance plan, and reconcile source-grounded memory |
| `context`, `search` | Build a cited task packet |
| `maintain plan [--limit N] [--json]` | List a bounded view of deterministic review proposals |
| `maintain context <task-id> [--max-tokens N] [--json]` | Compile one cited proposal packet |
| `remember` | Append a typed explicit memory event |
| `procedure learn\|approve\|outcome\|recall\|inspect` | Capture, review, attest, and retrieve structured tool sequences |
| `capture install\|uninstall\|hook\|list\|draft` | Opt into native Codex/Claude observations and prepare local review drafts |
| `checkpoint` | Append a handoff with current Git state |
| `session start` | Refresh and emit boot context for an agent session |
| `status` | Show freshness and integration health |
| `graph stats\|neighbors\|path\|components` | Explore the local repo graph |
| `graph sync neo4j` | Synchronize the graph into Neo4j |
| `mcp install\|serve` | Repair stdio configs, serve stdio, or opt into loopback HTTP |
| `agents install` | Repair managed agent instructions |
| `daemon start\|stop\|status` | Control fixed-cadence refresh |
| `sync store [--json\|--dry-run]` | Atomically project the exact repo ledger into governed storage |
| `harness verify` | Verify hashes, determinism, graph, citations, and budgets |
| `index --store`, `search --store` | Use the optional governed-memory HTTP store |
| `serve`, `doctor`, `watch` | Operate the optional Python local store |

Run `npx provena <command> --help` for command options.

`sync store` reads and validates the ledger once, sends those exact bytes and
their SHA-256 to the configured loopback store, and prints event/projection
counts plus server phase timings. Identical replay is a no-op. The command is
explicit in this release: refresh, context, MCP, and the daemon remain fully
offline when the store is unavailable.

For an authenticated gateway, provide the API key only through the environment:

```powershell
$env:PROVENA_API_KEY = "<secret-manager-value>"
npx provena sync store --json
```

The tracked config records the fixed `store_api_key_env` name
`PROVENA_API_KEY`, never the credential. Repositories cannot select a different
environment secret. When that variable is set, sync sends it as a bearer token;
when absent, the existing direct-local
tenant/principal/role headers remain available for an auth-disabled loopback
store. Sync output is a fixed response schema and never includes request
credentials or unrecognized fields reflected by a server.

Remote governed stores require HTTPS and the explicit local opt-in
`PROVENA_ALLOW_REMOTE_STORE=1`; loopback HTTP remains the zero-config default.

## Durable files

Commit these files:

```text
.provena/config.json
.provena/repo.brain.md
.provena/repo.map.json
.provena/graph.json
.provena/maintenance.plan.json
.provena/manifest.json
.provena/schema/memory-event.schema.json
.provena/memory/events.jsonl
.provena/views/*.md
.provena/agent-instructions.md
```

Cache, context packets, runtime dependencies, daemon state, and local databases
are selectively ignored. Provena never adds a blanket `.provena/` ignore.

The tracked `.provena/config.json` holds repository identity, tenant/project
scope, optional-store URL/database path, the non-secret store API-key
environment-variable name, and scan include/exclude globs.
Managed blocks or entries may also appear in `AGENTS.md`, `CLAUDE.md`,
`.github/copilot-instructions.md`,
`.cursor/rules/provena.mdc`, `.mcp.json`, `.codex/config.toml`,
`.vscode/mcp.json`, and eligible repo-local Git hooks. Existing conflicting MCP
entries are preserved and reported instead of overwritten.

## MCP

`init` writes project configs for Claude-compatible `.mcp.json`, Cursor, Codex,
and VS Code. Those managed configs stay on stdio. The stdio server provides
five brain/map/graph/manifest/memory resources and ten tools: context,
refresh, remember, graph-neighborhood, graph-path, read-only maintenance-plan,
read-only maintenance-context, procedure-learn, procedure-outcome, and read-only
procedure-recall. Loopback HTTP exposes the same surface. Human procedure
approval is available through the CLI only. See the
[procedure guide](../docs/PROCEDURAL_MEMORY.md) for payloads and limitations.

Native tool capture is a separate opt-in integration: `provena capture install
--provider codex` or `--provider claude`. It requires the persisted runtime and
the client's hook trust settings. Filtered observations and incomplete drafts
remain in ignored local cache; they enter the ledger only through an explicit
`procedure learn` call. Follow the [capture guide](../docs/TOOL_CAPTURE.md) before
enabling or sharing captured data.

For a local client that requires a Streamable HTTP URL, run:

```powershell
npx provena mcp serve --http                 # http://127.0.0.1:18093/mcp
npx provena mcp serve --http --port 19093    # optional canonical port
```

Health is `GET http://127.0.0.1:18093/healthz`. This foreground command stops
on Ctrl+C or SIGTERM and is also available through the persisted
`.provena/runtime/runtime.mjs`. Each POST uses a fresh stateless JSON-response
transport; GET/SSE and DELETE/session flows are not exposed.

The HTTP mode binds only `127.0.0.1` and checks local Host and exact local
Origin values, but it has no authentication: any local process can invoke read
and mutation tools while it runs. It does not provide TLS, remote binding,
permissive CORS, daemonization, rate limiting, client-config migration, or a
replacement for the authenticated enterprise MCP proxy.

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
in tracked configuration. Graph v2 includes current repository topology plus
every namespaced memory event, direct supersession/source/applicability edges,
and producer-effective intervals. The projection token binds both the source
fingerprint and exact raw-ledger fingerprint, so memory-only changes replace
the prior projection safely. The adapter is mock-driver tested; live Neo4j
proof and incremental CDC remain roadmap.

Use one explicit effective-time boundary for historical memory views:

```powershell
npx provena context "release" --memory-as-of 2026-07-13T12:00:00.000Z
npx provena graph neighbors <id-or-path> --memory-as-of 2026-07-13T12:00:00.000Z
npx provena graph timeline <memory-event-id>
```

Repository topology remains current in those queries. Backdated appends can
revise earlier effective views; transaction-time/bi-temporal history and
historical code snapshots are not implemented.

## Optional governed store

The repo brain does not require Python. Teams that need the full HTTP memory
plane can run the repository's Python store and use the legacy semantic index:

```powershell
$env:PROVENA_STORE_ROOT = "C:\path\to\provena"
npx provena serve --detach
npx provena index --store
npx provena search --store "authentication" --limit 10
```

The portable path defaults to stdio and opens no port. Explicit
`mcp serve --http` opens only the foreground loopback endpoint on
`127.0.0.1:18093`. The separate CLI-managed store remains on
`127.0.0.1:18092` by default.

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
  compileMaintenanceTaskContext,
  maintenancePlanView,
  pageRank,
  readRepoBrainArtifacts,
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

`readRepoBrainArtifacts` verifies every managed artifact against deterministic
regeneration and the manifest while holding the repo-brain lock. It returns the
attested `maintenancePlan` beside the map, graph, manifest, exact memory
snapshot, and captured verified `artifactContents`; callers should serve those
captured bytes instead of reopening files after verification. Use
`maintenancePlanView` for a bounded listing and
`compileMaintenanceTaskContext` for one proposal packet; neither helper
refreshes, writes, approves, or executes work.

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
