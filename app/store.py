from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import struct
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from pydantic import ValidationError
import rfc8785

from app.hot_cache import InMemoryHotCache, MemoryHotCache
from app.models import (
    ACLEntry,
    ACLPermission,
    AgentContextMemory,
    AgentContextRequest,
    AgentContextResponse,
    AuditEvent,
    ConnectorConfig,
    ConnectedSourceInspection,
    ConnectorSourceBatch,
    ConnectorSourceRecord,
    ConnectorStatus,
    DeleteResponse,
    EraseRequest,
    EraseResponse,
    IntegrationCoverageSummary,
    LegalHold,
    MemoryArchitectureOverview,
    MemoryCreate,
    MemoryFeedbackCreate,
    MemoryFeedbackRecord,
    MemoryFeedbackSummary,
    MemoryFeedbackType,
    MemoryHistoryEvent,
    MemoryHistoryEventType,
    MemoryInspectionResponse,
    MemoryKind,
    MemoryLayer,
    MemoryRecord,
    MemoryStorageState,
    MemoryStatus,
    MemoryUpdate,
    MemoryWriteResult,
    OnboardingStatus,
    OnboardingStep,
    PermissionLevel,
    PrincipalMapping,
    PrincipalMappingBatch,
    ProductionEvidenceMetric,
    ProductionEvidenceReport,
    ProductionUseCaseEvidence,
    ProjectSnapshot,
    RelatedMemory,
    RelationKind,
    RelationWrite,
    RepositoryMemoryEvent,
    RepositoryMemorySyncRequest,
    RepositoryMemorySyncResponse,
    RepositoryMemorySyncTimings,
    RTBFRequest,
    RTBFResponse,
    RetentionEnforcementResponse,
    RetentionPolicy,
    ScopeEnvelope,
    SearchRequest,
    SearchExplainCandidate,
    SearchExplainResponse,
    SearchResponse,
    SearchResult,
    StorageTierOverview,
    SourcePermissionBatch,
    SourcePermissionGrant,
    SourceReference,
    SourceSyncStatus,
    SyncJob,
    SyncJobStatus,
    TemporalGraphEdge,
    TemporalGraphNode,
    TemporalGraphRequest,
    TemporalGraphResponse,
)


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "storage" / "migrations" / "001_initial.sql"
READABLE_PERMISSION_LEVELS = frozenset(level.value for level in PermissionLevel)
COMMENTABLE_PERMISSION_LEVELS = frozenset(
    level.value for level in (PermissionLevel.COMMENT, PermissionLevel.EDIT, PermissionLevel.OWNER)
)
EDITABLE_PERMISSION_LEVELS = frozenset(
    level.value for level in (PermissionLevel.EDIT, PermissionLevel.OWNER)
)
OWNER_PERMISSION_LEVELS = frozenset({PermissionLevel.OWNER.value})
MEMORY_WRITE_ROLES = frozenset({"editor", "admin", "superadmin"})
REPOSITORY_LEDGER_PROJECTION_VERSION = 1
MAX_REPOSITORY_LEDGER_BYTES = 64 * 1024 * 1024
TENANT_OWNED_ID_TABLES = {
    "connector": ("connectors", "connector_id"),
    "connector_source": ("connector_sources", "source_id"),
    "principal_mapping": ("principal_mappings", "mapping_id"),
    "source_permission_grant": ("source_permission_grants", "grant_id"),
    "sync_job": ("sync_jobs", "job_id"),
    "retention_policy": ("retention_policies", "policy_id"),
    "legal_hold": ("legal_holds", "hold_id"),
    "entity": ("entity_registry", "entity_id"),
}


class RepositoryEventConflictError(ValueError):
    """Raised when an immutable repository event id is reused with new content."""


class TenantOwnershipConflictError(ValueError):
    """Raised when a globally unique resource id crosses a tenant boundary."""


@dataclass(slots=True)
class AccessContext:
    tenant_id: str | None = None
    role: str = "viewer"
    principal_id: str | None = None
    key_id: str | None = None
    groups: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchEvaluation:
    record: MemoryRecord
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    fts_rank: float | None = None
    vector_rank: float | None = None


@dataclass(slots=True)
class SearchCandidate:
    row: sqlite3.Row
    fts_rank: float | None = None
    vector_rank: float | None = None


_SEARCH_VECTOR_WEIGHT = 0.45


