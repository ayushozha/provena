"""PostgreSQL-backed Provena store (PLAN-15)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.hot_cache import InMemoryHotCache, MemoryHotCache
from app.models import MemoryLayer, MemoryStatus, ScopeEnvelope
from app.pg_connection import IntegrityError as PgIntegrityError
from app.pg_connection import OperationalError, PostgresConnection
from app.store import AccessContext, ProvenaStore, SearchCandidate, SearchRequest

POSTGRES_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "storage" / "migrations" / "001_postgres.sql"


class PostgresStore(ProvenaStore):
    """ProvenaStore backed by PostgreSQL via psycopg v3."""

    def __init__(
        self,
        database_url: str,
        hot_cache: MemoryHotCache | None = None,
        vector_dimensions: int = 768,
    ) -> None:
        self.conn = PostgresConnection.connect(database_url)
        self.hot_cache = hot_cache or InMemoryHotCache(ttl_seconds=300)
        self.vector_dimensions = vector_dimensions
        self.vec_enabled = False
        self._ensure_schema()
        self._ensure_compatibility()

    def close(self) -> None:
        self.conn.close()

    def update_memory(self, memory_id: str, payload, access: AccessContext | None = None):
        try:
            return super().update_memory(memory_id, payload, access=access)
        except PgIntegrityError as exc:
            raise ValueError("update would create a duplicate memory fingerprint") from exc

    def _ensure_schema(self) -> None:
        self.conn.executescript(POSTGRES_SCHEMA_PATH.read_text(encoding="utf-8"))

    def _ensure_compatibility(self) -> None:
        rows = self.conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'memories'
            """
        ).fetchall()
        columns = {row["column_name"] for row in rows}
        migrations = {
            "embedding_model": "ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_model TEXT",
            "embedding_json": "ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_json JSONB",
            "acl_json": "ALTER TABLE memories ADD COLUMN IF NOT EXISTS acl_json TEXT NOT NULL DEFAULT '[]'",
            "held": "ALTER TABLE memories ADD COLUMN IF NOT EXISTS held INTEGER NOT NULL DEFAULT 0",
            "hold_reason": "ALTER TABLE memories ADD COLUMN IF NOT EXISTS hold_reason TEXT",
            "hold_until": "ALTER TABLE memories ADD COLUMN IF NOT EXISTS hold_until TEXT",
            "pre_hold_status": "ALTER TABLE memories ADD COLUMN IF NOT EXISTS pre_hold_status TEXT",
            "memory_layer": (
                f"ALTER TABLE memories ADD COLUMN IF NOT EXISTS memory_layer TEXT "
                f"NOT NULL DEFAULT '{MemoryLayer.ORGANIZATION.value}'"
            ),
            "expires_at": "ALTER TABLE memories ADD COLUMN IF NOT EXISTS expires_at TEXT",
            "search_vector": "ALTER TABLE memories ADD COLUMN IF NOT EXISTS search_vector tsvector",
        }
        with self.conn:
            for column, statement in migrations.items():
                if column not in columns:
                    self.conn.execute(statement)

    def _init_vector_index(self) -> bool:
        # pgvector KNN is PLAN-16; linear cosine scan fallback is used until then.
        return False

    def _index_memory(
        self,
        memory_id: str,
        title: str | None,
        summary: str | None,
        content: str,
        tags: list[str],
        entity_keys: list[str],
    ) -> None:
        document = " ".join(
            part
            for part in [
                title or "",
                summary or "",
                content,
                " ".join(tags),
                " ".join(entity_keys),
            ]
            if part
        )
        self.conn.execute(
            """
            UPDATE memories
            SET search_vector = to_tsvector('english', %s)
            WHERE memory_id = %s
            """,
            (document, memory_id),
        )

    def _fts_candidate_rows(self, payload: SearchRequest, candidate_limit: int) -> list[SearchCandidate]:
        if not payload.query.strip():
            return []
        scope_clauses, scope_params = self._search_scope_clauses(payload.scope)
        scope_sql = f" AND {' AND '.join(scope_clauses)}" if scope_clauses else ""
        status_sql, status_params = self._search_status_filter(payload.include_deleted, alias="m")
        try:
            rows = self.conn.execute(
                f"""
                SELECT m.*,
                       ts_rank_cd(m.search_vector, plainto_tsquery('english', %s)) AS fts_rank
                FROM memories AS m
                WHERE m.search_vector @@ plainto_tsquery('english', %s)
                  AND m.tenant_id = %s{status_sql}{scope_sql}
                ORDER BY fts_rank DESC, m.updated_at DESC
                LIMIT %s
                """,
                (
                    payload.query,
                    payload.query,
                    payload.scope.tenant_id,
                    *status_params,
                    *scope_params,
                    candidate_limit,
                ),
            ).fetchall()
        except OperationalError:
            return []
        return [
            SearchCandidate(
                row=row,
                fts_rank=float(row["fts_rank"]) if row["fts_rank"] is not None else None,
            )
            for row in rows
        ]

    def _json_to_list(self, value: str | list[Any] | None) -> list[Any]:
        if isinstance(value, list):
            return value
        return super()._json_to_list(value)

    def _json_to_dict(self, value: str | dict[str, Any] | None) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        return super()._json_to_dict(value)

    def _json_to_float_list(self, value: str | list[Any] | None) -> list[float]:
        if isinstance(value, list):
            return [float(item) for item in value]
        return super()._json_to_float_list(value)

    def upsert_entity(
        self,
        *,
        entity_id: str,
        canonical_name: str,
        tenant_id: str,
        aliases: list[str] | None = None,
        entity_type: str | None = None,
    ) -> str:
        now = self._iso_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO entity_registry (
                    entity_id, canonical_name, aliases_json, entity_type, tenant_id, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (entity_id) DO UPDATE SET
                    canonical_name = EXCLUDED.canonical_name,
                    aliases_json = EXCLUDED.aliases_json,
                    entity_type = EXCLUDED.entity_type,
                    tenant_id = EXCLUDED.tenant_id,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    entity_id,
                    canonical_name,
                    self._to_json(aliases or []),
                    entity_type,
                    tenant_id,
                    now,
                    now,
                ),
            )
        return entity_id

    def _count_scalar(self, query: str, params: tuple[Any, ...] = ()) -> int:
        if "memories_fts" in query:
            row = self.conn.execute(
                "SELECT COUNT(*) AS count FROM memories WHERE tenant_id = %s AND search_vector IS NOT NULL",
                (params[0],) if params else (),
            ).fetchone()
            return int(row["count"]) if row else 0
        return super()._count_scalar(query, params)

    def get_entity(self, entity_id: str, tenant_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT entity_id, canonical_name, aliases_json, entity_type, tenant_id, created_at, updated_at
            FROM entity_registry
            WHERE entity_id = %s AND tenant_id = %s
            """,
            (entity_id, tenant_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "entity_id": row["entity_id"],
            "canonical_name": row["canonical_name"],
            "aliases": self._json_to_list(row["aliases_json"]),
            "entity_type": row["entity_type"],
            "tenant_id": row["tenant_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }