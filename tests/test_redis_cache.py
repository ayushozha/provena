"""Tests for Redis-backed search hot cache."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import Settings
from app.hot_cache import InMemoryHotCache
from app.models import MemoryCreate, MemoryKind, ScopeEnvelope, SearchRequest
from app.redis_cache import RedisHotCache, create_hot_cache
from app.store import AccessContext, ProvenaStore
from app.store_factory import create_store

try:
    import fakeredis
except ImportError:  # pragma: no cover - optional dev dependency
    fakeredis = None


@pytest.fixture
def fake_redis():
    if fakeredis is None:
        pytest.skip("fakeredis not installed")
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_cache(fake_redis) -> RedisHotCache:
    return RedisHotCache("redis://unused", client=fake_redis, ttl_seconds=60)


def test_backend_name_is_redis(redis_cache: RedisHotCache) -> None:
    assert redis_cache.backend_name == "redis"


def test_set_and_get_search_roundtrip(redis_cache: RedisHotCache) -> None:
    payload = {"results": [{"memory": {"memory_id": "mem-1"}, "score": 0.9}]}
    redis_cache.set_search("tenant-a", "cache-key-1", payload)
    assert redis_cache.get_search("tenant-a", "cache-key-1") == payload


def test_get_search_miss_returns_none(redis_cache: RedisHotCache) -> None:
    assert redis_cache.get_search("tenant-a", "missing") is None


def test_bump_search_version_invalidates_prior_entries(redis_cache: RedisHotCache) -> None:
    redis_cache.set_search("tenant-a", "cache-key-1", {"results": []})
    assert redis_cache.get_search("tenant-a", "cache-key-1") == {"results": []}

    redis_cache.bump_search_version("tenant-a")

    assert redis_cache.get_search("tenant-a", "cache-key-1") is None


def test_search_keys_use_versioned_prefix(redis_cache: RedisHotCache, fake_redis) -> None:
    redis_cache.set_search("tenant-a", "abc123", {"results": []})
    keys = fake_redis.keys("provena:search:tenant-a:v0:abc123")
    assert keys == ["provena:search:tenant-a:v0:abc123"]

    redis_cache.bump_search_version("tenant-a")
    redis_cache.set_search("tenant-a", "abc123", {"results": [{"score": 1}]})

    versioned_keys = set(fake_redis.keys("provena:search:tenant-a:v*"))
    assert "provena:search:tenant-a:v0:abc123" in versioned_keys
    assert "provena:search:tenant-a:v1:abc123" in versioned_keys


def test_ttl_uses_setex(redis_cache: RedisHotCache, fake_redis) -> None:
    redis_cache.set_search("tenant-a", "ttl-key", {"results": []})
    ttl = fake_redis.ttl("provena:search:tenant-a:v0:ttl-key")
    assert 0 < ttl <= 60


def test_create_hot_cache_without_redis_url_uses_in_memory() -> None:
    cache = create_hot_cache(Settings(redis_url=None))
    assert isinstance(cache, InMemoryHotCache)
    assert cache.backend_name == "in_memory"


def test_create_hot_cache_with_unreachable_redis_falls_back() -> None:
    cache = create_hot_cache(Settings(redis_url="redis://127.0.0.1:1"))
    assert isinstance(cache, InMemoryHotCache)
    assert cache.backend_name == "in_memory"


def test_store_factory_uses_in_memory_when_redis_unset(tmp_path: Path) -> None:
    settings = Settings(db_path=str(tmp_path / "factory.db"), redis_url=None)
    store = create_store(settings)
    assert store.hot_cache.backend_name == "in_memory"
    store.close()


def test_store_factory_uses_redis_when_url_set(tmp_path: Path, fake_redis, monkeypatch) -> None:
    def fake_create_hot_cache(settings: Settings, *, ttl_seconds: int = 300):
        if settings.redis_url:
            return RedisHotCache(settings.redis_url, client=fake_redis, ttl_seconds=ttl_seconds)
        return InMemoryHotCache(ttl_seconds=ttl_seconds)

    monkeypatch.setattr("app.store_factory.create_hot_cache", fake_create_hot_cache)
    settings = Settings(db_path=str(tmp_path / "factory-redis.db"), redis_url="redis://localhost:6379/0")
    store = create_store(settings)
    assert store.hot_cache.backend_name == "redis"
    store.close()


def test_store_search_cache_hit_then_invalidated(tmp_path: Path, fake_redis) -> None:
    cache = RedisHotCache("redis://unused", client=fake_redis)
    store = ProvenaStore(tmp_path / "search-cache.db", hot_cache=cache)
    access = AccessContext(tenant_id="tenant-cache", role="admin", principal_id="admin-1")
    tenant_id = "tenant-cache"

    store.create_memory(
        MemoryCreate(
            kind=MemoryKind.FACT,
            scope=ScopeEnvelope(tenant_id=tenant_id, workspace_id="ws-1"),
            title="Cache policy",
            content="Search cache should invalidate after memory writes.",
            tags=["cache"],
        ),
        access=access,
    )
    search = SearchRequest(
        query="cache invalidate",
        scope=ScopeEnvelope(tenant_id=tenant_id, workspace_id="ws-1"),
        limit=5,
    )

    first = store.search_memories(search, access=access)
    assert first.results
    # Memory create above already bumped the tenant cache version once.
    assert len(fake_redis.keys("provena:search:tenant-cache:v1:*")) == 1

    second = store.search_memories(search, access=access)
    assert second.results
    assert len(fake_redis.keys("provena:search:tenant-cache:v1:*")) == 1

    store.create_memory(
        MemoryCreate(
            kind=MemoryKind.FACT,
            scope=ScopeEnvelope(tenant_id=tenant_id, workspace_id="ws-1"),
            title="Another write",
            content="This write should bump the tenant cache version.",
        ),
        access=access,
    )
    assert fake_redis.get("provena:search_version:tenant-cache") == "2"

    third = store.search_memories(search, access=access)
    assert third.results
    assert len(fake_redis.keys("provena:search:tenant-cache:v2:*")) == 1

    store.close()


@pytest.mark.skipif(
    not os.environ.get("PROVENA_REDIS_URL"),
    reason="PROVENA_REDIS_URL not set",
)
def test_live_redis_roundtrip(tmp_path: Path) -> None:
    settings = Settings(
        db_path=str(tmp_path / "live-redis.db"),
        redis_url=os.environ["PROVENA_REDIS_URL"],
    )
    store = create_store(settings)
    assert store.hot_cache.backend_name == "redis"
    store.hot_cache.set_search("tenant-live", "live-key", {"results": []})
    assert store.hot_cache.get_search("tenant-live", "live-key") == {"results": []}
    store.close()