"""Integration tests for PostgresStore (skipped without PROVENA_DATABASE_URL)."""

from __future__ import annotations

import os
import uuid

import pytest

from app.models import (
    MemoryCreate,
    MemoryKind,
    RelationKind,
    RelationWrite,
    ScopeEnvelope,
    SearchRequest,
)
from app.store import AccessContext
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
    assert any(item.memory_id == second.memory.memory_id for item in related)


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