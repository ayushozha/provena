# GROK — Provena Living Memory & Evals (the differentiator + the proof)

> **You are one of three parallel agents.** You own **`intelligence/`,
> `evals/`, `scripts/hooks/`, `roadmap/`, `loop/`**. Your job is the two things
> that make Provena more than a code indexer: (1) the **living-memory capture**
> that turns user corrections and repeated agent failures into durable memory,
> and (2) the **evals** that prove the memory actually helps an agent — plus
> cleaning up the misleading progress ledger. Read **only this file**; the Shared
> Integration Contract below is duplicated verbatim into all three plans.
>
> **Go ambitious here — this is the moat.** Symbol indexing is a commodity;
> workflow/mistake memory and provable agent lift are not.

---

## Shared Integration Contract (identical in all three plans)

Coordinate ONLY through these frozen interfaces. Build against them; never import or edit another track's code.

**A. Directory ownership — never edit another track's files:**
- **claude** → `cli/**` (all TypeScript: chunkers, brain, init, Node stdio MCP)
- **codex** → `app/**`, `storage/**`, `tests/**` (Python store, schema, migrations, safety)
- **grok** → `intelligence/**`, `evals/**`, `scripts/hooks/**`, `roadmap/**`, `loop/**`
- Append-only / additive PRs only (coordinate): `README.md`, `docs/**`, `.github/**`

**B. Store HTTP API** (codex owns; you code against this). Base URL from `.provena/config.json` `store_url`, default `http://127.0.0.1:18092`, loopback-enforced:
- `GET /healthz` → `{ "status": "ok" }`
- `POST /v1/memories` (MemoryCreate) → `{ memory, created }`
- `POST /v1/memories/search` (SearchRequest) → `{ results: [{ memory, score, reasons[], related_memories[] }] }`
- `POST /v1/agent/context` **(NEW, codex is adding)** → `{ task, scope, max_characters? }` → `AgentContextResponse { context, token_estimate, citations[], reasons_by_memory }`
- `record_*` writes are just `POST /v1/memories` with the new kinds in (C).

**C. `MemoryKind` enum** (codex ships; you write these strings):
existing → `fact, episode, artifact, decision, relation, preference, instruction, metric, stakeholder, source`
**NEW → `workflow, mistake, task, handoff, command, api, architecture`**

**D. `.provena/` file contract** (claude owns format; you read, never write):
- `repo.brain.md` — agent bootloader
- `repo.map.json` — `{ version, root, files:[{path, role, importance, language, symbols?}], commands:{test,build,run,deploy}, entrypoints[], services[], env_vars[], routes[] }`
- `config.json`, `index-state.json`, `last-index.json` (existing)

**E. MCP tool names** (claude implements as Node stdio; you emit memories these tools also write):
`get_repo_brain`, `get_context_pack{task}`, `search_memory{query}`, `record_decision`, `record_mistake`, `create_handoff`, `explain_memory{memory_id}`

**F. Landing order:** codex lands B + C first. You build against a **mock store** (returns the documented shapes) meanwhile, then integrate once codex's contract is green. Each track = its own branch + PR, no direct pushes to `main`.

---

## What exists today (verified — start here)
- `intelligence/` — real write/read pipelines (classify, extract_facts, embeddings, rerank, contradiction, abstain, citations). `intelligence/eval/` has `benchmarks.py`, `retrieval_quality.py`, `run_benchmark.py`, `convert_locomo.py`, and an **orphaned** `cold_start_eval.py` (no test, no runner). Only a 3-row toy fixture ships; LoCoMo/LongMemEval need external datasets.
- **No `mistake`/`workflow` capture anywhere.** No agent-in-the-loop eval. No `code_recall` benchmark (PLAN-25 unbuilt).
- `loop/prd.json` **PROVENA-008** claims a shipped `app/slack_connector.py` (`green`) that **does not exist** — `tests/test_main.py:1425` asserts the opposite. **PROVENA-001** is both `passes:true` and `blocked`. `AGENTS.md`/`loop/`/`sdk/go` assume a `services/provena/` monorepo layout that isn't this checkout.
- Model IDs must come from env (`ANTHROPIC_MODEL` etc.) — **never hardcode** (repo rule).

