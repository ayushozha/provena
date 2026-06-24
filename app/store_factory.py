"""Store backend selection for standalone Provena deployments."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from app.config import Settings
from app.redis_cache import create_hot_cache
from app.store import ProvenaStore
from app.store_postgres import PostgresStore

if TYPE_CHECKING:
    from app.hot_cache import MemoryHotCache

StoreBackend = ProvenaStore | PostgresStore


def create_store(
    settings: Settings,
    *,
    hot_cache: MemoryHotCache | None = None,
) -> StoreBackend:
    """Return the configured storage backend.

    ``PROVENA_DATABASE_URL`` selects PostgreSQL; ``PROVENA_DB_PATH`` selects SQLite.
    When both are set, ``PROVENA_DATABASE_URL`` wins.
    """
    resolved_cache = hot_cache or create_hot_cache(settings)
    if settings.database_url:
        return PostgresStore(
            settings.database_url,
            hot_cache=resolved_cache,
            vector_dimensions=settings.vector_dimensions,
        )
    return ProvenaStore(
        Path(settings.resolved_db_path),
        hot_cache=resolved_cache,
        vector_dimensions=settings.vector_dimensions,
    )