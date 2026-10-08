"""Integration tests for PostgresStore (skipped without PROVENA_DATABASE_URL)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch

import pytest
import rfc8785

from app.models import (
    AgentContextRequest,
    ACLEntry,
    ACLPermission,
    ConnectorAuthType,
    ConnectorConfig,
    ConnectorProvider,
    ConnectorSourceBatch,
    ConnectorSourceRecord,
    EraseRequest,
    LegalHold,
    MemoryCreate,
    MemoryHistoryEventType,
    MemoryKind,
    MemoryStatus,
    MemoryUpdate,
    PermissionLevel,
    PrincipalMapping,
    PrincipalMappingBatch,
    RepositoryMemorySyncRequest,
    RTBFRequest,
    RelationKind,
    RelationWrite,
    RetentionPolicy,
    ScopeEnvelope,
    SearchRequest,
    SourcePermissionBatch,
    SourcePermissionGrant,
    SyncJob,
    SyncJobType,
)
from app.store import (
    AccessContext, TenantOwnershipConflictError, TenantOwnershipError, TenantParentNotFoundError,
)
from app.store_postgres import (
    POSTGRES_INTEGRITY_LOCK_NAMESPACE,
    POSTGRES_TENANT_INTEGRITY_MIGRATION_PATH,
    POSTGRES_TENANT_INTEGRITY_MIGRATION_LOCK,
    PostgresStore,
)

pytestmark = pytest.mark.postgres


def test_postgres_integrity_fence_uses_transaction_scoped_advisory_lock() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class RecordingConnection:
        def execute(self, query: str, params: tuple[object, ...]):
            calls.append((query, params))

    store = object.__new__(PostgresStore)
    store.conn = RecordingConnection()  # type: ignore[assignment]

    store._begin_immediate_write()

    assert calls == [
        (
            "SELECT pg_advisory_xact_lock(?, ?)",
            (POSTGRES_INTEGRITY_LOCK_NAMESPACE, 1),
        )
    ]


def test_postgres_tenant_integrity_upgrade_uses_distinct_advisory_lock() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class RecordingConnection:
        def execute(self, query: str, params: tuple[object, ...]):
            calls.append((query, params))

    store = object.__new__(PostgresStore)
    store.conn = RecordingConnection()  # type: ignore[assignment]

    store._acquire_tenant_integrity_migration_lock()

    assert calls == [
        (
            "SELECT pg_advisory_xact_lock(?, ?)",
            (
                POSTGRES_INTEGRITY_LOCK_NAMESPACE,
                POSTGRES_TENANT_INTEGRITY_MIGRATION_LOCK,
            ),
        )
    ]
    assert POSTGRES_TENANT_INTEGRITY_MIGRATION_LOCK != 1


def test_postgres_erasure_fence_survives_autocommit_connection() -> None:
    class AutocommitConnection:
        active = False
        fenced = False

        def __enter__(self):
            self.active = True
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.active = False
            self.fenced = False

        def execute(self, query: str, params: tuple[object, ...]):
            if "pg_advisory_xact_lock" in query:
                # An advisory transaction lock taken outside the context is
                # immediately released by PostgreSQL's autocommit transaction.
                self.fenced = self.active
            else:
                assert self.active and self.fenced, "erasure ran without its legal-hold fence"
            return self

        def fetchall(self):
            return []

    store = object.__new__(PostgresStore)
    store.conn = AutocommitConnection()  # type: ignore[assignment]
    with patch.object(store, "_purge_tenant_cache"):
        result = store.rtbf(RTBFRequest(tenant_id="tenant-fence"))
    assert result.deleted_memories == 0
    assert result.held_memories == []


def test_postgres_schema_bootstrap_runs_inside_migration_lock_transaction() -> None:
    events: list[str] = []

    class RecordingConnection:
        def __enter__(self):
            events.append("transaction-enter")
            return self

        def __exit__(self, exc_type, exc, traceback):
            events.append("transaction-exit")

    store = object.__new__(PostgresStore)
    store.conn = RecordingConnection()  # type: ignore[assignment]

    with (
        patch.object(
            store,
            "_acquire_tenant_integrity_migration_lock",
            side_effect=lambda: events.append("lock"),
        ),
        patch.object(store, "_ensure_schema", side_effect=lambda: events.append("schema")),
        patch.object(
            store,
            "_ensure_compatibility",
            side_effect=lambda: events.append("compatibility"),
        ),
        patch.object(
            store,
            "_ensure_tenant_integrity_locked",
            side_effect=lambda: events.append("tenant-integrity"),
        ),
    ):
        store._initialize_schema()

    assert events == [
        "transaction-enter",
        "lock",
        "schema",
        "compatibility",
        "tenant-integrity",
        "transaction-exit",
    ]


def test_postgres_tenant_integrity_migration_is_named_and_not_valid() -> None:
    migration = POSTGRES_TENANT_INTEGRITY_MIGRATION_PATH.read_text(encoding="utf-8")
    statements = PostgresStore._migration_statements(
        POSTGRES_TENANT_INTEGRITY_MIGRATION_PATH
    )
    expected_constraints = {
        "fk_connector_sources_connector_tenant",
        "fk_principal_mappings_connector_tenant",
        "fk_source_permission_grants_source_tenant",
        "fk_source_permission_grants_connector_tenant",
        "fk_sync_jobs_connector_tenant",
    }

    assert expected_constraints == {
        name
        for name in expected_constraints
        if f"ADD CONSTRAINT {name}" in migration
    }
    assert sum(
        "ADD CONSTRAINT" in statement and "NOT VALID" in statement
        for statement in statements
    ) == len(expected_constraints)
    assert "uq_connectors_id_tenant" in migration
    assert "uq_connector_sources_id_connector_tenant" in migration


def _database_url() -> str | None:
    return os.environ.get("PROVENA_DATABASE_URL")


@pytest.fixture
def postgres_store() -> PostgresStore:
    url = _database_url()
    if not url:
        pytest.skip("PROVENA_DATABASE_URL not set")
    store = PostgresStore(url)
    yield store
    store.close()


@pytest.fixture
def admin_access() -> AccessContext:
    return AccessContext(tenant_id="tenant-pg-test", role="admin", principal_id="admin-1")


@pytest.fixture
def unique_tenant() -> str:
    return f"tenant-pg-{uuid.uuid4().hex[:12]}"


def _count(store: PostgresStore, table: str, where: str, params: tuple[object, ...]) -> int:
    row = store.conn.execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE {where}",
        params,
    ).fetchone()
    return int(row["count"])


def _creation_counts(store: PostgresStore, memory_id: str) -> tuple[int, int, int]:
    return (
        _count(store, "memories", "memory_id = ?", (memory_id,)),
        _count(
            store,
            "audit_log",
            "memory_id = ? AND action = ?",
            (memory_id, "memory_created"),
        ),
        _count(
            store,
            "memory_history",
            "memory_id = ? AND event = ?",
            (memory_id, MemoryHistoryEventType.ADD.value),
        ),
    )


def test_existing_postgres_schema_adds_not_valid_guard_then_validates_when_clean(
    postgres_store: PostgresStore,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    connector_id = f"connector-upgrade-{suffix}"
    source_id = f"source-upgrade-{suffix}"
    constraint = "fk_connector_sources_connector_tenant"
    legacy_constraint = "fk_connector_sources_connector_legacy_test"
    try:
        with postgres_store.conn:
            postgres_store.conn.execute(
                f"ALTER TABLE connector_sources DROP CONSTRAINT IF EXISTS {constraint}"
            )
            postgres_store.conn.execute(
                "ALTER TABLE connector_sources "
                f"DROP CONSTRAINT IF EXISTS {legacy_constraint}"
            )
            postgres_store.conn.execute(
                "ALTER TABLE connector_sources "
                f"ADD CONSTRAINT {legacy_constraint} FOREIGN KEY (connector_id) "
                "REFERENCES connectors(connector_id) ON DELETE CASCADE"
            )
            postgres_store.conn.execute(
                """
                INSERT INTO connectors (
                    connector_id, tenant_id, provider, display_name, auth_type,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    connector_id,
                    "tenant-upgrade-a",
                    "custom",
                    "Upgrade connector",
                    "api_key",
                    "2026-07-13T00:00:00+00:00",
                    "2026-07-13T00:00:00+00:00",
                ),
            )
            postgres_store.conn.execute(
                """
                INSERT INTO connector_sources (
                    source_id, connector_id, tenant_id, remote_source_id,
                    source_type, display_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    connector_id,
                    "tenant-upgrade-b",
                    f"remote-{suffix}",
                    "repository",
                    "Legacy mismatched source",
                    "2026-07-13T00:00:00+00:00",
                    "2026-07-13T00:00:00+00:00",
                ),
            )

        postgres_store._ensure_tenant_integrity()
        assert postgres_store.tenant_integrity_status()["ready"] is False
        assert postgres_store.tenant_integrity_status()["issues"]["connector_sources"] >= 1
        assert postgres_store._postgres_constraint_validated(
            "connector_sources", constraint
        ) is False
        postgres_store.conn.commit()

        with postgres_store.conn:
            postgres_store.conn.execute(
                "DELETE FROM connector_sources WHERE source_id = ?",
                (source_id,),
            )
        postgres_store._ensure_tenant_integrity()
        assert postgres_store.tenant_integrity_status()["ready"] is True
        assert postgres_store._postgres_constraint_validated(
            "connector_sources", constraint
        ) is True
        postgres_store.conn.commit()
    finally:
        with postgres_store.conn:
            postgres_store.conn.execute(
                "DELETE FROM connector_sources WHERE source_id = ?",
                (source_id,),
            )
            postgres_store.conn.execute(
                "DELETE FROM connectors WHERE connector_id = ?",
                (connector_id,),
            )
            postgres_store.conn.execute(
                "ALTER TABLE connector_sources "
                f"DROP CONSTRAINT IF EXISTS {legacy_constraint}"
            )
        postgres_store._ensure_tenant_integrity()


def test_create_and_get_memory(postgres_store: PostgresStore, admin_access: AccessContext, unique_tenant: str) -> None:
    access = AccessContext(tenant_id=unique_tenant, role="admin", principal_id="admin-1")
    payload = MemoryCreate(
        kind=MemoryKind.DECISION,
        scope=ScopeEnvelope(tenant_id=unique_tenant, workspace_id="ws-1", project_id="proj-1"),
        title="Postgres memory",
        content="Every recall should expose provenance and retrieval reasons.",
        tags=["postgres", "plan-15"],
        entity_keys=["storage-backend"],
    )
    result = postgres_store.create_memory(payload, access=access)
    assert result.created is True
    fetched = postgres_store.get_memory(result.memory.memory_id, access=access)
    assert fetched is not None
    assert fetched.title == "Postgres memory"
    assert fetched.scope.tenant_id == unique_tenant


def test_search_memories_fts(postgres_store: PostgresStore, unique_tenant: str) -> None:
    access = AccessContext(tenant_id=unique_tenant, role="admin", principal_id="admin-1")
    postgres_store.create_memory(
        MemoryCreate(
            kind=MemoryKind.FACT,
            scope=ScopeEnvelope(tenant_id=unique_tenant, workspace_id="ws-search"),
            title="Citation policy",
            content="Agents must cite memory provenance in every answer.",
            tags=["citations"],
        ),
        access=access,
    )
    postgres_store.create_memory(
        MemoryCreate(
            kind=MemoryKind.FACT,
            scope=ScopeEnvelope(tenant_id=unique_tenant, workspace_id="ws-search"),
            title="Unrelated note",
            content="The office coffee machine was replaced last week.",
        ),
        access=access,
    )
    response = postgres_store.search_memories(
        SearchRequest(
            query="provenance citations",
            scope=ScopeEnvelope(tenant_id=unique_tenant, workspace_id="ws-search"),
            limit=5,
        ),
        access=access,
    )
    assert response.results
    assert any("provenance" in (item.memory.content or "").lower() for item in response.results)


def test_write_relation(postgres_store: PostgresStore, unique_tenant: str) -> None:
    access = AccessContext(tenant_id=unique_tenant, role="admin", principal_id="admin-1")
    scope = ScopeEnvelope(tenant_id=unique_tenant, workspace_id="ws-rel")
    first = postgres_store.create_memory(
        MemoryCreate(kind=MemoryKind.FACT, scope=scope, content="Source memory"),
        access=access,
    )
    second = postgres_store.create_memory(
        MemoryCreate(kind=MemoryKind.FACT, scope=scope, content="Target memory"),
        access=access,
    )
    postgres_store.write_relation(
        RelationWrite(
            from_memory_id=first.memory.memory_id,
            to_memory_id=second.memory.memory_id,
            relation=RelationKind.RELATED_TO,
            scope=scope,
        ),
        access=access,
    )
    related = postgres_store._related_memories(first.memory.memory_id, access)
    assert any(item.memory.memory_id == second.memory.memory_id for item in related)


def test_entity_registry(postgres_store: PostgresStore, unique_tenant: str) -> None:
    entity_id = f"entity-{uuid.uuid4().hex[:8]}"
    postgres_store.upsert_entity(
        entity_id=entity_id,
        canonical_name="ProvenaStore",
        tenant_id=unique_tenant,
        aliases=["store", "memory store"],
        entity_type="concept",
    )
    entity = postgres_store.get_entity(entity_id, unique_tenant)
    assert entity is not None
    assert entity["canonical_name"] == "ProvenaStore"
    assert "store" in entity["aliases"]
    with pytest.raises(TenantOwnershipConflictError, match="resource write conflict"):
        postgres_store.upsert_entity(
            entity_id=entity_id,
            canonical_name="Stolen entity",
            tenant_id=f"{unique_tenant}-other",
        )
    assert postgres_store.get_entity(entity_id, f"{unique_tenant}-other") is None
    assert postgres_store.get_entity(entity_id, unique_tenant)["canonical_name"] == "ProvenaStore"


def test_atomic_tenant_owned_connector_race() -> None:
    url = _database_url()
    if not url:
        pytest.skip("PROVENA_DATABASE_URL not set")
    barrier = threading.Barrier(2)

    class RacingStore(PostgresStore):
        def _assert_tenant_owned_id(
            self,
            resource: str,
            resource_id: str,
            tenant_id: str,
            *,
            require_exists: bool = False,
        ) -> None:
            super()._assert_tenant_owned_id(
                resource,
                resource_id,
                tenant_id,
                require_exists=require_exists,
            )
            if resource == "connector" and not require_exists:
                barrier.wait(timeout=10)

    stores = [RacingStore(url), RacingStore(url)]
    connector_id = f"connector-race-{uuid.uuid4().hex}"
    tenants = [f"tenant-race-a-{uuid.uuid4().hex}", f"tenant-race-b-{uuid.uuid4().hex}"]
    outcomes: list[tuple[str, str]] = []
    outcome_lock = threading.Lock()

    def write(store: PostgresStore, tenant_id: str) -> None:
        try:
            store.save_connector(
                ConnectorConfig(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    provider=ConnectorProvider.CUSTOM,
                    display_name="Atomic ownership race",
                    auth_type=ConnectorAuthType.API_KEY,
                )
            )
            outcome = ("created", tenant_id)
        except TenantOwnershipConflictError:
            outcome = ("conflict", tenant_id)
        with outcome_lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=write, args=(store, tenant), daemon=True)
        for store, tenant in zip(stores, tenants, strict=True)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(status for status, _ in outcomes) == ["conflict", "created"]
        row = stores[0].conn.execute(
            "SELECT tenant_id FROM connectors WHERE connector_id = ?",
            (connector_id,),
        ).fetchone()
        assert row is not None
        winner = next(tenant for status, tenant in outcomes if status == "created")
        assert row["tenant_id"] == winner
        audit_count = stores[0].conn.execute(
            """
            SELECT COUNT(*) AS count FROM audit_log
            WHERE action = 'connector_saved'
              AND details_json::jsonb ->> 'connector_id' = ?
            """,
            (connector_id,),
        ).fetchone()
        assert audit_count is not None and int(audit_count["count"]) == 1
    finally:
        for store in stores:
            store.close()


def test_tenant_owned_children_cannot_be_reparented_within_tenant(
    postgres_store: PostgresStore,
    unique_tenant: str,
) -> None:
    suffix = uuid.uuid4().hex
    connector_a = f"connector-a-{suffix}"
    connector_b = f"connector-b-{suffix}"
    source_a = f"source-a-{suffix}"
    source_b = f"source-b-{suffix}"
    for connector_id in (connector_a, connector_b):
        postgres_store.save_connector(
            ConnectorConfig(
                connector_id=connector_id,
                tenant_id=unique_tenant,
                provider=ConnectorProvider.CUSTOM,
                display_name=connector_id,
                auth_type=ConnectorAuthType.API_KEY,
            )
        )
    postgres_store.save_connector_sources(
        connector_a,
        unique_tenant,
        ConnectorSourceBatch(
            sources=[
                ConnectorSourceRecord(
                    source_id=source_a,
                    connector_id=connector_a,
                    tenant_id=unique_tenant,
                    remote_source_id="remote-a",
                    source_type="repository",
                    display_name="Source A",
                )
            ]
        ),
    )
    postgres_store.save_connector_sources(
        connector_b,
        unique_tenant,
        ConnectorSourceBatch(
            sources=[
                ConnectorSourceRecord(
                    source_id=source_b,
                    connector_id=connector_b,
                    tenant_id=unique_tenant,
                    remote_source_id="remote-b",
                    source_type="repository",
                    display_name="Source B",
                )
            ]
        ),
    )
    mapping_id = f"mapping-{suffix}"
    grant_id = f"grant-{suffix}"
    job_id = f"job-{suffix}"
    postgres_store.save_principal_mappings(
        connector_a,
        unique_tenant,
        PrincipalMappingBatch(
            mappings=[
                PrincipalMapping(
                    mapping_id=mapping_id,
                    connector_id=connector_a,
                    tenant_id=unique_tenant,
                    principal_type="user",
                    local_principal_id="local-a",
                    remote_principal_id="remote-a",
                )
            ]
        ),
    )
    postgres_store.save_source_permission_grants(
        connector_a,
        unique_tenant,
        SourcePermissionBatch(
            grants=[
                SourcePermissionGrant(
                    grant_id=grant_id,
                    source_id=source_a,
                    connector_id=connector_a,
                    tenant_id=unique_tenant,
                    principal_type="user",
                    principal_id="principal-a",
                    permission_level=PermissionLevel.VIEW,
                )
            ]
        ),
    )
    postgres_store.save_sync_job(
        connector_a,
        unique_tenant,
        SyncJob(
            job_id=job_id,
            connector_id=connector_a,
            tenant_id=unique_tenant,
            job_type=SyncJobType.FULL,
        ),
    )

    attempts = [
        lambda: postgres_store.save_connector_sources(
            connector_b,
            unique_tenant,
            ConnectorSourceBatch(
                sources=[
                    ConnectorSourceRecord(
                        source_id=source_a,
                        connector_id=connector_b,
                        tenant_id=unique_tenant,
                        remote_source_id="remote-reparented",
                        source_type="repository",
                        display_name="Reparented source",
                    )
                ]
            ),
        ),
        lambda: postgres_store.save_principal_mappings(
            connector_b,
            unique_tenant,
            PrincipalMappingBatch(
                mappings=[
                    PrincipalMapping(
                        mapping_id=mapping_id,
                        connector_id=connector_b,
                        tenant_id=unique_tenant,
                        principal_type="user",
                        local_principal_id="local-b",
                        remote_principal_id="remote-b",
                    )
                ]
            ),
        ),
        lambda: postgres_store.save_source_permission_grants(
            connector_b,
            unique_tenant,
            SourcePermissionBatch(
                grants=[
                    SourcePermissionGrant(
                        grant_id=grant_id,
                        source_id=source_b,
                        connector_id=connector_b,
                        tenant_id=unique_tenant,
                        principal_type="user",
                        principal_id="principal-b",
                        permission_level=PermissionLevel.OWNER,
                    )
                ]
            ),
        ),
        lambda: postgres_store.save_sync_job(
            connector_b,
            unique_tenant,
            SyncJob(
                job_id=job_id,
                connector_id=connector_b,
                tenant_id=unique_tenant,
                job_type=SyncJobType.FULL,
            ),
        ),
    ]
    for attempt in attempts:
        with pytest.raises(TenantOwnershipConflictError, match="resource write conflict"):
            attempt()

    expected_parents = (
        ("connector_sources", "source_id", source_a, "connector_id", connector_a),
        ("principal_mappings", "mapping_id", mapping_id, "connector_id", connector_a),
        ("source_permission_grants", "grant_id", grant_id, "source_id", source_a),
        ("sync_jobs", "job_id", job_id, "connector_id", connector_a),
    )
    for table, id_column, record_id, parent_column, expected_parent in expected_parents:
        row = postgres_store.conn.execute(
            f"SELECT {parent_column} FROM {table} WHERE {id_column} = ?",
            (record_id,),
        ).fetchone()
        assert row is not None and row[parent_column] == expected_parent


def test_shared_postgres_connection_serializes_transaction_outcomes(
    postgres_store: PostgresStore,
) -> None:
    rolled_back_id = f"policy-rollback-{uuid.uuid4().hex}"
    committed_id = f"policy-commit-{uuid.uuid4().hex}"
    first_inserted = threading.Event()
    second_attempting = threading.Event()
    failures: list[BaseException] = []

    def rollback_writer() -> None:
        try:
            with postgres_store.conn:
                postgres_store.conn.execute(
                    """
                    INSERT INTO retention_policies
                        (policy_id, tenant_id, kind, max_age_days, action, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (rolled_back_id, "tenant-transaction-a", None, 30, "delete_soft", "2026-07-13T00:00:00Z"),
                )
                first_inserted.set()
                assert second_attempting.wait(timeout=10)
                raise RuntimeError("injected rollback")
        except RuntimeError as exc:
            if str(exc) != "injected rollback":
                failures.append(exc)

    def commit_writer() -> None:
        try:
            assert first_inserted.wait(timeout=10)
            second_attempting.set()
            with postgres_store.conn:
                postgres_store.conn.execute(
                    """
                    INSERT INTO retention_policies
                        (policy_id, tenant_id, kind, max_age_days, action, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (committed_id, "tenant-transaction-b", None, 30, "delete_soft", "2026-07-13T00:00:00Z"),
                )
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    threads = [
        threading.Thread(target=rollback_writer, daemon=True),
        threading.Thread(target=commit_writer, daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    rows = postgres_store.conn.execute(
        "SELECT policy_id FROM retention_policies WHERE policy_id IN (?, ?)",
        (rolled_back_id, committed_id),
    ).fetchall()
    assert [row["policy_id"] for row in rows] == [committed_id]


def test_repository_first_sync_serializes_across_independent_connections() -> None:
    url = _database_url()
    if not url:
        pytest.skip("PROVENA_DATABASE_URL not set")
    stores = [PostgresStore(url), PostgresStore(url)]
    repository_id = f"repo-pg-race-{uuid.uuid4().hex}"
    tenant_id = f"tenant-pg-race-{uuid.uuid4().hex}"
    event = {
        "schema_version": 1,
        "id": "event-pg-concurrent-001",
        "kind": "invariant",
        "subject_type": "repo",
        "title": "Concurrent PostgreSQL first sync",
        "body": "Independent database sessions converge on one coherent projection.",
        "structured_data": {"backend": "postgres"},
        "status": "active",
        "applies_to": [],
        "sources": [],
        "provenance": {"actor": "postgres-test", "method": "observed"},
        "authority": "tool",
        "confidence": 1,
        "importance": 1,
        "sensitivity": "internal",
        "created_at": "2026-07-13T10:00:00.000Z",
        "updated_at": "2026-07-13T10:00:00.000Z",
        "supersedes": [],
        "tags": ["postgres"],
        "triggers": [],
    }
    ledger = json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    raw = ledger.encode("utf-8")
    payload = RepositoryMemorySyncRequest(
        schema_version=1,
        scope={"tenant_id": tenant_id, "project_id": "project-pg-race"},
        ledger_path=".provena/memory/events.jsonl",
        memory_fingerprint=hashlib.sha256(raw).hexdigest(),
        ledger_bytes=len(raw),
        ledger=ledger,
    )
    access = AccessContext(tenant_id=tenant_id, role="admin", principal_id="race-admin")
    barrier = threading.Barrier(2)
    results = []
    failures: list[BaseException] = []
    result_lock = threading.Lock()

    def sync(store: PostgresStore) -> None:
        try:
            barrier.wait(timeout=10)
            result = store.sync_repository_memory_events(repository_id, payload, access=access)
            with result_lock:
                results.append(result)
        except BaseException as exc:  # pragma: no cover - surfaced below
            with result_lock:
                failures.append(exc)

    threads = [threading.Thread(target=sync, args=(store,), daemon=True) for store in stores]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert all(not thread.is_alive() for thread in threads)
        assert failures == []
        assert sorted(result.created_memories for result in results) == [0, 1]
        assert sum(result.no_op for result in results) == 1
        checkpoint_count = stores[0].conn.execute(
            """
            SELECT COUNT(*) AS count FROM repo_memory_sync_state
            WHERE tenant_id = ? AND project_id = ? AND repository_id = ?
            """,
            (tenant_id, "project-pg-race", repository_id),
        ).fetchone()
        projection_count = stores[0].conn.execute(
            """
            SELECT COUNT(*) AS count FROM repo_memory_event_projections
            WHERE tenant_id = ? AND project_id = ? AND repository_id = ?
            """,
            (tenant_id, "project-pg-race", repository_id),
        ).fetchone()
        assert checkpoint_count is not None and int(checkpoint_count["count"]) == 1
        assert projection_count is not None and int(projection_count["count"]) == 1
    finally:
        for store in stores:
            store.close()


def test_vector_fallback_uses_valid_jsonb_predicate(
    postgres_store: PostgresStore,
    unique_tenant: str,
) -> None:
    access = AccessContext(tenant_id=unique_tenant, role="admin", principal_id="vector-admin")
    scope = ScopeEnvelope(tenant_id=unique_tenant, workspace_id="ws-vector")
    embedded = postgres_store.create_memory(
        MemoryCreate(
            kind=MemoryKind.FACT,
            scope=scope,
            content="PostgreSQL JSONB vector candidate",
            embedding=[1.0, 0.0, 0.0],
        ),
        access=access,
    )
    postgres_store.create_memory(
        MemoryCreate(
            kind=MemoryKind.FACT,
            scope=scope,
            content="Null vector candidate",
        ),
        access=access,
    )
    response = postgres_store.search_memories(
        SearchRequest(
            query="",
            query_embedding=[1.0, 0.0, 0.0],
            scope=scope,
            limit=5,
        ),
        access=access,
    )
    assert any(item.memory.memory_id == embedded.memory.memory_id for item in response.results)


def test_tenant_isolation(postgres_store: PostgresStore, unique_tenant: str) -> None:
    other_tenant = f"{unique_tenant}-other"
    access_a = AccessContext(tenant_id=unique_tenant, role="admin", principal_id="a")
    access_b = AccessContext(tenant_id=other_tenant, role="admin", principal_id="b")
    created = postgres_store.create_memory(
        MemoryCreate(
            kind=MemoryKind.FACT,
            scope=ScopeEnvelope(tenant_id=unique_tenant),
            content="Tenant A secret",
        ),
        access=access_a,
    )
    assert postgres_store.get_memory(created.memory.memory_id, access=access_b) is None


def test_repository_ledger_sync_is_idempotent_on_postgres(
    postgres_store: PostgresStore,
    unique_tenant: str,
) -> None:
    repository_id = f"repo-pg-{uuid.uuid4().hex[:12]}"
    event = {
        "schema_version": 1,
        "id": "event-pg-001",
        "kind": "invariant",
        "subject_type": "architecture",
        "title": "Postgres canonical event",
        "body": "The PostgreSQL projection preserves the canonical repository event.",
        "structured_data": {"backend": "postgres"},
        "status": "active",
        "applies_to": [],
        "sources": [],
        "provenance": {"actor": "postgres-test", "method": "observed"},
        "authority": "tool",
        "confidence": 1,
        "importance": 0.8,
        "sensitivity": "internal",
        "created_at": "2026-07-13T10:00:00.000Z",
        "updated_at": "2026-07-13T10:00:00.000Z",
        "supersedes": [],
        "tags": ["postgres"],
        "triggers": [],
    }
    ledger = json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    raw = ledger.encode("utf-8")
    payload = RepositoryMemorySyncRequest(
        schema_version=1,
        scope={"tenant_id": unique_tenant, "project_id": "project-pg-sync"},
        ledger_path=".provena/memory/events.jsonl",
        memory_fingerprint=hashlib.sha256(raw).hexdigest(),
        ledger_bytes=len(raw),
        ledger=ledger,
    )
    access = AccessContext(tenant_id=unique_tenant, role="admin", principal_id="admin-1")

    first = postgres_store.sync_repository_memory_events(repository_id, payload, access=access)
    assert first.created_memories == 1
    assert first.checkpoint_updated is True
    second = postgres_store.sync_repository_memory_events(repository_id, payload, access=access)
    assert second.no_op is True
    assert second.created_memories == 0
    assert second.unchanged_memories == 1


@pytest.mark.parametrize("reserved_key", ["procedure", "procedureOutcome"])
def test_repository_procedures_stay_out_of_postgres_generic_retrieval(
    postgres_store: PostgresStore, unique_tenant: str, reserved_key: str,
) -> None:
    repository_id = f"repo-procedural-gate-{uuid.uuid4().hex}"
    scope = ScopeEnvelope(tenant_id=unique_tenant, project_id="project-procedural-gate")
    access = AccessContext(tenant_id=unique_tenant, role="admin", principal_id="admin-1")
    event = {
        "schema_version": 1, "id": "procedure-reserved-event",
        "kind": "workflow" if reserved_key == "procedure" else "fact", "subject_type": "task",
        "title": "Reserved validation session evidence", "body": "PROCEDURE_ONLY_GUIDANCE validation session",
        "structured_data": {reserved_key: {"state": "candidate", "steps": [{"tool": "shell", "args": {"unicode": "😀", "epsilon": 1e-7}}]}},
        "status": "active", "applies_to": [], "sources": [],
        "provenance": {"actor": "procedure-test", "method": "explicit"}, "authority": "agent",
        "confidence": 1, "importance": 1, "sensitivity": "internal",
        "created_at": "2026-07-13T10:00:00.000Z", "updated_at": "2026-07-13T10:00:00.000Z",
        "supersedes": [], "tags": [], "triggers": [],
    }
    raw = rfc8785.dumps(event) + b"\n"
    payload = RepositoryMemorySyncRequest(
        scope=scope, ledger_path=".provena/memory/events.jsonl", ledger=raw.decode("utf-8"),
        memory_fingerprint=hashlib.sha256(raw).hexdigest(), ledger_bytes=len(raw),
    )
    postgres_store.sync_repository_memory_events(repository_id, payload, access=access)
    ordinary = postgres_store.create_memory(MemoryCreate(
        kind=MemoryKind.WORKFLOW, scope=scope, content="Ordinary validation session guidance.",
    ), access=access).memory
    for query in ("validation session", ""):
        searched = postgres_store.search_memories(SearchRequest(
            query=query, scope=scope, include_deleted=True, include_relations=True,
        ), access=access)
        assert [result.memory.memory_id for result in searched.results] == [ordinary.memory_id]
        assert "PROCEDURE_ONLY_GUIDANCE" not in searched.model_dump_json()
    context = postgres_store.agent_context(AgentContextRequest(query="validation session", scope=scope), access=access)
    assert [item.memory_id for item in context.memories] == [ordinary.memory_id]
    assert "PROCEDURE_ONLY_GUIDANCE" not in context.model_dump_json()
    projected_id = postgres_store._repository_memory_id(scope, repository_id, event["id"])
    preserved = postgres_store.get_memory(projected_id, access=access)
    assert preserved is not None and preserved.metadata["provena_event"] == event
    assert postgres_store.sync_repository_memory_events(repository_id, payload, access=access).no_op is True


def test_repository_ledger_rtbf_tombstone_prevents_postgres_replay(
    postgres_store: PostgresStore,
    unique_tenant: str,
) -> None:
    repository_id = f"repo-pg-rtbf-{uuid.uuid4().hex[:12]}"
    event = {
        "schema_version": 1,
        "id": "event-pg-rtbf-001",
        "kind": "fact",
        "subject_type": "repo",
        "title": "Postgres RTBF projection",
        "body": "A governed deletion must survive canonical ledger replay.",
        "structured_data": {},
        "status": "active",
        "applies_to": [],
        "sources": [],
        "provenance": {"actor": "postgres-test", "method": "observed"},
        "authority": "tool",
        "confidence": 1,
        "importance": 0.8,
        "sensitivity": "internal",
        "created_at": "2026-07-13T10:00:00.000Z",
        "updated_at": "2026-07-13T10:00:00.000Z",
        "supersedes": [],
        "tags": [],
        "triggers": [],
    }
    ledger = json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    raw = ledger.encode("utf-8")
    payload = RepositoryMemorySyncRequest(
        schema_version=1,
        scope={"tenant_id": unique_tenant, "project_id": "project-pg-rtbf"},
        ledger_path=".provena/memory/events.jsonl",
        memory_fingerprint=hashlib.sha256(raw).hexdigest(),
        ledger_bytes=len(raw),
        ledger=ledger,
    )
    access = AccessContext(tenant_id=unique_tenant, role="admin", principal_id="admin-rtbf")
    created = postgres_store.sync_repository_memory_events(repository_id, payload, access=access)
    assert created.created_memories == 1

    with postgres_store.conn:
        postgres_store.conn.execute(
            "DELETE FROM repo_memory_event_projections WHERE repository_id = ?",
            (repository_id,),
        )

    forgotten = postgres_store.rtbf(RTBFRequest(tenant_id=unique_tenant))
    assert forgotten.deleted_memories == 1
    replay = postgres_store.sync_repository_memory_events(repository_id, payload, access=access)
    assert replay.no_op is True
    assert replay.suppressed_events == 1
    assert replay.created_memories == 0


def test_tenant_owned_ids_cannot_be_reassigned(
    postgres_store: PostgresStore,
    unique_tenant: str,
) -> None:
    other_tenant = f"{unique_tenant}-other"
    suffix = uuid.uuid4().hex
    policy_id = f"policy-{suffix}"
    hold_id = f"hold-{suffix}"
    entity_id = f"entity-{suffix}"

    postgres_store.save_retention_policy(
        RetentionPolicy(
            policy_id=policy_id,
            tenant_id=other_tenant,
            max_age_days=30,
        )
    )
    postgres_store.place_legal_hold(
        LegalHold(
            hold_id=hold_id,
            tenant_id=other_tenant,
            reason="foreign evidence",
        )
    )
    postgres_store.upsert_entity(
        entity_id=entity_id,
        canonical_name="Foreign entity",
        tenant_id=other_tenant,
    )

    with pytest.raises(TenantOwnershipError):
        postgres_store.save_retention_policy(
            RetentionPolicy(
                policy_id=policy_id,
                tenant_id=unique_tenant,
                max_age_days=1,
            )
        )
    with pytest.raises(TenantOwnershipError):
        postgres_store.place_legal_hold(
            LegalHold(
                hold_id=hold_id,
                tenant_id=unique_tenant,
                reason="stolen evidence",
            )
        )
    with pytest.raises(TenantOwnershipError):
        postgres_store.upsert_entity(
            entity_id=entity_id,
            canonical_name="Stolen entity",
            tenant_id=unique_tenant,
        )

    for table, id_column, identifier in (
        ("retention_policies", "policy_id", policy_id),
        ("legal_holds", "hold_id", hold_id),
        ("entity_registry", "entity_id", entity_id),
    ):
        row = postgres_store.conn.execute(
            f"SELECT tenant_id FROM {table} WHERE {id_column} = ?",
            (identifier,),
        ).fetchone()
        assert row is not None
        assert row["tenant_id"] == other_tenant


def test_integration_children_require_tenant_owned_parents(
    postgres_store: PostgresStore,
    unique_tenant: str,
) -> None:
    other_tenant = f"{unique_tenant}-other"
    suffix = uuid.uuid4().hex
    connector_a = ConnectorConfig(
        connector_id=f"connector-a-{suffix}",
        tenant_id=unique_tenant,
        provider=ConnectorProvider.CUSTOM,
        display_name="Tenant A connector",
        auth_type=ConnectorAuthType.API_KEY,
    )
    connector_b = connector_a.model_copy(
        update={
            "connector_id": f"connector-b-{suffix}",
            "tenant_id": other_tenant,
            "display_name": "Tenant B connector",
        }
    )
    postgres_store.save_connector(connector_a)
    postgres_store.save_connector(connector_b)

    attempted_source = ConnectorSourceRecord(
        source_id=f"attacker-source-{suffix}",
        connector_id=connector_a.connector_id,
        tenant_id=other_tenant,
        remote_source_id=f"reserved-remote-{suffix}",
        source_type="repository",
        display_name="Foreign source",
    )
    with pytest.raises(TenantParentNotFoundError):
        postgres_store.save_connector_sources(
            connector_a.connector_id,
            other_tenant,
            ConnectorSourceBatch(sources=[attempted_source]),
        )

    owner_source = attempted_source.model_copy(
        update={
            "source_id": f"owner-source-{suffix}",
            "tenant_id": unique_tenant,
            "display_name": "Owner source",
        }
    )
    saved_sources = postgres_store.save_connector_sources(
        connector_a.connector_id,
        unique_tenant,
        ConnectorSourceBatch(sources=[owner_source]),
    )
    assert [source.source_id for source in saved_sources] == [owner_source.source_id]
    assert postgres_store.conn.execute(
        "SELECT source_id FROM connector_sources WHERE source_id = ?",
        (attempted_source.source_id,),
    ).fetchone() is None

    with pytest.raises(TenantParentNotFoundError):
        postgres_store.save_principal_mappings(
            connector_a.connector_id,
            other_tenant,
            PrincipalMappingBatch(
                mappings=[
                    PrincipalMapping(
                        mapping_id=f"foreign-mapping-{suffix}",
                        connector_id=connector_a.connector_id,
                        tenant_id=other_tenant,
                        principal_type="user",
                        local_principal_id="local-user",
                        remote_principal_id="remote-user",
                    )
                ]
            ),
        )

    with pytest.raises(TenantParentNotFoundError):
        postgres_store.save_sync_job(
            connector_a.connector_id,
            other_tenant,
            SyncJob(
                job_id=f"foreign-job-{suffix}",
                connector_id=connector_a.connector_id,
                tenant_id=other_tenant,
                job_type=SyncJobType.FULL,
            ),
        )

    with pytest.raises(TenantParentNotFoundError):
        postgres_store.save_source_permission_grants(
            connector_b.connector_id,
            other_tenant,
            SourcePermissionBatch(
                grants=[
                    SourcePermissionGrant(
                        grant_id=f"foreign-grant-{suffix}",
                        source_id=owner_source.source_id,
                        connector_id=connector_b.connector_id,
                        tenant_id=other_tenant,
                        principal_type="user",
                        principal_id="tenant-b-user",
                        permission_level=PermissionLevel.VIEW,
                    )
                ]
            ),
        )


def test_deterministic_create_race_and_tombstone_retry(
    postgres_store: PostgresStore,
    unique_tenant: str,
) -> None:
    second_store = PostgresStore(_database_url() or "")
    access = AccessContext(tenant_id=unique_tenant, role="admin", principal_id="admin-1")
    payload = MemoryCreate(
        memory_id=f"context-{uuid.uuid4().hex}",
        kind=MemoryKind.FACT,
        scope=ScopeEnvelope(tenant_id=unique_tenant, workspace_id="ws-race"),
        content="Concurrent deterministic context",
    )
    barrier = Barrier(2)

    def create(store: PostgresStore):
        barrier.wait()
        return store.create_memory(payload, access=access)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, (postgres_store, second_store)))
        assert sum(result.created for result in results) == 1
        assert len({result.memory.fingerprint for result in results}) == 1
        assert _creation_counts(postgres_store, payload.memory_id or "") == (1, 1, 1)

        deleted = postgres_store.delete_memory(payload.memory_id or "", access=access)
        assert deleted.deleted is True
        retry = second_store.create_memory(payload, access=access)
        assert retry.created is False
        assert retry.memory.status is MemoryStatus.DELETED

        with pytest.raises(ValueError, match="different create request"):
            second_store.create_memory(
                payload.model_copy(update={"content": "Conflicting deterministic context"}),
                access=access,
            )
    finally:
        second_store.close()


def test_legal_hold_delete_and_scope_erase_fencing(
    postgres_store: PostgresStore,
    unique_tenant: str,
) -> None:
    access = AccessContext(tenant_id=unique_tenant, role="admin", principal_id="admin-1")
    held_id = f"held-{uuid.uuid4().hex}"
    scope = ScopeEnvelope(tenant_id=unique_tenant, workspace_id="ws-held-delete")
    postgres_store.create_memory(
        MemoryCreate(
            memory_id=held_id,
            kind=MemoryKind.FACT,
            scope=scope,
            content="Held evidence",
        ),
        access=access,
    )
    postgres_store.place_legal_hold(
        LegalHold(
            hold_id=f"hold-{uuid.uuid4().hex}",
            tenant_id=unique_tenant,
            memory_ids=[held_id],
            reason="preserve evidence",
        )
    )

    assert postgres_store.delete_memory(held_id, access=access).deleted is True
    held = postgres_store.get_memory(held_id, access=access)
    assert held is not None
    assert held.held is True
    assert held.status is MemoryStatus.HELD
    with pytest.raises(ValueError, match="under legal hold"):
        postgres_store.delete_memory(held_id, hard_delete=True, access=access)

    hold_row = postgres_store.conn.execute(
        "SELECT hold_id FROM legal_holds WHERE tenant_id = ? AND memory_ids_json LIKE ?",
        (unique_tenant, f"%{held_id}%"),
    ).fetchone()
    assert hold_row is not None
    assert postgres_store.release_legal_hold(hold_row["hold_id"], unique_tenant) is True
    released = postgres_store.get_memory(held_id, access=access)
    assert released is not None
    assert released.held is False
    assert released.status is MemoryStatus.DELETED

    erase_scope = ScopeEnvelope(tenant_id=unique_tenant, workspace_id="ws-held-erase")
    erase_held_id = f"erase-held-{uuid.uuid4().hex}"
    erase_unheld_id = f"erase-unheld-{uuid.uuid4().hex}"
    for memory_id in (erase_held_id, erase_unheld_id):
        postgres_store.create_memory(
            MemoryCreate(
                memory_id=memory_id,
                kind=MemoryKind.FACT,
                scope=erase_scope,
                content=f"Evidence {memory_id}",
            ),
            access=access,
        )
    postgres_store.place_legal_hold(
        LegalHold(
            hold_id=f"hold-erase-{uuid.uuid4().hex}",
            tenant_id=unique_tenant,
            memory_ids=[erase_held_id],
            reason="scope erase fence",
        )
    )
    erased = postgres_store.erase_scope(
        EraseRequest(tenant_id=unique_tenant, workspace_id="ws-held-erase"),
        access=access,
    )
    assert erased.deleted_memories == 1
    assert postgres_store.get_memory(erase_held_id, access=access) is not None
    assert postgres_store.get_memory(erase_unheld_id, access=access) is None


def test_committed_hold_wins_against_concurrent_hard_delete(
    postgres_store: PostgresStore,
    unique_tenant: str,
) -> None:
    access = AccessContext(tenant_id=unique_tenant, role="admin", principal_id="admin-1")
    memory_id = f"held-race-{uuid.uuid4().hex}"
    postgres_store.create_memory(
        MemoryCreate(
            memory_id=memory_id,
            kind=MemoryKind.FACT,
            scope=ScopeEnvelope(tenant_id=unique_tenant, workspace_id="ws-hold-race"),
            content="Concurrent legal-hold evidence",
        ),
        access=access,
    )
    hold_store = PostgresStore(_database_url() or "")
    hold_has_lock = Event()
    release_hold = Event()
    delete_started = Event()
    original_refresh = PostgresStore._refresh_hold_state

    def gated_refresh(store: PostgresStore, memory_ids: list[str]) -> None:
        if store is hold_store:
            hold_has_lock.set()
            if not release_hold.wait(timeout=10):
                raise TimeoutError("test did not release the legal-hold transaction")
        original_refresh(store, memory_ids)

    def hard_delete() -> None:
        delete_started.set()
        postgres_store.delete_memory(memory_id, hard_delete=True, access=access)

    try:
        with patch.object(PostgresStore, "_refresh_hold_state", new=gated_refresh):
            with ThreadPoolExecutor(max_workers=2) as executor:
                hold_future = executor.submit(
                    hold_store.place_legal_hold,
                    LegalHold(
                        hold_id=f"hold-race-{uuid.uuid4().hex}",
                        tenant_id=unique_tenant,
                        memory_ids=[memory_id],
                        reason="concurrent deletion fence",
                    ),
                )
                assert hold_has_lock.wait(timeout=10)
                delete_future = executor.submit(hard_delete)
                assert delete_started.wait(timeout=10)
                release_hold.set()
                hold_future.result(timeout=10)
                with pytest.raises(ValueError, match="under legal hold"):
                    delete_future.result(timeout=10)
    finally:
        release_hold.set()
        hold_store.close()

    held = postgres_store.get_memory(memory_id, access=access)
    assert held is not None
    assert held.held is True


def test_supersession_requires_target_write_and_rolls_back_rejected_side_effects(
    postgres_store: PostgresStore,
    unique_tenant: str,
) -> None:
    scope = ScopeEnvelope(tenant_id=unique_tenant, workspace_id="ws-authz")
    access_a = AccessContext(tenant_id=unique_tenant, role="editor", principal_id="principal-a")
    access_b = AccessContext(tenant_id=unique_tenant, role="editor", principal_id="principal-b")
    target_id = f"target-{uuid.uuid4().hex}"
    postgres_store.create_memory(
        MemoryCreate(
            memory_id=target_id,
            kind=MemoryKind.FACT,
            scope=scope,
            content="Principal B evidence",
            acl=[
                ACLEntry(
                    principal_id="principal-b",
                    principal_type="user",
                    permissions=list(ACLPermission),
                ),
                ACLEntry(
                    principal_id="principal-a",
                    principal_type="user",
                    permissions=[ACLPermission.READ],
                ),
            ],
        ),
        access=access_b,
    )
    relation_count = _count(
        postgres_store,
        "memory_relations",
        "tenant_id = ?",
        (unique_tenant,),
    )
    target_counts = _creation_counts(postgres_store, target_id)

    replacement_id = f"replacement-{uuid.uuid4().hex}"
    with pytest.raises(ValueError, match="write access required for supersession target"):
        postgres_store.create_memory(
            MemoryCreate(
                memory_id=replacement_id,
                kind=MemoryKind.FACT,
                scope=scope,
                content="Unauthorized replacement",
                supersedes_memory_id=target_id,
            ),
            access=access_a,
        )
    assert _creation_counts(postgres_store, replacement_id) == (0, 0, 0)

    source_id = f"source-{uuid.uuid4().hex}"
    postgres_store.create_memory(
        MemoryCreate(
            memory_id=source_id,
            kind=MemoryKind.FACT,
            scope=scope,
            content="Original source",
        ),
        access=access_a,
    )
    source_audits = _count(postgres_store, "audit_log", "memory_id = ?", (source_id,))
    source_history = _count(postgres_store, "memory_history", "memory_id = ?", (source_id,))
    with pytest.raises(ValueError, match="write access required for supersession target"):
        postgres_store.update_memory(
            source_id,
            MemoryUpdate(content="must roll back", supersedes_memory_id=target_id),
            access=access_a,
        )
    source = postgres_store.get_memory(source_id, access=access_a)
    target = postgres_store.get_memory(target_id, access=access_b)
    assert source is not None and source.content == "Original source"
    assert target is not None and target.status is MemoryStatus.ACTIVE
    assert _count(postgres_store, "audit_log", "memory_id = ?", (source_id,)) == source_audits
    assert _count(postgres_store, "memory_history", "memory_id = ?", (source_id,)) == source_history
    assert _creation_counts(postgres_store, target_id) == target_counts
    assert _count(postgres_store, "memory_relations", "tenant_id = ?", (unique_tenant,)) == relation_count


def test_supersession_scope_self_and_state_fences(
    postgres_store: PostgresStore,
    unique_tenant: str,
) -> None:
    access = AccessContext(tenant_id=unique_tenant, role="admin", principal_id="admin-1")
    scope = ScopeEnvelope(tenant_id=unique_tenant, workspace_id="ws-source")
    self_id = f"self-{uuid.uuid4().hex}"
    with pytest.raises(ValueError, match="cannot supersede itself"):
        postgres_store.create_memory(
            MemoryCreate(
                memory_id=self_id,
                kind=MemoryKind.FACT,
                scope=scope,
                content="Self edge",
                supersedes_memory_id=self_id,
            ),
            access=access,
        )
    assert _creation_counts(postgres_store, self_id) == (0, 0, 0)

    cross_target_id = f"cross-{uuid.uuid4().hex}"
    postgres_store.create_memory(
        MemoryCreate(
            memory_id=cross_target_id,
            kind=MemoryKind.FACT,
            scope=scope.model_copy(update={"workspace_id": "ws-other"}),
            content="Cross-scope target",
        ),
        access=access,
    )
    cross_replacement_id = f"cross-replacement-{uuid.uuid4().hex}"
    with pytest.raises(ValueError, match="same scope"):
        postgres_store.create_memory(
            MemoryCreate(
                memory_id=cross_replacement_id,
                kind=MemoryKind.FACT,
                scope=scope,
                content="Cross-scope replacement",
                supersedes_memory_id=cross_target_id,
            ),
            access=access,
        )
    assert _creation_counts(postgres_store, cross_replacement_id) == (0, 0, 0)

    deleted_target_id = f"deleted-target-{uuid.uuid4().hex}"
    postgres_store.create_memory(
        MemoryCreate(
            memory_id=deleted_target_id,
            kind=MemoryKind.FACT,
            scope=scope,
            content="Deleted target",
        ),
        access=access,
    )
    postgres_store.delete_memory(deleted_target_id, access=access)
    replacement_id = f"deleted-replacement-{uuid.uuid4().hex}"
    with pytest.raises(ValueError, match="target must be active"):
        postgres_store.create_memory(
            MemoryCreate(
                memory_id=replacement_id,
                kind=MemoryKind.FACT,
                scope=scope,
                content="Must not persist",
                supersedes_memory_id=deleted_target_id,
            ),
            access=access,
        )
    assert _creation_counts(postgres_store, replacement_id) == (0, 0, 0)

    source_id = f"deleted-source-{uuid.uuid4().hex}"
    active_target_id = f"active-target-{uuid.uuid4().hex}"
    for memory_id in (source_id, active_target_id):
        postgres_store.create_memory(
            MemoryCreate(
                memory_id=memory_id,
                kind=MemoryKind.FACT,
                scope=scope,
                content=f"Evidence {memory_id}",
            ),
            access=access,
        )
    postgres_store.delete_memory(source_id, access=access)
    with pytest.raises(ValueError, match="source must be active"):
        postgres_store.update_memory(
            source_id,
            MemoryUpdate(content="must roll back", supersedes_memory_id=active_target_id),
            access=access,
        )
    source = postgres_store.get_memory(source_id, access=access)
    target = postgres_store.get_memory(active_target_id, access=access)
    assert source is not None and source.status is MemoryStatus.DELETED
    assert source.content != "must roll back"
    assert target is not None and target.status is MemoryStatus.ACTIVE

    held_target_id = f"held-target-{uuid.uuid4().hex}"
    postgres_store.create_memory(
        MemoryCreate(
            memory_id=held_target_id,
            kind=MemoryKind.FACT,
            scope=scope,
            content="Held target",
        ),
        access=access,
    )
    postgres_store.place_legal_hold(
        LegalHold(
            hold_id=f"hold-target-{uuid.uuid4().hex}",
            tenant_id=unique_tenant,
            memory_ids=[held_target_id],
            reason="freeze target",
        )
    )
    held_replacement_id = f"held-replacement-{uuid.uuid4().hex}"
    with pytest.raises(ValueError, match="target must be active"):
        postgres_store.create_memory(
            MemoryCreate(
                memory_id=held_replacement_id,
                kind=MemoryKind.FACT,
                scope=scope,
                content="Must not replace held target",
                supersedes_memory_id=held_target_id,
            ),
            access=access,
        )
    assert _creation_counts(postgres_store, held_replacement_id) == (0, 0, 0)

    held_source_id = f"held-source-{uuid.uuid4().hex}"
    held_source_target_id = f"held-source-target-{uuid.uuid4().hex}"
    for memory_id in (held_source_id, held_source_target_id):
        postgres_store.create_memory(
            MemoryCreate(
                memory_id=memory_id,
                kind=MemoryKind.FACT,
                scope=scope,
                content=f"Evidence {memory_id}",
            ),
            access=access,
        )
    postgres_store.place_legal_hold(
        LegalHold(
            hold_id=f"hold-source-{uuid.uuid4().hex}",
            tenant_id=unique_tenant,
            memory_ids=[held_source_id],
            reason="freeze source",
        )
    )
    with pytest.raises(ValueError, match="source must be active"):
        postgres_store.update_memory(
            held_source_id,
            MemoryUpdate(supersedes_memory_id=held_source_target_id),
            access=access,
        )
    held_source_target = postgres_store.get_memory(held_source_target_id, access=access)
    assert held_source_target is not None
    assert held_source_target.status is MemoryStatus.ACTIVE

    valid_create_target_id = f"valid-create-target-{uuid.uuid4().hex}"
    valid_replacement_id = f"valid-replacement-{uuid.uuid4().hex}"
    postgres_store.create_memory(
        MemoryCreate(
            memory_id=valid_create_target_id,
            kind=MemoryKind.FACT,
            scope=scope,
            content="Valid create target",
        ),
        access=access,
    )
    valid_replacement = postgres_store.create_memory(
        MemoryCreate(
            memory_id=valid_replacement_id,
            kind=MemoryKind.FACT,
            scope=scope,
            content="Valid replacement",
            supersedes_memory_id=valid_create_target_id,
        ),
        access=access,
    )
    valid_create_target = postgres_store.get_memory(valid_create_target_id, access=access)
    assert valid_replacement.created is True
    assert valid_create_target is not None
    assert valid_create_target.status is MemoryStatus.SUPERSEDED

    valid_update_source_id = f"valid-update-source-{uuid.uuid4().hex}"
    valid_update_target_id = f"valid-update-target-{uuid.uuid4().hex}"
    for memory_id in (valid_update_source_id, valid_update_target_id):
        postgres_store.create_memory(
            MemoryCreate(
                memory_id=memory_id,
                kind=MemoryKind.FACT,
                scope=scope,
                content=f"Evidence {memory_id}",
            ),
            access=access,
        )
    valid_update = postgres_store.update_memory(
        valid_update_source_id,
        MemoryUpdate(
            content="Authorized replacement",
            supersedes_memory_id=valid_update_target_id,
        ),
        access=access,
    )
    valid_update_target = postgres_store.get_memory(valid_update_target_id, access=access)
    assert valid_update.memory.content == "Authorized replacement"
    assert valid_update_target is not None
    assert valid_update_target.status is MemoryStatus.SUPERSEDED


def test_null_digest_and_injected_create_failures_are_atomic(
    postgres_store: PostgresStore,
    unique_tenant: str,
) -> None:
    access = AccessContext(tenant_id=unique_tenant, role="admin", principal_id="admin-1")
    scope = ScopeEnvelope(tenant_id=unique_tenant, workspace_id="ws-atomic")
    unverifiable_id = f"unverifiable-{uuid.uuid4().hex}"
    unverifiable = MemoryCreate(
        memory_id=unverifiable_id,
        kind=MemoryKind.FACT,
        scope=scope,
        content="Legacy deterministic row",
    )
    postgres_store.create_memory(unverifiable, access=access)
    with postgres_store.conn:
        postgres_store.conn.execute(
            "UPDATE memories SET create_request_digest = NULL WHERE memory_id = ?",
            (unverifiable_id,),
        )
    before = _creation_counts(postgres_store, unverifiable_id)
    with pytest.raises(ValueError, match="without a verifiable create request digest"):
        postgres_store.create_memory(unverifiable, access=access)
    with pytest.raises(ValueError, match="without a verifiable create request digest"):
        postgres_store.create_memory(
            unverifiable.model_copy(update={"content": "Conflicting retry"}),
            access=access,
        )
    assert _creation_counts(postgres_store, unverifiable_id) == before

    for method_name in ("_insert_audit", "_insert_history"):
        memory_id = f"failure-{method_name.removeprefix('_insert_')}-{uuid.uuid4().hex}"
        payload = MemoryCreate(
            memory_id=memory_id,
            kind=MemoryKind.FACT,
            scope=scope,
            content=f"Failure injection {method_name}",
        )
        with patch.object(
            postgres_store,
            method_name,
            side_effect=RuntimeError(f"injected {method_name} failure"),
        ):
            with pytest.raises(RuntimeError, match="injected"):
                postgres_store.create_memory(payload, access=access)
        assert _creation_counts(postgres_store, memory_id) == (0, 0, 0)
        assert postgres_store.create_memory(payload, access=access).created is True
        assert _creation_counts(postgres_store, memory_id) == (1, 1, 1)


@pytest.mark.skipif(_database_url() is None, reason="PROVENA_DATABASE_URL not set")
def test_factory_selects_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings, get_settings
    from app.store_factory import create_store

    monkeypatch.setenv("PROVENA_DATABASE_URL", _database_url() or "")
    monkeypatch.delenv("PROVENA_DB_PATH", raising=False)
    get_settings.cache_clear()
    settings = Settings()
    store = create_store(settings)
    try:
        assert isinstance(store, PostgresStore)
    finally:
        store.close()
        get_settings.cache_clear()