## Tasks (in order)

### G1 — Workflow/mistake capture engine — `intelligence/app/capture.py` (new module)
Turn signals into structured memory via the store API (Contract B/C):
- **Correction → `workflow` / `preference`:** when a user corrects an agent ("use pnpm not npm", "we always X"), extract a durable rule.
- **Repeated failure → `mistake`:** detect the same error/edit reverted twice; write a `mistake` memory with a "do-not-repeat" summary + evidence refs.
- **Decision → `decision`; handoff → `handoff`.**
- Dedup + **supersede** (use `supersedes_memory_id`) so rules update instead of piling up. Attach provenance (source refs to the transcript/commit).
- **Acceptance:** unit tests (mock store) prove a correction yields a `workflow` memory and a twice-seen failure yields one `mistake` memory (not two).

### G2 — Capture hooks — `scripts/hooks/`
Standalone, self-contained scripts (no import from other tracks):
- **Claude Code hook** — on user correction / repeated tool failure → POST `record_mistake`/`record_decision` to the store.
- **git `post-commit` hook** — extract the commit's decision (message + diff summary) → `record_decision`.
- Ship them so claude's CLI can install them (`provena hooks install` copies these files — you own the scripts, claude owns the command; the seam is the file path `scripts/hooks/`).
- **Acceptance:** running a hook against a mock store writes the expected memory; hooks are idempotent and never block the user's git op on store failure (fail-open, log).

### G3 — Eval harness — `evals/` (new top-level, prove the product works)
Model IDs from env; **no hardcoded models**. Build:
1. **`code_recall`** (PLAN-25) — over a sample repo + question set: does retrieval surface the right symbols/files **under a token cap**? Report recall@k, MRR, and tokens.
2. **Agent-in-the-loop** — run a fixed coding task **with vs without** the brain/`get_context_pack`; score task success with an **LLM judge** (env-configured model). This is the headline number: "context packets improve agent performance."
3. **Mistake-recall** — inject a corrected mistake, then re-issue the same task; assert the `mistake` memory is retrieved *before* the agent repeats it.
4. **Stale-fact suppression** — supersede a fact; assert the stale version is demoted/excluded.
5. **Secret-not-stored** — feed content with secrets; assert the store persisted redacted (pairs with codex C4).
- Wire `cold_start_eval.py` into the runner (it's currently orphaned).
- **Acceptance:** `python -m evals.run` produces a scorecard JSON; the with/without-brain delta is measured and printed.

### G4 — Reconcile the ledger (stop the docs from lying)
- Fix **PROVENA-008** in `loop/prd.json` (it's not green — the Slack worker doesn't ship; tests assert `provider_not_implemented`). Correct **PROVENA-001**'s contradictory state.
- Add `docs/POSITIONING.md`: one page reconciling the **repo-memory product** (the wedge) vs the **enterprise memory-plane** (Phase 4), so a new engineer knows which is the product now.
- Update `roadmap/README.md` statuses (15/18 landed → completed; note 11 shipped by claude, 23 superseded by the Node stdio MCP, 25 = your `code_recall`).

### G5 — Local-mode freeze note (flagged, low effort)
Document that for the **local product** the Node stdio MCP (claude) is primary and the Go MCP + Rust orchestration are **enterprise/polyglot-only** — so nobody wastes cycles on the Go stack for the wedge. Do **not** delete them; just scope them.

## Validation gate (run before every push)
`cd intelligence && python -m pytest tests -q` + `python -m evals.run` (must produce a scorecard). Ruff clean.

## Do NOT touch
`cli/**` (claude), `app/**`, `storage/**`, `tests/**` (codex), `control-plane/**`, `orchestration/**`.

## Definition of done
Capture engine + hooks write real `workflow`/`mistake`/`decision`/`handoff` memories; the eval scorecard shows a measurable with-vs-without-brain lift and mistake-recall working; the ledger no longer lies. Branch `feat/living-memory-and-evals`, PR against `main` with the scorecard JSON in the Testing section.
