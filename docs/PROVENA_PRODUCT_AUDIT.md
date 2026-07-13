# Provena Audit — Founding-Engineer / Product-Architect Review

> **Scope:** Read-only audit of the Provena repository against the vision of an
> installable, agent-native memory system for software repositories
> (`npx provena init` → a durable, self-updating `.provena/` memory layer that
> coding agents consult before and after tasks).
>
> **Method:** Findings were verified against actual source files, not the PRD,
> `loop/prd.json`, or roadmap claims. Every claim below cites a real path/line
> confirmed during the audit.
>
> - **Repo:** `github.com/ayushozha/provena`
> - **Branch audited:** `feat/plan-13-watch`
> - **Date:** 2026-07-05
> - **A companion comparison against the vendored copy in `ayushozha/NeverZero`
>   (`services/provena`) was performed separately; conclusion: standalone is the
>   source of truth and is ahead — nothing needs to be pulled back from NeverZero
>   except, arguably, an operator "observability console" that standalone never had.**

---

## 1. Current repo reality check

**What actually exists (verified in code, not docs):**

| Layer | Real? | Evidence |
|---|---|---|
| **CLI** `@provena/cli` (~4,700 LOC TS) | ✅ Real | `cli/src/` — commands: `init, serve, doctor, discover, config-show, index, search, watch`. Stubs: `status, connect, mcp` (`cli/src/cli.ts` `runStub`). |
| **Tree-sitter indexer** | ✅ Real but **TS/JS-only** | `cli/src/indexer/run.ts:198` filters `discovered.filter(isTsJsFile)`; only `chunkers/typescript.ts` exists. |
| **Incremental index** | ✅ Real, well-tested | `cli/src/indexer/incremental.ts`, sha256 diff, `--full`, delete-on-remove; `cli/tests/incremental.test.mjs`. |
| **Store** (Python FastAPI) | ✅ Real, rich | `app/main.py` ~24 routes; `app/store.py` hybrid retrieval + governance. |
| **Vector index** | ✅ Real | sqlite-vec `vec0`, 768-dim, linear-scan fallback (`store.py:144-170`). Postgres path has **no pgvector** (deferred, `001_postgres.sql:3`). |
| **Intelligence service** (Python) | ✅ Real | write/read pipelines, rerank, contradiction, abstain, citations. |
| **Go control-plane** | ✅ Real | gateway `:8080`, MCP `:8090` (JSON-RPC+SSE, 9 CRUD tools), queue `:8091`, lifecycle `:8092`. |
| **Rust orchestration** | ✅ Real | `:50051` trigger index, context-budget knapsack, prompt assembly, `/preflight`. |
| **SDKs** (py/go/ts/rust) | ⚠️ Packaged, **not published**; Go/Rust "smoke-only" | `RELEASE_CHECKLIST.md:78-85`. |
| **`.provena/repo.brain.md` bootloader** | ❌ **Does not exist anywhere** | Zero matches for `repo.brain`/`brain.md` in code, tests, docs. |

**Claims-vs-reality mismatches (the important ones):**

- **`loop/prd.json` PROVENA-008 is a phantom "green."** It records a shipped `app/slack_connector.py` + startup registration as `testerExecutionStatus:"green"`. **No such file exists**, and `tests/test_main.py:1425` asserts the *opposite* ("No first-party Slack worker ships" → `provider_not_implemented`). This is the single most misleading claim in the repo. (It matches an intentional upstream removal — commit `8bf929a "drop fabricating Slack stub"` — that `prd.json` was never rolled back to reflect.)
- **`repo.brain.md`, the headline artifact of the vision, is absent in every form.** `.provena/` only ever holds `config.json`, `provena.db`, `index-state.json`, `last-index.json`, `store.pid`, `index-errors.log`.
- **The context-pack generator exists but is unreachable.** `store.py:690-753 agent_context()` and `temporal_graph()` are implemented but **never registered as HTTP routes** in `app/main.py`. The Go gateway proxies `/v1/agent/context`, `/v1/memories/graph/temporal`, `.../inspect`, `.../search/explain` — all of which **404** against the store. Broken gateway↔store contract.
- **Layout mismatch.** `AGENTS.md`, all of `loop/prd.json`, and `sdk/go` (`module .../portfolio-generator/services/provena/...`) assume Provena lives at `services/provena/` inside a monorepo. This checkout is repo-root. The loop's `verificationCommands` won't run as written here.
- **PROVENA-001 is internally contradictory** — `"passes": true` while `status`/`engineerStatus`/`testerExecutionStatus` are all `"blocked"` (`loop/prd.json:104,152`). The `passes` flags are not trustworthy.
- **`001_initial.sql` is stale** — missing `expires_at, memory_layer, acl_json, held, embedding_*`; patched at runtime via `store.py:3076-3087 ALTER TABLE`.
- **"Publish-ready" ≠ published** — the npm badge in `README.md:3` may not resolve; publish is gated on an untagged `cli-v0.1.0` + missing `NPM_TOKEN`.

