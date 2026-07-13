# CODEX — Provena Memory Core & Safety (Python foundation)

> **You are one of three parallel agents.** You own the **Python store**. Land
> first: your API + schema is the contract the other two build against. Read
> **only this file** — the Shared Integration Contract below is duplicated
> verbatim into all three plans so you never need to read `claude.md` or
> `grok.md`.
>
> **Track goal:** turn the store into a coding-agent memory core that is typed
> for repo work, safe with secrets, and actually reachable over HTTP. Everything
> here is well-specified and test-heavy — your strength. **Zero dependencies on
> the other tracks.**

---

## Shared Integration Contract (identical in all three plans)

Coordinate ONLY through these frozen interfaces. Build against them; never import or edit another track's code.

**A. Directory ownership — never edit another track's files:**
- **claude** → `cli/**` (all TypeScript: chunkers, brain, init, Node stdio MCP)
- **codex** → `app/**`, `storage/**`, `tests/**` (Python store, schema, migrations, safety)
- **grok** → `intelligence/**`, `evals/**`, `scripts/hooks/**`, `roadmap/**`, `loop/**`
- Append-only / additive PRs only (coordinate): `README.md`, `docs/**`, `.github/**`

**B. Store HTTP API** (codex owns; others code against this). Base URL from `.provena/config.json` `store_url`, default `http://127.0.0.1:18092`, loopback-enforced:
- `GET /healthz` → `{ "status": "ok" }`
- `POST /v1/memories` (MemoryCreate) → `{ memory, created }`
- `POST /v1/memories/search` (SearchRequest) → `{ results: [{ memory, score, reasons[], related_memories[] }] }`
- `POST /v1/agent/context` **(NEW)** → body `{ task: string, scope, max_characters?: number }` → `AgentContextResponse { context: string, token_estimate: number, citations: [{ memory_id, uri, span_start, span_end }], reasons_by_memory: {…} }`
- `record_*` writes are just `POST /v1/memories` with the new kinds in (C).

**C. `MemoryKind` enum** (codex ships; others write these strings):
existing → `fact, episode, artifact, decision, relation, preference, instruction, metric, stakeholder, source`
**NEW → `workflow, mistake, task, handoff, command, api, architecture`**

**D. `.provena/` file contract** (claude owns format; you may read, never write):
- `repo.brain.md` — agent bootloader
- `repo.map.json` — `{ version, root, files:[{path, role, importance, language, symbols?}], commands:{test,build,run,deploy}, entrypoints[], services[], env_vars[], routes[] }`
- `config.json`, `index-state.json`, `last-index.json` (existing)

**E. MCP tool names** (claude implements as Node stdio; frozen so agent config is stable):
`get_repo_brain`, `get_context_pack{task}`, `search_memory{query}`, `record_decision`, `record_mistake`, `create_handoff`, `explain_memory{memory_id}`

**F. Landing order:** **codex lands B + C first.** claude + grok build against stubs, then integrate once your contract is green. Each track = its own branch + PR, no direct pushes to `main`.

---

## What exists today (verified — start here)
- `app/models.py` — `MemoryKind` (10 values, no coding-agent kinds), `MemoryCreate`/`MemoryRecord` (rich: scope, source_references, confidence, importance, strength, valid_from/to, supersedes, acl). No `sensitivity_level`, `owner`, `update_policy`.
- `app/main.py` — ~24 routes. **Missing** the routes the Go gateway already proxies → they 404: `/v1/agent/context`, `/v1/memories/graph/temporal`, `/v1/admin/memories/{id}/inspect`, `/v1/admin/memories/search/explain`.
- `app/store.py` — `agent_context()` (`~:690-753`) and `temporal_graph()` (`~:755`) **implemented but never routed**. `_ensure_compatibility()` (`~:3076-3087`) bolts columns on at runtime via `ALTER TABLE` because `storage/migrations/001_initial.sql` is stale.
- **No secret content scanning anywhere** in the write path — content is persisted verbatim. `audit_log` + `memory_history` exist.

