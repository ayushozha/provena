# Provena Positioning — Repo Memory Wedge vs Enterprise Memory Plane

This page reconciles what Provena ships **today** with the longer-term enterprise
vision so new engineers do not confuse the wedge product with Phase 4 platform work.

## Product now (the wedge)

**Provena is a repo-memory product:** `npx provena init` in a codebase → local
SQLite-backed memory of symbols, files, and workflow/mistake capture → hybrid
retrieval agents query through the **Node stdio MCP** (`get_repo_brain`,
`get_context_pack`, `search_memory`, `record_mistake`, …).

| Surface | Role today |
|---------|------------|
| `@provena/cli` | Init, index, search, hooks install, local serve |
| Python store (`app/`) | Loopback memory API, SQLite default, optional Postgres |
| `intelligence/` | Write/read pipelines, **workflow/mistake capture**, eval lift |
| Node stdio MCP | **Primary** agent bridge for the local product |
| `evals/` | Scorecard proving brain lift, mistake recall, code recall |

**Buyer:** individual developers and small teams who want governed, persistent
context in their repo without building RAG infrastructure.

**Proof:** `python -m evals.run` — measurable with-vs-without-brain delta,
mistake-recall, and code_recall under a token cap.

## Phase 4 (enterprise memory plane)

The **connected memory plane** adds connector execution, principal mapping,
permission-preserving sync, gateway auth, queue workers, and polyglot SDKs.
That path uses more of the stack:

| Surface | Enterprise / polyglot scope |
|---------|---------------------------|
| Go control-plane + gateway | Auth, tenancy, routing |
| Rust orchestration | Trigger phrases, context budgeting at scale |
| Go MCP server | **Enterprise / polyglot-only** — not required for the wedge |
| Connector plane | Slack, Drive, etc. (stubs → production workers) |

**Buyer:** platform teams embedding governed memory across many tools and agents.

**Status:** foundations exist; not all connector stories are shipped. See
`loop/prd.json` for honest per-story state — do not trust green flags without
matching tests in **this** checkout (root layout, not `services/provena/`).

## Local-mode freeze (read this before touching Go/Rust MCP)

For the **local repo-memory product**:

1. **Node stdio MCP is primary.** Agents should use `provena mcp` / Cursor config
   from the CLI track — not the Go MCP binary.
2. **Go MCP + Rust orchestration are enterprise/polyglot-only.** Do not spend wedge
   cycles porting features there; scope them in docs and issues instead.
3. **Do not delete** control-plane or orchestration — they remain valid for
   self-hosted polyglot deployments and Phase 4.

## How the three build tracks divide ownership

| Track | Owns |
|-------|------|
| claude | `cli/**` — chunkers, brain, init, Node stdio MCP |
| codex | `app/**`, `storage/**`, `tests/**` — store, schema, safety |
| grok | `intelligence/**`, `evals/**`, `scripts/hooks/**`, `roadmap/**`, `loop/**` |

Integrate only through frozen HTTP/MCP contracts documented in
`docs/build-plan/grok.md` (store API, `MemoryKind` strings, `.provena/` files).

## Related docs

- [PRODUCT.md](../PRODUCT.md) — category and buyer framing
- [roadmap/README.md](../roadmap/README.md) — plan index and completion status
- [docs/build-plan/grok.md](build-plan/grok.md) — living-memory + evals contract