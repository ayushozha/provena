-- Idempotency is coordinated by PostgresStore, which holds a transaction-scoped
-- advisory migration lock across schema bootstrap and each named-constraint
-- check before executing its ALTER TABLE statement.
-- NOT VALID protects new writes immediately without pretending legacy rows
-- were checked; startup audits them and validates each constraint only when
-- the audit is clean.

CREATE UNIQUE INDEX IF NOT EXISTS uq_connectors_id_tenant
    ON connectors(connector_id, tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_connector_sources_id_connector_tenant
    ON connector_sources(source_id, connector_id, tenant_id);

ALTER TABLE connector_sources
    ADD CONSTRAINT fk_connector_sources_connector_tenant
    FOREIGN KEY (connector_id, tenant_id)
    REFERENCES connectors(connector_id, tenant_id)
    ON DELETE CASCADE
    NOT VALID;

ALTER TABLE principal_mappings
    ADD CONSTRAINT fk_principal_mappings_connector_tenant
    FOREIGN KEY (connector_id, tenant_id)
    REFERENCES connectors(connector_id, tenant_id)
    ON DELETE CASCADE
    NOT VALID;

ALTER TABLE source_permission_grants
    ADD CONSTRAINT fk_source_permission_grants_source_tenant
    FOREIGN KEY (source_id, connector_id, tenant_id)
    REFERENCES connector_sources(source_id, connector_id, tenant_id)
    ON DELETE CASCADE
    NOT VALID;

ALTER TABLE source_permission_grants
    ADD CONSTRAINT fk_source_permission_grants_connector_tenant
    FOREIGN KEY (connector_id, tenant_id)
    REFERENCES connectors(connector_id, tenant_id)
    ON DELETE CASCADE
    NOT VALID;

ALTER TABLE sync_jobs
    ADD CONSTRAINT fk_sync_jobs_connector_tenant
    FOREIGN KEY (connector_id, tenant_id)
    REFERENCES connectors(connector_id, tenant_id)
    ON DELETE CASCADE
    NOT VALID;
