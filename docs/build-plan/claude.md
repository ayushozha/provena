# CLAUDE — Provena Brain & Onboarding (TypeScript CLI, the wedge)

> **You are one of three parallel agents.** You own **`cli/`** entirely. Your job
> is the product wedge: `npx provena init` must produce the agent bootloader
> (`repo.brain.md`), wire up Claude Code / Cursor / Codex automatically, and ship
> the MCP **inside the npm package** as a stdio server — no Go/Rust binary
> required for the local product. Read **only this file**; the Shared
> Integration Contract below is duplicated verbatim into all three plans.
>
> **Strategic bet baked into this plan (flag to the human if you disagree):** the
> local product ships a **Node stdio MCP inside the CLI**. The existing Go MCP
> (`control-plane/`) and Rust orchestration become enterprise/polyglot-only. This
> is what makes `npx provena` a real one-command install and removes your
> dependency on the Go stack.

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
- `POST /v1/agent/context` **(NEW, codex is adding)** → body `{ task: string, scope, max_characters?: number }` → `AgentContextResponse { context: string, token_estimate: number, citations: [{ memory_id, uri, span_start, span_end }], reasons_by_memory: {…} }`
- `record_*` writes are just `POST /v1/memories` with the new kinds in (C).

**C. `MemoryKind` enum** (codex ships; you write these strings):
existing → `fact, episode, artifact, decision, relation, preference, instruction, metric, stakeholder, source`
**NEW → `workflow, mistake, task, handoff, command, api, architecture`**

**D. `.provena/` file contract** (you own this format):
- `repo.brain.md` — agent bootloader
- `repo.map.json` — `{ version, root, files:[{path, role, importance, language, symbols?}], commands:{test,build,run,deploy}, entrypoints[], services[], env_vars[], routes[] }`
- `config.json`, `index-state.json`, `last-index.json` (existing)

**E. MCP tool names** (you implement as Node stdio; frozen so agent config is stable):
`get_repo_brain`, `get_context_pack{task}`, `search_memory{query}`, `record_decision`, `record_mistake`, `create_handoff`, `explain_memory{memory_id}`

**F. Landing order:** codex lands B + C first. You build against **stubs** (a fake store returning the documented shapes) meanwhile, then integrate once codex's contract is green. Each track = its own branch + PR, no direct pushes to `main`.

---

## What exists today (verified — start here)
- `cli/` is a real ~4,700 LOC TS package `@provena/cli` (`bin: provena → dist/cli.js`). Commands: `init, serve, doctor, discover, config-show, index, search, watch`. **Stubs**: `status, connect, mcp` (`cli/src/cli.ts` `runStub`).
- `cli/src/commands/init.ts` writes **only** `.provena/config.json` + a `.gitignore` entry. **No brain file.**
- `cli/src/indexer/run.ts:198` filters to **TS/JS only** (`discovered.filter(isTsJsFile)`); only `chunkers/typescript.ts` exists. `discover.ts` already: nested gitignore + `git check-ignore`, binary sniff, 512 KB cap, secret-path denylist, detects `py/go/rs/md`.
- `search.ts` returns ranked hits + `--explain`; there is **no** `get_context_pack` assembly and **no** MCP in the CLI.
- Install today is not real: `provena serve` needs the Python store present (`PROVENA_STORE_ROOT`); the npm package ships only `dist/`.

## Tasks (in order)

### A1 — File classifier — `cli/src/indexer/classify.ts`
Classify every discovered file: `role ∈ {entrypoint, config, test, doc, generated, vendor, source}` and an `importance` score (0–1) from import fan-in (reuse the relations graph), path heuristics (`src/index`, `main.*`, `app/`), size, and recency. This is the signal that separates high- from low-value files.

