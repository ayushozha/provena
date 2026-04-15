from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from app.models import (
    ACLEntry,
    ACLPermission,
    ConnectorConfig,
    ConnectorSourceBatch,
    ConnectorSourceRecord,
    ConnectorStatus,
    DeleteResponse,
    EraseRequest,
    EraseResponse,
    IntegrationCoverageSummary,
    LegalHold,
    MemoryCreate,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
    MemoryWriteResult,
    PermissionLevel,
    PrincipalMapping,
    PrincipalMappingBatch,
    ProjectSnapshot,
    RelatedMemory,
    RelationKind,
    RelationWrite,
    RTBFRequest,
    RTBFResponse,
    RetentionEnforcementResponse,
    RetentionPolicy,
    ScopeEnvelope,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SourcePermissionBatch,
    SourcePermissionGrant,
    SourceReference,
    SourceSyncStatus,
    SyncJob,
    SyncJobStatus,
)


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "storage" / "migrations" / "001_initial.sql"
READABLE_PERMISSION_LEVELS = frozenset(level.value for level in PermissionLevel)


@dataclass(slots=True)
class AccessContext:
    tenant_id: str | None = None
    role: str = "superadmin"
    principal_id: str | None = None
    key_id: str | None = None
    groups: list[str] = field(default_factory=list)


