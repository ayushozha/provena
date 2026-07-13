# Provena Product Positioning

## Category

Provena is not just "memory for agents" and it is not just enterprise search.
The right category is:

**Provena is a governed memory plane for AI products and enterprise agents.**

That framing matters because it positions Provena as infrastructure that can be
embedded into existing applications, copilots, MCP clients, and internal
platforms without requiring customers to replace their system of record.

The developer wedge is now concrete: **Provena is also the living repo brain
that initializes an existing codebase in one command once the package is
installed.** The local brain earns
daily usage through faster agent orientation, cited context, durable decisions,
and handoffs. Teams can later connect those same event and provenance contracts
to the governed shared memory plane. This is one product with a local-first
entry point, not two unrelated memory systems.

The CLI is currently distributed as a source-built npm tarball; the
`@provena/cli` registry release is pending. “One command” describes
`provena init`, not registry availability or a hosted enterprise service.

## Current product boundary

- The portable brain, typed ledger, deterministic current-code + temporal-memory
  graph, context packets, MCP, managed agent instructions, Git hooks, daemon,
  and integrity harness exist.
- Every normal refresh also emits an attested, bounded maintenance plan for
  exact evidence gaps, paths absent from a complete current map, and exact
  active-memory overlaps. CLI/MCP can compile cited task packets for human or
  agent review, but cannot approve, apply, or autonomously execute them.
- Refresh still performs a bounded eligible-file scan; incremental filesystem
  CDC is roadmap.
- The optional Neo4j adapter projects current code plus temporal memory history
  and is mock-driver tested; live-server conformance and a cross-repository
  enterprise graph are not shipped.
- Confidential/restricted events are rejected from the Git-tracked ledger and
  belong in the optional governed store.
- Agent/MCP/hook integrations are best-effort and preserve conflicting
  user-owned configuration instead of claiming universal installation.
- First-party SaaS crawlers, a hosted admin plane, learned local ranking,
  semantic consolidation/decay, autonomous subagent DAGs, automatic proposal
  approval/application, and commercial enterprise plans are not shipped.

## What Provena already does well

- Persists typed memory with provenance, citations, and scoped recall.
- Enforces lifecycle controls such as retention, RTBF, and legal hold.
- Supports per-memory and principal-based access control.
- Assembles context before generation with orchestration and context budgeting.
- Exposes API, MCP, queue, and SDK surfaces for multiple product shapes.
- Supports connected-mode foundations: connector registry, source inventory,
  principal mapping, permission grants, sync jobs, and coverage summaries.
- Produces deterministic proposal-only repository-memory review work without a
  model, network service, database, or second scheduler.

## Product surfaces

### MCP mode

Use when an agent client wants memory through a tool bridge with minimal setup.
The project MCP exposes five resources and seven tools over stdio or explicit
loopback HTTP, including read-only maintenance-plan and task-context tools.

### Standalone mode

Use when a startup or internal team wants a direct API and SDK-backed memory
service inside a trusted network boundary. Standalone does not enforce Provena
bearer keys; use the polyglot gateway when Provena-managed authentication,
verified identity claims, and rate limits are required.

### Connected mode

Use when a company already has existing products and enterprise tools and wants
Provena to ingest, preserve, and retrieve context without re-platforming.

## Why startups buy Provena

- They can add long-term memory without building storage, retrieval, citations,
  lifecycle, or permissioning from scratch.
- They can embed Provena into an existing app instead of building a search stack.
- They can ship governed memory faster than building bespoke RAG infrastructure.

## Why enterprises buy Provena

- Agents get the right context with evidence, freshness, and access control.
- Governance is built into the memory layer rather than bolted on later.
- Provena can sit across tools and vendors instead of forcing one suite lock-in.
- Security and platform teams get explicit coverage and sync-state visibility.

## Where Provena is differentiated today

### Governance-heavy memory requirements

The current codebase combines provenance, legal hold, RTBF, retention, scoped
recall, and permission-preserving retrieval foundations. Teams should evaluate
those implemented controls directly rather than infer overall product
superiority from the feature list.

### Runtime memory rather than ingestion only

Provena is designed for use cases that need a runtime memory plane with
retrieval policy, lifecycle, and agent interfaces rather than only extraction
pipelines.

### Embedded, neutral memory plane

Provena can be evaluated as a neutral memory layer embedded in an existing app,
agent, or internal platform. No checked-in competitive benchmark currently
establishes that it is better overall than another memory or RAG product.

## Enterprise-search gaps

Provena does not yet provide the following capabilities expected from a mature
enterprise-search product:

- breadth of mature production connectors
- out-of-the-box enterprise search UX
- deep freshness and crawl infrastructure
- polished admin controls for indexing and source operations
- broad permission sync across many SaaS systems

## What must exist before broad enterprise-search parity

1. First-party connectors for the highest-value systems:
   Slack, Google Drive, SharePoint/OneDrive, Notion, Confluence, Jira, GitHub,
   Salesforce, and email/calendar surfaces. None of these ship today; the
   provider list is a roadmap. Connected mode is currently push-based: an
   external caller fetches upstream data and writes it through the
   integration-plane batch and sync-job APIs.
2. Identity sync and group sync:
   SCIM/SSO-aware mapping for users, groups, service accounts, and agents.
3. Permission-preserving retrieval guarantees:
   external ACL sync, inherited grant translation, and deny-by-default recall.
4. Freshness guarantees:
   crawl scheduling, webhooks or CDC where available, stale-source detection,
   and clear recency indicators on retrieved memory.
5. Operator visibility:
   dashboards for connector health, sync lag, coverage gaps, and drift.
6. Retrieval quality and oversharing evals:
   recall, ranking quality, citation quality, freshness, and authorization leak rate.

## The most defensible sales story today

### One-line pitch

Search tools find documents. Provena gives AI systems the right context, with
proof, permissions, and lifecycle control.

### Startup pitch

Add durable, permission-aware memory to your AI product without building your
own retrieval, citation, lifecycle, and governance stack.

### Enterprise pitch

Give every agent the right context across existing systems, with evidence,
freshness, and access control, without locking yourself into a single suite or
model vendor.

## Product strategy

Near-term product strategy should focus on:

1. Connector depth for the highest-value sources.
2. Identity and permission sync that security teams can trust.
3. Coverage and freshness visibility for operators.
4. Retrieval evaluation that proves recall quality and authorization safety.
5. Packaging that makes Provena easy to embed into existing products.

Until those are complete, the strongest honest positioning is:

**Provena is the governed memory plane for enterprise AI apps and agents.**
