"""Redis-backed hot cache for search responses."""

from __future__ import annotations

import json
import logging
from typing import Any

import redis

from app.config import Settings
from app.hot_cache import InMemoryHotCache, MemoryHotCache

logger = logging.getLogger(__name__)

_SEARCH_PREFIX = "provena:search"
_VERSION_PREFIX = "provena:search_version"
_DEFAULT_TTL_SECONDS = 300


class RedisHotCache:
    """Distributed search cache with per-tenant versioned invalidation."""

    backend_name = "redis"

    def __init__(
        self,
        redis_url: str,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        client: redis.Redis | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        if client is not None:
            self._client = client
        else:
            pool = redis.ConnectionPool.from_url(redis_url, decode_responses=True)
            self._client = redis.Redis(connection_pool=pool)
            self._client.ping()

    def _version_key(self, tenant_id: str) -> str:
        return f"{_VERSION_PREFIX}:{tenant_id}"

    def _search_key(self, tenant_id: str, cache_key: str) -> str:
        version_raw = self._client.get(self._version_key(tenant_id))
        version = int(version_raw) if version_raw is not None else 0
        return f"{_SEARCH_PREFIX}:{tenant_id}:v{version}:{cache_key}"

    def get_search(self, tenant_id: str, cache_key: str) -> dict[str, Any] | None:
        try:
            raw = self._client.get(self._search_key(tenant_id, cache_key))
        except redis.RedisError as exc:
            logger.warning("Redis search cache get failed; treating as miss: %s", exc)
            return None
        if raw is None:
            return None
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Redis search cache payload invalid for tenant=%s", tenant_id)
            return None
        if not isinstance(loaded, dict):
            return None
        return loaded

    def set_search(self, tenant_id: str, cache_key: str, value: dict[str, Any]) -> None:
        try:
            self._client.setex(
                self._search_key(tenant_id, cache_key),
                self._ttl,
                json.dumps(value, separators=(",", ":")),
            )
        except redis.RedisError as exc:
            logger.warning("Redis search cache set failed: %s", exc)

    def bump_search_version(self, tenant_id: str) -> None:
        try:
            self._client.incr(self._version_key(tenant_id))
        except redis.RedisError as exc:
            logger.warning("Redis search cache version bump failed: %s", exc)


def create_hot_cache(settings: Settings, *, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> MemoryHotCache:
    """Return Redis cache when configured, otherwise in-memory.

    If Redis is configured but unreachable at startup, logs a warning and falls
    back to ``InMemoryHotCache``.
    """
    if not settings.redis_url:
        return InMemoryHotCache(ttl_seconds=ttl_seconds)
    try:
        return RedisHotCache(settings.redis_url, ttl_seconds=ttl_seconds)
    except redis.RedisError as exc:
        logger.warning(
            "Redis unavailable (%s); falling back to in-memory hot cache",
            exc,
        )
        return InMemoryHotCache(ttl_seconds=ttl_seconds)