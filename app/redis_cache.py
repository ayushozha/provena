"""Redis-backed hot cache for search responses."""

from __future__ import annotations

import json
import hashlib
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
        return f"{_VERSION_PREFIX}:{self._tenant_token(tenant_id)}"

    @staticmethod
    def _tenant_token(tenant_id: str) -> str:
        return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()

    def _version(self, tenant_id: str) -> str:
        version_raw = self._client.get(self._version_key(tenant_id))
        return str(version_raw) if version_raw is not None else "0"

    def _search_key(self, tenant_id: str, cache_key: str, version: str | None = None) -> str:
        generation = version if version is not None else self._version(tenant_id)
        return f"{_SEARCH_PREFIX}:{self._tenant_token(tenant_id)}:v{generation}:{cache_key}"

    def get_search(self, tenant_id: str, cache_key: str) -> dict[str, Any] | None:
        return self.lookup_search(tenant_id, cache_key)[0]

    def lookup_search(
        self,
        tenant_id: str,
        cache_key: str,
    ) -> tuple[dict[str, Any] | None, str]:
        try:
            version_key = self._version_key(tenant_id)
            raw: str | None = None
            generation = "0"
            with self._client.pipeline() as pipeline:
                for _ in range(3):
                    try:
                        pipeline.watch(version_key)
                        generation_raw = pipeline.get(version_key)
                        generation = str(generation_raw) if generation_raw is not None else "0"
                        raw = pipeline.get(
                            self._search_key(tenant_id, cache_key, generation)
                        )
                        pipeline.multi()
                        pipeline.get(version_key)
                        confirmed = pipeline.execute()[0]
                        confirmed_generation = (
                            str(confirmed) if confirmed is not None else "0"
                        )
                        if confirmed_generation == generation:
                            break
                    except redis.WatchError:
                        continue
                else:
                    return None, self._version(tenant_id)
        except redis.RedisError as exc:
            logger.warning("Redis search cache get failed; treating as miss: %s", exc)
            return None, "unavailable"
        if raw is None:
            return None, generation
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Redis search cache payload invalid for tenant=%s", tenant_id)
            return None, generation
        if not isinstance(loaded, dict):
            return None, generation
        return loaded, generation

    def set_search(self, tenant_id: str, cache_key: str, value: dict[str, Any]) -> None:
        try:
            generation = self._version(tenant_id)
            self._client.setex(
                self._search_key(tenant_id, cache_key, generation),
                self._ttl,
                json.dumps(value, separators=(",", ":")),
            )
        except redis.RedisError as exc:
            logger.warning("Redis search cache set failed: %s", exc)

    def set_search_if_current(
        self,
        tenant_id: str,
        cache_key: str,
        value: dict[str, Any],
        generation: int | str,
    ) -> bool:
        expected = str(generation)
        if expected == "unavailable":
            return False
        try:
            version_key = self._version_key(tenant_id)
            with self._client.pipeline() as pipeline:
                for _ in range(3):
                    try:
                        pipeline.watch(version_key)
                        current = pipeline.get(version_key) or "0"
                        if str(current) != expected:
                            pipeline.unwatch()
                            return False
                        pipeline.multi()
                        pipeline.set(
                            self._search_key(tenant_id, cache_key, expected),
                            json.dumps(value, separators=(",", ":")),
                            ex=self._ttl,
                        )
                        pipeline.execute()
                        return True
                    except redis.WatchError:
                        continue
                return False
        except redis.RedisError as exc:
            logger.warning("Redis conditional search cache set failed: %s", exc)
            return False

    def bump_search_version(self, tenant_id: str) -> None:
        try:
            self._client.incr(self._version_key(tenant_id))
        except redis.RedisError as exc:
            logger.error("Redis search cache version bump failed: %s", exc)
            raise

    def purge_tenant(self, tenant_id: str) -> None:
        """Physically delete every versioned payload for a governed erasure."""
        try:
            self._client.incr(self._version_key(tenant_id))
            batch: list[str] = []
            pattern = f"{_SEARCH_PREFIX}:{self._tenant_token(tenant_id)}:v*:*"
            for key in self._client.scan_iter(match=pattern, count=500):
                batch.append(key)
                if len(batch) == 500:
                    self._client.delete(*batch)
                    batch.clear()
            if batch:
                self._client.delete(*batch)
        except redis.RedisError as exc:
            logger.error("Redis tenant cache purge failed: %s", exc)
            raise


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
