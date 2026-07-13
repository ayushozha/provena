"""Integration tests for PostgresStore (skipped without PROVENA_DATABASE_URL)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid

import pytest

from app.models import (
    ConnectorAuthType,
    ConnectorConfig,
    ConnectorProvider,
    ConnectorSourceBatch,
    ConnectorSourceRecord,
    MemoryCreate,
    MemoryKind,
    PermissionLevel,
    PrincipalMapping,
    PrincipalMappingBatch,
    RepositoryMemorySyncRequest,
    RTBFRequest,
    RelationKind,
    RelationWrite,
    ScopeEnvelope,
    SearchRequest,
    SourcePermissionBatch,
    SourcePermissionGrant,
    SyncJob,
    SyncJobType,
)
from app.store import AccessContext, TenantOwnershipConflictError
from app.store_postgres import PostgresStore

pytestmark = pytest.mark.postgres


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
