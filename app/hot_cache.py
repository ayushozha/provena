"""Hot cache for search responses.

The store depends on a small cache surface (``get_search`` / ``set_search`` /
``bump_search_version`` / ``backend_name``). This module provides the interface
plus a process-local implementation; a Redis-backed cache can implement the
same surface later without touching the store.
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

    def bump_search_version(self, tenant_id: str) -> None: ...


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
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}
        self._write_count = 0

    def _versioned_key(self, tenant_id: str, cache_key: str) -> str:
        version = self._versions.get(tenant_id, 0)
        return f"{tenant_id}:{version}:{cache_key}"

    def get_search(self, tenant_id: str, cache_key: str) -> dict[str, Any] | None:
        now = time.time()
        with self._lock:
            key = self._versioned_key(tenant_id, cache_key)
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at < now:
                del self._entries[key]
                return None
            return value

    def set_search(self, tenant_id: str, cache_key: str, value: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            # Purge expired entries periodically rather than on every write, so
            # a hot write path doesn't eat an O(N) scan each time.
            self._write_count += 1
            if self._write_count % 100 == 0:
                self._purge_expired(now)
            self._entries[self._versioned_key(tenant_id, cache_key)] = (now + self._ttl, value)

    def bump_search_version(self, tenant_id: str) -> None:
        with self._lock:
            self._versions[tenant_id] = self._versions.get(tenant_id, 0) + 1

    def _purge_expired(self, now: float) -> None:
        expired = [key for key, (expires_at, _) in self._entries.items() if expires_at < now]
        for key in expired:
            del self._entries[key]
