# Provena Architecture

Provena is a polyglot, provenance-first memory service for LLM applications.
It splits into three language layers — each chosen for where it excels —
connected by HTTP interfaces today; protobuf/gRPC migration remains roadmap.

The installable repository-memory path is intentionally smaller: the Node CLI
creates a Git-tracked brain, event ledger, deterministic graph, context packets,
an attested maintenance-proposal plan, agent/MCP integrations, and an optional
Neo4j projection without requiring any of the services below. See
[Repo Memory Architecture](./docs/REPO_MEMORY_ARCHITECTURE.md). The polyglot
topology becomes an optional shared/enterprise projection rather than a
prerequisite for `provena init`. The portable path defaults to stdio MCP; an
explicit foreground command can expose the same surface on loopback HTTP.

## Language boundary pattern

| Layer | Language | Owns | Why |
|---|---|---|---|
| **Hot path** | Rust | Orchestration, trigger index, context budget, token counter, prompt assembly | Sub-ms latency, zero GC, CPU-bound |
| **Infrastructure** | Go | API gateway, MCP server, queue consumer, lifecycle, observability | Concurrency, I/O-bound, single binary |
| **Intelligence** | Python | Write pipeline, read pipeline, model router, embeddings, eval | ML ecosystem, LLM SDKs, rapid iteration |

## System diagram

The first diagram is the current wired repository-memory path. Solid arrows
are implemented. Manifest-backed package facts and command workflows pass
through the local authority-aware reconciler; already-canonical explicit events
bypass extraction and enter the same append-only ledger directly. The current
maintenance compiler performs exact, bounded checks over that map and ledger;
it emits review proposals and cited task packets, not autonomous mutations.

```mermaid
flowchart LR
    subgraph Local["Portable repository brain (Node.js)"]
        Truth["Git / files / manifests"] --> Index["Deterministic source index"]
        Index --> Extract["Manifest-backed package + workflow observations"]
        Extract --> Reconcile["ADD / NOOP / SUPERSEDE / RETRACT / DEFER / CONFLICT"]
        Reconcile --> Ledger
        Triggers["Session / Git hooks / MCP / 15-minute daemon"] --> Index
        Index --> CodeGraph["Current code topology"]
        Index --> Views["Rollback-capable brain / map / views"]
        Ledger[("Canonical append-only MemoryEvent ledger")] --> Views
        CodeGraph --> GraphV2["Graph v2<br/>full memory lineage + effective intervals"]
        Ledger --> GraphV2
        GraphV2 --> Algorithms["PageRank / degree / components / BFS"]
        GraphV2 --> Context["Current/as-of cited context + budgets"]
        Index --> Maintain["Linear maintenance compiler<br/>exact evidence / path / overlap checks"]
        Ledger --> Maintain
        Maintain --> Plan["Attested maintenance.plan.json<br/>proposal only"]
        Plan --> TaskContext["Bounded cited maintenance task packet"]
        GraphV2 --> TaskContext
        Ledger --> TaskContext
        Context --> MCP["Project MCP<br/>stdio or opt-in 127.0.0.1:18093"] --> Agents["Coding agents"]
        TaskContext --> MCP
        Ledger --> Sync["provena sync store<br/>exact SHA-256 + atomic checkpoint"]
    end

    subgraph Governed["Optional governed store"]
        Sync --> SQL[("SQLite / PostgreSQL")]
        SQL --> Search["FTS / optional vectors"]
        SQL --> Relations["Sources / relations / history"]
        SQL --> Checkpoint["Immutable event map + sync state"]
    end

    GraphV2 -. "optional full projection" .-> Neo4j["Neo4j"]
```

### Target living-memory organism

This diagram is the target architecture, not a shipped-feature claim. The
canonical event stream remains the sole authority; every database, vector
index, and Neo4j graph is a rebuildable projection. Semantic consolidation,
decay, autonomous subagent execution, and their scheduler are explicitly
roadmap nodes. If implemented, they may only propose events through the same
reconciliation and policy gate.

```mermaid
flowchart TB
    Repo["Git / filesystem / symbols / commands"] --> Index2["Deterministic source index"]
    Repo --> Extract["Source-grounded extraction"]
    Boot["Agent lifecycle adapters"] --> Sessions["Session / tool / feedback / outcome events"] --> Extract
    Extract --> Reconcile["Authority-aware reconciliation<br/>ADD / NOOP / SUPERSEDE / RETRACT / CONFLICT"]
    Index2 --> Reconcile
    Reconcile --> Guard["Policy / approval / rollback"] --> Events[("Canonical append-only memory events")]
    Events --> Temporal["Bi-temporal claim + event graph"]
    Events --> SQL2["SQLite / PostgreSQL"]
    Events --> Vector["FTS / vector indexes"]
    Temporal --> Neo4j["Neo4j temporal memory graph"]
    Index2 --> Compiler["Cited context compiler + budgets"]
    Events --> Compiler
    Temporal --> Compiler
    Compiler --> Server["Bundled loopback server + stdio MCP"] --> Agents2["Codex / Claude Code / Cursor / SDKs"]
    Scheduler["Roadmap: wake / sleep scheduler"] --> Dream["Roadmap: semantic consolidate / reorganize / decay / verify"]
    Dream --> Subagents["Roadmap: autonomous bounded memory subagent DAG"] --> Guard
    Events --> Proof["Trace ledger / benchmarks / task outcomes"]
    Compiler --> Proof --> Scheduler
```