### A2 — Repo-intelligence extractors — `cli/src/brain/detect.ts`
Language-agnostic detection from manifests + patterns: package managers (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`), **commands** (npm scripts, `Makefile`, `.github/workflows/*`, `justfile`) → `{test, build, run, deploy}`, entrypoints, **API routes** (framework heuristics: FastAPI/Express/Gin decorators & routers), **DB schemas** (`storage/migrations`, `prisma`, `*.sql`), **env vars** (`.env.example`, `process.env.X`, `os.environ["X"]`), config files, and top-level services.

### A3 — Brain generator — `cli/src/brain/brain.ts`
Emit the two files in Contract D. `repo.brain.md` is the **bootloader the first agent reads** — keep it **small (≤ ~6 KB)**, never the whole repo. Required sections:
1. **Identity** — one line: what this repo is.
2. **Repo map** — top dirs with role + one-line purpose (from A1).
3. **Architecture** — services + data flow (from A2), 3–6 lines.
4. **Entrypoints** — where execution starts.
5. **Commands** — test / build / run / deploy.
6. **Conventions** — package manager, language(s), test framework, notable patterns.
7. **How to get more** — call the `provena` MCP tools (`get_context_pack`, `search_memory`) or run `npx provena search <q>`; do not read the whole tree.
`repo.map.json` is the machine-readable twin (Contract D schema).

### A4 — `init` upgrade — agent wiring
After config, generate the brain (A3) and **wire the agents** so they actually consult Provena:
- `CLAUDE.md` (append a Provena block: "before a task call `get_context_pack`; after, call `record_decision`/`record_mistake`").
- `.mcp.json` + `.cursor/mcp.json` pointing at the Node stdio MCP (A5).
- Append an `AGENTS.md` consult-contract section.
Idempotent, gitignore-aware, `--force` to overwrite. **Do not clobber** a user's existing `CLAUDE.md` — append a marked block.

### A5 — Node stdio MCP — `cli/src/mcp/**` + `cli/src/commands/mcp.ts` (replace stub)
Implement Contract E over **stdio**, proxying to the store HTTP API. This ships the MCP inside `npx provena` — the whole reason the install becomes real.
- **Package research / supply-chain:** use `@modelcontextprotocol/sdk` (official). Run the supply-chain check (advisories, license, transitive, install scripts, lockfile impact) and record the decision + removal path. If it's heavier than warranted, a minimal hand-rolled JSON-RPC-over-stdio is acceptable — but do not hand-roll the protocol framing sloppily (trust boundary with the agent client).
- `get_repo_brain` → returns `.provena/repo.brain.md`. `get_context_pack{task}` → `POST /v1/agent/context`. `search_memory{query}` → `/v1/memories/search`. `record_decision/record_mistake/create_handoff` → `POST /v1/memories` with the right kind. `explain_memory{id}` → history/inspect.

### A6 — Multi-language chunkers — `cli/src/indexer/chunkers/{python,go,rust}.ts`
Add tree-sitter chunkers for Python/Go/Rust; register in `run.ts` (remove the TS/JS-only filter for these). Keep single-writer `index-state` safety.
- **Supply-chain:** add `tree-sitter-python`, `tree-sitter-go`, `tree-sitter-rust` (same family as existing TS grammars) — quick advisory/license check.

### A7 — `reset` / `status` / `doctor`
`provena reset` (wipe `.provena/` + purge memories via API, confirm first — data-loss guard, do not compress), `provena status` (backend, store health, index freshness, brain age, file counts), upgrade `doctor`.

### A8 — Make the install real
`npx provena init && npx provena index` must work with **zero manual Python setup**. Bundle a managed store bootstrap (download/pin a store sidecar, or embed) so `serve` isn't a prerequisite the user does by hand. Flag the chosen approach to the human — this is the make-or-break UX call.

## Validation gate (run before every push)
`cd cli && npm run build && npm test` (extend the suite: brain-generation snapshot on `cli/fixtures`, init agent-wiring idempotency, MCP tools/list + one round-trip against a stub store, python/go/rust chunker extraction).

## Do NOT touch
`app/**`, `storage/**`, `tests/**` (codex), `intelligence/**`, `evals/**`, `scripts/hooks/**`, `roadmap/**`, `loop/**` (grok).

## Definition of done
`npx provena init` → `repo.brain.md` + agent wiring; the Node stdio MCP serves Contract E; Python/Go/Rust indexable; install works with no manual store setup. Branch `feat/brain-and-onboarding`, PR against `main` with a Testing section + a screenshot/paste of a generated `repo.brain.md`.