**The core reality:** there are **two products in one repo with no reconciling doc.** The top-level docs (`README.md`, `PRODUCT.md`, `ARCHITECTURE.md`) and `loop/prd.json` describe an **enterprise governed memory-plane / Glean competitor** (connectors, RTBF, legal hold, polyglot topology). The `roadmap/` + `cli/` describe the **installable repo-memory tool** the vision wants. The enterprise half is far more built; the repo-memory half is a thin, TS/JS-only indexer feeding that enterprise backend.

---

## 2. Product gap analysis

### Install & onboarding
- `npx provena init` **exists** but only writes `.provena/config.json` + a `.gitignore` entry (`cli/src/commands/init.ts`). No brain, no agent wiring.
- **Works in an arbitrary repo? No.** `provena index`/`serve` require the Python store; the published npm package ships only `dist/` (`cli/package.json` `files:["dist"]`). Per `cli/README.md:14`, you must *also* clone the repo and `pip install -e .` and set `PROVENA_STORE_ROOT`. **The one-command install is not real.**
- Configures Claude Code / Codex / Cursor / MCP? **No.** No `.mcp.json`/`.cursorrules`/`CLAUDE.md` generation anywhere; `provena mcp` is a stub (PLAN-23 unbuilt).
- Respects `.gitignore` / skips binaries? **Yes, well** (`discover.ts` — nested gitignore + `git check-ignore`, binary sniff, 512 KB cap, secret-path denylist).
- Clean uninstall/reset? **Partial** — `.provena/` is a directory you can delete; no `provena reset` command (`reset` isn't implemented; `delete_all_memories` exists server-side).

### Repo memory generation
- Small agent-readable brain file? **No.** ❌ (the biggest gap)
- File-tree map / directory summaries? **No** structured `repo.map.json`; only per-file `artifact` memories in the store.
- Entrypoints, package managers, test/build/deploy commands, routes, DB schemas, env vars, config files? **None detected.** The indexer only chunks code symbols; it has no manifest/route/command/env extraction.
- Important vs low-signal file classification? **No** — every TS/JS file is chunked equally.
- Provenance to source files? **Yes, genuinely good** — `SourceReference{uri, span_start, span_end, excerpt}` on every memory (`emit.ts:169-185`).

### Indexing & scale
- 50,000+ files? **Not built for it.** Discovery hashes files sequentially; indexing is **single-threaded by default** (`run.ts:26` — parallel corrupts index-state). No background queue/worker on the CLI side (the Go `queue` is server-side, not wired to the CLI).
- Content hashing / incremental / skip-unchanged? **Yes, strong.** ✅
- Language-aware parsing? **Partial** — tree-sitter for TS/JS only. **No** AST for Python/Go/Rust (PLAN-11 unbuilt), **no** LSP, **no** package-manifest/OpenAPI/DB-schema/import-graph-beyond-TS parsing. Import-graph relations exist but TS/JS only.
- Not regex-based? ✅ (tree-sitter for what it covers).
- Local-first? **Yes** — SQLite default, loopback-enforced (`config.ts:38-48`).
- Queue/background worker for big repos? **No** on the CLI path.

### Memory schema
The store schema is the **strongest** part and already ~70% of the vision's list:

| Vision field | Status | Vision field | Status |
|---|---|---|---|
| type/kind | ✅ | superseded_by/supersedes | ⚠️ relation edge + status flip, no column |
| scope | ✅ (tenant/ws/proj/user/agent/session) | owner | ❌ (only acl principal / scope.user_id) |
| source_file/event | ⚠️ generic `source_references`, no file field | tags | ✅ |
| provenance | ✅ source refs + spans | retrieval_priority | ⚠️ `importance`+`strength`, not named |
| confidence | ✅ | sensitivity_level | ❌ |
| timestamp | ✅ created/updated/verified | update_policy | ❌ (tenant RetentionPolicy only) |
| validity window | ✅ valid_from/valid_to | evidence_references | ⚠️ = source_references |

**Memory types** (`MemoryKind`): `fact, episode, artifact, decision, relation, preference, instruction, metric, stakeholder, source`. Of the coding-agent taxonomy, only **decision, fact, preference** exist. **Missing: workflow, mistake, task, architecture, command, api, handoff.** The taxonomy is enterprise-knowledge, not coding-agent. Supersede-not-delete is **real** (`store.py:277-292, 2367-2368`).

### Retrieval & context packets
- Task-specific context API? **Implemented but unreachable** — `agent_context()` exists in `store.py` but isn't an HTTP route. The MCP exposes only `memory_search`, not `get_context_pack`.
- Combines signals? **Partially strong** — FTS (bm25) + vector + recency + importance + feedback + scope proximity + **permissions** (`store.py:2413-2475`). **No** symbol search, **no** graph-traversal-influenced ranking, **no** file-importance signal.
- Compact/cited vs raw dumps? **Yes** when reachable — `agent_context()` returns numbered, budget-bounded, cited blocks; read pipeline packages citations + abstains on low evidence.
- Explains why retrieved? **Yes** — `SearchResult.reasons[]` + `--explain` candidate strategy.

### Workflow learning
- Capture user corrections? **No.** ❌
- Detect repeated agent failures / "do-not-repeat" memories? **No.** No `mistake` kind, zero grep hits for mistake logic.
- Learn preferred tools/commands/patterns/review standards? **No** capture pipeline (a `preference` kind exists but nothing writes it from behavior).
- Agent handoffs / resume packets? **No** `handoff` kind or tool.
- Update memory after each agent run? **No** post-task write-back loop exists.

**This entire pillar — the "living memory" differentiator — is absent.**

### Graph & mind map
- Relationships across files/services/APIs/decisions/people/agents/tasks/tests? **No.** Relations are **memory-id ↔ memory-id only** (`001_initial.sql:93-108`); entities are bare string keys with no edges.
- Temporal (superseded not deleted)? **Yes, real.** ✅
- Visual/export (Obsidian/Neo4j)? **No** export.
- Provenance on nodes/edges? **Partial** (source refs on memories).

### MCP & agent integration
- MCP server? **Yes** (Go, `:8090`, JSON-RPC + SSE, gateway-proxied).
- Tools: `memory_create, memory_search, memory_get, memory_delete, memory_update, memory_list, delete_all_memories, list_entities, get_event_status`. **Missing every semantic vision tool**: `get_context_pack, get_repo_brain, record_decision, record_mistake, create_handoff, explain_memory, update_repo_map`.
- **No stdio transport** — Claude Code/Codex generally expect stdio; only HTTP/SSE exists.
- **No agent-instruction file** telling agents when/how to consult Provena. `AGENTS.md` is a dev-loop runbook, not a "search-before / record-after" contract. No `CLAUDE.md`/`.cursorrules`.

### Safety, privacy, governance
- Avoid storing secrets? **Only at the file-path level** — `.env*, *.pem, *.key, .aws, credentials.json` denied (`discover.ts:195`). **No content scanning** — a key hardcoded inside `src/foo.ts` is indexed and served verbatim. ❌
- Redaction? **None.** ❌
- Permissions? **Yes** — ACL + source grants + role/API-key middleware (`auth.go`).
- Deletion/reset? **Server yes** (erase/RTBF/legal-hold); **CLI no** `reset`.
- Audit trail? **Yes** — `audit_log` + append-only `memory_history` (`store.py:294,302`).

### Evaluation
Store/governance/connector/RBAC and CLI incremental indexing are **genuinely well-tested** (plus multi-service `scripts/run_e2e.py`). But every **product-outcome** eval is **absent or scaffolding**:

| Eval | Status |
|---|---|
| Fresh-repo onboarded end-to-end | ⚠️ `index.e2e.mjs` calls `runIndex()` directly; no `init→index→search` user-flow test |
| Repo brain useful/correct | ❌ `code_recall` (PLAN-25) unimplemented; `cold_start_eval.py` orphaned |
| Context packets improve agent perf | ❌ no agent-in-the-loop / task-success / LLM-judge eval anywhere |
| Agents avoid repeated mistakes | ❌ no mistake kind, no test |
| Retrieval accurate under token limits | ❌ abstain is score-based, not budget-based |
| Memory updates after changes | ✅ strongest eval in repo |
| Stale facts superseded | ⚠️ write+rank tested; no "excluded as stale" assertion |
| Secrets not stored | ⚠️ filename-only; no content-redaction test |
| Claude/Codex/Cursor/MCP work | ⚠️ generic Go MCP tested; no editor-specific test; config-gen unbuilt |

LoCoMo/LongMemEval need external datasets not shipped; only a 3-row toy fixture exists.

---

## 3. What you are doing wrong (blunt)

1. **You're building the enterprise memory-plane and calling it the agent repo-brain.** The heavy investment — Go gateway, Rust orchestration, connectors, RTBF/legal-hold, principal mapping, four "runtime surfaces," a Glean comparison — serves the *PRODUCT.md* enterprise story. The stated vision (local, agent-native, per-repo brain) is served by a thin TS/JS indexer and an unreachable `agent_context()`. **You are overbuilding the plane and underbuilding the brain.**

2. **The install story is fiction today.** `npx provena init` gives a config file; to actually index you must clone a Python monorepo and `pip install`. For a `npx`-first product this is the whole ballgame, and it doesn't work standalone. Either bundle a runnable store with the CLI (embedded/sidecar or a pure-Node store) or make the CLI fully local-first without the Python dependency.

3. **The headline artifact doesn't exist.** No `repo.brain.md`. The "first file an agent reads" — the entire wedge — is unbuilt. Everything else (symbol chunks in a memory DB) is plumbing that agents won't touch without the bootloader.

4. **Agents will ignore Provena as-is.** There's no stdio MCP (what Claude Code/Codex speak), no generated agent-instruction file, no `record_mistake`/`get_context_pack` tools, and `get_repo_brain` doesn't exist. Nothing tells an agent to consult Provena, and the tools it would want aren't there. A memory system nobody queries is dead weight.

5. **The memory is too lossy for code, and mis-typed.** You chunk only TS/JS, so a Python/Go/Rust repo (including *your own*) gets nothing useful. And the taxonomy has no `workflow/mistake/task/handoff` — the exact memories that make the product "living." You capture code symbols but not the decisions/corrections/mistakes that are the actual moat.

6. **The graph is premature.** A memory↔memory edge list plus Rust temporal-graph traversal is real engineering with no product behind it — no files/services/APIs/people nodes, no exposed route, no export, no consumer. Defer the graph until the brain + workflow memory exist to populate it.

7. **You will fail on large repos.** Single-threaded, server-coupled indexing that POSTs every chunk over HTTP to a store you must boot — for 50k files this is slow and fragile, with no durable queue on the CLI path. Local-first indexing should not require a running server at all.

8. **Your progress ledger lies.** `loop/prd.json` marks a nonexistent Slack connector green and carries contradictory `passes/blocked` states, on a `services/provena/` layout that isn't this repo. You cannot trust it for status, and neither can a new engineer.

9. **Secrets get stored in cleartext.** Filename denylist ≠ secret safety. Any credential embedded in indexed source is persisted and served. For a tool that ingests whole repos this is a launch-blocker.

**Where you're right:** the store's typed/provenanced/temporal schema, hybrid+permissioned retrieval, incremental hashing, and audit trail are solid and worth keeping. Don't rewrite those.

---

## 4. Recommended architecture

Keep the store as a **local-first backend library**; collapse the polyglot services until enterprise pull is real. Target shape:

```
provena (single npx-installable CLI + embedded store)
├── Repo scanner            ✅ have (discover.ts) — keep
├── File classifier         ❌ build — importance + role (entrypoint/config/test/doc/generated)
├── Language-aware parser   ⚠️ TS/JS only → add Python/Go/Rust (tree-sitter) + manifest/route/env/schema extractors
├── Symbol index            ✅ have (entities/relations) — extend cross-language
├── Dependency graph        ⚠️ TS imports → generalize; keep memory↔memory edges
├── Memory store            ✅ have (app/store.py) — add kinds + sensitivity + owner
├── Graph store             ⚠️ minimal — DEFER richer graph
├── Vector store            ✅ sqlite-vec — keep; add pgvector only for team mode
├── Event ledger            ✅ audit_log + memory_history — keep
├── Context-packet gen      ⚠️ agent_context() EXISTS — just expose it + add get_context_pack
├── MCP server              ⚠️ HTTP CRUD → add STDIO + semantic tools
├── CLI                     ✅ have — add init-brain, mcp, reset, status
├── Background watcher      ✅ watch.ts — keep; add durable queue for big repos
├── Workflow memory extractor ❌ BUILD — the differentiator
└── Evaluation harness      ⚠️ retrieval-only → add code-recall + agent-in-loop
```

**Build first (the wedge):** file classifier → `repo.brain.md` + `repo.map.json` generator → expose `agent_context()` as `POST /v1/agent/context` → **stdio MCP** with `get_repo_brain`, `get_context_pack`, `search_memory`, `record_decision`, `record_mistake`, `create_handoff` → generated agent-instruction file. **Add secret content-scanning** in the write path before any of this ships.

**Build second:** multi-language chunking (PLAN-11), workflow/mistake capture from agent transcripts + user corrections, `reset`/`status`, code-recall eval (PLAN-25).

**Defer:** connectors, RTBF/legal-hold polish, pgvector/Postgres team mode, Rust orchestration, richer knowledge graph + Obsidian/Neo4j export, multi-service polyglot deployment. These are enterprise features with no current buyer and they're starving the wedge.

---

## 5. MVP plan

### Phase 1 — "The brain an agent actually reads" (smallest impressive version)
- **User-visible:** `npx provena init` → produces `.provena/repo.brain.md` (a real, small bootloader: repo map, entrypoints, package manager, test/build/deploy commands, key dirs, conventions, "how to ask Provena for more") + `repo.map.json`. `npx provena index` builds the memory index. An MCP server (**stdio**) exposes `get_repo_brain` + `get_context_pack` + `search_memory`, and init writes a `CLAUDE.md`/`.mcp.json` so Claude Code consults it automatically. After one agent task, `record_decision`/`record_mistake` write back one durable memory.
- **Engineering:** file classifier (importance + role); brain/map generator (`cli/src/indexer/brain.ts`); expose `agent_context()` at `POST /v1/agent/context`; stdio MCP (`cli/src/commands/mcp.ts`) with the 6 semantic tools; agent-config generation in `init`; make the store runnable without a separate clone (embed or bundle); **secret content scanner** in emit/write path.
- **Tests/evals:** true `init→index→search→get_context_pack` e2e; brain-correctness check (does it name the right entrypoints/commands for 3 sample repos); secret-in-source redaction test; one agent-in-the-loop "with vs without brain" smoke.
- **Avoid:** connectors, graph export, Postgres, multi-language (English-brain works on any repo even before per-language chunking), Rust orchestration.

### Phase 2 — "Living memory"
- **User-visible:** Provena captures user corrections and repeated agent failures into `mistake`/`workflow` memories; `get_context_pack` surfaces relevant "do-not-repeat" notes before a task; `create_handoff` produces resume packets.
- **Engineering:** add `MemoryKind` values (workflow/mistake/task/handoff/command/api/architecture) + migration + `sensitivity_level`/`owner`; correction/mistake extractor from agent transcripts; write-back hooks (`record_*` after tasks); `provena watch` keeps it current.
- **Tests/evals:** mistake-recall eval (inject a corrected mistake → assert it's retrieved before the same task); stale-fact suppression assertion; token-budget retrieval eval.
- **Avoid:** premature graph UI; enterprise governance.

### Phase 3 — "Scale + polyglot repos"
- **User-visible:** works on 50k-file Python/Go/Rust monorepos fast; `provena status`, `provena reset`.
- **Engineering:** multi-language tree-sitter (PLAN-11); parallel/queued local indexing without corrupting index-state; manifest/OpenAPI/DB-schema/env extractors feed the brain; optional Postgres+pgvector team mode.
- **Tests/evals:** code-recall benchmark (PLAN-25) as a CI gate; 50k-file indexing perf budget.
- **Avoid:** connectors still deferred unless a design partner demands one.

### Phase 4 — "Team + enterprise (only if pulled)"
- **User-visible:** shared team memory (Postgres), connector ingestion, governance UI, coverage/freshness dashboards.
- **Engineering:** this is where today's enterprise plane (connectors, RTBF, principal sync, gateway) finally earns its place.
- **Evals:** authorization-leak rate, freshness, connector health.
- **Avoid:** building it before Phases 1–3 have real usage.

---

## 6. Final verdict

- **Clearest positioning:** *"Provena is the memory layer that lives in your repo. `npx provena init` gives every coding agent — Claude Code, Codex, Cursor — a durable, self-updating brain of your codebase: what it is, how it's built, what was decided, and what not to repeat."* Drop the Glean/enterprise framing from the front door; that's Phase 4, not the pitch.

- **Biggest missing piece:** the **`.provena/repo.brain.md` bootloader** — the "first file an agent reads." It's the entire wedge and it doesn't exist. A close second: the **workflow/mistake capture loop**, which is the actual moat (symbol indexing is a commodity).

- **Highest-leverage next task:** implement the **brain generator + a stdio MCP that serves it**, and **expose the already-written `agent_context()` over HTTP**. That single thread turns a pile of plumbing into a demoable product an agent will actually consult — and it reuses the strongest existing assets (schema, retrieval, `agent_context`).

- **Exact files to create/modify next:**
  - `cli/src/indexer/brain.ts` **(new)** — file classifier + `repo.brain.md` / `repo.map.json` generator (detect entrypoints, package manager, test/build/deploy commands, routes, env, config).
  - `cli/src/commands/init.ts` **(modify)** — after config, generate the brain, plus `CLAUDE.md` + `.mcp.json` / `.cursor/mcp.json` agent wiring.
  - `cli/src/commands/mcp.ts` **(new, replace stub)** — **stdio** MCP exposing `get_repo_brain, get_context_pack, search_memory, record_decision, record_mistake, create_handoff, explain_memory`.
  - `app/main.py` **(modify)** — register `POST /v1/agent/context` (and `/v1/memories/graph/temporal`, inspect, explain) to fix the 404 gateway↔store contract; `store.py:690` already implements it.
  - `app/models.py` + `storage/migrations/` **(modify)** — add `workflow, mistake, task, handoff, command, api, architecture` kinds + `sensitivity_level`, `owner`; regenerate `001_initial.sql` to match runtime columns.
  - `cli/src/indexer/emit.ts` (or store write path) **(modify)** — secret **content** scanner + redaction before persist.
  - `loop/prd.json` **(modify)** — roll back the phantom PROVENA-008 green; add a one-paragraph doc reconciling "repo-memory product vs enterprise plane."

- **The command a user should eventually run:**
  ```bash
  npx provena init      # writes .provena/repo.brain.md + repo.map.json, wires MCP into Claude Code/Cursor/Codex
  npx provena index     # builds the local memory index (no separate server needed)
  # agents now auto-read repo.brain.md and call provena.get_context_pack before tasks,
  # and provena.record_decision / record_mistake after them
  ```

---

*This document is a read-only audit. No product code was modified in producing it.*