### Connected-service deployment

The diagram below represents the optional polyglot deployment. The current
single-process standalone entrypoint is called out separately under
[Service topology](#service-topology). The Go gateway currently sends memory
writes directly to Python intelligence; the queue service exists but is not an
inline hop on that route.

```mermaid
flowchart TB
    subgraph Clients["Client entry points"]
        ProjectMCP["Project MCP<br/>stdio or opt-in loopback HTTP"]
        NetworkMCP["Network MCP<br/>Bearer authenticated"]
        Standalone["Standalone client<br/>direct local API"]
        Connected["Connected client<br/>SDK / HTTP API"]
    end

    subgraph Orchestration["Optional HTTP orchestration (Rust :50051)"]
        TI["Trigger index lookup<br/>0 LLM tokens"]
        CS["Cold start primer<br/>Inject project overview"]
        CB["Context budget<br/>+ prompt assembly"]
    end

    subgraph ControlPlane["Provena memory service (Go)"]
        GW["API + control plane :8080<br/>AuthN, RBAC, quotas"]
        MQ["Optional queue :8091<br/>Worker/backpressure surface"]
        MCPSrv["Network MCP proxy :8090"]
        Life["Lifecycle service :8092"]
    end

    subgraph Intelligence["Intelligence layer (Python :8081)"]
        WP["Write pipeline<br/>Classify, dedupe, embed<br/>Compaction proposals, entity resolution<br/>Advisory conflict signals, overview"]
        RP["Read pipeline<br/>Hybrid retrieval, rerank<br/>Contradiction, citation<br/>Context budget mgr"]
    end

    subgraph Integration["Integration plane"]
        CR["Connector registry<br/>Provider types (no shipped sync workers)"]
        SI["Source inventory<br/>Freshness + sync state"]
        PM["Principal mapping<br/>Users, groups, service accounts"]
        SG["Source permission grants<br/>Remote ACL -> local policy"]
        CV["Coverage summary<br/>Completeness + staleness"]
    end

    subgraph Storage["Storage tier"]
        Store["Python governed store :8000"]
        OP["Op store<br/>SQLite / PostgreSQL"]
        VF["Vector / FTS<br/>Semantic + keyword"]
        TIdx["Trigger index<br/>Phrase → memory"]
        EG["Entity graph<br/>Typed facts"]
        PS["Project snapshots<br/>Living overview"]
        AL["Audit log<br/>Immutable events"]
        TI2["Tenant isolation<br/>Row-level"]
        DR["Replication metadata only<br/>Automated DR is roadmap"]
        EC["Evidence + cache<br/>Provenance, sessions"]
    end

    subgraph Observability["Observability"]
        MT["Metrics + tracing"]
    end

    subgraph Eval["Eval + release gate"]
        UT["Unit + E2E tests"]
        BM["Benchmark harnesses<br/>No competitive result yet"]
    end

    ProjectMCP --> LocalKernel["Portable repo brain<br/>no shared service required"]
    NetworkMCP --> MCPSrv --> GW
    Standalone --> Store
    Connected --> GW
    GW --> WP
    MQ -.->|available async worker| WP
    GW --> RP
    GW --> Store
    GW --> Integration --> Store
    GW --> Life --> Store
    GW -.->|explicit /preflight| Orchestration
    WP --> Store
    RP --> Store
    WP --> Orchestration
    RP --> Orchestration
    Store --> OP
    WP -.->|feedback loop| RP
```

## Request flow

```mermaid
sequenceDiagram
    participant Client
    participant Gateway as Go Gateway :8080
    participant Orch as Rust Orchestration :50051
    participant Intel as Python Intelligence :8081
    participant Store as Memory Store
    participant Queue as Go Queue :8091

    Note over Client,Orch: Explicit preflight endpoint (not an implicit hop)
    Client->>Gateway: POST /preflight
    Gateway->>Orch: trigger lookup + budget request
    Orch-->>Gateway: trigger hits + allocation
    Gateway-->>Client: preflight response

    Note over Client,Store: Read path (search)
    Client->>Gateway: POST /v1/memories/search
    Gateway->>Gateway: AuthN + RBAC + rate limit
    Gateway->>Intel: POST /v1/pipeline/search
    Intel->>Store: FTS + vector search
    Store-->>Intel: candidate memories
    Intel->>Intel: rerank + contradiction + cite
    Intel->>Orch: POST /budget (context budget)
    Orch-->>Intel: selected memories
    Intel-->>Gateway: ranked results + citations
    Gateway-->>Client: SearchResponse

    Note over Client,Store: Current write path (gateway directly to intelligence)
    Client->>Gateway: POST /v1/memories
    Gateway->>Intel: POST /v1/pipeline/write
    Intel->>Intel: classify + dedupe + embed
    Intel->>Intel: entity resolve + advisory conflict detect
    Intel->>Store: persist memory + embedding
    Intel->>Orch: POST /trigger/index (register phrases)
    Intel-->>Gateway: WriteResponse
    Gateway-->>Client: MemoryWriteResult

    Note over Client,Store: Canonical repository-ledger projection
    Client->>Gateway: POST /v1/repositories/{repository_id}/memory-events/sync + bearer
    Gateway->>Store: verified tenant, principal/key, groups, and write role
    Store->>Store: verify exact SHA-256 + validate full JSONL batch
    Store->>Store: project stable records, sources, lifecycle, relations
    Store->>Store: commit checkpoint last or roll back all
    Store-->>Gateway: counts + phase timings + fingerprint
    Gateway-->>Client: attested fixed-schema response

    Note over Client,Store: Auth-disabled private loopback mode may call Store directly

    Note over Client,Store: Connected-mode sync path
    Client->>Gateway: POST /v1/integrations/connectors/{id}/sources|permissions
    Gateway->>Store: persist source inventory + principal mapping + grants
    Store-->>Gateway: sync ledger + coverage summary
    Gateway-->>Client: sync job + coverage response
```

## Service topology

| Service | Language | Port | Responsibility |
|---|---|---|---|
| `repo mcp --http` | Node | 18093 | Opt-in foreground, unauthenticated loopback Streamable HTTP over the same five-resource, seven-tool repository MCP surface |
| `orchestration` | Rust | 50051 | Trigger index, context budget, token counter, prompt assembly, cold start |
| `gateway` | Go | 8080 | API + control plane (AuthN, RBAC, quotas, routing) |
| `mcp` | Go | 8090 | Bearer-authenticated network MCP proxy; local Compose binds it to loopback |
| `queue` | Go | 8091 | Message queue consumer with backpressure |
| `lifecycle` | Go | 8092 | Retention policies, legal hold, RTBF |
| `intelligence` | Python | 8081 | Write pipeline, read pipeline, model router, embeddings, overview |
| `store` | Python | 8000 | Core memory CRUD, FTS, relations, audit log |

Current shipped deployment shapes:

- Portable repository MCP defaults to stdio. `provena mcp serve --http` is a
  separate loopback-only foreground compatibility mode; it is not the
  authenticated Go proxy and any local process can invoke its mutation tools.
- Standalone runs the Python store app directly, and clients call it on `:8000`
  by default.
- Polyglot exposes the Go gateway and MCP host ports on loopback for local
  development. Store, orchestration, intelligence, queue, and lifecycle remain
  internal data-plane services; deployed MCP forwards caller credentials to
  the authenticated gateway.
- Connected mode currently lands through those gateway and store APIs rather
  than a separate connector daemon.

The integration-plane APIs own connector registry,
source inventory, principal mapping, source permission grants, sync jobs, and
tenant-level coverage summaries. Ingestion today is push-based: an external
caller writes sources, principal mappings, permission grants, and sync-job
ledger entries through those APIs. There are no first-party scheduled sync
workers yet, so the connector registry stores provider *types* only and does
not imply Provena can crawl those systems.

`scripts/run_scheduler_tick.py` provides an optional fixed-cadence tick against
the store. It reads per-connector `metadata.scheduler` settings and dispatches
through the shared connector execution contract, but it only acts on providers
that have a registered real worker. The tick raises `KeyError` for an
unregistered provider and records the connector as skipped with reason
`provider_not_implemented` (writing nothing). The bundled CLI registers no
workers, so it is a no-op against the ledger today.

## Storage tier

| Store | Technology | Purpose |
|---|---|---|
| Op store | SQLite / PostgreSQL | Core memory records |
| Vector / FTS | SQLite FTS5 + sqlite-vec when available; PostgreSQL `tsvector` | Keyword search plus KNN or linear cosine fallback; PostgreSQL pgvector KNN is roadmap |
| Trigger index | In-memory (Rust DashMap) | Sub-ms phrase → memory lookup |
| Entity graph | Relations table | Typed links between memories |
| Repo graph | Deterministic JSON / optional Neo4j | Graph v2 combines current files, symbols, packages, commands, environment variables, and imports/uses with full ledger-event lineage, current-source links, producer-effective intervals, and graph analytics |
| Maintenance plan | Canonical JSON artifact | Attested, bounded, proposal-only evidence/path/exact-overlap review tasks; no autonomous apply or process execution |
| Project snapshots | Snapshot table | Living project overview |
| Audit log | Append-only table | Immutable lifecycle events |
| Tenant isolation | Row-level filtering | Schema/row-level isolation |
| Replication metadata | Replication state table | Tracking records only; automated backup, failover, and proven RPO/RTO are roadmap |
| Repository event projection | Immutable event map + sync checkpoint | Exact-byte, idempotent ledger-to-store replay |
| Evidence + cache | Cache table | Session provenance, cached retrievals |
| Connector registry | Connector tables | External system definitions and sync policy |
| Source inventory | Source tables | Source freshness, status, and metadata |
| Principal mapping | Mapping tables | Local principal to remote identity resolution |
| Source grants | Grant tables | Permission-preserving recall foundations |
| Sync jobs | Job ledger | Connector freshness and operational coverage |

## Connected mode and integration plane

Connected mode is the path that lets Provena plug into an existing product or
enterprise environment instead of asking teams to replace their current stack.
It adds five foundations:

1. Connector registry for system-level integrations.
2. Source inventory for every synced document, channel, ticket, or record.
3. Principal mapping from local users/groups to remote identities.
4. Source permission grants so retrieval inherits external ACLs.
5. Coverage summaries so operators can see completeness, staleness, and sync health.

Current admin API coverage:

- `POST /v1/integrations/connectors`
- `GET /v1/integrations/connectors`
- `POST /v1/integrations/connectors/{connector_id}/sources/batch`
- `POST /v1/integrations/connectors/{connector_id}/principal-mappings/batch`
- `POST /v1/integrations/connectors/{connector_id}/permissions/batch`
- `POST /v1/integrations/connectors/{connector_id}/sync-jobs`
- `GET /v1/integrations/coverage`

Scheduled sync creation complements those routes. Operators opt a connector
into cadence-driven job creation with `metadata.scheduler.enabled=true` plus a
fixed `cadence_seconds` value, then run the internal scheduler tick on the same
store boundary in standalone or polyglot deployments.

## Interfaces between layers

- **Go → Rust**: HTTP POST to orchestration service (`:50051`)
- **Go → Python**: HTTP POST to intelligence service (`:8081`)
- **Python → Rust**: HTTP POST for trigger index + budget operations
- **Python → Store**: HTTP POST to existing memory store (`:8000`)
- Future: migrate to gRPC with protobuf contracts in `proto/provena/v1/`

## Design choices

- **Rust for hot path**: trigger index lookup and context budget are on every request's critical path. Rust eliminates GC pauses and provides predictable sub-ms latency.
- **Go for infrastructure**: API gateway, queue consumer, and lifecycle services are I/O-bound with high concurrency. Go's goroutine model handles this efficiently.
- **Python for intelligence**: LLM calls, embedding generation, and ML inference lean on the Python ecosystem (sentence-transformers, Anthropic SDK, OpenAI SDK).
- **Local-first storage**: SQLite for development and PostgreSQL for production;
  both currently retain a linear cosine fallback, while pgvector KNN remains
  roadmap.
- **Explainable recall**: search results always include scoring reasons and citations.
- **Optional queued backpressure**: the queue service is available for async workers, while the current gateway write route calls intelligence directly.
- **Per-memory ACL**: RBAC at the memory level, not just tenant level.
- **Connected-mode first**: Provena can embed into existing products and enterprise systems through SDKs and admin APIs instead of forcing tool replacement.
- **Permission-preserving sync**: source ACLs and principal mappings are first-class data, not a downstream afterthought.
- **Coverage as a product signal**: tenant-level coverage exposes missing connectors, stale sources, and sync gaps before agents silently lose context.

## Verification and evaluation status

| Gate | Current status |
|---|---|
| Unit + E2E tests | Implemented across the Node, Python, Go, and Rust surfaces |
| Deterministic maintenance proposals | Implemented in the portable Node path; exact checks and cited task-packet compilation only |
| Benchmark harnesses | LoCoMo/LongMemEval-oriented harness code exists; no checked-in competitive result proves superiority |
| Retrieval quality | Metrics and evaluation plumbing exist; a release-blocking production baseline is not yet established |
| Shadow / red-team | Roadmap |
| Cold-start quality gate | Roadmap |
| Chaos / failover and proven RPO/RTO | Roadmap |
