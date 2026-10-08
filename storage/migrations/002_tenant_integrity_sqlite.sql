-- Upgrade legacy integration tables from identifier-only foreign keys to
-- tenant-coupled foreign keys. The application audits every legacy row before
-- executing this script and runs these statements inside one write transaction
-- with foreign-key enforcement temporarily disabled on the migration connection.

CREATE TABLE connector_sources__tenant_integrity_new (
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
    CONSTRAINT fk_connector_sources_connector_tenant
        FOREIGN KEY(connector_id, tenant_id)
        REFERENCES connectors(connector_id, tenant_id) ON DELETE CASCADE
);

CREATE TABLE principal_mappings__tenant_integrity_new (
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
    CONSTRAINT fk_principal_mappings_connector_tenant
        FOREIGN KEY(connector_id, tenant_id)
        REFERENCES connectors(connector_id, tenant_id) ON DELETE CASCADE
);

CREATE TABLE source_permission_grants__tenant_integrity_new (
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
    CONSTRAINT fk_source_permission_grants_source_tenant
        FOREIGN KEY(source_id, connector_id, tenant_id)
        REFERENCES connector_sources(source_id, connector_id, tenant_id) ON DELETE CASCADE,
    CONSTRAINT fk_source_permission_grants_connector_tenant
        FOREIGN KEY(connector_id, tenant_id)
        REFERENCES connectors(connector_id, tenant_id) ON DELETE CASCADE
);

CREATE TABLE sync_jobs__tenant_integrity_new (
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
    CONSTRAINT fk_sync_jobs_connector_tenant
        FOREIGN KEY(connector_id, tenant_id)
        REFERENCES connectors(connector_id, tenant_id) ON DELETE CASCADE
);

INSERT INTO connector_sources__tenant_integrity_new (
    source_id, connector_id, tenant_id, remote_source_id, source_type,
    display_name, path, status, last_synced_at, stale_after, acl_hash,
    metadata_json, created_at, updated_at
)
SELECT
    source_id, connector_id, tenant_id, remote_source_id, source_type,
    display_name, path, status, last_synced_at, stale_after, acl_hash,
    metadata_json, created_at, updated_at
FROM connector_sources;

INSERT INTO principal_mappings__tenant_integrity_new (
    mapping_id, connector_id, tenant_id, principal_type, local_principal_id,
    remote_principal_id, remote_name, groups_json, last_synced_at, created_at,
    updated_at
)
SELECT
    mapping_id, connector_id, tenant_id, principal_type, local_principal_id,
    remote_principal_id, remote_name, groups_json, last_synced_at, created_at,
    updated_at
FROM principal_mappings;

INSERT INTO source_permission_grants__tenant_integrity_new (
    grant_id, source_id, connector_id, tenant_id, principal_type, principal_id,
    permission_level, inherited, remote_permission_id, created_at
)
SELECT
    grant_id, source_id, connector_id, tenant_id, principal_type, principal_id,
    permission_level, inherited, remote_permission_id, created_at
FROM source_permission_grants;

INSERT INTO sync_jobs__tenant_integrity_new (
    job_id, connector_id, tenant_id, job_type, status, cursor, stats_json,
    error_message, started_at, finished_at, created_at
)
SELECT
    job_id, connector_id, tenant_id, job_type, status, cursor, stats_json,
    error_message, started_at, finished_at, created_at
FROM sync_jobs;

DROP TABLE source_permission_grants;
DROP TABLE principal_mappings;
DROP TABLE sync_jobs;
DROP TABLE connector_sources;

ALTER TABLE connector_sources__tenant_integrity_new RENAME TO connector_sources;
ALTER TABLE principal_mappings__tenant_integrity_new RENAME TO principal_mappings;
ALTER TABLE source_permission_grants__tenant_integrity_new RENAME TO source_permission_grants;
ALTER TABLE sync_jobs__tenant_integrity_new RENAME TO sync_jobs;

CREATE INDEX idx_connector_sources_connector ON connector_sources(connector_id);
CREATE INDEX idx_connector_sources_tenant ON connector_sources(tenant_id);
CREATE INDEX idx_connector_sources_status ON connector_sources(status);
CREATE UNIQUE INDEX uq_connector_sources_id_connector_tenant
    ON connector_sources(source_id, connector_id, tenant_id);

CREATE INDEX idx_principal_mappings_connector ON principal_mappings(connector_id);
CREATE INDEX idx_principal_mappings_tenant ON principal_mappings(tenant_id);

CREATE INDEX idx_source_permission_source ON source_permission_grants(source_id);
CREATE INDEX idx_source_permission_connector ON source_permission_grants(connector_id);
CREATE INDEX idx_source_permission_tenant ON source_permission_grants(tenant_id);

CREATE INDEX idx_sync_jobs_connector ON sync_jobs(connector_id);
CREATE INDEX idx_sync_jobs_tenant ON sync_jobs(tenant_id);
CREATE INDEX idx_sync_jobs_status ON sync_jobs(status);
