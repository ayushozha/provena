-- Provena Storage Schema v1
-- Supports: Op store, Vector/FTS, Trigger index, Entity graph,
--           Project snapshots, Audit log, Tenant isolation,
--           DR+replication metadata, Evidence+cache

-- ---------------------------------------------------------------------------
-- Op store: core memories table with tenant isolation (row-level)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS memories (
    memory_id       TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    -- Tenant isolation: every row scoped to tenant
    tenant_id       TEXT NOT NULL,
    workspace_id    TEXT,
    project_id      TEXT,
    user_id         TEXT,
    agent_id        TEXT,
    session_id      TEXT,
    -- Content
    title           TEXT,
    content         TEXT NOT NULL,
    summary         TEXT,
    entity_keys_json TEXT NOT NULL DEFAULT '[]',
    tags_json       TEXT NOT NULL DEFAULT '[]',
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    -- Scores
    importance      REAL NOT NULL DEFAULT 0.5,
    confidence      REAL NOT NULL DEFAULT 0.7,
    strength        REAL NOT NULL DEFAULT 0.7,
    -- Temporal validity
    valid_from      TEXT,
    valid_to        TEXT,
    -- Timestamps
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_verified_at TEXT,
    -- Embedding
    embedding_model TEXT,
    embedding_json  TEXT,  -- JSON array of floats (for SQLite; pgvector in Postgres)
    -- ACL (per-memory RBAC)
    acl_json        TEXT NOT NULL DEFAULT '[]',
    -- Legal hold
    held            INTEGER NOT NULL DEFAULT 0,
    hold_reason     TEXT,
    hold_until      TEXT
);

CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories(tenant_id);
CREATE INDEX IF NOT EXISTS idx_memories_tenant_project ON memories(tenant_id, project_id);
CREATE INDEX IF NOT EXISTS idx_memories_tenant_user ON memories(tenant_id, user_id);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC);

-- ---------------------------------------------------------------------------
-- Vector / FTS: full-text search index (SQLite FTS5)
-- For Postgres: replace with pg_trgm + tsvector + pgvector
-- ---------------------------------------------------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    memory_id UNINDEXED,
    title,
    summary,
    content,
    tags,
    entity_keys
);