class ProvenaStore:
    def __init__(
        self,
        db_path: Path | str,
        hot_cache: MemoryHotCache | None = None,
        vector_dimensions: int = 768,
    ) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.hot_cache = hot_cache or InMemoryHotCache(ttl_seconds=300)
        self.vector_dimensions = vector_dimensions
        self._ensure_schema()
        self._ensure_compatibility()
        # Optional KNN fast path; falls back to a linear cosine scan if the
        # sqlite-vec extension can't be loaded in this environment.
        self.vec_enabled = False
        self._init_vector_index()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # Vector index (sqlite-vec) — optional KNN fast path
    # ------------------------------------------------------------------

    def _init_vector_index(self) -> bool:
        """Load sqlite-vec and create + backfill the KNN table.

        Returns False (and leaves search on the linear-scan fallback) if the
        extension can't be loaded — e.g. a Python build without loadable
        extension support. Never raises: the index is an accelerator, not a
        requirement.
        """
        try:
            import sqlite_vec

            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            self.conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
                    memory_id TEXT PRIMARY KEY,
                    embedding float[{self.vector_dimensions}] distance_metric=cosine
                )
                """
            )
            self.vec_enabled = True
            self._backfill_vector_index()
            return True
        except Exception:
            return False

    def _backfill_vector_index(self) -> None:
        """Populate the vec table from any embeddings already in `memories`."""
        existing = {row[0] for row in self.conn.execute("SELECT memory_id FROM memories_vec")}
        rows = self.conn.execute(
            "SELECT memory_id, embedding_json FROM memories "
            "WHERE embedding_json IS NOT NULL AND embedding_json NOT IN ('[]', 'null', '')"
        ).fetchall()
        with self.conn:
            for row in rows:
                if row["memory_id"] in existing:
                    continue
                self._vec_upsert(row["memory_id"], self._json_to_float_list(row["embedding_json"]))

    def _vec_upsert(self, memory_id: str, embedding: list[float] | None) -> None:
        """Mirror an embedding into the vec index (delete-then-insert; vec0
        rejects INSERT OR REPLACE). No-op unless the index is enabled and the
        embedding matches the configured dimension."""
        if not self.vec_enabled:
            return
        if not embedding or len(embedding) != self.vector_dimensions:
            self.conn.execute("DELETE FROM memories_vec WHERE memory_id = ?", (memory_id,))
            return
        packed = struct.pack(f"{len(embedding)}f", *embedding)
        self.conn.execute("DELETE FROM memories_vec WHERE memory_id = ?", (memory_id,))
        self.conn.execute(
            "INSERT INTO memories_vec(memory_id, embedding) VALUES (?, ?)",
            (memory_id, packed),
        )

    def _vec_delete(self, memory_id: str) -> None:
        if not self.vec_enabled:
            return
        self.conn.execute("DELETE FROM memories_vec WHERE memory_id = ?", (memory_id,))

    def _purge_memory_indexes(self, memory_ids: Iterable[str]) -> None:
        for chunk in self._chunks(memory_ids):
            placeholders = ", ".join("?" for _ in chunk)
            self.conn.execute(
                f"DELETE FROM memories_fts WHERE memory_id IN ({placeholders})",
                chunk,
            )
            for memory_id in chunk:
                self._vec_delete(memory_id)

    def _delete_memory_rows(self, memory_ids: Iterable[str]) -> None:
        for chunk in self._chunks(memory_ids):
            placeholders = ", ".join("?" for _ in chunk)
            self.conn.execute(
                f"DELETE FROM memories WHERE memory_id IN ({placeholders})",
                chunk,
            )

    def create_memory(
        self,
        payload: MemoryCreate,
        access: AccessContext | None = None,
    ) -> MemoryWriteResult:
        if (
            access is not None
            and access.role != "superadmin"
            and access.tenant_id not in {None, payload.scope.tenant_id}
        ):
            raise ValueError("memory scope tenant does not match access tenant")
        fingerprint = self._fingerprint(
            payload.scope,
            payload.kind.value,
            payload.title,
            payload.content,
            payload.metadata,
        )
        now = self._iso_now()
        memory_id = payload.memory_id or str(uuid.uuid4())
        acl = payload.acl or self._default_acl(payload.scope, access)
        memory_layer = payload.memory_layer or self._infer_memory_layer(payload.scope)
        source_authorization_record = MemoryRecord(
            **payload.model_dump(
                exclude={"acl", "memory_id", "memory_layer", "supersedes_memory_id"}
            ),
            memory_id=memory_id,
            fingerprint=fingerprint,
            status=MemoryStatus.ACTIVE,
            memory_layer=memory_layer,
            created_at=now,
            updated_at=now,
            acl=self._default_acl(payload.scope, access),
        )
        source_write_allowed = not self._connected_source_rows(
            source_authorization_record
        ) or self._can_access_memory(
            source_authorization_record,
            access,
            ACLPermission.WRITE,
            source_permission_levels=EDITABLE_PERMISSION_LEVELS,
            allowed_roles=MEMORY_WRITE_ROLES,
        )
        existing = self.conn.execute(
            "SELECT * FROM memories WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if existing:
            old_record = self._row_to_record(existing)
            if not self._can_read_memory(old_record, access):
                raise ValueError("memory not found")
            if not source_write_allowed:
                raise ValueError("connected source write access denied")
            if existing["status"] == MemoryStatus.DELETED.value:
                raise ValueError(
                    "memory fingerprint is tombstoned; deleted memories require an explicit restore workflow"
                )
            record = self.get_memory(
                existing["memory_id"],
                access=access,
                enforce_source_grants=True,
            )
            if record is None:
                raise ValueError("memory not found")
            # A retry after an invalidation backend failure must still advance
            # the generation even when the durable write deduplicates.
            self._invalidate_tenant_cache(payload.scope.tenant_id)
            return MemoryWriteResult(created=False, memory=record)

        if not source_write_allowed:
            raise ValueError("connected source write access denied")
        if payload.supersedes_memory_id:
            self._validate_supersession_target(
                payload.supersedes_memory_id,
                memory_id,
                payload.scope.tenant_id,
                access,
            )

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO memories (
                    memory_id, fingerprint, kind, status, tenant_id, workspace_id,
                    project_id, user_id, agent_id, session_id, title, content, summary,
                    entity_keys_json, tags_json, metadata_json, importance, confidence,
                    strength, valid_from, valid_to, expires_at, created_at, updated_at,
                    memory_layer,
                    embedding_model, embedding_json, acl_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    self._to_iso(payload.expires_at),
                    now,
                    now,
                    memory_layer.value,
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
            row = self.conn.execute("SELECT * FROM memories WHERE memory_id = ?", (memory_id,)).fetchone()
            if row is None:
                raise RuntimeError("created memory disappeared before transaction commit")
            record = self._row_to_record(row)
            self._insert_history(
                memory_id,
                record.scope.tenant_id,
                MemoryHistoryEventType.ADD,
                old_memory={},
                new_memory=record.model_dump(mode="json"),
                details={"kind": payload.kind.value, "memory_layer": memory_layer.value},
                actor_id=access.principal_id if access else None,
            )
            self._vec_upsert(memory_id, record.embedding)
        self._invalidate_tenant_cache(payload.scope.tenant_id)
        return MemoryWriteResult(created=True, memory=record)

    def sync_repository_memory_events(
        self,
        repository_id: str,
        payload: RepositoryMemorySyncRequest,
        access: AccessContext | None = None,
    ) -> RepositoryMemorySyncResponse:
        started = time.perf_counter()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}", repository_id):
            raise ValueError("repository_id must be a stable identifier")
        if (
            access is not None
            and access.role != "superadmin"
            and access.tenant_id not in {None, payload.scope.tenant_id}
        ):
            raise ValueError("repository scope tenant does not match access tenant")

        raw_ledger = payload.ledger.encode("utf-8")
        if len(raw_ledger) > MAX_REPOSITORY_LEDGER_BYTES:
            raise ValueError("repository memory ledger exceeds the 67108864 byte safety cap")
        if len(raw_ledger) != payload.ledger_bytes:
            raise ValueError("ledger_bytes does not match the exact UTF-8 ledger bytes")
        ledger_fingerprint = hashlib.sha256(raw_ledger).hexdigest()
        if ledger_fingerprint != payload.memory_fingerprint:
            raise ValueError("memory_fingerprint does not match the exact ledger bytes")

        parsed_events: list[tuple[RepositoryMemoryEvent, dict[str, Any], str]] = []
        event_ids: set[str] = set()
        canonical_events = bytearray()
        for line_number, line in enumerate(payload.ledger.split("\n"), start=1):
            if line.endswith("\r"):
                line = line[:-1]
            if not line.strip():
                continue
            try:
                record = json.loads(line, object_pairs_hook=self._repository_json_object)
            except (json.JSONDecodeError, RecursionError) as exc:
                raise ValueError(f"repository memory ledger line {line_number} is invalid JSON") from exc
            except ValueError as exc:
                raise ValueError(
                    f"repository memory ledger line {line_number} contains duplicate JSON object keys"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(f"repository memory ledger line {line_number} must be an object")
            try:
                event = RepositoryMemoryEvent.model_validate(record)
            except ValidationError as exc:
                first = exc.errors(include_url=False)[0]
                location = ".".join(str(part) for part in first.get("loc", ())) or "event"
                raise ValueError(
                    f"repository memory ledger line {line_number} has invalid {location}: {first['msg']}"
                ) from exc
            except RecursionError as exc:
                raise ValueError(
                    f"repository memory ledger line {line_number} exceeds the maximum JSON nesting depth"
                ) from exc
            if event.id in event_ids:
                raise ValueError(f"repository memory ledger contains duplicate event id {event.id}")
            event_ids.add(event.id)
            try:
                canonical = self._canonical_repository_json(record)
            except rfc8785.CanonicalizationError as exc:
                raise ValueError(
                    f"repository memory ledger line {line_number} cannot be canonicalized as RFC 8785 JSON"
                ) from exc
            if canonical != line:
                raise ValueError(
                    f"repository memory ledger line {line_number} must be canonical RFC 8785 JSON"
                )
            event_fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            canonical_events.extend(canonical.encode("utf-8"))
            canonical_events.extend(b"\n")
            parsed_events.append((event, record, event_fingerprint))

        events_fingerprint = hashlib.sha256(canonical_events).hexdigest()
        validation_ms = self._elapsed_ms(started)
        project_id = payload.scope.project_id
        projection_scope = ScopeEnvelope(
            tenant_id=payload.scope.tenant_id,
            project_id=project_id,
        )
        event_count = len(parsed_events)
        now = self._iso_now()
        created_memories = 0
        unchanged_memories = 0
        repaired_memories = 0
        suppressed_events = 0
        status_updates = 0
        created_relations = 0
        unchanged_relations = 0
        suppressed_relations = 0
        write_started = time.perf_counter()

        with self.conn:
            # This is deliberately the first statement in the transaction. An
            # upsert on one scoped lock row serializes concurrent first-syncs
            # in SQLite (database writer lock) and PostgreSQL (row conflict).
            self.conn.execute(
                """
                INSERT INTO repo_memory_sync_locks (
                    tenant_id, project_id, repository_id, locked_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (tenant_id, project_id, repository_id) DO UPDATE SET
                    locked_at = excluded.locked_at
                """,
                (payload.scope.tenant_id, project_id, repository_id, now),
            )
            state = self.conn.execute(
                """
                SELECT projection_version, ledger_path, ledger_fingerprint,
                       events_fingerprint, ledger_bytes, event_count
                FROM repo_memory_sync_state
                WHERE tenant_id = ? AND project_id = ? AND repository_id = ?
                """,
                (payload.scope.tenant_id, project_id, repository_id),
            ).fetchone()
            checkpoint_matches = bool(
                state is not None
                and int(state["projection_version"]) == REPOSITORY_LEDGER_PROJECTION_VERSION
                and state["ledger_path"] == payload.ledger_path
                and state["ledger_fingerprint"] == ledger_fingerprint
                and state["events_fingerprint"] == events_fingerprint
                and int(state["ledger_bytes"]) == payload.ledger_bytes
                and int(state["event_count"]) == event_count
            )

            all_referenced_ids = event_ids.union(
                target_id
                for event, _, _ in parsed_events
                for target_id in event.supersedes
            )
            mappings: dict[str, Any] = {}
            for id_chunk in self._chunks(sorted(all_referenced_ids)):
                placeholders = ", ".join("?" for _ in id_chunk)
                rows = self.conn.execute(
                    f"""
                    SELECT p.event_id, p.event_fingerprint, p.projection_version,
                           p.memory_id, m.fingerprint, m.kind, m.status,
                           m.tenant_id, m.workspace_id, m.project_id, m.user_id,
                           m.agent_id, m.session_id, m.title, m.content,
                           m.entity_keys_json, m.tags_json, m.metadata_json,
                           m.importance, m.confidence, m.valid_from, m.valid_to,
                           m.updated_at, m.memory_layer, m.acl_json, m.held,
                           m.pre_hold_status
                    FROM repo_memory_event_projections AS p
                    LEFT JOIN memories AS m ON m.memory_id = p.memory_id
                    WHERE p.tenant_id = ? AND p.project_id = ? AND p.repository_id = ?
                      AND p.event_id IN ({placeholders})
                    """,
                    (
                        payload.scope.tenant_id,
                        project_id,
                        repository_id,
                        *id_chunk,
                    ),
                ).fetchall()
                mappings.update({row["event_id"]: row for row in rows})

            erasures: dict[str, Any] = {}
            for id_chunk in self._chunks(sorted(all_referenced_ids)):
                placeholders = ", ".join("?" for _ in id_chunk)
                rows = self.conn.execute(
                    f"""
                    SELECT event_id, event_fingerprint, authority, event_created_at
                    FROM repo_memory_event_erasures
                    WHERE tenant_id = ? AND project_id = ? AND repository_id = ?
                      AND event_id IN ({placeholders})
                    """,
                    (
                        payload.scope.tenant_id,
                        project_id,
                        repository_id,
                        *id_chunk,
                    ),
                ).fetchall()
                erasures.update({row["event_id"]: row for row in rows})

            restored_mappings: set[str] = set()
            for event, record, event_fingerprint in parsed_events:
                if event.id in mappings or event.id in erasures:
                    continue
                memory_id = self._repository_memory_id(projection_scope, repository_id, event.id)
                collision = self.conn.execute(
                    "SELECT * FROM memories WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()
                if collision is None:
                    continue
                attested = self._attested_repository_projection(
                    memory_id,
                    row=collision,
                    expected_scope=projection_scope,
                    expected_repository_id=repository_id,
                    expected_event_record=record,
                    expected_event_fingerprint=event_fingerprint,
                )
                if attested is None:
                    continue
                self.conn.execute(
                    """
                    INSERT INTO repo_memory_event_projections (
                        tenant_id, project_id, repository_id, event_id,
                        event_fingerprint, projection_version, memory_id,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload.scope.tenant_id,
                        project_id,
                        repository_id,
                        event.id,
                        event_fingerprint,
                        REPOSITORY_LEDGER_PROJECTION_VERSION,
                        memory_id,
                        now,
                        now,
                    ),
                )
                restored = dict(collision)
                restored.update(
                    {
                        "event_id": event.id,
                        "event_fingerprint": event_fingerprint,
                        "projection_version": REPOSITORY_LEDGER_PROJECTION_VERSION,
                    }
                )
                mappings[event.id] = restored
                restored_mappings.add(event.id)
                self._insert_audit(
                    "repository_memory_event_mapping_restored",
                    memory_id,
                    payload.scope.tenant_id,
                    {"repository_id": repository_id, "event_id": event.id},
                )

            parsed_by_id = {event.id: event for event, _, _ in parsed_events}
            batch_position = {
                event.id: position for position, (event, _, _) in enumerate(parsed_events)
            }
            authority_rank = {"tool": 1, "agent": 2, "system": 3, "human": 4}

            for position, (event, _, event_fingerprint) in enumerate(parsed_events):
                mapping = mappings.get(event.id)
                if mapping is not None and mapping["event_fingerprint"] != event_fingerprint:
                    raise RepositoryEventConflictError(
                        f"repository event {event.id} is immutable and was already projected with different content"
                    )
                erasure = erasures.get(event.id)
                if erasure is not None and erasure["event_fingerprint"] != event_fingerprint:
                    raise RepositoryEventConflictError(
                        f"erased repository event {event.id} is immutable and cannot be replaced"
                    )
                for target_id in event.supersedes:
                    target_position = batch_position.get(target_id)
                    if target_position is not None and target_position >= position:
                        raise ValueError(
                            f"repository event {event.id} must supersede an earlier ledger event, not {target_id}"
                        )
                    target_event = parsed_by_id.get(target_id)
                    target_mapping = mappings.get(target_id)
                    target_erasure = erasures.get(target_id)
                    if target_event is not None:
                        target_authority = target_event.authority.value
                        target_created_at = target_event.created_at
                    elif target_mapping is not None:
                        if target_mapping["metadata_json"] is None:
                            raise RepositoryEventConflictError(
                                f"repository event {target_id} has a projection mapping without a memory"
                            )
                        target_metadata = self._json_to_dict(target_mapping["metadata_json"])
                        try:
                            stored_target = RepositoryMemoryEvent.model_validate(
                                target_metadata.get("provena_event")
                            )
                        except ValidationError as exc:
                            raise RepositoryEventConflictError(
                                f"repository event {target_id} has invalid stored provenance"
                            ) from exc
                        target_authority = stored_target.authority.value
                        target_created_at = stored_target.created_at
                    elif target_erasure is not None:
                        target_authority = target_erasure["authority"]
                        target_created_at = target_erasure["event_created_at"]
                    else:
                        raise ValueError(
                            f"repository event {event.id} supersedes unknown event id {target_id}"
                        )
                    if authority_rank[event.authority.value] < authority_rank[target_authority]:
                        raise ValueError(
                            f"{event.authority.value} repository event {event.id} cannot supersede "
                            f"{target_authority} event {target_id}"
                        )
                    if self._from_iso(event.created_at) < self._from_iso(target_created_at):
                        raise ValueError(
                            f"repository event {event.id} cannot supersede newer event {target_id}"
                        )

            referenced_at: dict[str, str] = {}
            referenced_status: dict[str, MemoryStatus] = {}
            for event, _, _ in parsed_events:
                for target_id in event.supersedes:
                    existing_time = referenced_at.get(target_id)
                    if existing_time is None or event.created_at < existing_time:
                        referenced_at[target_id] = event.created_at
                    desired = (
                        MemoryStatus.RETRACTED
                        if event.status.value == MemoryStatus.RETRACTED.value
                        else MemoryStatus.SUPERSEDED
                    )
                    if desired == MemoryStatus.RETRACTED or target_id not in referenced_status:
                        referenced_status[target_id] = desired

            for event, record, event_fingerprint in parsed_events:
                if event.id in erasures:
                    suppressed_events += 1
                    continue

                mapping = mappings.get(event.id)
                memory_id = self._repository_memory_id(projection_scope, repository_id, event.id)
                status, pre_hold_status = self._repository_projection_status(
                    event,
                    referenced_status.get(event.id),
                    mapping["status"] if mapping is not None else None,
                    mapping["pre_hold_status"] if mapping is not None else None,
                )
                valid_to = referenced_at.get(event.id)
                if event.status.value != MemoryStatus.ACTIVE.value:
                    valid_to = min(
                        filter(None, [valid_to, event.updated_at]),
                        default=event.updated_at,
                    )
                if (
                    mapping is not None
                    and mapping["valid_to"] is not None
                    and mapping["status"] in {
                        MemoryStatus.SUPERSEDED.value,
                        MemoryStatus.RETRACTED.value,
                        MemoryStatus.HELD.value,
                    }
                    and (valid_to is None or self._from_iso(mapping["valid_to"]) < self._from_iso(valid_to))
                ):
                    valid_to = mapping["valid_to"]
                entity_keys = self._repository_event_entity_keys(repository_id, event)
                tags = sorted(
                    {
                        *event.tags,
                        "provena-ledger",
                        f"authority:{event.authority.value}",
                        f"sensitivity:{event.sensitivity.value}",
                    },
                    key=self._repository_text_sort_key,
                )
                existing_metadata = (
                    self._json_to_dict(mapping["metadata_json"])
                    if mapping is not None and mapping["metadata_json"] is not None
                    else {}
                )
                metadata = {
                    "provena_projection": "repo-ledger",
                    "provena_projection_version": REPOSITORY_LEDGER_PROJECTION_VERSION,
                    "provena_repository_id": repository_id,
                    "provena_event": record,
                    "provena_event_fingerprint": event_fingerprint,
                    "provena_ingested_at": existing_metadata.get("provena_ingested_at", now),
                }
                fingerprint = self._repository_projection_fingerprint(
                    projection_scope,
                    repository_id,
                    event_fingerprint,
                )
                expected_sources = self._repository_event_sources(
                    repository_id,
                    payload.ledger_path,
                    event,
                    event_fingerprint,
                )
                if mapping is not None:
                    if mapping["metadata_json"] is None:
                        raise RepositoryEventConflictError(
                            f"repository event {event.id} has a projection mapping without a memory"
                        )
                    if mapping["memory_id"] != memory_id:
                        raise RepositoryEventConflictError(
                            f"repository event {event.id} uses a legacy or cross-scope memory id"
                        )
                    actual_sources = self._sources_for(memory_id)
                    actual_triggers = self._trigger_rows_for(memory_id)
                    source_changed = sorted(
                        self._canonical_repository_json(item.model_dump(mode="json", exclude_none=True))
                        for item in actual_sources
                    ) != sorted(
                        self._canonical_repository_json(item.model_dump(mode="json", exclude_none=True))
                        for item in expected_sources
                    )
                    triggers_changed = actual_triggers != [
                        (phrase, payload.scope.tenant_id)
                        for phrase in sorted(event.triggers, key=self._repository_text_sort_key)
                    ]
                    index_changed = not self._memory_index_matches(
                        memory_id,
                        event.title,
                        None,
                        event.body,
                        tags,
                        entity_keys,
                    )
                    status_changed = (
                        mapping["status"] != status.value
                        or mapping["pre_hold_status"]
                        != (pre_hold_status.value if pre_hold_status is not None else None)
                    )
                    projection_changed = any(
                        [
                            int(mapping["projection_version"]) != REPOSITORY_LEDGER_PROJECTION_VERSION,
                            mapping["fingerprint"] != fingerprint,
                            mapping["kind"] != event.kind.value,
                            mapping["tenant_id"] != payload.scope.tenant_id,
                            mapping["workspace_id"] is not None,
                            mapping["project_id"] != project_id,
                            mapping["user_id"] is not None,
                            mapping["agent_id"] is not None,
                            mapping["session_id"] is not None,
                            mapping["title"] != event.title,
                            mapping["content"] != event.body,
                            self._json_to_list(mapping["entity_keys_json"]) != entity_keys,
                            self._json_to_list(mapping["tags_json"]) != tags,
                            existing_metadata != metadata,
                            float(mapping["importance"]) != event.importance,
                            float(mapping["confidence"]) != event.confidence,
                            mapping["valid_from"] != event.created_at,
                            mapping["valid_to"] != valid_to,
                            mapping["memory_layer"] != MemoryLayer.ORGANIZATION.value,
                            self._json_to_list(mapping["acl_json"]) != [],
                            status_changed,
                            source_changed,
                            triggers_changed,
                            index_changed,
                        ]
                    )
                    if not projection_changed:
                        if event.id in restored_mappings:
                            repaired_memories += 1
                        else:
                            unchanged_memories += 1
                        continue
                    old_projection = self._row_to_record(
                        self.conn.execute(
                            "SELECT * FROM memories WHERE memory_id = ?",
                            (memory_id,),
                        ).fetchone()
                    )
                    self.conn.execute(
                        """
                        UPDATE memories SET
                            fingerprint = ?, kind = ?, status = ?, tenant_id = ?,
                            workspace_id = ?, project_id = ?, user_id = ?, agent_id = ?,
                            session_id = ?, title = ?, content = ?,
                            entity_keys_json = ?, tags_json = ?, metadata_json = ?,
                            importance = ?, confidence = ?, valid_from = ?, valid_to = ?,
                            updated_at = ?, memory_layer = ?, acl_json = ?,
                            pre_hold_status = ?
                        WHERE memory_id = ?
                        """,
                        (
                            fingerprint,
                            event.kind.value,
                            status.value,
                            payload.scope.tenant_id,
                            None,
                            project_id,
                            None,
                            None,
                            None,
                            event.title,
                            event.body,
                            self._to_json(entity_keys),
                            self._to_json(tags),
                            self._to_json(metadata),
                            event.importance,
                            event.confidence,
                            event.created_at,
                            valid_to,
                            now,
                            MemoryLayer.ORGANIZATION.value,
                            self._to_json([]),
                            pre_hold_status.value if pre_hold_status is not None else None,
                            memory_id,
                        ),
                    )
                    if source_changed:
                        self.conn.execute("DELETE FROM memory_sources WHERE memory_id = ?", (memory_id,))
                        for source in expected_sources:
                            self._insert_source(memory_id, source)
                    if triggers_changed:
                        self._replace_trigger_phrases(
                            memory_id,
                            payload.scope.tenant_id,
                            event.triggers,
                        )
                    self._index_memory(memory_id, event.title, None, event.body, tags, entity_keys)
                    self.conn.execute(
                        """
                        UPDATE repo_memory_event_projections
                        SET projection_version = ?, last_seen_at = ?
                        WHERE tenant_id = ? AND project_id = ? AND repository_id = ? AND event_id = ?
                        """,
                        (
                            REPOSITORY_LEDGER_PROJECTION_VERSION,
                            now,
                            payload.scope.tenant_id,
                            project_id,
                            repository_id,
                            event.id,
                        ),
                    )
                    new_projection = self._row_to_record(
                        self.conn.execute(
                            "SELECT * FROM memories WHERE memory_id = ?",
                            (memory_id,),
                        ).fetchone()
                    )
                    self._insert_history(
                        memory_id,
                        payload.scope.tenant_id,
                        MemoryHistoryEventType.UPDATE,
                        old_memory=old_projection.model_dump(mode="json"),
                        new_memory=new_projection.model_dump(mode="json"),
                        details={
                            "repository_id": repository_id,
                            "event_id": event.id,
                            "reason": "projection_reconciled",
                            "projection_version": REPOSITORY_LEDGER_PROJECTION_VERSION,
                        },
                        actor_id=access.principal_id if access else None,
                    )
                    self._insert_audit(
                        "repository_memory_event_reconciled",
                        memory_id,
                        payload.scope.tenant_id,
                        {"repository_id": repository_id, "event_id": event.id},
                    )
                    repaired_memories += 1
                    if status_changed:
                        status_updates += 1
                    continue

                collision = self.conn.execute(
                    "SELECT memory_id FROM memories WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()
                if collision is not None:
                    raise RepositoryEventConflictError(
                        f"deterministic memory id collision for repository event {event.id}"
                    )

                self.conn.execute(
                    """
                    INSERT INTO memories (
                        memory_id, fingerprint, kind, status, tenant_id, workspace_id,
                        project_id, user_id, agent_id, session_id, title, content, summary,
                        entity_keys_json, tags_json, metadata_json, importance, confidence,
                        strength, valid_from, valid_to, expires_at, created_at, updated_at,
                        memory_layer, embedding_model, embedding_json, acl_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        fingerprint,
                        event.kind.value,
                        status.value,
                        payload.scope.tenant_id,
                        None,
                        project_id,
                        None,
                        None,
                        None,
                        event.title,
                        event.body,
                        None,
                        self._to_json(entity_keys),
                        self._to_json(tags),
                        self._to_json(metadata),
                        event.importance,
                        event.confidence,
                        0.7,
                        event.created_at,
                        valid_to,
                        None,
                        event.created_at,
                        event.updated_at,
                        MemoryLayer.ORGANIZATION.value,
                        None,
                        self._to_json([]),
                        self._to_json([]),
                    ),
                )
                self._replace_trigger_phrases(
                    memory_id,
                    payload.scope.tenant_id,
                    event.triggers,
                )
                for source in expected_sources:
                    self._insert_source(memory_id, source)
                self._index_memory(
                    memory_id,
                    event.title,
                    None,
                    event.body,
                    tags,
                    entity_keys,
                )
                self._insert_audit(
                    "repository_memory_event_projected",
                    memory_id,
                    payload.scope.tenant_id,
                    {
                        "repository_id": repository_id,
                        "event_id": event.id,
                        "event_fingerprint": event_fingerprint,
                    },
                )
                self.conn.execute(
                    """
                    INSERT INTO repo_memory_event_projections (
                        tenant_id, project_id, repository_id, event_id,
                        event_fingerprint, projection_version, memory_id,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload.scope.tenant_id,
                        project_id,
                        repository_id,
                        event.id,
                        event_fingerprint,
                        REPOSITORY_LEDGER_PROJECTION_VERSION,
                        memory_id,
                        now,
                        now,
                    ),
                )
                row = self.conn.execute(
                    "SELECT * FROM memories WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()
                projected = self._row_to_record(row)
                self._insert_history(
                    memory_id,
                    payload.scope.tenant_id,
                    MemoryHistoryEventType.ADD,
                    old_memory={},
                    new_memory=projected.model_dump(mode="json"),
                    details={
                        "repository_id": repository_id,
                        "event_id": event.id,
                        "event_fingerprint": event_fingerprint,
                    },
                    actor_id=access.principal_id if access else None,
                )
                mappings[event.id] = {
                    "event_id": event.id,
                    "event_fingerprint": event_fingerprint,
                    "memory_id": memory_id,
                }
                created_memories += 1

            write_ms = self._elapsed_ms(write_started)
            relations_started = time.perf_counter()
            repaired_relations = 0
            for event, _, _ in parsed_events:
                if event.id in erasures:
                    suppressed_relations += len(event.supersedes)
                    continue
                from_memory_id = mappings[event.id]["memory_id"]
                for target_id in event.supersedes:
                    if target_id in erasures:
                        suppressed_relations += 1
                        continue
                    to_memory_id = mappings[target_id]["memory_id"]
                    existing_relation = self.conn.execute(
                        """
                        SELECT relation_id, tenant_id, workspace_id, project_id,
                               user_id, agent_id, session_id
                        FROM memory_relations
                        WHERE from_memory_id = ? AND to_memory_id = ? AND relation = ?
                        """,
                        (from_memory_id, to_memory_id, RelationKind.SUPERSEDES.value),
                    ).fetchone()
                    if existing_relation is None:
                        self._insert_relation(
                            from_memory_id,
                            to_memory_id,
                            RelationKind.SUPERSEDES.value,
                            projection_scope,
                        )
                        created_relations += 1
                    elif any(
                        [
                            existing_relation["tenant_id"] != payload.scope.tenant_id,
                            existing_relation["workspace_id"] is not None,
                            existing_relation["project_id"] != project_id,
                            existing_relation["user_id"] is not None,
                            existing_relation["agent_id"] is not None,
                            existing_relation["session_id"] is not None,
                        ]
                    ):
                        self.conn.execute(
                            """
                            UPDATE memory_relations
                            SET tenant_id = ?, workspace_id = NULL, project_id = ?,
                                user_id = NULL, agent_id = NULL, session_id = NULL
                            WHERE relation_id = ?
                            """,
                            (
                                payload.scope.tenant_id,
                                project_id,
                                existing_relation["relation_id"],
                            ),
                        )
                        repaired_relations += 1
                    else:
                        unchanged_relations += 1

                    target = self.conn.execute(
                        "SELECT status, valid_to, pre_hold_status FROM memories WHERE memory_id = ?",
                        (to_memory_id,),
                    ).fetchone()
                    if target is None:
                        raise RepositoryEventConflictError(
                            f"repository event {target_id} has no projected memory"
                        )
                    next_valid_to = target["valid_to"]
                    if next_valid_to is None or self._from_iso(next_valid_to) > self._from_iso(event.created_at):
                        next_valid_to = event.created_at
                    next_status = target["status"]
                    next_pre_hold_status = target["pre_hold_status"]
                    desired_status = referenced_status[target_id].value
                    if next_status == MemoryStatus.HELD.value:
                        current_pre_hold = next_pre_hold_status or MemoryStatus.ACTIVE.value
                        if desired_status == MemoryStatus.RETRACTED.value:
                            next_pre_hold_status = desired_status
                        elif current_pre_hold == MemoryStatus.ACTIVE.value:
                            next_pre_hold_status = desired_status
                    elif (
                        desired_status == MemoryStatus.RETRACTED.value
                        and next_status != MemoryStatus.RETRACTED.value
                    ) or (
                        desired_status == MemoryStatus.SUPERSEDED.value
                        and next_status == MemoryStatus.ACTIVE.value
                    ):
                        next_status = desired_status
                        status_updates += 1
                    if (
                        next_status != target["status"]
                        or next_valid_to != target["valid_to"]
                        or next_pre_hold_status != target["pre_hold_status"]
                    ):
                        old_projection = self._row_to_record(
                            self.conn.execute(
                                "SELECT * FROM memories WHERE memory_id = ?",
                                (to_memory_id,),
                            ).fetchone()
                        )
                        self.conn.execute(
                            """
                            UPDATE memories
                            SET status = ?, valid_to = ?, pre_hold_status = ?, updated_at = ?
                            WHERE memory_id = ?
                            """,
                            (
                                next_status,
                                next_valid_to,
                                next_pre_hold_status,
                                max(event.updated_at, event.created_at),
                                to_memory_id,
                            ),
                        )
                        new_projection = self._row_to_record(
                            self.conn.execute(
                                "SELECT * FROM memories WHERE memory_id = ?",
                                (to_memory_id,),
                            ).fetchone()
                        )
                        self._insert_history(
                            to_memory_id,
                            payload.scope.tenant_id,
                            MemoryHistoryEventType.UPDATE,
                            old_memory=old_projection.model_dump(mode="json"),
                            new_memory=new_projection.model_dump(mode="json"),
                            details={
                                "repository_id": repository_id,
                                "superseded_by_event_id": event.id,
                            },
                            actor_id=access.principal_id if access else None,
                        )

            relations_ms = self._elapsed_ms(relations_started)
            checkpoint_updated = not (
                checkpoint_matches
                and created_memories == 0
                and repaired_memories == 0
                and status_updates == 0
                and created_relations == 0
                and repaired_relations == 0
            )
            if checkpoint_updated:
                self.conn.execute(
                    """
                    INSERT INTO repo_memory_sync_state (
                        tenant_id, project_id, repository_id, projection_version,
                        ledger_path, ledger_fingerprint, events_fingerprint,
                        ledger_bytes, event_count, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (tenant_id, project_id, repository_id) DO UPDATE SET
                        projection_version = excluded.projection_version,
                        ledger_path = excluded.ledger_path,
                        ledger_fingerprint = excluded.ledger_fingerprint,
                        events_fingerprint = excluded.events_fingerprint,
                        ledger_bytes = excluded.ledger_bytes,
                        event_count = excluded.event_count,
                        synced_at = excluded.synced_at
                    """,
                    (
                        payload.scope.tenant_id,
                        project_id,
                        repository_id,
                        REPOSITORY_LEDGER_PROJECTION_VERSION,
                        payload.ledger_path,
                        ledger_fingerprint,
                        events_fingerprint,
                        payload.ledger_bytes,
                        event_count,
                        now,
                    ),
                )
                for event, _, _ in parsed_events:
                    if event.id not in erasures:
                        self.conn.execute(
                            """
                            UPDATE repo_memory_event_projections SET last_seen_at = ?
                            WHERE tenant_id = ? AND project_id = ?
                              AND repository_id = ? AND event_id = ?
                            """,
                            (now, payload.scope.tenant_id, project_id, repository_id, event.id),
                        )
                replica_id = "repo-ledger:" + hashlib.sha256(
                    f"{payload.scope.tenant_id}\0{project_id}\0{repository_id}".encode("utf-8")
                ).hexdigest()[:32]
                self.conn.execute(
                    """
                    INSERT INTO replication_state (
                        replica_id, source_path, target_path, last_sync_at, last_lsn,
                        status, rpo_seconds, rto_seconds, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (replica_id) DO UPDATE SET
                        source_path = excluded.source_path,
                        target_path = excluded.target_path,
                        last_sync_at = excluded.last_sync_at,
                        last_lsn = excluded.last_lsn,
                        status = excluded.status
                    """,
                    (
                        replica_id,
                        payload.ledger_path,
                        f"store://repositories/{quote(repository_id, safe='')}/memory-events",
                        now,
                        ledger_fingerprint,
                        "active",
                        60,
                        300,
                        now,
                    ),
                )

        self._invalidate_tenant_cache(payload.scope.tenant_id)
        total_ms = self._elapsed_ms(started)
        return RepositoryMemorySyncResponse(
            repository_id=repository_id,
            ledger_fingerprint=ledger_fingerprint,
            events_fingerprint=events_fingerprint,
            received_events=event_count,
            created_memories=created_memories,
            unchanged_memories=unchanged_memories,
            repaired_memories=repaired_memories,
            suppressed_events=suppressed_events,
            status_updates=status_updates,
            created_relations=created_relations,
            repaired_relations=repaired_relations,
            unchanged_relations=unchanged_relations,
            suppressed_relations=suppressed_relations,
            checkpoint_updated=checkpoint_updated,
            no_op=not checkpoint_updated,
            duration_ms=total_ms,
            timings_ms=RepositoryMemorySyncTimings(
                validate=validation_ms,
                write=write_ms,
                relations=relations_ms,
                total=total_ms,
            ),
        )

    def update_memory(
        self,
        memory_id: str,
        payload: MemoryUpdate,
        access: AccessContext | None = None,
    ) -> MemoryWriteResult:
        existing = self._get_authorized_memory(
            memory_id,
            access,
            ACLPermission.WRITE,
            source_permission_levels=EDITABLE_PERMISSION_LEVELS,
            allowed_roles=MEMORY_WRITE_ROLES,
        )
        if existing is None:
            raise ValueError("memory not found")
        if self._is_repository_event_projection(memory_id):
            raise RepositoryEventConflictError(
                "repository ledger projections are immutable; append a superseding or retraction event"
            )

        updates = payload.model_dump(exclude_unset=True)
        if not updates:
            return MemoryWriteResult(created=False, memory=existing)
        if payload.supersedes_memory_id:
            self._validate_supersession_target(
                payload.supersedes_memory_id,
                memory_id,
                existing.scope.tenant_id,
                access,
            )

        next_kind = payload.kind or existing.kind
        next_layer = payload.memory_layer or existing.memory_layer
        next_title = payload.title if "title" in updates else existing.title
        next_content = payload.content if "content" in updates else existing.content
        next_summary = payload.summary if "summary" in updates else existing.summary
        next_entity_keys = payload.entity_keys if payload.entity_keys is not None else existing.entity_keys
        next_tags = payload.tags if payload.tags is not None else existing.tags
        next_metadata = payload.metadata if payload.metadata is not None else existing.metadata
        next_importance = payload.importance if payload.importance is not None else existing.importance
        next_confidence = payload.confidence if payload.confidence is not None else existing.confidence
        next_strength = payload.strength if payload.strength is not None else existing.strength
        next_valid_from = payload.valid_from if "valid_from" in updates else existing.valid_from
        next_valid_to = payload.valid_to if "valid_to" in updates else existing.valid_to
        next_expires_at = payload.expires_at if "expires_at" in updates else existing.expires_at
        next_embedding_model = payload.embedding_model if "embedding_model" in updates else existing.embedding_model
        next_embedding = payload.embedding if payload.embedding is not None else existing.embedding
        next_acl = payload.acl if payload.acl is not None else existing.acl
        next_sources = payload.source_references if payload.source_references is not None else existing.source_references
        next_triggers = payload.trigger_phrases if payload.trigger_phrases is not None else existing.trigger_phrases
        source_authorization_record = existing.model_copy(
            update={"source_references": next_sources}
        )
        if not self._can_access_memory(
            source_authorization_record,
            access,
            ACLPermission.WRITE,
            source_permission_levels=EDITABLE_PERMISSION_LEVELS,
            allowed_roles=MEMORY_WRITE_ROLES,
        ):
            raise ValueError("memory not found")
        existing_connected_sources = {
            (row["source_id"], row["connector_id"])
            for row in self._connected_source_rows(existing)
        }
        next_connected_sources = {
            (row["source_id"], row["connector_id"])
            for row in self._connected_source_rows(source_authorization_record)
        }
        removed_connected_sources = (
            existing_connected_sources - next_connected_sources
        )
        removes_connected_source = bool(removed_connected_sources)
        changes_connected_acl = bool(
            existing_connected_sources
            and payload.acl is not None
            and next_acl != existing.acl
        )
        if removes_connected_source or changes_connected_acl:
            if changes_connected_acl:
                governed_sources = [
                    *existing.source_references,
                    *next_sources,
                ]
            else:
                removed_source_ids = {
                    source_id for source_id, _connector_id in removed_connected_sources
                }
                governed_sources = [
                    source
                    for source in existing.source_references
                    if source.source_id in removed_source_ids
                ]
            governance_record = existing.model_copy(
                update={"source_references": governed_sources}
            )
            if not self._can_access_memory(
                governance_record,
                access,
                ACLPermission.SHARE,
                source_permission_levels=OWNER_PERMISSION_LEVELS,
                allowed_roles=MEMORY_WRITE_ROLES,
            ):
                raise ValueError("memory not found")
        next_fingerprint = self._fingerprint(
            existing.scope,
            next_kind.value,
            next_title,
            next_content,
            next_metadata,
        )
        old_snapshot = existing.model_dump(mode="json")
        now = self._iso_now()

        try:
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE memories
                    SET fingerprint = ?,
                        kind = ?,
                        title = ?,
                        content = ?,
                        summary = ?,
                        entity_keys_json = ?,
                        tags_json = ?,
                        metadata_json = ?,
                        importance = ?,
                        confidence = ?,
                        strength = ?,
                        valid_from = ?,
                        valid_to = ?,
                        expires_at = ?,
                        updated_at = ?,
                        memory_layer = ?,
                        embedding_model = ?,
                        embedding_json = ?,
                        acl_json = ?
                    WHERE memory_id = ?
                    """,
                    (
                        next_fingerprint,
                        next_kind.value,
                        next_title,
                        next_content,
                        next_summary,
                        self._to_json(next_entity_keys),
                        self._to_json(next_tags),
                        self._to_json(next_metadata),
                        next_importance,
                        next_confidence,
                        next_strength,
                        self._to_iso(next_valid_from),
                        self._to_iso(next_valid_to),
                        self._to_iso(next_expires_at),
                        now,
                        next_layer.value,
                        next_embedding_model,
                        self._to_json(next_embedding),
                        self._to_json([entry.model_dump() for entry in next_acl]),
                        memory_id,
                    ),
                )
                if payload.source_references is not None:
                    self.conn.execute("DELETE FROM memory_sources WHERE memory_id = ?", (memory_id,))
                    for source in next_sources:
                        self._insert_source(memory_id, source)
                if payload.trigger_phrases is not None:
                    self._replace_trigger_phrases(memory_id, existing.scope.tenant_id, next_triggers)
                if payload.supersedes_memory_id:
                    self.conn.execute(
                        "UPDATE memories SET status = ?, updated_at = ? WHERE memory_id = ? AND tenant_id = ?",
                        (
                            MemoryStatus.SUPERSEDED.value,
                            now,
                            payload.supersedes_memory_id,
                            existing.scope.tenant_id,
                        ),
                    )
                    self._insert_relation(
                        payload.supersedes_memory_id,
                        memory_id,
                        RelationKind.SUPERSEDES.value,
                        existing.scope,
                    )
                self._index_memory(memory_id, next_title, next_summary, next_content, next_tags, next_entity_keys)
                self._insert_audit(
                    "memory_updated",
                    memory_id,
                    existing.scope.tenant_id,
                    {"updated_fields": sorted(updates.keys())},
                )
                self._refresh_hold_state([memory_id])
                row = self.conn.execute(
                    "SELECT * FROM memories WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("updated memory disappeared before transaction commit")
                updated = self._row_to_record(row)
                self._insert_history(
                    memory_id,
                    existing.scope.tenant_id,
                    MemoryHistoryEventType.UPDATE,
                    old_memory=old_snapshot,
                    new_memory=updated.model_dump(mode="json"),
                    details={"updated_fields": sorted(updates.keys())},
                    actor_id=access.principal_id if access else None,
                )
                self._vec_upsert(memory_id, updated.embedding)
        except sqlite3.IntegrityError as exc:
            raise ValueError("update would create a duplicate memory fingerprint") from exc

        self._invalidate_tenant_cache(existing.scope.tenant_id)
        return MemoryWriteResult(created=False, memory=updated)

    def get_memory(
        self,
        memory_id: str,
        access: AccessContext | None = None,
        enforce_source_grants: bool = False,
    ) -> MemoryRecord | None:
        return self._get_authorized_memory(
            memory_id,
            access,
            ACLPermission.READ,
            source_permission_levels=(
                READABLE_PERMISSION_LEVELS if enforce_source_grants else None
            ),
        )

    def _get_authorized_memory(
        self,
        memory_id: str,
        access: AccessContext | None,
        required_acl: ACLPermission,
        *,
        source_permission_levels: frozenset[str] | None = None,
        allowed_roles: frozenset[str] | None = None,
    ) -> MemoryRecord | None:
        self._refresh_hold_state([memory_id])
        row = self.conn.execute(
            "SELECT * FROM memories WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            return None
        record = self._row_to_record(row)
        if not self._can_access_memory(
            record,
            access,
            required_acl,
            source_permission_levels=source_permission_levels,
            allowed_roles=allowed_roles,
        ):
            return None
        return record

    def inspect_memory(
        self,
        memory_id: str,
        audit_limit: int = 25,
    ) -> MemoryInspectionResponse | None:
        self._refresh_hold_state([memory_id])
        row = self._raw_memory_row(memory_id)
        if row is None:
            return None
        record = self._row_to_record(row)
        connected_sources = self._connected_sources_for_record(record)
        return MemoryInspectionResponse(
            memory=record,
            storage=self._storage_state(record, connected_source_count=len(connected_sources)),
            connected_sources=connected_sources,
            related_memories=self._related_memories(record.memory_id, access=None),
            audit_trail=self._audit_events_for_memory(record.memory_id, limit=audit_limit),
            history=self.list_memory_history(record.memory_id, limit=audit_limit),
            feedback_summary=self.feedback_summary(record.memory_id),
        )

    def list_memory_history(self, memory_id: str, limit: int = 50) -> list[MemoryHistoryEvent]:
        rows = self.conn.execute(
            """
            SELECT history_id, memory_id, event, actor_id, old_memory_json, new_memory_json, details_json, created_at
            FROM memory_history
            WHERE memory_id = ?
            ORDER BY datetime(created_at) DESC, history_id DESC
            LIMIT ?
            """,
            (memory_id, limit),
        ).fetchall()
        return [
            MemoryHistoryEvent(
                history_id=row["history_id"],
                memory_id=row["memory_id"],
                event=MemoryHistoryEventType(row["event"]),
                actor_id=row["actor_id"],
                old_memory=self._json_to_dict(row["old_memory_json"]),
                new_memory=self._json_to_dict(row["new_memory_json"]),
                details=self._json_to_dict(row["details_json"]),
                created_at=self._from_iso(row["created_at"]) or datetime.now(UTC),
            )
            for row in rows
        ]

    def add_memory_feedback(
        self,
        memory_id: str,
        payload: MemoryFeedbackCreate,
        access: AccessContext | None = None,
    ) -> MemoryFeedbackRecord:
        record = self._get_authorized_memory(
            memory_id,
            access,
            ACLPermission.READ,
            source_permission_levels=COMMENTABLE_PERMISSION_LEVELS,
            allowed_roles=MEMORY_WRITE_ROLES,
        )
        if record is None:
            raise ValueError("memory not found")
        created_at = self._iso_now()
        feedback = MemoryFeedbackRecord(
            feedback_id=payload.feedback_id or str(uuid.uuid4()),
            memory_id=memory_id,
            tenant_id=record.scope.tenant_id,
            feedback_type=payload.feedback_type,
            principal_id=access.principal_id if access is not None else payload.principal_id,
            reason=payload.reason,
            metadata=payload.metadata,
            created_at=self._from_iso(created_at) or datetime.now(UTC),
        )
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO memory_feedback (
                    feedback_id, memory_id, tenant_id, feedback_type, principal_id, reason, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback.feedback_id,
                    feedback.memory_id,
                    feedback.tenant_id,
                    feedback.feedback_type.value,
                    feedback.principal_id,
                    feedback.reason,
                    self._to_json(feedback.metadata),
                    created_at,
                ),
            )
            self._insert_audit(
                "memory_feedback_added",
                memory_id,
                record.scope.tenant_id,
                {"feedback_type": feedback.feedback_type.value},
            )
            self._insert_history(
                memory_id,
                record.scope.tenant_id,
                MemoryHistoryEventType.FEEDBACK,
                old_memory={},
                new_memory={},
                details={
                    "feedback_type": feedback.feedback_type.value,
                    "reason": feedback.reason,
                    "principal_id": feedback.principal_id,
                },
                actor_id=feedback.principal_id,
            )
        self._invalidate_tenant_cache(record.scope.tenant_id)
        return feedback

    def list_memory_feedback(
        self,
        memory_id: str,
        access: AccessContext | None = None,
        limit: int = 100,
    ) -> list[MemoryFeedbackRecord]:
        record = self._get_authorized_memory(
            memory_id,
            access,
            ACLPermission.READ,
            source_permission_levels=READABLE_PERMISSION_LEVELS,
        )
        if record is None:
            raise ValueError("memory not found")
        rows = self.conn.execute(
            """
            SELECT feedback_id, memory_id, tenant_id, feedback_type, principal_id, reason, metadata_json, created_at
            FROM memory_feedback
            WHERE memory_id = ?
            ORDER BY datetime(created_at) DESC, feedback_id DESC
            LIMIT ?
            """,
            (memory_id, limit),
        ).fetchall()
        return [
            MemoryFeedbackRecord(
                feedback_id=row["feedback_id"],
                memory_id=row["memory_id"],
                tenant_id=row["tenant_id"],
                feedback_type=MemoryFeedbackType(row["feedback_type"]),
                principal_id=row["principal_id"],
                reason=row["reason"],
                metadata=self._json_to_dict(row["metadata_json"]),
                created_at=self._from_iso(row["created_at"]) or datetime.now(UTC),
            )
            for row in rows
        ]

    def feedback_summary(self, memory_id: str) -> MemoryFeedbackSummary:
        rows = self.conn.execute(
            """
            SELECT feedback_type, COUNT(*) AS count, MAX(created_at) AS latest_created_at
            FROM memory_feedback
            WHERE memory_id = ?
            GROUP BY feedback_type
            """,
            (memory_id,),
        ).fetchall()
        counts = {row["feedback_type"]: int(row["count"]) for row in rows}
        latest_values = [self._from_iso(row["latest_created_at"]) for row in rows if row["latest_created_at"]]
        summary = MemoryFeedbackSummary(
            positive=counts.get(MemoryFeedbackType.POSITIVE.value, 0),
            negative=counts.get(MemoryFeedbackType.NEGATIVE.value, 0),
            correction=counts.get(MemoryFeedbackType.CORRECTION.value, 0),
            pin=counts.get(MemoryFeedbackType.PIN.value, 0),
            latest_feedback_at=max(latest_values) if latest_values else None,
        )
        summary.net_score = self._feedback_net_score(summary)
        return summary

    def search_memories(
        self,
        payload: SearchRequest,
        access: AccessContext | None = None,
    ) -> SearchResponse:
        cache_key = self._search_cache_key(payload, access)
        cached, cache_generation = self.hot_cache.lookup_search(
            payload.scope.tenant_id,
            cache_key,
        )
        if cached is not None:
            return SearchResponse(**cached)

        query_terms = self._query_terms(payload.query)
        candidates, _ = self._candidate_rows_with_strategy(payload)
        self._refresh_hold_state([candidate.row["memory_id"] for candidate in candidates])
        candidates, _ = self._candidate_rows_with_strategy(payload)
        results: list[SearchResult] = []
        for candidate in candidates:
            record = self._row_to_record(candidate.row)
            evaluation = self._evaluate_search_candidate(
                record,
                payload,
                access,
                query_terms,
                candidate.fts_rank,
                candidate.vector_rank,
            )
            if evaluation.rejection_reasons:
                continue
            related = self._related_memories(record.memory_id, access) if payload.include_relations else []
            results.append(
                SearchResult(
                    memory=record,
                    score=evaluation.score,
                    reasons=evaluation.reasons,
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
        response = SearchResponse(results=results[: payload.limit])
        self.hot_cache.set_search_if_current(
            payload.scope.tenant_id,
            cache_key,
            response.model_dump(mode="json"),
            cache_generation,
        )
        return response

    def agent_context(
        self,
        payload: AgentContextRequest,
        access: AccessContext | None = None,
    ) -> AgentContextResponse:
        search = SearchRequest(
            query=payload.query,
            scope=payload.scope,
            kinds=payload.kinds,
            memory_layers=payload.memory_layers,
            tags=payload.tags,
            entity_keys=payload.entity_keys,
            include_relations=True,
            limit=payload.max_memories,
        )
        response = self.search_memories(search, access=access)
        context_blocks: list[str] = []
        context_memories: list[AgentContextMemory] = []
        all_citations: list[str] = []
        remaining = payload.max_characters

        for index, result in enumerate(response.results, start=1):
            record = result.memory
            content = record.summary or record.content
            citations = self._citation_labels(record)
            all_citations.extend(citation for citation in citations if citation not in all_citations)
            header = f"[{index}] {record.kind.value}/{record.memory_layer.value}: {record.title or record.memory_id}"
            block_parts = [header, content]
            if payload.include_citations and citations:
                block_parts.append("Sources: " + "; ".join(citations[:5]))
            if result.reasons:
                block_parts.append("Retrieval reasons: " + ", ".join(result.reasons[:5]))
            block = "\n".join(block_parts)
            if len(block) > remaining:
                if remaining < 120:
                    break
                block = block[: remaining - 3].rstrip() + "..."
            context_blocks.append(block)
            remaining -= len(block) + 2
            context_memories.append(
                AgentContextMemory(
                    memory_id=record.memory_id,
                    title=record.title,
                    kind=record.kind,
                    memory_layer=record.memory_layer,
                    content=content,
                    score=result.score,
                    reasons=result.reasons,
                    citations=citations,
                    updated_at=record.updated_at,
                )
            )
            if remaining <= 0:
                break

        context = "\n\n".join(context_blocks)
        return AgentContextResponse(
            query=payload.query,
            scope=payload.scope,
            context=context,
            token_estimate=max(1, len(context) // 4) if context else 0,
            memories=context_memories,
            citations=all_citations,
        )

    def temporal_graph(
        self,
        payload: TemporalGraphRequest,
        access: AccessContext | None = None,
    ) -> TemporalGraphResponse:
        seed_ids = list(dict.fromkeys(payload.seed_memory_ids))
        if payload.query or not seed_ids:
            search = SearchRequest(
                query=payload.query or "",
                scope=payload.scope,
                memory_layers=payload.memory_layers,
                include_relations=False,
                include_deleted=payload.include_expired,
                limit=payload.limit,
            )
            seed_ids.extend(result.memory.memory_id for result in self.search_memories(search, access=access).results)
            seed_ids = list(dict.fromkeys(seed_ids))

        visited: dict[str, MemoryRecord] = {}
        frontier = seed_ids[: payload.limit]
        now = datetime.now(UTC)
        for depth in range(payload.depth + 1):
            next_frontier: list[str] = []
            for memory_id in frontier:
                if len(visited) >= payload.limit or memory_id in visited:
                    continue
                record = self.get_memory(memory_id, access=access, enforce_source_grants=True)
                if record is None:
                    continue
                if not self._scope_matches(record.scope, payload.scope):
                    continue
                if payload.memory_layers and record.memory_layer not in payload.memory_layers:
                    continue
                if not payload.include_expired and record.expires_at and record.expires_at <= now:
                    continue
                visited[memory_id] = record
                if depth >= payload.depth:
                    continue
                next_frontier.extend(self._adjacent_memory_ids(memory_id, payload.scope.tenant_id))
            frontier = [memory_id for memory_id in dict.fromkeys(next_frontier) if memory_id not in visited]
            if not frontier or len(visited) >= payload.limit:
                break

        nodes = [
            TemporalGraphNode(
                memory=record,
                observed_at=record.updated_at,
                valid_from=record.valid_from,
                valid_to=record.valid_to,
                expires_at=record.expires_at,
                layer=record.memory_layer,
            )
            for record in sorted(
                visited.values(),
                key=lambda item: (
                    item.valid_from or item.updated_at,
                    item.updated_at,
                ),
                reverse=True,
            )
        ]
        edges = self._temporal_edges(sorted(visited), payload.scope.tenant_id)
        return TemporalGraphResponse(
            scope=payload.scope,
            query=payload.query,
            seed_memory_ids=seed_ids[: payload.limit],
            nodes=nodes,
            edges=edges,
        )

    def explain_search(
        self,
        payload: SearchRequest,
        access: AccessContext | None = None,
    ) -> SearchExplainResponse:
        query_terms = self._query_terms(payload.query)
        candidates, strategy = self._candidate_rows_with_strategy(payload)
        self._refresh_hold_state([candidate.row["memory_id"] for candidate in candidates])
        candidates, strategy = self._candidate_rows_with_strategy(payload)

        accepted: list[SearchExplainCandidate] = []
        filtered_out: list[SearchExplainCandidate] = []
        for candidate in candidates:
            record = self._row_to_record(candidate.row)
            evaluation = self._evaluate_search_candidate(
                record,
                payload,
                access,
                query_terms,
                candidate.fts_rank,
                candidate.vector_rank,
            )
            candidate = SearchExplainCandidate(
                memory=record,
                score=evaluation.score,
                reasons=evaluation.reasons,
                rejection_reasons=evaluation.rejection_reasons,
                fts_rank=evaluation.fts_rank,
            )
            if evaluation.rejection_reasons:
                filtered_out.append(candidate)
            else:
                accepted.append(candidate)

        accepted.sort(
            key=lambda item: (
                item.score,
                item.memory.updated_at,
                item.memory.created_at,
            ),
            reverse=True,
        )
        for index, candidate in enumerate(accepted, start=1):
            candidate.rank = index

        filtered_out.sort(
            key=lambda item: (
                item.memory.updated_at,
                item.memory.created_at,
            ),
            reverse=True,
        )

        returned = accepted[: payload.limit]
        not_returned = accepted[payload.limit :]
        return SearchExplainResponse(
            query=payload.query,
            query_terms=sorted(query_terms),
            candidate_strategy=strategy,
            total_candidates=len(candidates),
            returned=returned,
            not_returned=not_returned,
            filtered_out=filtered_out,
        )

    def storage_overview(self, tenant_id: str) -> MemoryArchitectureOverview:
        storage = StorageTierOverview(
            memories_total=self._count_scalar(
                "SELECT COUNT(*) FROM memories WHERE tenant_id = ?",
                (tenant_id,),
            ),
            memories_active=self._count_scalar(
                "SELECT COUNT(*) FROM memories WHERE tenant_id = ? AND status = ?",
                (tenant_id, MemoryStatus.ACTIVE.value),
            ),
            memories_superseded=self._count_scalar(
                "SELECT COUNT(*) FROM memories WHERE tenant_id = ? AND status = ?",
                (tenant_id, MemoryStatus.SUPERSEDED.value),
            ),
            memories_deleted=self._count_scalar(
                "SELECT COUNT(*) FROM memories WHERE tenant_id = ? AND status = ?",
                (tenant_id, MemoryStatus.DELETED.value),
            ),
            memories_retracted=self._count_scalar(
                "SELECT COUNT(*) FROM memories WHERE tenant_id = ? AND status = ?",
                (tenant_id, MemoryStatus.RETRACTED.value),
            ),
            memories_held=self._count_scalar(
                "SELECT COUNT(*) FROM memories WHERE tenant_id = ? AND held = 1",
                (tenant_id,),
            ),
            conversation_layer_total=self._count_scalar(
                "SELECT COUNT(*) FROM memories WHERE tenant_id = ? AND memory_layer = ?",
                (tenant_id, MemoryLayer.CONVERSATION.value),
            ),
            session_layer_total=self._count_scalar(
                "SELECT COUNT(*) FROM memories WHERE tenant_id = ? AND memory_layer = ?",
                (tenant_id, MemoryLayer.SESSION.value),
            ),
            user_layer_total=self._count_scalar(
                "SELECT COUNT(*) FROM memories WHERE tenant_id = ? AND memory_layer = ?",
                (tenant_id, MemoryLayer.USER.value),
            ),
            agent_layer_total=self._count_scalar(
                "SELECT COUNT(*) FROM memories WHERE tenant_id = ? AND memory_layer = ?",
                (tenant_id, MemoryLayer.AGENT.value),
            ),
            organization_layer_total=self._count_scalar(
                "SELECT COUNT(*) FROM memories WHERE tenant_id = ? AND memory_layer = ?",
                (tenant_id, MemoryLayer.ORGANIZATION.value),
            ),
            source_references_total=self._count_scalar(
                """
                SELECT COUNT(*) FROM memory_sources
                WHERE memory_id IN (SELECT memory_id FROM memories WHERE tenant_id = ?)
                """,
                (tenant_id,),
            ),
            connected_sources_total=self._count_scalar(
                "SELECT COUNT(*) FROM connector_sources WHERE tenant_id = ?",
                (tenant_id,),
            ),
            relations_total=self._count_scalar(
                "SELECT COUNT(*) FROM memory_relations WHERE tenant_id = ?",
                (tenant_id,),
            ),
            trigger_phrases_total=self._count_scalar(
                "SELECT COUNT(*) FROM trigger_index WHERE tenant_id = ?",
                (tenant_id,),
            ),
            entity_registry_total=self._count_scalar(
                "SELECT COUNT(*) FROM entity_registry WHERE tenant_id = ?",
                (tenant_id,),
            ),
            token_usage_total=self._count_scalar(
                "SELECT COUNT(*) FROM token_usage WHERE tenant_id = ?",
                (tenant_id,),
            ),
            evidence_cache_total=self._count_scalar(
                "SELECT COUNT(*) FROM evidence_cache WHERE tenant_id = ?",
                (tenant_id,),
            ),
            replication_state_total=self._count_scalar("SELECT COUNT(*) FROM replication_state"),
            embedding_models_total=self._count_scalar("SELECT COUNT(*) FROM embedding_models"),
            fts_indexed_memories=self._count_scalar(
                """
                SELECT COUNT(*) FROM memories_fts
                WHERE memory_id IN (SELECT memory_id FROM memories WHERE tenant_id = ?)
                """,
                (tenant_id,),
            ),
            vectorized_memories=self._count_scalar(
                """
                SELECT COUNT(*) FROM memories
                WHERE tenant_id = ?
                  AND embedding_json IS NOT NULL
                  AND embedding_json != '[]'
                """,
                (tenant_id,),
            ),
            project_snapshots_total=self._count_scalar(
                "SELECT COUNT(*) FROM project_snapshots WHERE tenant_id = ?",
                (tenant_id,),
            ),
            audit_events_total=self._count_scalar(
                "SELECT COUNT(*) FROM audit_log WHERE tenant_id = ?",
                (tenant_id,),
            ),
            history_events_total=self._count_scalar(
                "SELECT COUNT(*) FROM memory_history WHERE tenant_id = ?",
                (tenant_id,),
            ),
            feedback_total=self._count_scalar(
                "SELECT COUNT(*) FROM memory_feedback WHERE tenant_id = ?",
                (tenant_id,),
            ),
            connectors_total=self._count_scalar(
                "SELECT COUNT(*) FROM connectors WHERE tenant_id = ?",
                (tenant_id,),
            ),
            principal_mappings_total=self._count_scalar(
                "SELECT COUNT(*) FROM principal_mappings WHERE tenant_id = ?",
                (tenant_id,),
            ),
            permission_grants_total=self._count_scalar(
                "SELECT COUNT(*) FROM source_permission_grants WHERE tenant_id = ?",
                (tenant_id,),
            ),
            sync_jobs_total=self._count_scalar(
                "SELECT COUNT(*) FROM sync_jobs WHERE tenant_id = ?",
                (tenant_id,),
            ),
        )
        return MemoryArchitectureOverview(
            tenant_id=tenant_id,
            generated_at=datetime.now(UTC),
            storage=storage,
            coverage=self.integration_coverage(tenant_id),
        )

    def onboarding_status(self, tenant_id: str) -> OnboardingStatus:
        overview = self.storage_overview(tenant_id)
        storage = overview.storage
        coverage = overview.coverage
        cache_backend = self.hot_cache.backend_name
        steps = [
            self._onboarding_step(
                "storage",
                "Storage schema initialized",
                True,
                f"{storage.memories_total} memories and {storage.audit_events_total} audit events are visible.",
            ),
            self._onboarding_step(
                "redis_hot_cache",
                "Redis hot cache configured",
                cache_backend == "redis",
                f"Current cache backend is {cache_backend}.",
                required=False,
                warning=True,
            ),
            self._onboarding_step(
                "connector_registry",
                "At least one enterprise connector registered",
                coverage.connectors_total > 0,
                f"{coverage.connectors_total} connectors registered; {coverage.connectors_healthy} active.",
            ),
            self._onboarding_step(
                "source_inventory",
                "Source inventory synchronized",
                coverage.sources_total > 0,
                f"{coverage.sources_total} sources indexed; {coverage.sources_stale} stale.",
            ),
            self._onboarding_step(
                "principal_mapping",
                "Principal mappings synchronized",
                coverage.principals_mapped > 0,
                f"{coverage.principals_mapped} principals mapped.",
            ),
            self._onboarding_step(
                "source_permissions",
                "Source permissions synchronized",
                coverage.grants_total > 0,
                f"{coverage.grants_total} source permission grants stored.",
            ),
            self._onboarding_step(
                "memory_lifecycle",
                "Memory lifecycle events observable",
                storage.history_events_total > 0,
                f"{storage.history_events_total} history events and {storage.feedback_total} feedback events stored.",
                required=False,
            ),
            self._onboarding_step(
                "benchmark_proof",
                "Benchmark harness available",
                True,
                "Run python scripts/run_memory_benchmarks.py --output reports/memory-benchmark.json.",
                required=False,
            ),
        ]
        recommended_next_actions: list[str] = []
        if coverage.connectors_total == 0:
            recommended_next_actions.append("Register the first production connector with principal and ACL sync enabled.")
        if coverage.sources_total == 0:
            recommended_next_actions.append("Run a connector sync to populate source inventory.")
        if coverage.principals_mapped == 0:
            recommended_next_actions.append("Sync identity mappings before enabling broad enterprise recall.")
        if coverage.grants_total == 0:
            recommended_next_actions.append("Sync source permission grants so search cannot leak restricted memories.")
        if cache_backend != "redis":
            recommended_next_actions.append("Set PROVENA_REDIS_URL and PROVENA_HOT_CACHE_ENABLED=true before load testing.")

        ready = all(step.status == "complete" for step in steps if step.required)
        return OnboardingStatus(
            tenant_id=tenant_id,
            generated_at=datetime.now(UTC),
            ready=ready,
            cache_backend=cache_backend,
            steps=steps,
            recommended_next_actions=recommended_next_actions,
        )

    def production_evidence(self, tenant_id: str) -> ProductionEvidenceReport:
        overview = self.storage_overview(tenant_id)
        storage = overview.storage
        coverage = overview.coverage
        cache_backend = self.hot_cache.backend_name
        gaps = list(coverage.missing_foundations)
        if cache_backend != "redis":
            gaps.append("redis_hot_cache")
        if storage.memories_total == 0:
            gaps.append("tenant_memory_corpus")
        if storage.source_references_total == 0:
            gaps.append("source_citations")

        scale_level = "demo"
        if coverage.connectors_total > 0 and coverage.sources_total > 0 and coverage.grants_total > 0:
            scale_level = "pilot"
        if storage.memories_total >= 1000 and coverage.sources_total >= 100 and cache_backend == "redis":
            scale_level = "enterprise_candidate"

        metrics = [
            ProductionEvidenceMetric(
                name="memories_total",
                value=storage.memories_total,
                unit="memories",
                status="ok" if storage.memories_total > 0 else "missing",
                detail="Stored tenant memories across all memory layers.",
            ),
            ProductionEvidenceMetric(
                name="source_references_total",
                value=storage.source_references_total,
                unit="citations",
                status="ok" if storage.source_references_total > 0 else "missing",
                detail="Citations available for proving what the agent remembered.",
            ),
            ProductionEvidenceMetric(
                name="connectors_total",
                value=coverage.connectors_total,
                unit="connectors",
                status="ok" if coverage.connectors_total > 0 else "missing",
                detail="Registered systems of record feeding enterprise context.",
            ),
            ProductionEvidenceMetric(
                name="permission_grants_total",
                value=coverage.grants_total,
                unit="grants",
                status="ok" if coverage.grants_total > 0 else "missing",
                detail="Source ACL grants used to preserve enterprise access boundaries.",
            ),
            ProductionEvidenceMetric(
                name="hot_cache_backend",
                value=cache_backend,
                status="ok" if cache_backend == "redis" else "warning",
                detail="Redis is expected for production latency evidence.",
            ),
        ]
        use_cases = [
            ProductionUseCaseEvidence(
                name="Permission-preserving enterprise assistant",
                readiness="ready" if coverage.grants_total > 0 and storage.memories_total > 0 else "blocked",
                detail="Requires memories plus synced source permission grants.",
            ),
            ProductionUseCaseEvidence(
                name="Auditable memory investigation",
                readiness="ready" if storage.audit_events_total > 0 and storage.history_events_total > 0 else "partial",
                detail="Uses inspect, history, feedback, and storage overview APIs.",
            ),
            ProductionUseCaseEvidence(
                name="Agent-native context injection",
                readiness="ready" if storage.memories_total > 0 else "blocked",
                detail="Uses POST /v1/agent/context for citation-aware prompt context.",
            ),
        ]
        return ProductionEvidenceReport(
            tenant_id=tenant_id,
            generated_at=datetime.now(UTC),
            cache_backend=cache_backend,
            scale_level=scale_level,
            metrics=metrics,
            use_cases=use_cases,
            benchmark_command="python scripts/run_memory_benchmarks.py --output reports/memory-benchmark.json",
            gaps=sorted(dict.fromkeys(gaps)),
        )

    def write_relation(
        self,
        payload: RelationWrite,
        access: AccessContext | None = None,
    ) -> None:
        source = self._get_authorized_memory(
            payload.from_memory_id,
            access,
            ACLPermission.WRITE,
            source_permission_levels=EDITABLE_PERMISSION_LEVELS,
            allowed_roles=MEMORY_WRITE_ROLES,
        )
        target = self._get_authorized_memory(
            payload.to_memory_id,
            access,
            ACLPermission.WRITE,
            source_permission_levels=EDITABLE_PERMISSION_LEVELS,
            allowed_roles=MEMORY_WRITE_ROLES,
        )
        if source is None or target is None:
            raise ValueError("relation requires accessible source and target memories")
        derived_scope = source.scope
        if target.scope != derived_scope or payload.scope != derived_scope:
            raise ValueError("relation requires source, target, and payload to share one scope")
        if self._is_repository_event_projection(payload.from_memory_id) or self._is_repository_event_projection(
            payload.to_memory_id
        ):
            raise RepositoryEventConflictError(
                "repository ledger projection relations are immutable; append a source-attested event"
            )
        with self.conn:
            self._insert_relation(
                payload.from_memory_id,
                payload.to_memory_id,
                payload.relation.value,
                derived_scope,
            )
            self._insert_audit(
                "relation_created",
                payload.from_memory_id,
                derived_scope.tenant_id,
                {"to_memory_id": payload.to_memory_id, "relation": payload.relation.value},
            )

    def delete_memory(
        self,
        memory_id: str,
        hard_delete: bool = False,
        access: AccessContext | None = None,
    ) -> DeleteResponse:
        record = self._get_authorized_memory(
            memory_id,
            access,
            ACLPermission.DELETE,
            source_permission_levels=OWNER_PERMISSION_LEVELS,
            allowed_roles=MEMORY_WRITE_ROLES,
        )
        if record is None:
            return DeleteResponse(memory_id=memory_id, deleted=False, hard_delete=hard_delete)
        if self._is_repository_event_projection(memory_id):
            raise RepositoryEventConflictError(
                "repository ledger projections are immutable; append a retraction event"
            )
        old_snapshot = record.model_dump(mode="json")

        with self.conn:
            if hard_delete:
                self._purge_memory_indexes([memory_id])
                self._delete_memory_rows([memory_id])
            else:
                self.conn.execute(
                    "UPDATE memories SET status = ?, updated_at = ? WHERE memory_id = ?",
                    (MemoryStatus.DELETED.value, self._iso_now(), memory_id),
                )
            self._insert_audit("memory_deleted", memory_id, record.scope.tenant_id, {"hard_delete": hard_delete})
            # A hard delete removes the memory row, which cascades away its
            # history; recording a DELETE event here would reference a row that
            # no longer exists (FK violation) and be wiped anyway. The audit_log
            # (no FK) is the durable record of a hard delete. Soft deletes keep
            # the row, so their history event is retained.
            if not hard_delete:
                self._insert_history(
                    memory_id,
                    record.scope.tenant_id,
                    MemoryHistoryEventType.DELETE,
                    old_memory=old_snapshot,
                    new_memory={**old_snapshot, "status": MemoryStatus.DELETED.value},
                    details={"hard_delete": hard_delete},
                    actor_id=access.principal_id if access else None,
                )
        self._purge_tenant_cache(record.scope.tenant_id)
        return DeleteResponse(memory_id=memory_id, deleted=True, hard_delete=hard_delete)

    def erase_scope(
        self,
        payload: EraseRequest,
        access: AccessContext | None = None,
    ) -> EraseResponse:
        if (
            access is not None
            and access.role != "superadmin"
            and access.tenant_id not in {None, payload.tenant_id}
        ):
            raise ValueError("erase scope tenant does not match access tenant")
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
            self._purge_tenant_cache(payload.tenant_id)
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
            self._record_repository_event_erasures(ids, "admin_erase")
            self._purge_memory_indexes(ids)
            self._delete_memory_rows(ids)
            self._insert_audit("scope_erased", None, payload.tenant_id, {"memory_ids": ids})
        self._purge_tenant_cache(payload.tenant_id)
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
        self._assert_tenant_owned_id(
            "connector",
            connector.connector_id,
            connector.tenant_id,
        )
        now = self._iso_now()
        created_at = self._to_iso(connector.created_at) or now
        updated_at = self._to_iso(connector.updated_at) or now
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO connectors (
                    connector_id, tenant_id, provider, display_name, remote_workspace_id,
                    auth_type, sync_mode, status, principal_sync_enabled, acl_sync_enabled,
                    freshness_sla_seconds, metadata_json, created_at, updated_at,
                    last_synced_at, last_webhook_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(connector_id) DO UPDATE SET
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
                WHERE connectors.tenant_id = excluded.tenant_id
                RETURNING tenant_id
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
            self._require_tenant_owned_upsert(cursor)
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
        self._assert_tenant_owned_id(
            "connector",
            connector_id,
            tenant_id,
            require_exists=True,
        )
        for source in batch.sources:
            if source.connector_id != connector_id or source.tenant_id != tenant_id:
                raise TenantOwnershipConflictError("resource write conflict")
            self._assert_tenant_owned_id("connector_source", source.source_id, tenant_id)
        now = self._iso_now()
        with self.conn:
            for source in batch.sources:
                cursor = self.conn.execute(
                    """
                    INSERT INTO connector_sources (
                        source_id, connector_id, tenant_id, remote_source_id, source_type,
                        display_name, path, status, last_synced_at, stale_after, acl_hash,
                        metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        connector_id = excluded.connector_id,
                        remote_source_id = excluded.remote_source_id,
                        source_type = excluded.source_type,
                        display_name = excluded.display_name,
                        path = excluded.path,
                        status = excluded.status,
                        last_synced_at = excluded.last_synced_at,
                        stale_after = excluded.stale_after,
                        acl_hash = excluded.acl_hash,
                        metadata_json = excluded.metadata_json,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at
                    WHERE connector_sources.tenant_id = excluded.tenant_id
                      AND connector_sources.connector_id = excluded.connector_id
                    RETURNING tenant_id
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
                self._require_tenant_owned_upsert(cursor)
            self._insert_audit(
                "connector_sources_saved",
                None,
                tenant_id,
                {"connector_id": connector_id, "count": len(batch.sources)},
            )
        self._invalidate_tenant_cache(tenant_id)
        return self.list_connector_sources(connector_id, tenant_id)

    def list_connector_sources(self, connector_id: str, tenant_id: str) -> list[ConnectorSourceRecord]:
        rows = self.conn.execute(
            "SELECT * FROM connector_sources WHERE connector_id = ? AND tenant_id = ? ORDER BY updated_at DESC",
            (connector_id, tenant_id),
        ).fetchall()
        return [self._row_to_connector_source(row) for row in rows]

    def save_principal_mappings(self, connector_id: str, tenant_id: str, batch: PrincipalMappingBatch) -> list[PrincipalMapping]:
        self._assert_tenant_owned_id(
            "connector",
            connector_id,
            tenant_id,
            require_exists=True,
        )
        for mapping in batch.mappings:
            if mapping.connector_id != connector_id or mapping.tenant_id != tenant_id:
                raise TenantOwnershipConflictError("resource write conflict")
            self._assert_tenant_owned_id(
                "principal_mapping",
                mapping.mapping_id,
                tenant_id,
            )
        now = self._iso_now()
        with self.conn:
            for mapping in batch.mappings:
                cursor = self.conn.execute(
                    """
                    INSERT INTO principal_mappings (
                        mapping_id, connector_id, tenant_id, principal_type, local_principal_id,
                        remote_principal_id, remote_name, groups_json, last_synced_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(mapping_id) DO UPDATE SET
                        connector_id = excluded.connector_id,
                        principal_type = excluded.principal_type,
                        local_principal_id = excluded.local_principal_id,
                        remote_principal_id = excluded.remote_principal_id,
                        remote_name = excluded.remote_name,
                        groups_json = excluded.groups_json,
                        last_synced_at = excluded.last_synced_at,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at
                    WHERE principal_mappings.tenant_id = excluded.tenant_id
                      AND principal_mappings.connector_id = excluded.connector_id
                    RETURNING tenant_id
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
                self._require_tenant_owned_upsert(cursor)
            self._insert_audit(
                "principal_mappings_saved",
                None,
                tenant_id,
                {"connector_id": connector_id, "count": len(batch.mappings)},
            )
        self._invalidate_tenant_cache(tenant_id)
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
        self._assert_tenant_owned_id(
            "connector",
            connector_id,
            tenant_id,
            require_exists=True,
        )
        for grant in batch.grants:
            if grant.connector_id != connector_id or grant.tenant_id != tenant_id:
                raise TenantOwnershipConflictError("resource write conflict")
            self._assert_tenant_owned_id(
                "source_permission_grant",
                grant.grant_id,
                tenant_id,
            )
            self._assert_tenant_owned_id(
                "connector_source",
                grant.source_id,
                tenant_id,
                require_exists=True,
            )
            source = self.conn.execute(
                "SELECT connector_id FROM connector_sources WHERE source_id = ? AND tenant_id = ?",
                (grant.source_id, tenant_id),
            ).fetchone()
            if source is None or source["connector_id"] != connector_id:
                raise TenantOwnershipConflictError("resource write conflict")
        now = self._iso_now()
        with self.conn:
            for grant in batch.grants:
                cursor = self.conn.execute(
                    """
                    INSERT INTO source_permission_grants (
                        grant_id, source_id, connector_id, tenant_id, principal_type,
                        principal_id, permission_level, inherited, remote_permission_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(grant_id) DO UPDATE SET
                        source_id = excluded.source_id,
                        connector_id = excluded.connector_id,
                        principal_type = excluded.principal_type,
                        principal_id = excluded.principal_id,
                        permission_level = excluded.permission_level,
                        inherited = excluded.inherited,
                        remote_permission_id = excluded.remote_permission_id,
                        created_at = excluded.created_at
                    WHERE source_permission_grants.tenant_id = excluded.tenant_id
                      AND source_permission_grants.connector_id = excluded.connector_id
                      AND source_permission_grants.source_id = excluded.source_id
                    RETURNING tenant_id
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
                self._require_tenant_owned_upsert(cursor)
            self._insert_audit(
                "source_permission_grants_saved",
                None,
                tenant_id,
                {"connector_id": connector_id, "count": len(batch.grants)},
            )
        self._invalidate_tenant_cache(tenant_id)
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
        self._assert_tenant_owned_id(
            "connector",
            connector_id,
            tenant_id,
            require_exists=True,
        )
        if job.connector_id != connector_id or job.tenant_id != tenant_id:
            raise TenantOwnershipConflictError("resource write conflict")
        self._assert_tenant_owned_id("sync_job", job.job_id, tenant_id)
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO sync_jobs (
                    job_id, connector_id, tenant_id, job_type, status, cursor, stats_json,
                    error_message, started_at, finished_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    connector_id = excluded.connector_id,
                    job_type = excluded.job_type,
                    status = excluded.status,
                    cursor = excluded.cursor,
                    stats_json = excluded.stats_json,
                    error_message = excluded.error_message,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    created_at = excluded.created_at
                WHERE sync_jobs.tenant_id = excluded.tenant_id
                  AND sync_jobs.connector_id = excluded.connector_id
                RETURNING tenant_id
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
            self._require_tenant_owned_upsert(cursor)
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
        self._assert_tenant_owned_id(
            "retention_policy",
            policy.policy_id,
            policy.tenant_id,
        )
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO retention_policies (
                    policy_id, tenant_id, kind, max_age_days, action, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    kind = excluded.kind,
                    max_age_days = excluded.max_age_days,
                    action = excluded.action,
                    created_at = excluded.created_at
                WHERE retention_policies.tenant_id = excluded.tenant_id
                RETURNING tenant_id
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
            self._require_tenant_owned_upsert(cursor)
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
        self._assert_tenant_owned_id("legal_hold", hold.hold_id, hold.tenant_id)
        resolved_ids = self._resolve_legal_hold_targets(hold.tenant_id, hold.memory_ids, hold.scope)
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO legal_holds (
                    hold_id, tenant_id, memory_ids_json, scope_json, reason, hold_until, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hold_id) DO UPDATE SET
                    memory_ids_json = excluded.memory_ids_json,
                    scope_json = excluded.scope_json,
                    reason = excluded.reason,
                    hold_until = excluded.hold_until,
                    created_at = excluded.created_at
                WHERE legal_holds.tenant_id = excluded.tenant_id
                RETURNING tenant_id
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
            self._require_tenant_owned_upsert(cursor)
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
            with self.conn:
                self._record_repository_event_erasures(deletable, "right_to_be_forgotten")
                self._purge_memory_indexes(deletable)
                self._delete_memory_rows(deletable)
        self._purge_tenant_cache(payload.tenant_id)
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
                (policy.tenant_id, policy.kind, policy.kind, cutoff.isoformat()),
            ).fetchall()
            eligible = [row["memory_id"] for row in rows if int(row["held"]) == 0]
            if not eligible:
                self._purge_tenant_cache(policy.tenant_id)
                continue
            expired.extend(eligible)
            placeholders = ", ".join("?" for _ in eligible)
            with self.conn:
                self._record_repository_event_erasures(
                    eligible,
                    f"retention:{policy.policy_id}:{policy.action}",
                )
                if policy.action == "delete_soft":
                    self.conn.execute(
                        f"UPDATE memories SET status = ?, updated_at = ? WHERE memory_id IN ({placeholders})",
                        (MemoryStatus.DELETED.value, self._iso_now(), *eligible),
                    )
                else:
                    self._purge_memory_indexes(eligible)
                    self._delete_memory_rows(eligible)
            self._purge_tenant_cache(policy.tenant_id)
        return RetentionEnforcementResponse(expired_memory_ids=expired)

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        if row is None:
            raise ValueError("row is required")
        memory_id = row["memory_id"]
        return MemoryRecord(
            memory_id=memory_id,
            fingerprint=row["fingerprint"],
            kind=MemoryKind(row["kind"]),
            memory_layer=MemoryLayer(row["memory_layer"] or self._infer_memory_layer(
                ScopeEnvelope(
                    tenant_id=row["tenant_id"],
                    workspace_id=row["workspace_id"],
                    project_id=row["project_id"],
                    user_id=row["user_id"],
                    agent_id=row["agent_id"],
                    session_id=row["session_id"],
                )
            )),
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
            expires_at=self._from_iso(row["expires_at"]),
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

    def _count_scalar(self, query: str, params: tuple[Any, ...] = ()) -> int:
        row = self.conn.execute(query, params).fetchone()
        return int(row[0]) if row else 0

    def _onboarding_step(
        self,
        step_id: str,
        label: str,
        complete: bool,
        detail: str,
        *,
        required: bool = True,
        warning: bool = False,
    ) -> OnboardingStep:
        status = "complete" if complete else ("warning" if warning else "pending")
        return OnboardingStep(
            step_id=step_id,
            label=label,
            status=status,
            detail=detail,
            required=required,
        )

    def _raw_memory_row(self, memory_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM memories WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()

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
        return self._can_access_memory(
            record,
            access,
            ACLPermission.READ,
            source_permission_levels=READABLE_PERMISSION_LEVELS,
        )

    def _can_access_memory(
        self,
        record: MemoryRecord,
        access: AccessContext | None,
        required_acl: ACLPermission,
        *,
        source_permission_levels: frozenset[str] | None = None,
        allowed_roles: frozenset[str] | None = None,
    ) -> bool:
        if access is not None and allowed_roles is not None and access.role not in allowed_roles:
            return False
        if not self._can_access(record, access, required_acl):
            return False
        return source_permission_levels is None or self._has_required_source_grants(
            record,
            access,
            source_permission_levels,
        )

    def _has_required_source_grants(
        self,
        record: MemoryRecord,
        access: AccessContext | None,
        permission_levels: frozenset[str] = READABLE_PERMISSION_LEVELS,
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
        permission_placeholders = ", ".join("?" for _ in permission_levels)
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
                *sorted(permission_levels),
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
            ORDER BY source_id ASC, connector_id ASC
            """,
            (record.scope.tenant_id, *source_ids),
        ).fetchall()

    def _storage_state(
        self,
        record: MemoryRecord,
        connected_source_count: int | None = None,
    ) -> MemoryStorageState:
        connected_source_count = (
            connected_source_count
            if connected_source_count is not None
            else len(self._connected_sources_for_record(record))
        )
        return MemoryStorageState(
            fingerprint=record.fingerprint,
            embedding_dimensions=len(record.embedding),
            source_reference_count=self._count_scalar(
                "SELECT COUNT(*) FROM memory_sources WHERE memory_id = ?",
                (record.memory_id,),
            ),
            connected_source_count=connected_source_count,
            trigger_phrase_count=self._count_scalar(
                "SELECT COUNT(*) FROM trigger_index WHERE memory_id = ?",
                (record.memory_id,),
            ),
            relation_count=self._count_scalar(
                """
                SELECT COUNT(*) FROM memory_relations
                WHERE from_memory_id = ? OR to_memory_id = ?
                """,
                (record.memory_id, record.memory_id),
            ),
            audit_event_count=self._count_scalar(
                "SELECT COUNT(*) FROM audit_log WHERE memory_id = ?",
                (record.memory_id,),
            ),
            fts_indexed=bool(
                self.conn.execute(
                    "SELECT 1 FROM memories_fts WHERE memory_id = ? LIMIT 1",
                    (record.memory_id,),
                ).fetchone()
            ),
            trigger_indexed=bool(
                self.conn.execute(
                    "SELECT 1 FROM trigger_index WHERE memory_id = ? LIMIT 1",
                    (record.memory_id,),
                ).fetchone()
            ),
        )

    def _audit_events_for_memory(self, memory_id: str, limit: int = 25) -> list[AuditEvent]:
        rows = self.conn.execute(
            """
            SELECT audit_id, action, memory_id, actor_id, tenant_id, details_json, created_at
            FROM audit_log
            WHERE memory_id = ?
            ORDER BY datetime(created_at) DESC, audit_id DESC
            LIMIT ?
            """,
            (memory_id, limit),
        ).fetchall()
        return [
            AuditEvent(
                audit_id=row["audit_id"],
                action=row["action"],
                memory_id=row["memory_id"],
                actor_id=row["actor_id"],
                tenant_id=row["tenant_id"],
                details=self._json_to_dict(row["details_json"]),
                created_at=self._from_iso(row["created_at"]) or datetime.now(UTC),
            )
            for row in rows
        ]

    def _connected_sources_for_record(self, record: MemoryRecord) -> list[ConnectedSourceInspection]:
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
        rows = self.conn.execute(
            f"""
            SELECT
                cs.source_id,
                cs.connector_id,
                cs.source_type,
                cs.display_name,
                cs.status,
                cs.last_synced_at,
                cs.stale_after,
                c.freshness_sla_seconds,
                COUNT(spg.grant_id) AS grant_count
            FROM connector_sources AS cs
            LEFT JOIN connectors AS c
                ON c.connector_id = cs.connector_id
               AND c.tenant_id = cs.tenant_id
            LEFT JOIN source_permission_grants AS spg
                ON spg.source_id = cs.source_id
               AND spg.connector_id = cs.connector_id
               AND spg.tenant_id = cs.tenant_id
            WHERE cs.tenant_id = ?
              AND cs.source_id IN ({placeholders})
            GROUP BY
                cs.source_id,
                cs.connector_id,
                cs.source_type,
                cs.display_name,
                cs.status,
                cs.last_synced_at,
                cs.stale_after,
                c.freshness_sla_seconds
            ORDER BY cs.source_id ASC
            """,
            (record.scope.tenant_id, *source_ids),
        ).fetchall()
        inspections: list[ConnectedSourceInspection] = []
        for row in rows:
            inspections.append(
                ConnectedSourceInspection(
                    source_id=row["source_id"],
                    connector_id=row["connector_id"],
                    source_type=row["source_type"],
                    display_name=row["display_name"],
                    status=SourceSyncStatus(row["status"]) if row["status"] else None,
                    grant_count=int(row["grant_count"]),
                    last_synced_at=self._from_iso(row["last_synced_at"]),
                    stale_after=self._from_iso(row["stale_after"]),
                    freshness_deadline=self._source_freshness_deadline(row),
                )
            )
        return inspections

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

    def _search_status_filter(self, include_deleted: bool, alias: str = "") -> tuple[str, list[Any]]:
        prefix = f"{alias}." if alias else ""
        if include_deleted:
            return "", []
        # Generic superseded revisions remain searchable for compatibility;
        # source-attested repo projections are rejected during evaluation.
        return (
            f" AND {prefix}status NOT IN (?, ?)",
            [MemoryStatus.DELETED.value, MemoryStatus.RETRACTED.value],
        )

    def _evaluate_search_candidate(
        self,
        record: MemoryRecord,
        payload: SearchRequest,
        access: AccessContext | None,
        query_terms: set[str],
        fts_rank: float | None = None,
        vector_rank: float | None = None,
    ) -> SearchEvaluation:
        rejection_reasons: list[str] = []
        now = datetime.now(UTC)
        if not self._can_access(record, access, ACLPermission.READ):
            rejection_reasons.append("acl denied")
        elif not self._has_required_source_grants(record, access):
            rejection_reasons.append("source grants denied")
        if not self._scope_matches(record.scope, payload.scope):
            rejection_reasons.append("scope mismatch")
        if payload.kinds and record.kind not in payload.kinds:
            rejection_reasons.append("kind filtered")
        if payload.memory_layers and record.memory_layer not in payload.memory_layers:
            rejection_reasons.append("memory layer filtered")
        missing_tags = sorted(set(payload.tags).difference(record.tags))
        if missing_tags:
            rejection_reasons.append(f"missing tags: {', '.join(missing_tags)}")
        if payload.entity_keys and not set(payload.entity_keys).intersection(record.entity_keys):
            rejection_reasons.append("entity keys filtered")
        if not payload.include_deleted:
            if record.status in {MemoryStatus.DELETED, MemoryStatus.RETRACTED}:
                rejection_reasons.append(record.status.value)
            elif (
                record.status == MemoryStatus.SUPERSEDED
                and record.metadata.get("provena_projection") == "repo-ledger"
            ):
                rejection_reasons.append(record.status.value)
        if record.valid_from and now < record.valid_from:
            rejection_reasons.append("not yet valid")
        if record.valid_to and now > record.valid_to:
            rejection_reasons.append("validity ended")
        if record.expires_at and record.expires_at <= now:
            rejection_reasons.append("expired")
        if rejection_reasons:
            return SearchEvaluation(
                record=record,
                rejection_reasons=rejection_reasons,
                fts_rank=fts_rank,
                vector_rank=vector_rank,
            )

        retrieval_score, retrieval_reasons = self._fuse_retrieval_score(
            fts_rank,
            vector_rank,
            payload.query_embedding,
            record.embedding,
        )
        score, reasons = self._score_record(
            record,
            payload.scope,
            query_terms,
            retrieval_score=retrieval_score,
        )
        reasons = retrieval_reasons + reasons
        if score <= 0:
            return SearchEvaluation(
                record=record,
                score=score,
                reasons=reasons,
                rejection_reasons=["query terms not present"],
                fts_rank=fts_rank,
                vector_rank=vector_rank,
            )
        return SearchEvaluation(
            record=record,
            score=score,
            reasons=reasons,
            fts_rank=fts_rank,
            vector_rank=vector_rank,
        )

    def _score_record(
        self,
        record: MemoryRecord,
        scope: ScopeEnvelope,
        query_terms: set[str],
        retrieval_score: float = 0.0,
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
        if query_terms and matches == 0 and retrieval_score <= 0:
            return 0.0, []

        reasons: list[str] = []
        score = float(matches)
        if retrieval_score > 0:
            score += retrieval_score * 2.5
        entity_matches = sum(1 for term in query_terms if term in {key.lower() for key in record.entity_keys})
        if entity_matches:
            score += entity_matches * 0.6
            reasons.append("entity match")
        tag_matches = sum(1 for term in query_terms if term in {tag.lower() for tag in record.tags})
        if tag_matches:
            score += tag_matches * 0.3
            reasons.append("tag match")
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
        layer_bonus, layer_reason = self._memory_layer_bonus(record, scope)
        score += layer_bonus
        if layer_reason:
            reasons.append(layer_reason)
        if record.status == MemoryStatus.ACTIVE:
            score += 0.15
        if record.status == MemoryStatus.SUPERSEDED:
            score -= 0.2
            reasons.append("superseded memory")
        feedback_summary = self.feedback_summary(record.memory_id)
        feedback_score = self._feedback_net_score(feedback_summary)
        score += feedback_score
        if feedback_score > 0:
            reasons.append("positive feedback")
        elif feedback_score < 0:
            reasons.append("negative feedback")
        score += min(record.importance, 1.0) * 0.25
        score += self._recency_bonus(record.updated_at)
        return score, reasons or ["content match"]

    def _candidate_rows(self, payload: SearchRequest) -> list[sqlite3.Row]:
        candidates, _ = self._candidate_rows_with_strategy(payload)
        return [candidate.row for candidate in candidates]

    def _search_scope_clauses(self, scope: ScopeEnvelope) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for scope_field in ("workspace_id", "project_id", "user_id", "agent_id", "session_id"):
            value = getattr(scope, scope_field)
            if value:
                clauses.append(f"(m.{scope_field} IS NULL OR m.{scope_field} = ?)")
                params.append(value)
        return clauses, params

    def _candidate_rows_with_strategy(self, payload: SearchRequest) -> tuple[list[SearchCandidate], str]:
        candidate_limit = max(payload.limit * 5, payload.limit)
        fts_candidates = self._fts_candidate_rows(payload, candidate_limit)
        vector_candidates = self._vector_candidate_rows(payload, candidate_limit)
        if fts_candidates or vector_candidates:
            merged = self._merge_search_candidates(fts_candidates, vector_candidates)
            if fts_candidates and vector_candidates:
                strategy = "hybrid"
            elif fts_candidates:
                strategy = "fts"
            else:
                strategy = "vector"
            return merged, strategy

        fallback_scope_clauses, fallback_scope_params = self._search_scope_clauses(payload.scope)
        fallback_scope_sql = ""
        if fallback_scope_clauses:
            fallback_scope_sql = " AND " + " AND ".join(
                clause.replace("m.", "") for clause in fallback_scope_clauses
            )
        status_sql, status_params = self._search_status_filter(payload.include_deleted)
        params = [
            payload.scope.tenant_id,
            *status_params,
            *fallback_scope_params,
            candidate_limit,
        ]
        rows = self.conn.execute(
            f"""
            SELECT *, NULL AS fts_rank
            FROM memories
            WHERE tenant_id = ?{status_sql}{fallback_scope_sql}
            ORDER BY datetime(updated_at) DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [SearchCandidate(row=row) for row in rows], "recency_fallback"

    def _fts_candidate_rows(self, payload: SearchRequest, candidate_limit: int) -> list[SearchCandidate]:
        if not payload.query.strip():
            return []
        query = self._fts_query(payload.query)
        if not query:
            return []
        scope_clauses, scope_params = self._search_scope_clauses(payload.scope)
        scope_sql = f" AND {' AND '.join(scope_clauses)}" if scope_clauses else ""
        status_sql, status_params = self._search_status_filter(payload.include_deleted, alias="m")
        try:
            rows = self.conn.execute(
                f"""
                SELECT m.*, bm25(memories_fts) AS fts_rank
                FROM memories_fts
                JOIN memories AS m ON m.memory_id = memories_fts.memory_id
                WHERE memories_fts MATCH ?
                  AND m.tenant_id = ?{status_sql}{scope_sql}
                ORDER BY bm25(memories_fts), datetime(m.updated_at) DESC
                LIMIT ?
                """,
                (
                    query,
                    payload.scope.tenant_id,
                    *status_params,
                    *scope_params,
                    candidate_limit,
                ),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            SearchCandidate(
                row=row,
                fts_rank=float(row["fts_rank"]) if row["fts_rank"] is not None else None,
            )
            for row in rows
        ]

    def _vector_candidates_knn(
        self, payload: SearchRequest, candidate_limit: int
    ) -> list[SearchCandidate] | None:
        """True KNN over the whole tenant's embeddings via sqlite-vec.

        Returns None to signal the caller to fall back to the linear scan (e.g.
        if the vec query errors). Over-fetches `k` then applies tenant / scope /
        status filters in SQL, so superseded/other-tenant rows can't leak.
        """
        scope_clauses, scope_params = self._search_scope_clauses(payload.scope)
        scope_sql = f" AND {' AND '.join(scope_clauses)}" if scope_clauses else ""
        status_sql, status_params = self._search_status_filter(payload.include_deleted, alias="m")
        k = max(candidate_limit * 10, candidate_limit)
        query = struct.pack(f"{len(payload.query_embedding)}f", *payload.query_embedding)
        try:
            knn_rows = self.conn.execute(
                "SELECT memory_id, distance FROM memories_vec "
                "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (query, k),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        if not knn_rows:
            return []
        distance_by_id = {row["memory_id"]: row["distance"] for row in knn_rows}
        ids = list(distance_by_id.keys())
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""
            SELECT m.*, NULL AS fts_rank
            FROM memories AS m
            WHERE m.tenant_id = ?{status_sql}{scope_sql}
              AND m.memory_id IN ({placeholders})
            """,
            (payload.scope.tenant_id, *status_params, *scope_params, *ids),
        ).fetchall()
        scored: list[SearchCandidate] = []
        for row in rows:
            distance = distance_by_id.get(row["memory_id"])
            similarity = 1.0 - distance if distance is not None else 0.0
            if similarity <= 0:
                continue
            scored.append(SearchCandidate(row=row, vector_rank=similarity))
        scored.sort(
            key=lambda candidate: (candidate.vector_rank or 0.0, candidate.row["updated_at"]),
            reverse=True,
        )
        return scored[:candidate_limit]

    def _vector_candidate_rows(self, payload: SearchRequest, candidate_limit: int) -> list[SearchCandidate]:
        if not payload.query_embedding:
            return []
        if self.vec_enabled and len(payload.query_embedding) == self.vector_dimensions:
            knn = self._vector_candidates_knn(payload, candidate_limit)
            if knn is not None:
                return knn
        scope_clauses, scope_params = self._search_scope_clauses(payload.scope)
        scope_sql = f" AND {' AND '.join(scope_clauses)}" if scope_clauses else ""
        status_sql, status_params = self._search_status_filter(payload.include_deleted, alias="m")
        scan_limit = max(candidate_limit * 6, candidate_limit)
        rows = self.conn.execute(
            f"""
            SELECT m.*, NULL AS fts_rank
            FROM memories AS m
            WHERE m.tenant_id = ?{status_sql}
              AND {self._embedding_candidate_predicate('m')}{scope_sql}
            ORDER BY datetime(m.updated_at) DESC
            LIMIT ?
            """,
            (
                payload.scope.tenant_id,
                *status_params,
                *scope_params,
                scan_limit,
            ),
        ).fetchall()
        scored: list[SearchCandidate] = []
        for row in rows:
            embedding = self._json_to_float_list(row["embedding_json"])
            similarity = self._cosine_similarity(payload.query_embedding, embedding)
            if similarity <= 0:
                continue
            scored.append(SearchCandidate(row=row, vector_rank=similarity))
        scored.sort(
            key=lambda candidate: (
                candidate.vector_rank or 0.0,
                candidate.row["updated_at"],
            ),
            reverse=True,
        )
        return scored[:candidate_limit]

    @staticmethod
    def _embedding_candidate_predicate(alias: str) -> str:
        return (
            f"{alias}.embedding_json IS NOT NULL "
            f"AND {alias}.embedding_json NOT IN ('[]', 'null', '')"
        )

    def _merge_search_candidates(
        self,
        fts_candidates: list[SearchCandidate],
        vector_candidates: list[SearchCandidate],
    ) -> list[SearchCandidate]:
        merged: dict[str, SearchCandidate] = {}
        for candidate in fts_candidates:
            memory_id = candidate.row["memory_id"]
            merged[memory_id] = candidate
        for candidate in vector_candidates:
            memory_id = candidate.row["memory_id"]
            existing = merged.get(memory_id)
            if existing is None:
                merged[memory_id] = candidate
                continue
            if candidate.vector_rank is not None:
                existing.vector_rank = candidate.vector_rank
        return list(merged.values())

    def _normalize_fts_rank(self, fts_rank: float | None) -> float:
        if fts_rank is None:
            return 0.0
        magnitude = abs(fts_rank)
        return magnitude / (1.0 + magnitude)

    def _fuse_retrieval_score(
        self,
        fts_rank: float | None,
        vector_rank: float | None,
        query_embedding: list[float],
        record_embedding: list[float],
    ) -> tuple[float, list[str]]:
        fts_score = self._normalize_fts_rank(fts_rank)
        vector_score = vector_rank
        if vector_score is None and query_embedding and record_embedding:
            vector_score = self._cosine_similarity(query_embedding, record_embedding)
        reasons: list[str] = []
        if fts_score > 0:
            reasons.append("fts match")
        if vector_score and vector_score > 0:
            reasons.append("vector similarity")
        if not query_embedding:
            return fts_score, reasons
        if fts_score > 0 and vector_score and vector_score > 0:
            fused = fts_score * (1.0 - _SEARCH_VECTOR_WEIGHT) + vector_score * _SEARCH_VECTOR_WEIGHT
            reasons.append("hybrid retrieval")
            return fused, reasons
        if vector_score and vector_score > 0:
            return vector_score, reasons
        return fts_score, reasons

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
        now = datetime.now(UTC)
        for row in rows:
            record = self.get_memory(
                row["to_memory_id"],
                access=access,
                enforce_source_grants=True,
            )
            if (
                record is None
                or record.status in {MemoryStatus.DELETED, MemoryStatus.RETRACTED}
                or (record.valid_from is not None and now < record.valid_from)
                or (record.valid_to is not None and now > record.valid_to)
                or (record.expires_at is not None and record.expires_at <= now)
            ):
                continue
            related.append(RelatedMemory(relation=RelationKind(row["relation"]), memory=record))
        return related

    def _citation_labels(self, record: MemoryRecord) -> list[str]:
        labels: list[str] = []
        for source in record.source_references:
            label = f"{source.source_type}:{source.source_id}"
            if source.title:
                label = f"{label} ({source.title})"
            if source.uri:
                label = f"{label} <{source.uri}>"
            labels.append(label)
        return labels

    def _adjacent_memory_ids(self, memory_id: str, tenant_id: str) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT from_memory_id, to_memory_id
            FROM memory_relations
            WHERE tenant_id = ?
              AND (from_memory_id = ? OR to_memory_id = ?)
            ORDER BY datetime(created_at) DESC
            """,
            (tenant_id, memory_id, memory_id),
        ).fetchall()
        adjacent: list[str] = []
        for row in rows:
            adjacent.append(row["to_memory_id"] if row["from_memory_id"] == memory_id else row["from_memory_id"])
        return adjacent

    def _temporal_edges(self, memory_ids: list[str], tenant_id: str) -> list[TemporalGraphEdge]:
        if not memory_ids:
            return []
        placeholders = ", ".join("?" for _ in memory_ids)
        rows = self.conn.execute(
            f"""
            SELECT from_memory_id, to_memory_id, relation, created_at
            FROM memory_relations
            WHERE tenant_id = ?
              AND from_memory_id IN ({placeholders})
              AND to_memory_id IN ({placeholders})
            ORDER BY datetime(created_at) DESC
            """,
            (tenant_id, *memory_ids, *memory_ids),
        ).fetchall()
        return [
            TemporalGraphEdge(
                from_memory_id=row["from_memory_id"],
                to_memory_id=row["to_memory_id"],
                relation=RelationKind(row["relation"]),
                created_at=self._from_iso(row["created_at"]) or datetime.now(UTC),
            )
            for row in rows
        ]

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
            """
            SELECT * FROM memory_sources
            WHERE memory_id = ?
            ORDER BY source_type ASC, source_id ASC,
                     COALESCE(span_start, 0) ASC, source_ref_id ASC
            """,
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
            """
            SELECT phrase FROM trigger_index
            WHERE memory_id = ?
            ORDER BY phrase ASC, trigger_id ASC
            """,
            (memory_id,),
        ).fetchall()
        return [row["phrase"] for row in rows]

    def _trigger_rows_for(self, memory_id: str) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            """
            SELECT phrase, tenant_id FROM trigger_index
            WHERE memory_id = ?
            ORDER BY phrase ASC, trigger_id ASC
            """,
            (memory_id,),
        ).fetchall()
        return sorted(
            [(row["phrase"], row["tenant_id"]) for row in rows],
            key=lambda item: (self._repository_text_sort_key(item[0]), item[1]),
        )

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

    def _memory_index_matches(
        self,
        memory_id: str,
        title: str | None,
        summary: str | None,
        content: str,
        tags: list[str],
        entity_keys: list[str],
    ) -> bool:
        rows = self.conn.execute(
            """
            SELECT title, summary, content, tags, entity_keys
            FROM memories_fts WHERE memory_id = ?
            """,
            (memory_id,),
        ).fetchall()
        expected = (
            title,
            summary,
            content,
            " ".join(tags),
            " ".join(entity_keys),
        )
        return len(rows) == 1 and tuple(rows[0]) == expected

    def _assert_tenant_owned_id(
        self,
        resource: str,
        resource_id: str,
        tenant_id: str,
        *,
        require_exists: bool = False,
    ) -> None:
        """Prevent a globally unique id from being moved across tenants.

        The database keeps these ids globally unique for stable connector and
        governance references. Every upsert must therefore prove ownership
        before its conflict clause can update the row. `resource` is selected
        from a closed internal map so table and column names never come from a
        request.
        """

        try:
            table, id_column = TENANT_OWNED_ID_TABLES[resource]
        except KeyError as exc:  # pragma: no cover - internal programming error
            raise RuntimeError(f"unknown tenant-owned resource: {resource}") from exc
        row = self.conn.execute(
            f"SELECT tenant_id FROM {table} WHERE {id_column} = ?",
            (resource_id,),
        ).fetchone()
        if row is None:
            if require_exists:
                raise TenantOwnershipConflictError("resource write conflict")
            return
        if row["tenant_id"] != tenant_id:
            raise TenantOwnershipConflictError("resource write conflict")

    @staticmethod
    def _require_tenant_owned_upsert(cursor: Any) -> None:
        """Reject an atomic guarded upsert that lost a tenant-id race."""

        if cursor.fetchone() is None:
            raise TenantOwnershipConflictError("resource write conflict")

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
        for scope_field in ("workspace_id", "project_id", "user_id", "agent_id", "session_id"):
            value = scope.get(scope_field)
            if value:
                clauses.append(f"{scope_field} = ?")
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
        for scope_field in ("workspace_id", "project_id", "user_id", "agent_id", "session_id"):
            expected = scope_filter.get(scope_field)
            if expected and getattr(memory_scope, scope_field) != expected:
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
            "memory_layer": f"ALTER TABLE memories ADD COLUMN memory_layer TEXT NOT NULL DEFAULT '{MemoryLayer.ORGANIZATION.value}'",
            "expires_at": "ALTER TABLE memories ADD COLUMN expires_at TEXT",
        }
        with self.conn:
            for column, statement in migrations.items():
                if column not in columns:
                    self.conn.execute(statement)
            projection_columns = {
                row["name"]
                for row in self.conn.execute(
                    "PRAGMA table_info(repo_memory_event_projections)"
                ).fetchall()
            }
            if "projection_version" not in projection_columns:
                self.conn.execute(
                    """
                    ALTER TABLE repo_memory_event_projections
                    ADD COLUMN projection_version INTEGER NOT NULL DEFAULT 1
                    """
                )

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

    def _memory_layer_bonus(self, record: MemoryRecord, scope: ScopeEnvelope) -> tuple[float, str | None]:
        if record.memory_layer == MemoryLayer.CONVERSATION and record.scope.session_id == scope.session_id:
            return 1.1, "conversation layer"
        if record.memory_layer == MemoryLayer.SESSION and record.scope.session_id == scope.session_id:
            return 0.9, "session layer"
        if record.memory_layer == MemoryLayer.USER and record.scope.user_id == scope.user_id:
            return 0.7, "user layer"
        if record.memory_layer == MemoryLayer.AGENT and record.scope.agent_id == scope.agent_id:
            return 0.55, "agent layer"
        if record.memory_layer == MemoryLayer.ORGANIZATION:
            return 0.35, "organization layer"
        return 0.0, None

    def _feedback_net_score(self, summary: MemoryFeedbackSummary) -> float:
        return round(
            min(summary.positive * 0.12, 0.6)
            - min(summary.negative * 0.15, 0.6)
            + min(summary.correction * 0.05, 0.25)
            + min(summary.pin * 0.4, 0.8),
            4,
        )

    def _search_cache_key(self, payload: SearchRequest, access: AccessContext | None) -> str:
        envelope = {
            "query": payload.query.strip().lower(),
            "scope": payload.scope.model_dump(exclude_none=True),
            "kinds": sorted(kind.value for kind in payload.kinds),
            "memory_layers": sorted(layer.value for layer in payload.memory_layers),
            "tags": sorted(payload.tags),
            "entity_keys": sorted(payload.entity_keys),
            "query_embedding": payload.query_embedding,
            "include_relations": payload.include_relations,
            "include_deleted": payload.include_deleted,
            "limit": payload.limit,
            "access": {
                "tenant_id": access.tenant_id if access else None,
                "role": access.role if access else None,
                "principal_id": access.principal_id if access else None,
                "groups": sorted(access.groups) if access else [],
            },
        }
        return hashlib.sha256(self._to_json(envelope).encode("utf-8")).hexdigest()

    def _invalidate_tenant_cache(self, tenant_id: str) -> None:
        self.hot_cache.bump_search_version(tenant_id)

    def _purge_tenant_cache(self, tenant_id: str) -> None:
        self.hot_cache.purge_tenant(tenant_id)

    def _insert_history(
        self,
        memory_id: str,
        tenant_id: str,
        event: MemoryHistoryEventType,
        old_memory: dict[str, Any],
        new_memory: dict[str, Any],
        details: dict[str, Any],
        actor_id: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO memory_history (
                history_id, memory_id, tenant_id, event, actor_id, old_memory_json, new_memory_json, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                memory_id,
                tenant_id,
                event.value,
                actor_id,
                self._to_json(old_memory),
                self._to_json(new_memory),
                self._to_json(details),
                self._iso_now(),
            ),
        )

    def _infer_memory_layer(self, scope: ScopeEnvelope) -> MemoryLayer:
        if scope.session_id:
            return MemoryLayer.SESSION
        if scope.agent_id and not scope.user_id:
            return MemoryLayer.AGENT
        if scope.user_id:
            return MemoryLayer.USER
        return MemoryLayer.ORGANIZATION

    def _canonical_repository_json(self, value: Any) -> str:
        return rfc8785.dumps(value).decode("utf-8")

    def _repository_json_object(self, pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def _sort_repository_json(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._sort_repository_json(item) for item in value]
        if isinstance(value, dict):
            return {
                key: self._sort_repository_json(value[key])
                for key in sorted(value, key=self._repository_text_sort_key)
            }
        return value

    def _repository_text_sort_key(self, value: str) -> bytes:
        return value.encode("utf-16-be", errors="surrogatepass")

    def _elapsed_ms(self, started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 3)

    def _chunks(self, values: Iterable[str], size: int = 500) -> Iterable[list[str]]:
        items = list(values)
        for offset in range(0, len(items), size):
            yield items[offset : offset + size]

    def _repository_memory_id(
        self,
        scope: ScopeEnvelope,
        repository_id: str,
        event_id: str,
    ) -> str:
        if scope.project_id is None:
            raise ValueError("repository projection requires project_id")
        digest = hashlib.sha256(
            (
                f"repo-ledger\0{scope.tenant_id}\0{scope.project_id}\0"
                f"{repository_id}\0{event_id}"
            ).encode("utf-8")
        ).hexdigest()
        return f"repoevt_{digest}"

    def _repository_projection_fingerprint(
        self,
        scope: ScopeEnvelope,
        repository_id: str,
        event_fingerprint: str,
    ) -> str:
        value = self._canonical_repository_json(
            {
                "projection_version": REPOSITORY_LEDGER_PROJECTION_VERSION,
                "scope": scope.model_dump(exclude_none=True),
                "repository_id": repository_id,
                "event_fingerprint": event_fingerprint,
            }
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _repository_projection_status(
        self,
        event: RepositoryMemoryEvent,
        referenced_status: MemoryStatus | None,
        current_status: str | None = None,
        pre_hold_status: str | None = None,
    ) -> tuple[MemoryStatus, MemoryStatus | None]:
        rank = {
            MemoryStatus.ACTIVE: 0,
            MemoryStatus.SUPERSEDED: 1,
            MemoryStatus.RETRACTED: 2,
        }
        desired = MemoryStatus(event.status.value)
        if referenced_status is not None and rank[referenced_status] > rank[desired]:
            desired = referenced_status
        if current_status == MemoryStatus.HELD.value:
            held_base = (
                MemoryStatus(pre_hold_status)
                if pre_hold_status in {item.value for item in rank}
                else MemoryStatus.ACTIVE
            )
            return MemoryStatus.HELD, max((held_base, desired), key=rank.get)
        if current_status in {item.value for item in rank}:
            current = MemoryStatus(current_status)
            desired = max((current, desired), key=rank.get)
        return desired, None

    def _attested_repository_projection(
        self,
        memory_id: str,
        *,
        row: Any | None = None,
        expected_scope: ScopeEnvelope | None = None,
        expected_repository_id: str | None = None,
        expected_event_record: dict[str, Any] | None = None,
        expected_event_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        row = row or self.conn.execute(
            "SELECT * FROM memories WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            return None
        metadata = self._json_to_dict(row["metadata_json"])
        metadata_keys = {
            "provena_projection",
            "provena_projection_version",
            "provena_repository_id",
            "provena_event",
            "provena_event_fingerprint",
            "provena_ingested_at",
        }
        event_record = metadata.get("provena_event")
        repository_id = metadata.get("provena_repository_id")
        event_fingerprint = metadata.get("provena_event_fingerprint")
        if not all(
            [
                set(metadata) == metadata_keys,
                metadata.get("provena_projection") == "repo-ledger",
                metadata.get("provena_projection_version")
                == REPOSITORY_LEDGER_PROJECTION_VERSION,
                isinstance(repository_id, str),
                bool(repository_id),
                isinstance(event_record, dict),
                isinstance(event_fingerprint, str),
                isinstance(metadata.get("provena_ingested_at"), str),
                bool(metadata.get("provena_ingested_at")),
            ]
        ):
            return None
        try:
            event = RepositoryMemoryEvent.model_validate(event_record)
            canonical_event = self._canonical_repository_json(event_record)
        except (ValidationError, ValueError, TypeError, rfc8785.CanonicalizationError):
            return None
        if hashlib.sha256(canonical_event.encode("utf-8")).hexdigest() != event_fingerprint:
            return None
        if expected_repository_id is not None and repository_id != expected_repository_id:
            return None
        if expected_event_fingerprint is not None and event_fingerprint != expected_event_fingerprint:
            return None
        if expected_event_record is not None:
            try:
                if canonical_event != self._canonical_repository_json(expected_event_record):
                    return None
            except (ValueError, TypeError, rfc8785.CanonicalizationError):
                return None

        scopes: list[ScopeEnvelope] = []
        if expected_scope is not None:
            scopes.append(expected_scope)
        else:
            scope_rows = self.conn.execute(
                """
                SELECT tenant_id, project_id
                FROM repo_memory_sync_state
                WHERE repository_id = ?
                """,
                (repository_id,),
            ).fetchall()
            scopes.extend(
                ScopeEnvelope(tenant_id=item["tenant_id"], project_id=item["project_id"])
                for item in scope_rows
            )
        for scope in scopes:
            if self._repository_memory_id(scope, repository_id, event.id) != memory_id:
                continue
            projection_fingerprint = self._repository_projection_fingerprint(
                scope,
                repository_id,
                event_fingerprint,
            )
            if row["fingerprint"] != projection_fingerprint:
                continue
            return {
                "tenant_id": scope.tenant_id,
                "project_id": scope.project_id,
                "repository_id": repository_id,
                "event_id": event.id,
                "event_fingerprint": event_fingerprint,
                "event": event,
                "row": row,
            }
        return None

    def _is_repository_event_projection(self, memory_id: str) -> bool:
        mapped = self.conn.execute(
            "SELECT 1 FROM repo_memory_event_projections WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        return mapped is not None or self._attested_repository_projection(memory_id) is not None

    def _validate_supersession_target(
        self,
        target_memory_id: str,
        successor_memory_id: str,
        tenant_id: str,
        access: AccessContext | None,
    ) -> None:
        if target_memory_id == successor_memory_id:
            raise ValueError("a memory cannot supersede itself")
        row = self.conn.execute(
            "SELECT * FROM memories WHERE memory_id = ?",
            (target_memory_id,),
        ).fetchone()
        if row is None:
            raise ValueError("superseded memory not found or inaccessible")
        target = self._row_to_record(row)
        if target.scope.tenant_id != tenant_id or not self._can_access_memory(
            target,
            access,
            ACLPermission.WRITE,
            source_permission_levels=EDITABLE_PERMISSION_LEVELS,
            allowed_roles=MEMORY_WRITE_ROLES,
        ):
            raise ValueError("superseded memory not found or inaccessible")
        if self._is_repository_event_projection(target_memory_id):
            raise RepositoryEventConflictError(
                "repository ledger projections are immutable; append a source-attested superseding event"
            )

    def _record_repository_event_erasures(
        self,
        memory_ids: Iterable[str],
        reason: str,
    ) -> int:
        ids = sorted(set(memory_ids))
        if not ids:
            return 0
        rows: list[Any] = []
        mapped_ids: set[str] = set()
        for id_chunk in self._chunks(ids):
            placeholders = ", ".join("?" for _ in id_chunk)
            mapped_rows = self.conn.execute(
                    f"""
                    SELECT p.memory_id, p.tenant_id, p.project_id, p.repository_id, p.event_id,
                           p.event_fingerprint, m.metadata_json, m.created_at
                    FROM repo_memory_event_projections AS p
                    JOIN memories AS m ON m.memory_id = p.memory_id
                    WHERE p.memory_id IN ({placeholders})
                    """,
                    id_chunk,
                ).fetchall()
            rows.extend(mapped_rows)
            mapped_ids.update(row["memory_id"] for row in mapped_rows)
        for memory_id in ids:
            if memory_id in mapped_ids:
                continue
            attested = self._attested_repository_projection(memory_id)
            if attested is None:
                continue
            memory_row = attested["row"]
            rows.append(
                {
                    "tenant_id": attested["tenant_id"],
                    "project_id": attested["project_id"],
                    "repository_id": attested["repository_id"],
                    "event_id": attested["event_id"],
                    "event_fingerprint": attested["event_fingerprint"],
                    "metadata_json": memory_row["metadata_json"],
                    "created_at": memory_row["created_at"],
                }
            )
        erased_at = self._iso_now()
        for row in rows:
            metadata = self._json_to_dict(row["metadata_json"])
            try:
                event = RepositoryMemoryEvent.model_validate(metadata.get("provena_event"))
                authority = event.authority.value
                event_created_at = event.created_at
            except ValidationError:
                # Governance deletion must not be blockable by legacy or
                # corrupted projection metadata. Highest authority is the
                # conservative admission fallback for later supersession.
                authority = "human"
                event_created_at = row["created_at"]
                self._insert_audit(
                    "repository_memory_erasure_provenance_fallback",
                    None,
                    row["tenant_id"],
                    {
                        "repository_id": row["repository_id"],
                        "event_id": row["event_id"],
                        "reason": reason,
                    },
                )
            self.conn.execute(
                """
                INSERT INTO repo_memory_event_erasures (
                    tenant_id, project_id, repository_id, event_id,
                    event_fingerprint, authority, event_created_at, erased_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, project_id, repository_id, event_id) DO UPDATE SET
                    event_fingerprint = excluded.event_fingerprint,
                    authority = excluded.authority,
                    event_created_at = excluded.event_created_at,
                    erased_at = excluded.erased_at,
                    reason = excluded.reason
                """,
                (
                    row["tenant_id"],
                    row["project_id"],
                    row["repository_id"],
                    row["event_id"],
                    row["event_fingerprint"],
                    authority,
                    event_created_at,
                    erased_at,
                    reason,
                ),
            )
        return len(rows)

    def _repository_event_entity_keys(
        self,
        repository_id: str,
        event: RepositoryMemoryEvent,
    ) -> list[str]:
        keys = {
            f"repo:{repository_id}",
            f"subject:{event.subject_type.value}",
        }
        for path in event.applies_to:
            keys.add(f"file:{repository_id}:{path}")
        for source in event.sources:
            keys.add(f"file:{repository_id}:{source.path}")
            if source.symbol:
                keys.add(f"symbol:{repository_id}:{source.path}#{source.symbol}")
        return sorted(keys, key=self._repository_text_sort_key)

    def _repository_event_sources(
        self,
        repository_id: str,
        ledger_path: str,
        event: RepositoryMemoryEvent,
        event_fingerprint: str,
    ) -> list[SourceReference]:
        encoded_repository_id = quote(repository_id, safe="")
        sources = [
            SourceReference(
                source_type="repository_ledger",
                source_id=f"repo-event:{repository_id}:{event.id}",
                uri=(
                    f"provena://repository/{encoded_repository_id}/memory-events/"
                    f"{quote(event.id, safe='')}"
                ),
                title=ledger_path,
                metadata={
                    "repository_id": repository_id,
                    "event_id": event.id,
                    "event_fingerprint": event_fingerprint,
                },
            )
        ]
        for source in event.sources:
            source_identity = self._canonical_repository_json(
                {
                    "repository_id": repository_id,
                    "path": source.path,
                    "symbol": source.symbol,
                    "start_line": source.start_line,
                    "end_line": source.end_line,
                }
            )
            source_id = "repo-file:" + hashlib.sha256(source_identity.encode("utf-8")).hexdigest()
            metadata = {
                "repository_id": repository_id,
                "event_id": event.id,
                **source.model_dump(exclude={"path", "start_line", "end_line"}, exclude_none=True),
            }
            sources.append(
                SourceReference(
                    source_type="repository_file",
                    source_id=source_id,
                    uri=(
                        f"provena://repository/{encoded_repository_id}/"
                        f"{quote(source.path, safe='/')}"
                    ),
                    title=source.path,
                    span_start=source.start_line,
                    span_end=source.end_line,
                    metadata=metadata,
                )
            )
        return sources

    def _fingerprint(
        self,
        scope: ScopeEnvelope,
        kind: str,
        title: str | None,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        # JSON.stringify emits Unicode directly; preserve exact CLI parity for
        # deterministic fingerprints when scope identifiers are non-ASCII.
        scope_json = json.dumps(
            scope.model_dump(exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        generated = (metadata or {}).get("provena_generated_fingerprint")
        parts = [
            scope_json,
            kind,
            (title or "").strip().lower(),
            content.strip().lower(),
        ]
        if isinstance(generated, str) and re.fullmatch(r"[0-9a-f]{64}", generated):
            parts.extend(["provena-generated-v2", generated])
        value = "|".join(parts)
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