## Tasks (in order; each is one PR-sized slice)

### C1 — Coding-agent memory taxonomy + governance fields
- Add to `MemoryKind`: `workflow, mistake, task, handoff, command, api, architecture`.
- Add to `MemoryCreate`/`MemoryRecord`: `sensitivity_level: Literal["public","internal","secret"] = "internal"`, `owner: str | None`, `update_policy: Literal["append","replace","manual"] = "append"`. (`retrieval_priority` already exists in spirit as `importance`+`strength` — alias, don't duplicate.)
- **Kill the runtime `ALTER` drift:** regenerate `storage/migrations/001_initial.sql` so a fresh DB has every column `_ensure_compatibility()` currently adds (`expires_at, memory_layer, embedding_model, embedding_json, acl_json, held, hold_reason, hold_until, pre_hold_status`, + the new fields). Keep `_ensure_compatibility()` as an idempotent no-op safety net for old DBs.
- Ship an additive migration for existing SQLite/Postgres DBs.
- **Acceptance:** fresh DB created purely from migrations has all columns; `pytest tests/ -q` green; new kinds round-trip through `POST /v1/memories`.

### C2 — Expose the unreachable routes (fix the gateway↔store 404 contract)
- Register in `app/main.py`, wrapping existing `store.py` methods: `POST /v1/agent/context` (Contract B), `POST /v1/memories/graph/temporal`, `GET /v1/admin/memories/{id}/inspect`, `POST /v1/admin/memories/search/explain`.
- **Acceptance:** a test hits each route and asserts a 200 with the documented shape; none 404.

### C3 — `get_context_pack` server logic
- Extend `agent_context()` to accept `{ task, scope, max_characters? }` and return `AgentContextResponse` (Contract B): compact, **cited**, budget-bounded, with a `reasons_by_memory` map explaining *why* each memory was included (scope match, recency, entity/tag hit, feedback, mistake-relevance). Prefer active over superseded; include `mistake`/`decision` memories relevant to the task near the top.
- **Acceptance:** test asserts output ≤ `max_characters`, every block has a citation, and reasons are populated.

### C4 — Secret content scanning + redaction (launch-blocker; trust boundary)
- In the store write path (before persist), scan `content`/`summary`/`title` for secrets: known token shapes (AWS `AKIA…`, GitHub `ghp_/gho_`, Slack `xox[baprs]-…`, Google `AIza…`, private-key headers `-----BEGIN … PRIVATE KEY-----`, JWTs) **and** a high-entropy heuristic for generic API keys. Redact matches to `«REDACTED:kind»`, set `sensitivity_level="secret"`, and write an `audit_log` event.
- **Package research / supply-chain:** prefer a vetted ruleset over hand-rolling — evaluate `detect-secrets` (Yelp) rules; if adding a dep, run the supply-chain check (advisories, license, transitive, native bins, lockfile impact) and record the decision + removal path. **Do not compress this code** (trust boundary).
- **Acceptance:** a memory whose content contains each secret type is stored **redacted + flagged + audited**; a companion test greps the DB to prove the raw secret is absent.

### C5 — Tests & gate
- Cover: new kinds + fields + migration, all four newly-exposed routes, context-pack budget/citations/reasons, secret redaction (per type + entropy), superseded-not-returned-as-active assertion.
- **Validation gate (run before every push):** `uv run ruff format --check . && uv run ruff check . && uv run pytest -q` (add `-m postgres` run if `PROVENA_DATABASE_URL` is set).

## Do NOT touch
`cli/**` (claude), `intelligence/**`, `evals/**`, `scripts/hooks/**`, `roadmap/**`, `loop/**` (grok), `control-plane/**`, `orchestration/**`.

## Definition of done
Contract B + C are live and green; secrets never persist in cleartext; the 404 routes resolve; `pytest` green. Branch `feat/memory-core-safety`, PR against `main` with a Testing section. **Publish that your API is green so claude + grok can integrate.**