class ProvenaStore:
    def __init__(self, db_path: Path | str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema()
        self._ensure_compatibility()

    def close(self) -> None:
        self.conn.close()

    def create_memory(
        self,
        payload: MemoryCreate,
        access: AccessContext | None = None,
    ) -> MemoryWriteResult:
        fingerprint = self._fingerprint(payload.scope, payload.kind.value, payload.title, payload.content)
        existing = self.conn.execute(
            "SELECT memory_id FROM memories WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if existing:
            record = self.get_memory(existing["memory_id"], access=access)
            if record is None:
                row = self.conn.execute(
                    "SELECT * FROM memories WHERE memory_id = ?",
                    (existing["memory_id"],),
                ).fetchone()
                record = self._row_to_record(row)
            return MemoryWriteResult(created=False, memory=record)

        now = self._iso_now()
        memory_id = payload.memory_id or str(uuid.uuid4())
        acl = payload.acl or self._default_acl(payload.scope, access)

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO memories (
                    memory_id, fingerprint, kind, status, tenant_id, workspace_id,
                    project_id, user_id, agent_id, session_id, title, content, summary,
                    entity_keys_json, tags_json, metadata_json, importance, confidence,
                    strength, valid_from, valid_to, created_at, updated_at,
                    embedding_model, embedding_json, acl_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    fingerprint,
                    payload.kind.value,
                    MemoryStatus.ACTIVE.value,
                    payload.scope.tenant_id,
                    payload.scope.workspace_id,
                    payload.scope.project_id,
                    payload.scope.user_id,
                    payload.scope.agent_id,
                    payload.scope.session_id,
                    payload.title,
                    payload.content,
                    payload.summary,
                    self._to_json(payload.entity_keys),
                    self._to_json(payload.tags),
                    self._to_json(payload.metadata),
                    payload.importance,
                    payload.confidence,
                    payload.strength,
                    self._to_iso(payload.valid_from),
                    self._to_iso(payload.valid_to),
                    now,
                    now,
                    payload.embedding_model,
                    self._to_json(payload.embedding),
                    self._to_json([entry.model_dump() for entry in acl]),
                ),
            )
            self._replace_trigger_phrases(memory_id, payload.scope.tenant_id, payload.trigger_phrases)
            for source in payload.source_references:
                self._insert_source(memory_id, source)
            if payload.supersedes_memory_id:
                self.conn.execute(
                    "UPDATE memories SET status = ?, updated_at = ? WHERE memory_id = ? AND tenant_id = ?",
                    (
                        MemoryStatus.SUPERSEDED.value,
                        now,
                        payload.supersedes_memory_id,
                        payload.scope.tenant_id,
                    ),
                )
                self._insert_relation(
                    payload.supersedes_memory_id,
                    memory_id,
                    RelationKind.SUPERSEDES.value,
                    payload.scope,
                )
            self._index_memory(memory_id, payload.title, payload.summary, payload.content, payload.tags, payload.entity_keys)
            self._insert_audit("memory_created", memory_id, payload.scope.tenant_id, {"kind": payload.kind.value})
            self._refresh_hold_state([memory_id])

        record = self.get_memory(memory_id, access=access)
        if record is None:
            row = self.conn.execute("SELECT * FROM memories WHERE memory_id = ?", (memory_id,)).fetchone()
            record = self._row_to_record(row)
        return MemoryWriteResult(created=True, memory=record)

    def get_memory(
        self,
        memory_id: str,
        access: AccessContext | None = None,
        enforce_source_grants: bool = False,
    ) -> MemoryRecord | None:
        self._refresh_hold_state([memory_id])
        row = self.conn.execute(
            "SELECT * FROM memories WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            return None
        record = self._row_to_record(row)
        if not self._can_access(record, access, ACLPermission.READ):
            return None
        if enforce_source_grants and not self._has_required_source_grants(record, access):
            return None
        return record

    def search_memories(
        self,
        payload: SearchRequest,
        access: AccessContext | None = None,
    ) -> SearchResponse:
        query_terms = self._query_terms(payload.query)
        rows = self._candidate_rows(payload)
        self._refresh_hold_state([row["memory_id"] for row in rows])
        rows = self._candidate_rows(payload)
        results: list[SearchResult] = []
        for row in rows:
            record = self._row_to_record(row)
            if not self._can_read_memory(record, access):
                continue
            if not self._scope_matches(record.scope, payload.scope):
                continue
            if payload.kinds and record.kind not in payload.kinds:
                continue
            if payload.tags and not set(payload.tags).issubset(record.tags):
                continue
            if payload.entity_keys and not set(payload.entity_keys).intersection(record.entity_keys):
                continue
            fts_rank = float(row["fts_rank"]) if "fts_rank" in row.keys() and row["fts_rank"] is not None else None
            score, reasons = self._score_record(record, payload.scope, query_terms, fts_rank)
            if payload.query_embedding and record.embedding:
                vector_score = self._cosine_similarity(payload.query_embedding, record.embedding)
                score += vector_score * 0.35
                reasons.append("vector similarity")
            if score <= 0:
                continue
            related = self._related_memories(record.memory_id, access) if payload.include_relations else []
            results.append(
                SearchResult(
                    memory=record,
                    score=score,
                    reasons=reasons,
                    related_memories=related,
                )
            )

        results.sort(
            key=lambda item: (
                item.score,
                item.memory.updated_at,
                item.memory.created_at,
            ),
            reverse=True,
        )
        return SearchResponse(results=results[: payload.limit])

    def write_relation(
        self,
        payload: RelationWrite,
        access: AccessContext | None = None,
    ) -> None:
        source = self.get_memory(payload.from_memory_id, access=access)
        target = self.get_memory(payload.to_memory_id, access=access)
        if source is None or target is None:
            raise ValueError("relation requires accessible source and target memories")
        if not self._can_access(source, access, ACLPermission.WRITE):
            raise ValueError("write access required")
        with self.conn:
            self._insert_relation(
                payload.from_memory_id,
                payload.to_memory_id,
                payload.relation.value,
                payload.scope,
            )
            self._insert_audit(
                "relation_created",
                payload.from_memory_id,
                payload.scope.tenant_id,
                {"to_memory_id": payload.to_memory_id, "relation": payload.relation.value},
            )

    def delete_memory(
        self,
        memory_id: str,
        hard_delete: bool = False,
        access: AccessContext | None = None,
    ) -> DeleteResponse:
        record = self.get_memory(memory_id, access=access)
        if record is None:
            return DeleteResponse(memory_id=memory_id, deleted=False, hard_delete=hard_delete)
        if not self._can_access(record, access, ACLPermission.DELETE):
            raise ValueError("delete access required")

        with self.conn:
            if hard_delete:
                self.conn.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))
            else:
                self.conn.execute(
                    "UPDATE memories SET status = ?, updated_at = ? WHERE memory_id = ?",
                    (MemoryStatus.DELETED.value, self._iso_now(), memory_id),
                )
            self._insert_audit("memory_deleted", memory_id, record.scope.tenant_id, {"hard_delete": hard_delete})
        return DeleteResponse(memory_id=memory_id, deleted=True, hard_delete=hard_delete)

    def erase_scope(
        self,
        payload: EraseRequest,
        access: AccessContext | None = None,
    ) -> EraseResponse:
        selector, params = self._erase_selector(payload)
        ids = [
            row["memory_id"]
            for row in self.conn.execute(
                f"SELECT memory_id FROM memories WHERE {selector}",
                params,
            ).fetchall()
        ]
        if access is not None:
            ids = [
                memory_id
                for memory_id in ids
                if (record := self.get_memory(memory_id, access=access)) is not None
                and self._can_access(record, access, ACLPermission.DELETE)
            ]
        if not ids:
            return EraseResponse(deleted_memories=0, deleted_sources=0, deleted_relations=0)

        placeholders = ", ".join("?" for _ in ids)
        source_count = self.conn.execute(
            f"SELECT COUNT(*) AS count FROM memory_sources WHERE memory_id IN ({placeholders})",
            ids,
        ).fetchone()["count"]
        relation_count = self.conn.execute(
            f"SELECT COUNT(*) AS count FROM memory_relations WHERE from_memory_id IN ({placeholders}) OR to_memory_id IN ({placeholders})",
            (*ids, *ids),
        ).fetchone()["count"]

        with self.conn:
            self.conn.execute(
                f"DELETE FROM memories WHERE memory_id IN ({placeholders})",
                ids,
            )
            self._insert_audit("scope_erased", None, payload.tenant_id, {"memory_ids": ids})
        return EraseResponse(
            deleted_memories=len(ids),
            deleted_sources=source_count,
            deleted_relations=relation_count,
        )

    def upsert_project_snapshot(self, snapshot: ProjectSnapshot) -> ProjectSnapshot:
        now = self._to_iso(snapshot.created_at) or self._iso_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO project_snapshots (
                    snapshot_id, tenant_id, project_id, summary, entity_summary_json,
                    decision_log_json, memory_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    snapshot.tenant_id,
                    snapshot.project_id,
                    snapshot.summary,
                    self._to_json(snapshot.entity_summary),
                    self._to_json(snapshot.decision_log),
                    snapshot.memory_count,
                    now,
                ),
            )
        return ProjectSnapshot(
            tenant_id=snapshot.tenant_id,
            project_id=snapshot.project_id,
            summary=snapshot.summary,
            entity_summary=snapshot.entity_summary,
            decision_log=snapshot.decision_log,
            memory_count=snapshot.memory_count,
            created_at=self._from_iso(now) or snapshot.created_at,
        )

    def get_latest_project_snapshot(self, tenant_id: str, project_id: str) -> ProjectSnapshot | None:
        row = self.conn.execute(
            """
            SELECT tenant_id, project_id, summary, entity_summary_json, decision_log_json, memory_count, created_at
            FROM project_snapshots
            WHERE tenant_id = ? AND project_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (tenant_id, project_id),
        ).fetchone()
        if row is None:
            return None
        return ProjectSnapshot(
            tenant_id=row["tenant_id"],
            project_id=row["project_id"],
            summary=row["summary"],
            entity_summary=self._json_to_dict(row["entity_summary_json"]),
            decision_log=self._json_to_list(row["decision_log_json"]),
            memory_count=int(row["memory_count"]),
            created_at=self._from_iso(row["created_at"]) or datetime.now(UTC),
        )

    def save_connector(self, connector: ConnectorConfig) -> ConnectorConfig:
        now = self._iso_now()
        created_at = self._to_iso(connector.created_at) or now
        updated_at = self._to_iso(connector.updated_at) or now
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO connectors (
                    connector_id, tenant_id, provider, display_name, remote_workspace_id,
                    auth_type, sync_mode, status, principal_sync_enabled, acl_sync_enabled,
                    freshness_sla_seconds, metadata_json, created_at, updated_at,
                    last_synced_at, last_webhook_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(connector_id) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    provider = excluded.provider,
                    display_name = excluded.display_name,
                    remote_workspace_id = excluded.remote_workspace_id,
                    auth_type = excluded.auth_type,
                    sync_mode = excluded.sync_mode,
                    status = excluded.status,
                    principal_sync_enabled = excluded.principal_sync_enabled,
                    acl_sync_enabled = excluded.acl_sync_enabled,
                    freshness_sla_seconds = excluded.freshness_sla_seconds,
                    metadata_json = excluded.metadata_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    last_synced_at = excluded.last_synced_at,
                    last_webhook_at = excluded.last_webhook_at
                """,
                (
                    connector.connector_id,
                    connector.tenant_id,
                    connector.provider.value,
                    connector.display_name,
                    connector.remote_workspace_id,
                    connector.auth_type.value,
                    connector.sync_mode.value,
                    connector.status.value,
                    1 if connector.principal_sync_enabled else 0,
                    1 if connector.acl_sync_enabled else 0,
                    connector.freshness_sla_seconds,
                    self._to_json(connector.metadata),
                    created_at,
                    updated_at,
                    self._to_iso(connector.last_synced_at),
                    self._to_iso(connector.last_webhook_at),
                ),
            )
            self._insert_audit(
                "connector_saved",
                None,
                connector.tenant_id,
                {"connector_id": connector.connector_id, "provider": connector.provider.value},
            )
        return self.get_connector(connector.connector_id, connector.tenant_id) or connector

    def list_connectors(self, tenant_id: str, provider: str | None = None) -> list[ConnectorConfig]:
        if provider:
            rows = self.conn.execute(
                "SELECT * FROM connectors WHERE tenant_id = ? AND provider = ? ORDER BY updated_at DESC",
                (tenant_id, provider),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM connectors WHERE tenant_id = ? ORDER BY updated_at DESC",
                (tenant_id,),
            ).fetchall()
        return [self._row_to_connector(row) for row in rows]

    def list_all_connectors(self, provider: str | None = None) -> list[ConnectorConfig]:
        if provider:
            rows = self.conn.execute(
                "SELECT * FROM connectors WHERE provider = ? ORDER BY tenant_id ASC, updated_at DESC",
                (provider,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM connectors ORDER BY tenant_id ASC, updated_at DESC",
            ).fetchall()
        return [self._row_to_connector(row) for row in rows]

    def get_connector(self, connector_id: str, tenant_id: str) -> ConnectorConfig | None:
        row = self.conn.execute(
            "SELECT * FROM connectors WHERE connector_id = ? AND tenant_id = ?",
            (connector_id, tenant_id),
        ).fetchone()
        return self._row_to_connector(row) if row is not None else None

    def save_connector_sources(self, connector_id: str, tenant_id: str, batch: ConnectorSourceBatch) -> list[ConnectorSourceRecord]:
        now = self._iso_now()
        with self.conn:
            for source in batch.sources:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO connector_sources (
                        source_id, connector_id, tenant_id, remote_source_id, source_type,
                        display_name, path, status, last_synced_at, stale_after, acl_hash,
                        metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source.source_id,
                        connector_id,
                        tenant_id,
                        source.remote_source_id,
                        source.source_type,
                        source.display_name,
                        source.path,
                        source.status.value,
                        self._to_iso(source.last_synced_at),
                        self._to_iso(source.stale_after),
                        source.acl_hash,
                        self._to_json(source.metadata),
                        self._to_iso(source.created_at) or now,
                        self._to_iso(source.updated_at) or now,
                    ),
                )
            self._insert_audit(
                "connector_sources_saved",
                None,
                tenant_id,
                {"connector_id": connector_id, "count": len(batch.sources)},
            )
        return self.list_connector_sources(connector_id, tenant_id)

    def list_connector_sources(self, connector_id: str, tenant_id: str) -> list[ConnectorSourceRecord]:
        rows = self.conn.execute(
            "SELECT * FROM connector_sources WHERE connector_id = ? AND tenant_id = ? ORDER BY updated_at DESC",
            (connector_id, tenant_id),
        ).fetchall()
        return [self._row_to_connector_source(row) for row in rows]

    def save_principal_mappings(self, connector_id: str, tenant_id: str, batch: PrincipalMappingBatch) -> list[PrincipalMapping]:
        now = self._iso_now()
        with self.conn:
            for mapping in batch.mappings:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO principal_mappings (
                        mapping_id, connector_id, tenant_id, principal_type, local_principal_id,
                        remote_principal_id, remote_name, groups_json, last_synced_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mapping.mapping_id,
                        connector_id,
                        tenant_id,
                        mapping.principal_type,
                        mapping.local_principal_id,
                        mapping.remote_principal_id,
                        mapping.remote_name,
                        self._to_json(mapping.groups),
                        self._to_iso(mapping.last_synced_at) or now,
                        self._to_iso(mapping.created_at) or now,
                        self._to_iso(mapping.updated_at) or now,
                    ),
                )
            self._insert_audit(
                "principal_mappings_saved",
                None,
                tenant_id,
                {"connector_id": connector_id, "count": len(batch.mappings)},
            )
        return self.list_principal_mappings(connector_id, tenant_id)

    def list_principal_mappings(self, connector_id: str, tenant_id: str) -> list[PrincipalMapping]:
        rows = self.conn.execute(
            "SELECT * FROM principal_mappings WHERE connector_id = ? AND tenant_id = ? ORDER BY updated_at DESC",
            (connector_id, tenant_id),
        ).fetchall()
        return [self._row_to_principal_mapping(row) for row in rows]

    def save_source_permission_grants(
        self,
        connector_id: str,
        tenant_id: str,
        batch: SourcePermissionBatch,
    ) -> list[SourcePermissionGrant]:
        now = self._iso_now()
        with self.conn:
            for grant in batch.grants:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO source_permission_grants (
                        grant_id, source_id, connector_id, tenant_id, principal_type,
                        principal_id, permission_level, inherited, remote_permission_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        grant.grant_id,
                        grant.source_id,
                        connector_id,
                        tenant_id,
                        grant.principal_type,
                        grant.principal_id,
                        grant.permission_level.value,
                        1 if grant.inherited else 0,
                        grant.remote_permission_id,
                        self._to_iso(grant.created_at) or now,
                    ),
                )
            self._insert_audit(
                "source_permission_grants_saved",
                None,
                tenant_id,
                {"connector_id": connector_id, "count": len(batch.grants)},
            )
        return self.list_source_permission_grants(connector_id, tenant_id)

    def list_source_permission_grants(self, connector_id: str, tenant_id: str, source_id: str | None = None) -> list[SourcePermissionGrant]:
        if source_id:
            rows = self.conn.execute(
                """
                SELECT * FROM source_permission_grants
                WHERE connector_id = ? AND tenant_id = ? AND source_id = ?
                ORDER BY created_at DESC
                """,
                (connector_id, tenant_id, source_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM source_permission_grants
                WHERE connector_id = ? AND tenant_id = ?
                ORDER BY created_at DESC
                """,
                (connector_id, tenant_id),
            ).fetchall()
        return [self._row_to_source_permission_grant(row) for row in rows]

    def save_sync_job(self, connector_id: str, tenant_id: str, job: SyncJob) -> SyncJob:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO sync_jobs (
                    job_id, connector_id, tenant_id, job_type, status, cursor, stats_json,
                    error_message, started_at, finished_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    connector_id,
                    tenant_id,
                    job.job_type.value,
                    job.status.value,
                    job.cursor,
                    self._to_json(job.stats),
                    job.error_message,
                    self._to_iso(job.started_at),
                    self._to_iso(job.finished_at),
                    self._to_iso(job.created_at) or self._iso_now(),
                ),
            )
            self._insert_audit(
                "sync_job_saved",
                None,
                tenant_id,
                {"connector_id": connector_id, "job_id": job.job_id, "status": job.status.value},
            )
        return self.get_sync_job(job.job_id, connector_id, tenant_id) or job

    def get_sync_job(self, job_id: str, connector_id: str, tenant_id: str) -> SyncJob | None:
        row = self.conn.execute(
            "SELECT * FROM sync_jobs WHERE job_id = ? AND connector_id = ? AND tenant_id = ?",
            (job_id, connector_id, tenant_id),
        ).fetchone()
        return self._row_to_sync_job(row) if row is not None else None

    def list_sync_jobs(self, connector_id: str, tenant_id: str) -> list[SyncJob]:
        rows = self.conn.execute(
            "SELECT * FROM sync_jobs WHERE connector_id = ? AND tenant_id = ? ORDER BY created_at DESC",
            (connector_id, tenant_id),
        ).fetchall()
        return [self._row_to_sync_job(row) for row in rows]

    def integration_coverage(self, tenant_id: str) -> IntegrationCoverageSummary:
        evaluated_at = datetime.now(UTC)
        connectors_total = int(
            self.conn.execute("SELECT COUNT(*) AS count FROM connectors WHERE tenant_id = ?", (tenant_id,)).fetchone()["count"]
        )
        connectors_healthy = int(
            self.conn.execute(
                "SELECT COUNT(*) AS count FROM connectors WHERE tenant_id = ? AND status = ?",
                (tenant_id, ConnectorStatus.ACTIVE.value),
            ).fetchone()["count"]
        )
        sources_total = int(
            self.conn.execute("SELECT COUNT(*) AS count FROM connector_sources WHERE tenant_id = ?", (tenant_id,)).fetchone()["count"]
        )
        sources_stale = int(
            self.conn.execute(
                "SELECT COUNT(*) AS count FROM connector_sources WHERE tenant_id = ? AND status = ?",
                (tenant_id, SourceSyncStatus.STALE.value),
            ).fetchone()["count"]
        )
        sources_error = int(
            self.conn.execute(
                "SELECT COUNT(*) AS count FROM connector_sources WHERE tenant_id = ? AND status = ?",
                (tenant_id, SourceSyncStatus.ERROR.value),
            ).fetchone()["count"]
        )
        freshness_rows = self.conn.execute(
            """
            SELECT cs.last_synced_at, cs.stale_after, c.freshness_sla_seconds
            FROM connector_sources AS cs
            LEFT JOIN connectors AS c
                ON c.connector_id = cs.connector_id
               AND c.tenant_id = cs.tenant_id
            WHERE cs.tenant_id = ?
            """,
            (tenant_id,),
        ).fetchall()
        sources_past_freshness_sla = sum(
            1
            for row in freshness_rows
            if (deadline := self._source_freshness_deadline(row)) is not None and deadline <= evaluated_at
        )
        principals_mapped = int(
            self.conn.execute("SELECT COUNT(*) AS count FROM principal_mappings WHERE tenant_id = ?", (tenant_id,)).fetchone()["count"]
        )
        grants_total = int(
            self.conn.execute(
                "SELECT COUNT(*) AS count FROM source_permission_grants WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()["count"]
        )
        sync_jobs_running = int(
            self.conn.execute(
                "SELECT COUNT(*) AS count FROM sync_jobs WHERE tenant_id = ? AND status = ?",
                (tenant_id, SyncJobStatus.RUNNING.value),
            ).fetchone()["count"]
        )
        last_sync_values = self.conn.execute(
            "SELECT MAX(last_synced_at) AS last_synced_at FROM connectors WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        missing_foundations: list[str] = []
        if connectors_total == 0:
            missing_foundations.append("connectors")
        if principals_mapped == 0:
            missing_foundations.append("principal_mappings")
        if grants_total == 0:
            missing_foundations.append("source_permission_grants")
        if sources_total == 0:
            missing_foundations.append("source_inventory")
        return IntegrationCoverageSummary(
            tenant_id=tenant_id,
            connectors_total=connectors_total,
            connectors_healthy=connectors_healthy,
            sources_total=sources_total,
            sources_stale=sources_stale,
            sources_past_freshness_sla=sources_past_freshness_sla,
            sources_error=sources_error,
            principals_mapped=principals_mapped,
            grants_total=grants_total,
            sync_jobs_running=sync_jobs_running,
            last_synced_at=self._from_iso(last_sync_values["last_synced_at"]) if last_sync_values else None,
            missing_foundations=missing_foundations,
        )

    def save_retention_policy(self, policy: RetentionPolicy) -> RetentionPolicy:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO retention_policies (
                    policy_id, tenant_id, kind, max_age_days, action, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    policy.policy_id,
                    policy.tenant_id,
                    policy.kind,
                    policy.max_age_days,
                    policy.action,
                    self._to_iso(policy.created_at) or self._iso_now(),
                ),
            )
        return policy

    def list_retention_policies(self, tenant_id: str | None = None) -> list[RetentionPolicy]:
        if tenant_id is None:
            rows = self.conn.execute(
                "SELECT * FROM retention_policies ORDER BY created_at DESC",
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM retention_policies WHERE tenant_id = ? ORDER BY created_at DESC",
                (tenant_id,),
            ).fetchall()
        return [
            RetentionPolicy(
                policy_id=row["policy_id"],
                tenant_id=row["tenant_id"],
                kind=row["kind"],
                max_age_days=int(row["max_age_days"]),
                action=row["action"],
                created_at=self._from_iso(row["created_at"]) or datetime.now(UTC),
            )
            for row in rows
        ]

    def place_legal_hold(self, hold: LegalHold) -> LegalHold:
        resolved_ids = self._resolve_legal_hold_targets(hold.tenant_id, hold.memory_ids, hold.scope)
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO legal_holds (
                    hold_id, tenant_id, memory_ids_json, scope_json, reason, hold_until, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hold.hold_id,
                    hold.tenant_id,
                    self._to_json(resolved_ids),
                    self._to_json(hold.scope) if hold.scope else None,
                    hold.reason,
                    self._to_iso(hold.hold_until),
                    self._to_iso(hold.created_at) or self._iso_now(),
                ),
            )
            self._refresh_hold_state(resolved_ids)
        return hold.model_copy(update={"memory_ids": resolved_ids})

    def release_legal_hold(self, hold_id: str, tenant_id: str | None = None) -> bool:
        row = self.conn.execute(
            "SELECT * FROM legal_holds WHERE hold_id = ?",
            (hold_id,),
        ).fetchone()
        if row is None:
            return False
        if tenant_id and row["tenant_id"] != tenant_id:
            return False
        memory_ids = self._hold_targets_from_row(row)
        with self.conn:
            self.conn.execute("DELETE FROM legal_holds WHERE hold_id = ?", (hold_id,))
            self._refresh_hold_state(memory_ids)
        return True

    def rtbf(self, payload: RTBFRequest) -> RTBFResponse:
        self._refresh_hold_state(
            [
                row["memory_id"]
                for row in self.conn.execute(
                    "SELECT memory_id FROM memories WHERE tenant_id = ?",
                    (payload.tenant_id,),
                ).fetchall()
            ]
        )
        rows = self.conn.execute(
            "SELECT memory_id, held FROM memories WHERE tenant_id = ?",
            (payload.tenant_id,),
        ).fetchall()
        held = [row["memory_id"] for row in rows if int(row["held"]) == 1]
        deletable = [row["memory_id"] for row in rows if int(row["held"]) == 0]
        if deletable:
            placeholders = ", ".join("?" for _ in deletable)
            with self.conn:
                self.conn.execute(f"DELETE FROM memories WHERE memory_id IN ({placeholders})", deletable)
        return RTBFResponse(deleted_memories=len(deletable), held_memories=held)

    def enforce_retention(self, tenant_id: str | None = None) -> RetentionEnforcementResponse:
        now = datetime.now(UTC)
        expired: list[str] = []
        tenant_rows = self.conn.execute(
            "SELECT memory_id FROM memories WHERE (? IS NULL OR tenant_id = ?)",
            (tenant_id, tenant_id),
        ).fetchall()
        self._refresh_hold_state([row["memory_id"] for row in tenant_rows])
        for policy in self.list_retention_policies(tenant_id):
            cutoff = now - timedelta(days=policy.max_age_days)
            rows = self.conn.execute(
                """
                SELECT memory_id, held FROM memories
                WHERE tenant_id = ?
                  AND (? IS NULL OR kind = ?)
                  AND datetime(created_at) < datetime(?)
                """,
                (tenant_id, policy.kind, policy.kind, cutoff.isoformat()),
            ).fetchall()
            eligible = [row["memory_id"] for row in rows if int(row["held"]) == 0]
            if not eligible:
                continue
            expired.extend(eligible)
            placeholders = ", ".join("?" for _ in eligible)
            with self.conn:
                if policy.action == "delete_soft":
                    self.conn.execute(
                        f"UPDATE memories SET status = ?, updated_at = ? WHERE memory_id IN ({placeholders})",
                        (MemoryStatus.DELETED.value, self._iso_now(), *eligible),
                    )
                else:
                    self.conn.execute(f"DELETE FROM memories WHERE memory_id IN ({placeholders})", eligible)
        return RetentionEnforcementResponse(expired_memory_ids=expired)

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        if row is None:
            raise ValueError("row is required")
        memory_id = row["memory_id"]
        return MemoryRecord(
            memory_id=memory_id,
            fingerprint=row["fingerprint"],
            kind=MemoryKind(row["kind"]),
            status=MemoryStatus(row["status"]),
            scope=ScopeEnvelope(
                tenant_id=row["tenant_id"],
                workspace_id=row["workspace_id"],
                project_id=row["project_id"],
                user_id=row["user_id"],
                agent_id=row["agent_id"],
                session_id=row["session_id"],
            ),
            title=row["title"],
            content=row["content"],
            summary=row["summary"],
            entity_keys=self._json_to_list(row["entity_keys_json"]),
            tags=self._json_to_list(row["tags_json"]),
            metadata=self._json_to_dict(row["metadata_json"]),
            importance=float(row["importance"]),
            confidence=float(row["confidence"]),
            strength=float(row["strength"]),
            valid_from=self._from_iso(row["valid_from"]),
            valid_to=self._from_iso(row["valid_to"]),
            created_at=self._from_iso(row["created_at"]) or datetime.now(UTC),
            updated_at=self._from_iso(row["updated_at"]) or datetime.now(UTC),
            last_verified_at=self._from_iso(row["last_verified_at"]),
            source_references=self._sources_for(memory_id),
            acl=[ACLEntry(**item) for item in self._json_to_list(row["acl_json"])],
            trigger_phrases=self._trigger_phrases_for(memory_id),
            embedding_model=row["embedding_model"],
            embedding=self._json_to_float_list(row["embedding_json"]),
            held=bool(row["held"]),
            hold_reason=row["hold_reason"],
            hold_until=self._from_iso(row["hold_until"]),
        )

    def _row_to_connector(self, row: sqlite3.Row) -> ConnectorConfig:
        return ConnectorConfig(
            connector_id=row["connector_id"],
            tenant_id=row["tenant_id"],
            provider=row["provider"],
            display_name=row["display_name"],
            remote_workspace_id=row["remote_workspace_id"],
            auth_type=row["auth_type"],
            sync_mode=row["sync_mode"],
            status=row["status"],
            principal_sync_enabled=bool(row["principal_sync_enabled"]),
            acl_sync_enabled=bool(row["acl_sync_enabled"]),
            freshness_sla_seconds=int(row["freshness_sla_seconds"]),
            metadata=self._json_to_dict(row["metadata_json"]),
            created_at=self._from_iso(row["created_at"]) or datetime.now(UTC),
            updated_at=self._from_iso(row["updated_at"]) or datetime.now(UTC),
            last_synced_at=self._from_iso(row["last_synced_at"]),
            last_webhook_at=self._from_iso(row["last_webhook_at"]),
        )

    def _row_to_connector_source(self, row: sqlite3.Row) -> ConnectorSourceRecord:
        return ConnectorSourceRecord(
            source_id=row["source_id"],
            connector_id=row["connector_id"],
            tenant_id=row["tenant_id"],
            remote_source_id=row["remote_source_id"],
            source_type=row["source_type"],
            display_name=row["display_name"],
            path=row["path"],
            status=row["status"],
            last_synced_at=self._from_iso(row["last_synced_at"]),
            stale_after=self._from_iso(row["stale_after"]),
            acl_hash=row["acl_hash"],
            metadata=self._json_to_dict(row["metadata_json"]),
            created_at=self._from_iso(row["created_at"]) or datetime.now(UTC),
            updated_at=self._from_iso(row["updated_at"]) or datetime.now(UTC),
        )

    def _row_to_principal_mapping(self, row: sqlite3.Row) -> PrincipalMapping:
        return PrincipalMapping(
            mapping_id=row["mapping_id"],
            connector_id=row["connector_id"],
            tenant_id=row["tenant_id"],
            principal_type=row["principal_type"],
            local_principal_id=row["local_principal_id"],
            remote_principal_id=row["remote_principal_id"],
            remote_name=row["remote_name"],
            groups=self._json_to_list(row["groups_json"]),
            last_synced_at=self._from_iso(row["last_synced_at"]) or datetime.now(UTC),
            created_at=self._from_iso(row["created_at"]) or datetime.now(UTC),
            updated_at=self._from_iso(row["updated_at"]) or datetime.now(UTC),
        )

    def _row_to_source_permission_grant(self, row: sqlite3.Row) -> SourcePermissionGrant:
        return SourcePermissionGrant(
            grant_id=row["grant_id"],
            source_id=row["source_id"],
            connector_id=row["connector_id"],
            tenant_id=row["tenant_id"],
            principal_type=row["principal_type"],
            principal_id=row["principal_id"],
            permission_level=PermissionLevel(row["permission_level"]),
            inherited=bool(row["inherited"]),
            remote_permission_id=row["remote_permission_id"],
            created_at=self._from_iso(row["created_at"]) or datetime.now(UTC),
        )

    def _row_to_sync_job(self, row: sqlite3.Row) -> SyncJob:
        return SyncJob(
            job_id=row["job_id"],
            connector_id=row["connector_id"],
            tenant_id=row["tenant_id"],
            job_type=row["job_type"],
            status=row["status"],
            cursor=row["cursor"],
            stats=self._json_to_dict(row["stats_json"]),
            error_message=row["error_message"],
            started_at=self._from_iso(row["started_at"]),
            finished_at=self._from_iso(row["finished_at"]),
            created_at=self._from_iso(row["created_at"]) or datetime.now(UTC),
        )

    def _default_acl(self, scope: ScopeEnvelope, access: AccessContext | None) -> list[ACLEntry]:
        principal_id = access.principal_id if access and access.principal_id else scope.user_id
        if not principal_id:
            return []
        return [
            ACLEntry(
                principal_id=principal_id,
                principal_type="user",
                permissions=[
                    ACLPermission.READ,
                    ACLPermission.WRITE,
                    ACLPermission.DELETE,
                    ACLPermission.SHARE,
                    ACLPermission.ADMIN,
                ],
            )
        ]

    def _can_access(
        self,
        record: MemoryRecord,
        access: AccessContext | None,
        required: ACLPermission,
    ) -> bool:
        if access is None:
            return True
        if access.role == "superadmin":
            return True
        if access.tenant_id and access.tenant_id != record.scope.tenant_id:
            return False
        if access.role == "admin":
            return True
        if not record.acl:
            return access.tenant_id in {None, record.scope.tenant_id}
        principal_ids = {access.principal_id, *(f"group:{group}" for group in access.groups)}
        for entry in record.acl:
            candidate = entry.principal_id
            if entry.principal_type == "group" and not candidate.startswith("group:"):
                candidate = f"group:{candidate}"
            if candidate in principal_ids and (
                required in entry.permissions or ACLPermission.ADMIN in entry.permissions
            ):
                return True
        return False

    def _can_read_memory(
        self,
        record: MemoryRecord,
        access: AccessContext | None,
    ) -> bool:
        if not self._can_access(record, access, ACLPermission.READ):
            return False
        return self._has_required_source_grants(record, access)

    def _has_required_source_grants(
        self,
        record: MemoryRecord,
        access: AccessContext | None,
    ) -> bool:
        source_rows = self._connected_source_rows(record)
        if not source_rows:
            return True
        if access is None:
            return False
        if access.role in {"admin", "superadmin"}:
            return True

        source_keys = list(
            dict.fromkeys((row["source_id"], row["connector_id"]) for row in source_rows)
        )
        source_ids = [source_id for source_id, _ in source_keys]
        permission_placeholders = ", ".join("?" for _ in READABLE_PERMISSION_LEVELS)
        source_placeholders = ", ".join("?" for _ in source_ids)
        grant_rows = self.conn.execute(
            f"""
            SELECT source_id, connector_id, principal_type, principal_id, permission_level
            FROM source_permission_grants
            WHERE tenant_id = ?
              AND source_id IN ({source_placeholders})
              AND permission_level IN ({permission_placeholders})
            """,
            (
                record.scope.tenant_id,
                *source_ids,
                *sorted(READABLE_PERMISSION_LEVELS),
            ),
        ).fetchall()
        grants_by_source: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in grant_rows:
            grants_by_source.setdefault((row["source_id"], row["connector_id"]), []).append(row)

        candidates_by_connector: dict[str, tuple[set[str], set[str]]] = {}
        for source_id, connector_id in source_keys:
            candidates = candidates_by_connector.get(connector_id)
            if candidates is None:
                candidates = self._read_principal_candidates(
                    record.scope.tenant_id,
                    connector_id,
                    access,
                )
                candidates_by_connector[connector_id] = candidates
            user_ids, group_ids = candidates
            if not any(
                self._source_grant_matches(grant, user_ids, group_ids)
                for grant in grants_by_source.get((source_id, connector_id), [])
            ):
                return False
        return True

    def _connected_source_rows(self, record: MemoryRecord) -> list[sqlite3.Row]:
        source_ids = list(
            dict.fromkeys(
                source.source_id
                for source in record.source_references
                if source.source_id
            )
        )
        if not source_ids:
            return []
        placeholders = ", ".join("?" for _ in source_ids)
        return self.conn.execute(
            f"""
            SELECT source_id, connector_id
            FROM connector_sources
            WHERE tenant_id = ?
              AND source_id IN ({placeholders})
            ORDER BY rowid ASC
            """,
            (record.scope.tenant_id, *source_ids),
        ).fetchall()

    def _read_principal_candidates(
        self,
        tenant_id: str,
        connector_id: str,
        access: AccessContext,
    ) -> tuple[set[str], set[str]]:
        user_ids = {access.principal_id} if access.principal_id else set()
        group_ids = {
            self._normalize_group_principal(group)
            for group in access.groups
            if group and group.strip()
        }
        if not access.principal_id:
            return user_ids, group_ids

        rows = self.conn.execute(
            """
            SELECT remote_principal_id, groups_json
            FROM principal_mappings
            WHERE tenant_id = ? AND connector_id = ? AND local_principal_id = ?
            ORDER BY updated_at DESC
            """,
            (tenant_id, connector_id, access.principal_id),
        ).fetchall()
        for row in rows:
            remote_principal_id = row["remote_principal_id"]
            if remote_principal_id:
                user_ids.add(remote_principal_id)
            for group in self._json_to_list(row["groups_json"]):
                if isinstance(group, str) and group.strip():
                    group_ids.add(self._normalize_group_principal(group))
        return user_ids, group_ids

    def _source_grant_matches(
        self,
        grant: sqlite3.Row,
        user_ids: set[str],
        group_ids: set[str],
    ) -> bool:
        principal_type = str(grant["principal_type"]).strip().lower()
        principal_id = str(grant["principal_id"]).strip()
        if principal_type == "user":
            return principal_id in user_ids
        if principal_type == "group":
            return self._normalize_group_principal(principal_id) in group_ids
        return False

    def _normalize_group_principal(self, principal_id: str) -> str:
        value = principal_id.strip()
        if value.startswith("group:"):
            return value.removeprefix("group:")
        return value

    def _score_record(
        self,
        record: MemoryRecord,
        scope: ScopeEnvelope,
        query_terms: set[str],
        fts_rank: float | None = None,
    ) -> tuple[float, list[str]]:
        haystack = " ".join(
            [
                record.title or "",
                record.summary or "",
                record.content,
                " ".join(record.tags),
                " ".join(record.entity_keys),
            ]
        ).lower()
        matches = sum(1 for term in query_terms if term in haystack)
        if query_terms and matches == 0:
            return 0.0, []

        reasons: list[str] = []
        score = float(matches)
        if fts_rank is not None:
            score += max(0.0, 1.0 - min(abs(fts_rank), 10.0) / 10.0)
            reasons.append("fts match")
        if record.scope.workspace_id and record.scope.workspace_id == scope.workspace_id:
            score += 0.4
            reasons.append("exact workspace scope")
        if record.scope.project_id and record.scope.project_id == scope.project_id:
            score += 0.6
            reasons.append("exact project scope")
        if record.scope.user_id and record.scope.user_id == scope.user_id:
            score += 0.8
            reasons.append("exact user scope")
        if record.scope.session_id and record.scope.session_id == scope.session_id:
            score += 1.2
            reasons.append("exact session scope")
        if record.status == MemoryStatus.ACTIVE:
            score += 0.15
        if record.status == MemoryStatus.SUPERSEDED:
            score -= 0.2
            reasons.append("superseded memory")
        score += min(record.importance, 1.0) * 0.25
        score += self._recency_bonus(record.updated_at)
        return score, reasons or ["content match"]

    def _candidate_rows(self, payload: SearchRequest) -> list[sqlite3.Row]:
        params: list[Any]
        if payload.query.strip():
            query = self._fts_query(payload.query)
            if query:
                try:
                    rows = self.conn.execute(
                        """
                        SELECT m.*, bm25(memories_fts) AS fts_rank
                        FROM memories_fts
                        JOIN memories AS m ON m.memory_id = memories_fts.memory_id
                        WHERE memories_fts MATCH ?
                          AND m.tenant_id = ?
                          AND (? = 1 OR m.status != ?)
                        ORDER BY bm25(memories_fts), datetime(m.updated_at) DESC
                        LIMIT ?
                        """,
                        (
                            query,
                            payload.scope.tenant_id,
                            1 if payload.include_deleted else 0,
                            MemoryStatus.DELETED.value,
                            max(payload.limit * 5, payload.limit),
                        ),
                    ).fetchall()
                    if rows:
                        return rows
                except sqlite3.OperationalError:
                    pass

        params = [
            payload.scope.tenant_id,
            1 if payload.include_deleted else 0,
            MemoryStatus.DELETED.value,
            max(payload.limit * 5, payload.limit),
        ]
        return self.conn.execute(
            """
            SELECT *, NULL AS fts_rank
            FROM memories
            WHERE tenant_id = ?
              AND (? = 1 OR status != ?)
            ORDER BY datetime(updated_at) DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    def _cosine_similarity(self, left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right, strict=False))
        left_norm = sum(a * a for a in left) ** 0.5
        right_norm = sum(b * b for b in right) ** 0.5
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return dot / (left_norm * right_norm)

    def _fts_query(self, query: str) -> str:
        tokens = [token.replace('"', "").strip() for token in self._query_terms(query)]
        tokens = [token for token in tokens if token]
        return " OR ".join(f'"{token}"' for token in tokens)

    def _related_memories(
        self,
        memory_id: str,
        access: AccessContext | None,
    ) -> list[RelatedMemory]:
        rows = self.conn.execute(
            """
            SELECT relation, to_memory_id FROM memory_relations WHERE from_memory_id = ?
            UNION ALL
            SELECT relation, from_memory_id AS to_memory_id FROM memory_relations WHERE to_memory_id = ?
            """,
            (memory_id, memory_id),
        ).fetchall()
        related: list[RelatedMemory] = []
        for row in rows:
            record = self.get_memory(
                row["to_memory_id"],
                access=access,
                enforce_source_grants=True,
            )
            if record is None:
                continue
            related.append(RelatedMemory(relation=RelationKind(row["relation"]), memory=record))
        return related

    def _source_freshness_deadline(self, row: sqlite3.Row) -> datetime | None:
        stale_after = self._from_iso(row["stale_after"])
        if stale_after is not None:
            return stale_after

        last_synced_at = self._from_iso(row["last_synced_at"])
        if last_synced_at is None:
            return None

        freshness_sla_seconds = row["freshness_sla_seconds"]
        if freshness_sla_seconds is None:
            return None

        try:
            freshness_sla_seconds = int(freshness_sla_seconds)
        except (TypeError, ValueError):
            return None

        if freshness_sla_seconds <= 0:
            return None

        return last_synced_at + timedelta(seconds=freshness_sla_seconds)

    def _sources_for(self, memory_id: str) -> list[SourceReference]:
        rows = self.conn.execute(
            "SELECT * FROM memory_sources WHERE memory_id = ? ORDER BY rowid ASC",
            (memory_id,),
        ).fetchall()
        return [
            SourceReference(
                source_type=row["source_type"],
                source_id=row["source_id"],
                uri=row["uri"],
                title=row["title"],
                excerpt=row["excerpt"],
                span_start=row["span_start"],
                span_end=row["span_end"],
                metadata=self._json_to_dict(row["metadata_json"]),
            )
            for row in rows
        ]

    def _trigger_phrases_for(self, memory_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT phrase FROM trigger_index WHERE memory_id = ? ORDER BY rowid ASC",
            (memory_id,),
        ).fetchall()
        return [row["phrase"] for row in rows]

    def _replace_trigger_phrases(self, memory_id: str, tenant_id: str, phrases: Iterable[str]) -> None:
        self.conn.execute("DELETE FROM trigger_index WHERE memory_id = ?", (memory_id,))
        created_at = self._iso_now()
        for phrase in dict.fromkeys(phrase.strip() for phrase in phrases if phrase.strip()):
            self.conn.execute(
                """
                INSERT INTO trigger_index (trigger_id, memory_id, phrase, tenant_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), memory_id, phrase, tenant_id, created_at),
            )

    def _insert_source(self, memory_id: str, source: SourceReference) -> None:
        self.conn.execute(
            """
            INSERT INTO memory_sources (
                source_ref_id, memory_id, source_type, source_id, uri, title,
                excerpt, span_start, span_end, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                memory_id,
                source.source_type,
                source.source_id,
                source.uri,
                source.title,
                source.excerpt,
                source.span_start,
                source.span_end,
                self._to_json(source.metadata),
            ),
        )

    def _insert_relation(self, from_memory_id: str, to_memory_id: str, relation: str, scope: ScopeEnvelope) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO memory_relations (
                relation_id, from_memory_id, to_memory_id, relation, tenant_id,
                workspace_id, project_id, user_id, agent_id, session_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                from_memory_id,
                to_memory_id,
                relation,
                scope.tenant_id,
                scope.workspace_id,
                scope.project_id,
                scope.user_id,
                scope.agent_id,
                scope.session_id,
                self._iso_now(),
            ),
        )

    def _index_memory(
        self,
        memory_id: str,
        title: str | None,
        summary: str | None,
        content: str,
        tags: list[str],
        entity_keys: list[str],
    ) -> None:
        self.conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
        self.conn.execute(
            """
            INSERT INTO memories_fts (memory_id, title, summary, content, tags, entity_keys)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (memory_id, title, summary, content, " ".join(tags), " ".join(entity_keys)),
        )

    def _insert_audit(self, action: str, memory_id: str | None, tenant_id: str | None, details: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO audit_log (audit_id, action, memory_id, actor_id, tenant_id, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                action,
                memory_id,
                None,
                tenant_id,
                self._to_json(details),
                self._iso_now(),
            ),
        )

    def _resolve_legal_hold_targets(
        self,
        tenant_id: str,
        memory_ids: list[str],
        scope: dict[str, Any] | None,
    ) -> list[str]:
        resolved = list(dict.fromkeys(memory_ids))
        if scope:
            resolved.extend(
                memory_id
                for memory_id in self._memory_ids_for_hold_scope(tenant_id, scope)
                if memory_id not in resolved
            )
        return resolved

    def _hold_targets_from_row(self, row: sqlite3.Row) -> list[str]:
        memory_ids = self._json_to_list(row["memory_ids_json"])
        scope = self._json_to_dict(row["scope_json"])
        return self._resolve_legal_hold_targets(row["tenant_id"], memory_ids, scope or None)

    def _memory_ids_for_hold_scope(self, tenant_id: str, scope: dict[str, Any]) -> list[str]:
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        for field in ("workspace_id", "project_id", "user_id", "agent_id", "session_id"):
            value = scope.get(field)
            if value:
                clauses.append(f"{field} = ?")
                params.append(value)
        rows = self.conn.execute(
            f"SELECT memory_id FROM memories WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()
        return [row["memory_id"] for row in rows]

    def _active_hold_rows_for_memory(self, record: MemoryRecord) -> list[sqlite3.Row]:
        now = datetime.now(UTC)
        rows = self.conn.execute(
            "SELECT * FROM legal_holds WHERE tenant_id = ?",
            (record.scope.tenant_id,),
        ).fetchall()
        active: list[sqlite3.Row] = []
        for row in rows:
            hold_until = self._from_iso(row["hold_until"])
            if hold_until and hold_until < now:
                continue
            explicit_ids = self._json_to_list(row["memory_ids_json"])
            scope = self._json_to_dict(row["scope_json"])
            if record.memory_id in explicit_ids or self._hold_scope_matches(scope, record.scope):
                active.append(row)
        return active

    def _hold_scope_matches(self, scope_filter: dict[str, Any], memory_scope: ScopeEnvelope) -> bool:
        if not scope_filter:
            return False
        for field in ("workspace_id", "project_id", "user_id", "agent_id", "session_id"):
            expected = scope_filter.get(field)
            if expected and getattr(memory_scope, field) != expected:
                return False
        return True

    def _refresh_hold_state(self, memory_ids: list[str]) -> None:
        unique_ids = list(dict.fromkeys(memory_ids))
        if not unique_ids:
            return
        rows = self.conn.execute(
            f"SELECT * FROM memories WHERE memory_id IN ({', '.join('?' for _ in unique_ids)})",
            unique_ids,
        ).fetchall()
        for row in rows:
            record = self._row_to_record(row)
            active_holds = self._active_hold_rows_for_memory(record)
            if active_holds:
                reasons = sorted({hold["reason"] for hold in active_holds if hold["reason"]})
                hold_until_values = [self._from_iso(hold["hold_until"]) for hold in active_holds]
                effective_hold_until = None if any(value is None for value in hold_until_values) else max(
                    value for value in hold_until_values if value is not None
                )
                pre_hold_status = row["pre_hold_status"] or (
                    row["status"] if row["status"] != MemoryStatus.HELD.value else MemoryStatus.ACTIVE.value
                )
                self.conn.execute(
                    """
                    UPDATE memories
                    SET held = 1,
                        hold_reason = ?,
                        hold_until = ?,
                        pre_hold_status = ?,
                        status = ?
                    WHERE memory_id = ?
                    """,
                    (
                        "; ".join(reasons) if reasons else None,
                        self._to_iso(effective_hold_until),
                        pre_hold_status,
                        MemoryStatus.HELD.value,
                        row["memory_id"],
                    ),
                )
            else:
                restored_status = row["pre_hold_status"] or (
                    MemoryStatus.ACTIVE.value if row["status"] == MemoryStatus.HELD.value else row["status"]
                )
                self.conn.execute(
                    """
                    UPDATE memories
                    SET held = 0,
                        hold_reason = NULL,
                        hold_until = NULL,
                        status = ?,
                        pre_hold_status = NULL
                    WHERE memory_id = ?
                    """,
                    (restored_status, row["memory_id"]),
                )

    def _erase_selector(self, payload: EraseRequest) -> tuple[str, list[str]]:
        clauses = ["tenant_id = ?"]
        params = [payload.tenant_id]
        if payload.workspace_id:
            clauses.append("workspace_id = ?")
            params.append(payload.workspace_id)
        if payload.project_id:
            clauses.append("project_id = ?")
            params.append(payload.project_id)
        if payload.user_id:
            clauses.append("user_id = ?")
            params.append(payload.user_id)
        return " AND ".join(clauses), params

    def _ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def _ensure_compatibility(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(memories)").fetchall()}
        migrations = {
            "embedding_model": "ALTER TABLE memories ADD COLUMN embedding_model TEXT",
            "embedding_json": "ALTER TABLE memories ADD COLUMN embedding_json TEXT",
            "acl_json": "ALTER TABLE memories ADD COLUMN acl_json TEXT NOT NULL DEFAULT '[]'",
            "held": "ALTER TABLE memories ADD COLUMN held INTEGER NOT NULL DEFAULT 0",
            "hold_reason": "ALTER TABLE memories ADD COLUMN hold_reason TEXT",
            "hold_until": "ALTER TABLE memories ADD COLUMN hold_until TEXT",
            "pre_hold_status": "ALTER TABLE memories ADD COLUMN pre_hold_status TEXT",
        }
        with self.conn:
            for column, statement in migrations.items():
                if column not in columns:
                    self.conn.execute(statement)

    def _iso_now(self) -> str:
        return datetime.now(UTC).isoformat()

    def _to_iso(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.replace(tzinfo=UTC).isoformat()

    def _from_iso(self, value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _to_json(self, value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), default=str)

    def _json_to_list(self, value: str | None) -> list[Any]:
        if not value:
            return []
        loaded = json.loads(value)
        return loaded if isinstance(loaded, list) else []

    def _json_to_dict(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}

    def _json_to_float_list(self, value: str | None) -> list[float]:
        return [float(item) for item in self._json_to_list(value)]

    def _query_terms(self, query: str) -> set[str]:
        return {term.lower() for term in query.replace(",", " ").split() if term.strip()}

    def _recency_bonus(self, updated_at: datetime) -> float:
        age = datetime.now(UTC) - updated_at.astimezone(UTC)
        if age <= timedelta(days=1):
            return 0.3
        if age <= timedelta(days=7):
            return 0.15
        if age <= timedelta(days=30):
            return 0.05
        return 0.0

    def _fingerprint(self, scope: ScopeEnvelope, kind: str, title: str | None, content: str) -> str:
        scope_json = self._to_json(scope.model_dump(exclude_none=True))
        value = "|".join([scope_json, kind, (title or "").strip().lower(), content.strip().lower()])
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _scope_matches(self, record_scope: ScopeEnvelope, request_scope: ScopeEnvelope) -> bool:
        if record_scope.tenant_id != request_scope.tenant_id:
            return False
        checks = [
            (record_scope.workspace_id, request_scope.workspace_id),
            (record_scope.project_id, request_scope.project_id),
            (record_scope.user_id, request_scope.user_id),
            (record_scope.agent_id, request_scope.agent_id),
            (record_scope.session_id, request_scope.session_id),
        ]
        for record_value, request_value in checks:
            if record_value and request_value and record_value != request_value:
                return False
        return True