-- ---------------------------------------------------------------------------
-- Trigger index: phrase → memory_id mapping for sub-ms lookup
-- The Rust orchestration layer loads this into memory on startup
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS trigger_index (
    trigger_id  TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL,
    phrase      TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY(memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trigger_phrase ON trigger_index(phrase, tenant_id);
CREATE INDEX IF NOT EXISTS idx_trigger_memory ON trigger_index(memory_id);

-- ---------------------------------------------------------------------------
-- Entity graph: typed facts and relations between memories
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS memory_relations (
    relation_id     TEXT PRIMARY KEY,
    from_memory_id  TEXT NOT NULL,
    to_memory_id    TEXT NOT NULL,
    relation        TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    workspace_id    TEXT,
    project_id      TEXT,
    user_id         TEXT,
    agent_id        TEXT,
    session_id      TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(from_memory_id, to_memory_id, relation),
    FOREIGN KEY(from_memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE,
    FOREIGN KEY(to_memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_relations_from ON memory_relations(from_memory_id);
CREATE INDEX IF NOT EXISTS idx_relations_to ON memory_relations(to_memory_id);

-- Entity resolution table: maps entity keys to canonical forms
CREATE TABLE IF NOT EXISTS entity_registry (
    entity_id       TEXT PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    aliases_json    TEXT NOT NULL DEFAULT '[]',
    entity_type     TEXT,  -- person, org, concept, product, etc.
    tenant_id       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entity_tenant ON entity_registry(tenant_id);
CREATE INDEX IF NOT EXISTS idx_entity_canonical ON entity_registry(canonical_name);

-- ---------------------------------------------------------------------------
-- Source references: provenance tracking
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS memory_sources (
    source_ref_id   TEXT PRIMARY KEY,
    memory_id       TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    uri             TEXT,
    title           TEXT,
    excerpt         TEXT,
    span_start      INTEGER,
    span_end        INTEGER,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sources_memory ON memory_sources(memory_id);

-- ---------------------------------------------------------------------------
-- Project snapshots: living overview of project state
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS project_snapshots (
    snapshot_id     TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    summary         TEXT NOT NULL,
    entity_summary_json TEXT NOT NULL DEFAULT '{}',
    decision_log_json   TEXT NOT NULL DEFAULT '[]',
    memory_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_project ON project_snapshots(tenant_id, project_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_created ON project_snapshots(created_at DESC);

-- ---------------------------------------------------------------------------
-- Audit log: immutable lifecycle events
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id        TEXT PRIMARY KEY,
    action          TEXT NOT NULL,
    memory_id       TEXT,
    actor_id        TEXT,
    tenant_id       TEXT,
    details_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_memory ON audit_log(memory_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_log(tenant_id);

-- ---------------------------------------------------------------------------
-- Retention policies
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS retention_policies (
    policy_id       TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    kind            TEXT,  -- NULL = applies to all kinds
    max_age_days    INTEGER NOT NULL DEFAULT 365,
    action          TEXT NOT NULL DEFAULT 'delete_soft',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_retention_tenant ON retention_policies(tenant_id);

-- ---------------------------------------------------------------------------
-- Legal holds
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS legal_holds (
    hold_id         TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    memory_ids_json TEXT NOT NULL DEFAULT '[]',
    scope_json      TEXT,
    reason          TEXT NOT NULL,
    hold_until      TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_holds_tenant ON legal_holds(tenant_id);

-- ---------------------------------------------------------------------------
-- Token accounting: usage tracking per tenant/operation
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS token_usage (
    usage_id        TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    operation       TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    model           TEXT,
    cost_usd        REAL NOT NULL DEFAULT 0.0,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_tenant ON token_usage(tenant_id);
CREATE INDEX IF NOT EXISTS idx_usage_operation ON token_usage(operation);
CREATE INDEX IF NOT EXISTS idx_usage_created ON token_usage(created_at DESC);

-- ---------------------------------------------------------------------------
-- Evidence + cache: provenance sessions and cached retrievals
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS evidence_cache (
    cache_id        TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    query           TEXT NOT NULL,
    result_ids_json TEXT NOT NULL DEFAULT '[]',
    score_map_json  TEXT NOT NULL DEFAULT '{}',
    token_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    expires_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_cache_session ON evidence_cache(session_id);
CREATE INDEX IF NOT EXISTS idx_cache_tenant ON evidence_cache(tenant_id);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON evidence_cache(expires_at);

-- ---------------------------------------------------------------------------
-- DR + replication: replication state tracking
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS replication_state (
    replica_id      TEXT PRIMARY KEY,
    source_path     TEXT NOT NULL,
    target_path     TEXT NOT NULL,
    last_sync_at    TEXT,
    last_lsn        TEXT,  -- log sequence number
    status          TEXT NOT NULL DEFAULT 'active',
    rpo_seconds     INTEGER NOT NULL DEFAULT 60,
    rto_seconds     INTEGER NOT NULL DEFAULT 300,
    created_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Embedding model registry: track which models produced which embeddings
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS embedding_models (
    model_id        TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    dimensions      INTEGER NOT NULL,
    max_tokens      INTEGER NOT NULL DEFAULT 8192,
    version         TEXT NOT NULL DEFAULT '1',
    is_default      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Integration plane: external system connectors, source inventory,
-- principal mapping, source grants, and sync jobs
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS connectors (
    connector_id             TEXT PRIMARY KEY,
    tenant_id                TEXT NOT NULL,
    provider                 TEXT NOT NULL,
    display_name             TEXT NOT NULL,
    remote_workspace_id      TEXT,
    auth_type                TEXT NOT NULL,
    sync_mode                TEXT NOT NULL DEFAULT 'hybrid',
    status                   TEXT NOT NULL DEFAULT 'active',
    principal_sync_enabled   INTEGER NOT NULL DEFAULT 1,
    acl_sync_enabled         INTEGER NOT NULL DEFAULT 1,
    freshness_sla_seconds    INTEGER NOT NULL DEFAULT 3600,
    metadata_json            TEXT NOT NULL DEFAULT '{}',
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    last_synced_at           TEXT,
    last_webhook_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_connectors_tenant ON connectors(tenant_id);
CREATE INDEX IF NOT EXISTS idx_connectors_provider ON connectors(provider);
CREATE INDEX IF NOT EXISTS idx_connectors_status ON connectors(status);

CREATE TABLE IF NOT EXISTS connector_sources (
    source_id          TEXT PRIMARY KEY,
    connector_id       TEXT NOT NULL,
    tenant_id          TEXT NOT NULL,
    remote_source_id   TEXT NOT NULL,
    source_type        TEXT NOT NULL,
    display_name       TEXT NOT NULL,
    path               TEXT,
    status             TEXT NOT NULL DEFAULT 'indexed',
    last_synced_at     TEXT,
    stale_after        TEXT,
    acl_hash           TEXT,
    metadata_json      TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE(connector_id, remote_source_id),
    FOREIGN KEY(connector_id) REFERENCES connectors(connector_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_connector_sources_connector ON connector_sources(connector_id);
CREATE INDEX IF NOT EXISTS idx_connector_sources_tenant ON connector_sources(tenant_id);
CREATE INDEX IF NOT EXISTS idx_connector_sources_status ON connector_sources(status);

CREATE TABLE IF NOT EXISTS principal_mappings (
    mapping_id            TEXT PRIMARY KEY,
    connector_id          TEXT NOT NULL,
    tenant_id             TEXT NOT NULL,
    principal_type        TEXT NOT NULL,
    local_principal_id    TEXT NOT NULL,
    remote_principal_id   TEXT NOT NULL,
    remote_name           TEXT,
    groups_json           TEXT NOT NULL DEFAULT '[]',
    last_synced_at        TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE(connector_id, principal_type, local_principal_id, remote_principal_id),
    FOREIGN KEY(connector_id) REFERENCES connectors(connector_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_principal_mappings_connector ON principal_mappings(connector_id);
CREATE INDEX IF NOT EXISTS idx_principal_mappings_tenant ON principal_mappings(tenant_id);

CREATE TABLE IF NOT EXISTS source_permission_grants (
    grant_id              TEXT PRIMARY KEY,
    source_id             TEXT NOT NULL,
    connector_id          TEXT NOT NULL,
    tenant_id             TEXT NOT NULL,
    principal_type        TEXT NOT NULL,
    principal_id          TEXT NOT NULL,
    permission_level      TEXT NOT NULL,
    inherited             INTEGER NOT NULL DEFAULT 1,
    remote_permission_id  TEXT,
    created_at            TEXT NOT NULL,
    UNIQUE(source_id, principal_type, principal_id, permission_level),
    FOREIGN KEY(source_id) REFERENCES connector_sources(source_id) ON DELETE CASCADE,
    FOREIGN KEY(connector_id) REFERENCES connectors(connector_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_source_permission_source ON source_permission_grants(source_id);
CREATE INDEX IF NOT EXISTS idx_source_permission_connector ON source_permission_grants(connector_id);
CREATE INDEX IF NOT EXISTS idx_source_permission_tenant ON source_permission_grants(tenant_id);

CREATE TABLE IF NOT EXISTS sync_jobs (
    job_id             TEXT PRIMARY KEY,
    connector_id       TEXT NOT NULL,
    tenant_id          TEXT NOT NULL,
    job_type           TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'queued',
    cursor             TEXT,
    stats_json         TEXT NOT NULL DEFAULT '{}',
    error_message      TEXT,
    started_at         TEXT,
    finished_at        TEXT,
    created_at         TEXT NOT NULL,
    FOREIGN KEY(connector_id) REFERENCES connectors(connector_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sync_jobs_connector ON sync_jobs(connector_id);
CREATE INDEX IF NOT EXISTS idx_sync_jobs_tenant ON sync_jobs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_sync_jobs_status ON sync_jobs(status);

-- ---------------------------------------------------------------------------
-- API keys for auth (development; production uses external auth)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS api_keys (
    key_id          TEXT PRIMARY KEY,
    key_hash        TEXT NOT NULL UNIQUE,
    tenant_id       TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'editor',
    description     TEXT,
    created_at      TEXT NOT NULL,
    expires_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_apikeys_tenant ON api_keys(tenant_id);
CREATE INDEX IF NOT EXISTS idx_apikeys_hash ON api_keys(key_hash);

-- ---------------------------------------------------------------------------
-- Memory history: append-only audit trail of add/update/delete/feedback events
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS memory_history (
    history_id      TEXT PRIMARY KEY,
    memory_id       TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    event           TEXT NOT NULL,
    actor_id        TEXT,
    old_memory_json TEXT NOT NULL DEFAULT '{}',
    new_memory_json TEXT NOT NULL DEFAULT '{}',
    details_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_memory ON memory_history(memory_id);
CREATE INDEX IF NOT EXISTS idx_history_tenant ON memory_history(tenant_id);
CREATE INDEX IF NOT EXISTS idx_history_created ON memory_history(created_at DESC);

-- ---------------------------------------------------------------------------
-- Memory feedback: per-memory signals (positive/negative/correction/pin)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS memory_feedback (
    feedback_id     TEXT PRIMARY KEY,
    memory_id       TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    feedback_type   TEXT NOT NULL,
    principal_id    TEXT,
    reason          TEXT,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_memory ON memory_feedback(memory_id);
CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON memory_feedback(tenant_id);
