"""Hot cache for search responses.

The store depends on a small cache surface (``get_search`` / ``set_search`` /
``bump_search_version`` / ``purge_tenant`` / ``backend_name``). This module
provides the interface plus a process-local implementation; a Redis-backed
cache implements the same surface without changing store policy.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Protocol


class MemoryHotCache(Protocol):
    """Interface the store uses to cache search responses."""

    backend_name: str

    def get_search(self, tenant_id: str, cache_key: str) -> dict[str, Any] | None: ...

    def set_search(self, tenant_id: str, cache_key: str, value: dict[str, Any]) -> None: ...

    def lookup_search(
        self,
        tenant_id: str,
        cache_key: str,
    ) -> tuple[dict[str, Any] | None, int | str]: ...

    def set_search_if_current(
        self,
        tenant_id: str,
        cache_key: str,
        value: dict[str, Any],
        generation: int | str,
    ) -> bool: ...

    def bump_search_version(self, tenant_id: str) -> None: ...

    def purge_tenant(self, tenant_id: str) -> None: ...


class InMemoryHotCache:
    """Process-local search cache with TTL and per-tenant versioned invalidation.

    Invalidation is O(1): bumping a tenant's version moves its reads and writes
    into a fresh keyspace, so previously cached entries become unreachable and
    age out via TTL instead of being scanned and deleted on every write.
    """

    backend_name = "in_memory"

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._versions: dict[str, int] = {}
        self._entries: dict[tuple[str, int, str], tuple[float, dict[str, Any]]] = {}
        self._write_count = 0

    def _versioned_key(self, tenant_id: str, cache_key: str) -> tuple[str, int, str]:
        return tenant_id, self._versions.get(tenant_id, 0), cache_key

    def get_search(self, tenant_id: str, cache_key: str) -> dict[str, Any] | None:
        return self.lookup_search(tenant_id, cache_key)[0]

    def lookup_search(
        self,
        tenant_id: str,
        cache_key: str,
    ) -> tuple[dict[str, Any] | None, int]:
        now = time.time()
        with self._lock:
            generation = self._versions.get(tenant_id, 0)
            key = self._versioned_key(tenant_id, cache_key)
            entry = self._entries.get(key)
            if entry is None:
                return None, generation
            expires_at, value = entry
            if expires_at < now:
                del self._entries[key]
                return None, generation
            return value, generation

    def set_search(self, tenant_id: str, cache_key: str, value: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            # Purge expired entries periodically rather than on every write, so
            # a hot write path doesn't eat an O(N) scan each time.
            self._write_count += 1
            if self._write_count % 100 == 0:
                self._purge_expired(now)
            self._entries[self._versioned_key(tenant_id, cache_key)] = (now + self._ttl, value)

    def set_search_if_current(
        self,
        tenant_id: str,
        cache_key: str,
        value: dict[str, Any],
        generation: int | str,
    ) -> bool:
        if not isinstance(generation, int):
            return False
        now = time.time()
        with self._lock:
            if self._versions.get(tenant_id, 0) != generation:
                return False
            self._entries[(tenant_id, generation, cache_key)] = (now + self._ttl, value)
            return True

    def bump_search_version(self, tenant_id: str) -> None:
        with self._lock:
            self._versions[tenant_id] = self._versions.get(tenant_id, 0) + 1

    def purge_tenant(self, tenant_id: str) -> None:
        """Physically remove cached payloads for strict erasure workflows."""
        with self._lock:
            self._versions[tenant_id] = self._versions.get(tenant_id, 0) + 1
            for key in [key for key in self._entries if key[0] == tenant_id]:
                del self._entries[key]

    def _purge_expired(self, now: float) -> None:
        expired = [key for key, (expires_at, _) in self._entries.items() if expires_at < now]
        for key in expired:
            del self._entries[key]
