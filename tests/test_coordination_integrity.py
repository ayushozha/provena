import importlib
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.models import (
    ACLEntry,
    ACLPermission,
    ConnectorAuthType,
    ConnectorConfig,
    ConnectorProvider,
    ConnectorSourceBatch,
    ConnectorSourceRecord,
    EraseRequest,
    LegalHold,
    MemoryCreate,
    MemoryHistoryEventType,
    MemoryKind,
    MemoryStatus,
    MemoryUpdate,
    PermissionLevel,
    PrincipalMapping,
    PrincipalMappingBatch,
    RTBFRequest,
    RetentionPolicy,
    ScopeEnvelope,
    SearchRequest,
    SourcePermissionBatch,
    SourcePermissionGrant,
    SyncJob,
    SyncJobType,
)
from app.store import (
    AccessContext,
    IDENTITY_FINGERPRINT_PREFIX,
    ProvenaStore,
    SCHEMA_PATH,
    TenantIntegrityError,
    TenantOwnershipError,
    TenantParentNotFoundError,
)


def _origin_main_sqlite_schema_shape() -> str:
    """Return the checked-in schema with the origin/main integration FK shape."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    schema = schema.replace("    create_request_digest TEXT,\n", "")
    schema = schema.replace(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_connectors_id_tenant ON connectors(connector_id, tenant_id);\n",
        "",
    )
    schema = schema.replace(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_connector_sources_id_connector_tenant\n"
        "    ON connector_sources(source_id, connector_id, tenant_id);\n",
        "",
    )
    replacements = {
        "    CONSTRAINT fk_connector_sources_connector_tenant\n"
        "        FOREIGN KEY(connector_id, tenant_id) REFERENCES connectors(connector_id, tenant_id) ON DELETE CASCADE":
            "    FOREIGN KEY(connector_id) REFERENCES connectors(connector_id) ON DELETE CASCADE",
        "    CONSTRAINT fk_principal_mappings_connector_tenant\n"
        "        FOREIGN KEY(connector_id, tenant_id) REFERENCES connectors(connector_id, tenant_id) ON DELETE CASCADE":
            "    FOREIGN KEY(connector_id) REFERENCES connectors(connector_id) ON DELETE CASCADE",
        "    CONSTRAINT fk_source_permission_grants_source_tenant\n"
        "        FOREIGN KEY(source_id, connector_id, tenant_id)\n"
        "        REFERENCES connector_sources(source_id, connector_id, tenant_id) ON DELETE CASCADE":
            "    FOREIGN KEY(source_id) REFERENCES connector_sources(source_id) ON DELETE CASCADE",
        "    CONSTRAINT fk_source_permission_grants_connector_tenant\n"
        "        FOREIGN KEY(connector_id, tenant_id) REFERENCES connectors(connector_id, tenant_id) ON DELETE CASCADE":
            "    FOREIGN KEY(connector_id) REFERENCES connectors(connector_id) ON DELETE CASCADE",
        "    CONSTRAINT fk_sync_jobs_connector_tenant\n"
        "        FOREIGN KEY(connector_id, tenant_id) REFERENCES connectors(connector_id, tenant_id) ON DELETE CASCADE":
            "    FOREIGN KEY(connector_id) REFERENCES connectors(connector_id) ON DELETE CASCADE",
    }
    for current, legacy in replacements.items():
        if current not in schema:
            raise AssertionError(f"current schema no longer contains expected migration fixture: {current}")
        schema = schema.replace(current, legacy)
    return schema


class TenantIntegrityUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="provena-schema-upgrade-"))
        self.db_path = self.temp_dir / "legacy.db"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed_legacy_database(
        self,
        *,
        invalid_table: str | None = None,
        orphan_source: bool = False,
    ) -> None:
        now = "2026-07-13T00:00:00+00:00"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(_origin_main_sqlite_schema_shape())
            conn.execute(
                """
                INSERT INTO connectors (
                    connector_id, tenant_id, provider, display_name, auth_type,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("connector-upgrade", "tenant-a", "custom", "Legacy connector", "api_key", now, now),
            )
            source_tenant = "tenant-b" if invalid_table == "connector_sources" else "tenant-a"
            source_connector = "missing-connector" if orphan_source else "connector-upgrade"
            conn.execute(
                """
                INSERT INTO connector_sources (
                    source_id, connector_id, tenant_id, remote_source_id,
                    source_type, display_name, path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "source-upgrade",
                    source_connector,
                    source_tenant,
                    "remote-upgrade",
                    "repository",
                    "Private legacy source",
                    "/tenant-a/private",
                    now,
                    now,
                ),
            )
            if invalid_table != "connector_sources" and not orphan_source:
                conn.execute(
                    """
                    INSERT INTO principal_mappings (
                        mapping_id, connector_id, tenant_id, principal_type,
                        local_principal_id, remote_principal_id, last_synced_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "mapping-upgrade",
                        "connector-upgrade",
                        "tenant-b" if invalid_table == "principal_mappings" else "tenant-a",
                        "user",
                        "local-user",
                        "remote-user",
                        now,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO source_permission_grants (
                        grant_id, source_id, connector_id, tenant_id,
                        principal_type, principal_id, permission_level, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "grant-upgrade",
                        "source-upgrade",
                        "connector-upgrade",
                        "tenant-b" if invalid_table == "source_permission_grants" else "tenant-a",
                        "user",
                        "local-user",
                        "view",
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO sync_jobs (
                        job_id, connector_id, tenant_id, job_type, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "job-upgrade",
                        "connector-upgrade",
                        "tenant-b" if invalid_table == "sync_jobs" else "tenant-a",
                        "full",
                        now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def test_origin_main_schema_upgrades_atomically_and_preserves_valid_rows(self) -> None:
        self._seed_legacy_database()

        store = ProvenaStore(self.db_path)
        try:
            self.assertTrue(store.tenant_integrity_status()["ready"])
            self.assertFalse(store._sqlite_requires_tenant_integrity_rebuild())
            for table in (
                "connector_sources",
                "principal_mappings",
                "source_permission_grants",
                "sync_jobs",
            ):
                count = store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertEqual(count, 1, table)

            with self.assertRaises(sqlite3.IntegrityError):
                with store.conn:
                    store.conn.execute(
                        """
                        INSERT INTO connector_sources (
                            source_id, connector_id, tenant_id, remote_source_id,
                            source_type, display_name, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "source-cross-tenant",
                            "connector-upgrade",
                            "tenant-b",
                            "remote-cross-tenant",
                            "repository",
                            "Must fail",
                            store._iso_now(),
                            store._iso_now(),
                        ),
                    )
        finally:
            store.close()

        reopened = ProvenaStore(self.db_path)
        try:
            self.assertTrue(reopened.tenant_integrity_status()["ready"])
            self.assertFalse(reopened._sqlite_requires_tenant_integrity_rebuild())
            self.assertEqual(
                reopened.conn.execute("SELECT COUNT(*) FROM connector_sources").fetchone()[0],
                1,
            )
        finally:
            reopened.close()

    def test_origin_main_schema_with_mismatch_fails_before_table_rebuild(self) -> None:
        invalid_rows = {
            "connector_sources": ("connector_sources", "source_id", "source-upgrade"),
            "principal_mappings": ("principal_mappings", "mapping_id", "mapping-upgrade"),
            "source_permission_grants": (
                "source_permission_grants",
                "grant_id",
                "grant-upgrade",
            ),
            "sync_jobs": ("sync_jobs", "job_id", "job-upgrade"),
        }
        for invalid_table, (table, id_column, row_id) in invalid_rows.items():
            with self.subTest(table=invalid_table):
                self.db_path = self.temp_dir / f"legacy-{invalid_table}.db"
                self._seed_legacy_database(invalid_table=invalid_table)

                with self.assertRaisesRegex(TenantIntegrityError, f"{invalid_table}=1"):
                    ProvenaStore(self.db_path)

                conn = sqlite3.connect(self.db_path)
                try:
                    tenant = conn.execute(
                        f"SELECT tenant_id FROM {table} WHERE {id_column} = ?",
                        (row_id,),
                    ).fetchone()
                    self.assertEqual(tenant, ("tenant-b",))
                    temporary_tables = conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE name LIKE '%tenant_integrity_new%'"
                    ).fetchall()
                    self.assertEqual(temporary_tables, [])
                    foreign_keys = conn.execute(
                        "PRAGMA foreign_key_list(connector_sources)"
                    ).fetchall()
                    self.assertEqual({row[3] for row in foreign_keys}, {"connector_id"})
                finally:
                    conn.close()

    def test_origin_main_schema_with_orphan_fails_before_table_rebuild(self) -> None:
        self._seed_legacy_database(orphan_source=True)

        with self.assertRaisesRegex(TenantIntegrityError, "connector_sources=1"):
            ProvenaStore(self.db_path)

        conn = sqlite3.connect(self.db_path)
        try:
            source = conn.execute(
                "SELECT connector_id, path FROM connector_sources WHERE source_id = ?",
                ("source-upgrade",),
            ).fetchone()
            self.assertEqual(source, ("missing-connector", "/tenant-a/private"))
            self.assertEqual(
                conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE name LIKE '%tenant_integrity_new%'"
                ).fetchall(),
                [],
            )
        finally:
            conn.close()


class DeterministicMemoryLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="provena-integrity-"))
        self.store = ProvenaStore(self.temp_dir / "provena.db")
        self.scope = ScopeEnvelope(
            tenant_id="tenant-neverzero",
            workspace_id="workspace-shared",
        )
        self.access = AccessContext(
            tenant_id=self.scope.tenant_id,
            role="editor",
            principal_id="neverzero-service",
        )

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _payload(self, memory_id: str, content: str = "Shared context body") -> MemoryCreate:
        return MemoryCreate(
            memory_id=memory_id,
            kind=MemoryKind.FACT,
            scope=self.scope,
            title="NeverZero context",
            content=content,
        )

    def _count(self, table: str, where: str, params: tuple[object, ...]) -> int:
        row = self.store.conn.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE {where}",
            params,
        ).fetchone()
        return int(row["count"])

    def _creation_counts(self, memory_id: str) -> tuple[int, int, int]:
        return (
            self._count("memories", "memory_id = ?", (memory_id,)),
            self._count(
                "audit_log",
                "memory_id = ? AND action = ?",
                (memory_id, "memory_created"),
            ),
            self._count(
                "memory_history",
                "memory_id = ? AND event = ?",
                (memory_id, MemoryHistoryEventType.ADD.value),
            ),
        )

    def _relation_count(self) -> int:
        return self._count("memory_relations", "1 = 1", ())

    def test_deterministic_ids_generate_isolated_fingerprints_for_equal_content(self) -> None:
        first = self.store.create_memory(self._payload("context-a"), access=self.access)
        second = self.store.create_memory(self._payload("context-b"), access=self.access)

        self.assertTrue(first.created)
        self.assertTrue(second.created)
        self.assertTrue(first.memory.fingerprint.startswith(IDENTITY_FINGERPRINT_PREFIX))
        self.assertTrue(second.memory.fingerprint.startswith(IDENTITY_FINGERPRINT_PREFIX))
        self.assertNotEqual(first.memory.fingerprint, second.memory.fingerprint)

    def test_soft_delete_tombstone_wins_over_ambiguous_create_and_update_retries(self) -> None:
        payload = self._payload("context-retry")
        created = self.store.create_memory(payload, access=self.access)

        # A committed create whose response timed out is idempotent on retry.
        create_retry = self.store.create_memory(payload, access=self.access)
        self.assertFalse(create_retry.created)
        self.assertEqual(create_retry.memory.status, MemoryStatus.ACTIVE)

        updated = self.store.update_memory(
            payload.memory_id,
            MemoryUpdate(content="Newer context body"),
            access=self.access,
        )
        self.assertEqual(updated.memory.fingerprint, created.memory.fingerprint)

        deleted = self.store.delete_memory(payload.memory_id, access=self.access)
        self.assertTrue(deleted.deleted)

        # Delayed writes may change tombstone metadata, but can never reactivate it.
        delayed_update = self.store.update_memory(
            payload.memory_id,
            MemoryUpdate(content="Stale delayed update"),
            access=self.access,
        )
        delayed_create = self.store.create_memory(payload, access=self.access)
        self.assertEqual(delayed_update.memory.status, MemoryStatus.DELETED)
        self.assertFalse(delayed_create.created)
        self.assertEqual(delayed_create.memory.status, MemoryStatus.DELETED)
        self.assertEqual(delayed_create.memory.fingerprint, created.memory.fingerprint)

        visible = self.store.search_memories(
            SearchRequest(query="context", scope=self.scope),
            access=self.access,
        )
        self.assertEqual(visible.results, [])

    def test_concurrent_deterministic_creates_converge_on_one_memory(self) -> None:
        second_store = ProvenaStore(self.temp_dir / "provena.db")
        barrier = Barrier(2)
        payload = self._payload("context-concurrent")

        def create(store: ProvenaStore):
            barrier.wait()
            return store.create_memory(payload, access=self.access)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(create, (self.store, second_store)))
        finally:
            second_store.close()

        self.assertEqual(sum(result.created for result in results), 1)
        self.assertEqual(
            {result.memory.memory_id for result in results},
            {payload.memory_id},
        )
        self.assertEqual(len({result.memory.fingerprint for result in results}), 1)
        self.assertEqual(self._creation_counts(payload.memory_id or ""), (1, 1, 1))

    def test_deterministic_id_rejects_a_different_create_request(self) -> None:
        payload = self._payload("context-conflict")
        self.store.create_memory(payload, access=self.access)

        with self.assertRaisesRegex(ValueError, "different create request"):
            self.store.create_memory(
                self._payload("context-conflict", content="Conflicting context body"),
                access=self.access,
            )

    def test_tenant_owned_resource_ids_cannot_be_reassigned(self) -> None:
        tenant_a = self.scope.tenant_id
        tenant_b = "tenant-foreign"
        connector_a = ConnectorConfig(
            connector_id="connector-a",
            tenant_id=tenant_a,
            provider=ConnectorProvider.CUSTOM,
            display_name="Tenant A connector",
            auth_type=ConnectorAuthType.API_KEY,
        )
        connector_b = connector_a.model_copy(
            update={
                "connector_id": "connector-b",
                "tenant_id": tenant_b,
                "display_name": "Tenant B connector",
            }
        )
        self.store.save_connector(connector_a)
        self.store.save_connector(connector_b)

        shared_connector = connector_b.model_copy(
            update={"connector_id": "shared-connector"}
        )
        self.store.save_connector(shared_connector)
        with self.assertRaises(TenantOwnershipError):
            self.store.save_connector(
                shared_connector.model_copy(
                    update={"tenant_id": tenant_a, "display_name": "stolen"}
                )
            )

        source_a = ConnectorSourceRecord(
            source_id="source-a",
            connector_id=connector_a.connector_id,
            tenant_id=tenant_a,
            remote_source_id="remote-a",
            source_type="repository",
            display_name="Tenant A source",
        )
        shared_source = source_a.model_copy(
            update={
                "source_id": "shared-source",
                "connector_id": connector_b.connector_id,
                "tenant_id": tenant_b,
                "remote_source_id": "remote-b",
                "display_name": "Tenant B source",
            }
        )
        self.store.save_connector_sources(
            connector_a.connector_id,
            tenant_a,
            ConnectorSourceBatch(sources=[source_a]),
        )
        self.store.save_connector_sources(
            connector_b.connector_id,
            tenant_b,
            ConnectorSourceBatch(sources=[shared_source]),
        )
        with self.assertRaises(TenantOwnershipError):
            self.store.save_connector_sources(
                connector_a.connector_id,
                tenant_a,
                ConnectorSourceBatch(
                    sources=[
                        shared_source.model_copy(
                            update={
                                "connector_id": connector_a.connector_id,
                                "tenant_id": tenant_a,
                                "remote_source_id": "stolen-source",
                            }
                        )
                    ]
                ),
            )

        shared_mapping = PrincipalMapping(
            mapping_id="shared-mapping",
            connector_id=connector_b.connector_id,
            tenant_id=tenant_b,
            principal_type="user",
            local_principal_id="foreign-user",
            remote_principal_id="remote-foreign-user",
        )
        self.store.save_principal_mappings(
            connector_b.connector_id,
            tenant_b,
            PrincipalMappingBatch(mappings=[shared_mapping]),
        )
        with self.assertRaises(TenantOwnershipError):
            self.store.save_principal_mappings(
                connector_a.connector_id,
                tenant_a,
                PrincipalMappingBatch(
                    mappings=[
                        shared_mapping.model_copy(
                            update={
                                "connector_id": connector_a.connector_id,
                                "tenant_id": tenant_a,
                            }
                        )
                    ]
                ),
            )

        shared_grant = SourcePermissionGrant(
            grant_id="shared-grant",
            source_id=shared_source.source_id,
            connector_id=connector_b.connector_id,
            tenant_id=tenant_b,
            principal_type="user",
            principal_id="foreign-user",
            permission_level=PermissionLevel.VIEW,
        )
        self.store.save_source_permission_grants(
            connector_b.connector_id,
            tenant_b,
            SourcePermissionBatch(grants=[shared_grant]),
        )
        with self.assertRaises(TenantOwnershipError):
            self.store.save_source_permission_grants(
                connector_a.connector_id,
                tenant_a,
                SourcePermissionBatch(
                    grants=[
                        shared_grant.model_copy(
                            update={
                                "source_id": source_a.source_id,
                                "connector_id": connector_a.connector_id,
                                "tenant_id": tenant_a,
                            }
                        )
                    ]
                ),
            )

        shared_job = SyncJob(
            job_id="shared-job",
            connector_id=connector_b.connector_id,
            tenant_id=tenant_b,
            job_type=SyncJobType.FULL,
        )
        self.store.save_sync_job(connector_b.connector_id, tenant_b, shared_job)
        with self.assertRaises(TenantOwnershipError):
            self.store.save_sync_job(
                connector_a.connector_id,
                tenant_a,
                shared_job.model_copy(
                    update={
                        "connector_id": connector_a.connector_id,
                        "tenant_id": tenant_a,
                    }
                ),
            )

        shared_policy = RetentionPolicy(
            policy_id="shared-policy",
            tenant_id=tenant_b,
            max_age_days=30,
        )
        self.store.save_retention_policy(shared_policy)
        with self.assertRaises(TenantOwnershipError):
            self.store.save_retention_policy(
                shared_policy.model_copy(update={"tenant_id": tenant_a, "max_age_days": 1})
            )

        shared_hold = LegalHold(
            hold_id="shared-hold",
            tenant_id=tenant_b,
            reason="foreign evidence",
        )
        self.store.place_legal_hold(shared_hold)
        with self.assertRaises(TenantOwnershipError):
            self.store.place_legal_hold(
                shared_hold.model_copy(update={"tenant_id": tenant_a, "reason": "stolen"})
            )

        expected_tenants = {
            "connectors": ("connector_id", "shared-connector"),
            "connector_sources": ("source_id", "shared-source"),
            "principal_mappings": ("mapping_id", "shared-mapping"),
            "source_permission_grants": ("grant_id", "shared-grant"),
            "sync_jobs": ("job_id", "shared-job"),
            "retention_policies": ("policy_id", "shared-policy"),
            "legal_holds": ("hold_id", "shared-hold"),
        }
        for table, (id_column, identifier) in expected_tenants.items():
            with self.subTest(table=table):
                row = self.store.conn.execute(
                    f"SELECT tenant_id FROM {table} WHERE {id_column} = ?",
                    (identifier,),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["tenant_id"], tenant_b)

    def test_integration_children_require_tenant_owned_parents(self) -> None:
        tenant_a = "tenant-parent-a"
        tenant_b = "tenant-parent-b"
        connector_a = ConnectorConfig(
            connector_id="connector-parent-a",
            tenant_id=tenant_a,
            provider=ConnectorProvider.CUSTOM,
            display_name="Tenant A connector",
            auth_type=ConnectorAuthType.API_KEY,
        )
        connector_b = connector_a.model_copy(
            update={
                "connector_id": "connector-parent-b",
                "tenant_id": tenant_b,
                "display_name": "Tenant B connector",
            }
        )
        self.store.save_connector(connector_a)
        self.store.save_connector(connector_b)

        attempted_source = ConnectorSourceRecord(
            source_id="attacker-source",
            connector_id=connector_a.connector_id,
            tenant_id=tenant_b,
            remote_source_id="reserved-remote-source",
            source_type="repository",
            display_name="Foreign source",
        )
        with self.assertRaises(TenantParentNotFoundError):
            self.store.save_connector_sources(
                connector_a.connector_id,
                tenant_b,
                ConnectorSourceBatch(sources=[attempted_source]),
            )

        owner_source = attempted_source.model_copy(
            update={
                "source_id": "owner-source",
                "tenant_id": tenant_a,
                "display_name": "Owner source",
            }
        )
        saved_sources = self.store.save_connector_sources(
            connector_a.connector_id,
            tenant_a,
            ConnectorSourceBatch(sources=[owner_source]),
        )
        self.assertEqual([source.source_id for source in saved_sources], ["owner-source"])
        self.assertIsNone(
            self.store.conn.execute(
                "SELECT source_id FROM connector_sources WHERE source_id = ?",
                (attempted_source.source_id,),
            ).fetchone()
        )

        foreign_mapping = PrincipalMapping(
            mapping_id="foreign-parent-mapping",
            connector_id=connector_a.connector_id,
            tenant_id=tenant_b,
            principal_type="user",
            local_principal_id="local-user",
            remote_principal_id="remote-user",
        )
        with self.assertRaises(TenantParentNotFoundError):
            self.store.save_principal_mappings(
                connector_a.connector_id,
                tenant_b,
                PrincipalMappingBatch(mappings=[foreign_mapping]),
            )

        foreign_job = SyncJob(
            job_id="foreign-parent-job",
            connector_id=connector_a.connector_id,
            tenant_id=tenant_b,
            job_type=SyncJobType.FULL,
        )
        with self.assertRaises(TenantParentNotFoundError):
            self.store.save_sync_job(connector_a.connector_id, tenant_b, foreign_job)

        foreign_source_grant = SourcePermissionGrant(
            grant_id="foreign-parent-grant",
            source_id=owner_source.source_id,
            connector_id=connector_b.connector_id,
            tenant_id=tenant_b,
            principal_type="user",
            principal_id="tenant-b-user",
            permission_level=PermissionLevel.VIEW,
        )
        with self.assertRaises(TenantParentNotFoundError):
            self.store.save_source_permission_grants(
                connector_b.connector_id,
                tenant_b,
                SourcePermissionBatch(grants=[foreign_source_grant]),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.conn:
                self.store.conn.execute(
                    """
                    INSERT INTO connector_sources (
                        source_id, connector_id, tenant_id, remote_source_id, source_type,
                        display_name, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "raw-cross-tenant-source",
                        connector_a.connector_id,
                        tenant_b,
                        "raw-cross-tenant-remote",
                        "repository",
                        "Raw cross-tenant source",
                        "{}",
                        self.store._iso_now(),
                        self.store._iso_now(),
                    ),
                )

    def test_null_create_request_digest_fails_closed(self) -> None:
        payload = self._payload("context-unverifiable")
        self.store.create_memory(payload, access=self.access)
        before = self._creation_counts(payload.memory_id or "")
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE memories SET create_request_digest = NULL WHERE memory_id = ?",
                (payload.memory_id,),
            )

        with self.assertRaisesRegex(ValueError, "without a verifiable create request digest"):
            self.store.create_memory(payload, access=self.access)
        with self.assertRaisesRegex(ValueError, "without a verifiable create request digest"):
            self.store.create_memory(
                payload.model_copy(update={"content": "Conflicting retry"}),
                access=self.access,
            )
        self.assertEqual(self._creation_counts(payload.memory_id or ""), before)

    def test_create_audit_and_history_failures_roll_back_before_clean_retry(self) -> None:
        for method_name in ("_insert_audit", "_insert_history"):
            with self.subTest(method_name=method_name):
                memory_id = f"context-failure-{method_name.removeprefix('_insert_')}"
                payload = self._payload(memory_id)
                with patch.object(
                    self.store,
                    method_name,
                    side_effect=RuntimeError(f"injected {method_name} failure"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "injected"):
                        self.store.create_memory(payload, access=self.access)

                self.assertEqual(self._creation_counts(memory_id), (0, 0, 0))
                self.assertEqual(
                    self._count("memories_fts", "memory_id = ?", (memory_id,)),
                    0,
                )
                retry = self.store.create_memory(payload, access=self.access)
                self.assertTrue(retry.created)
                self.assertEqual(self._creation_counts(memory_id), (1, 1, 1))

    def test_held_soft_delete_releases_to_deleted_and_blocks_hard_delete(self) -> None:
        memory_id = "context-held-delete"
        self.store.create_memory(self._payload(memory_id), access=self.access)
        self.store.place_legal_hold(
            LegalHold(
                hold_id="hold-delete",
                tenant_id=self.scope.tenant_id,
                memory_ids=[memory_id],
                reason="preserve evidence",
            )
        )

        deleted = self.store.delete_memory(memory_id, access=self.access)
        self.assertTrue(deleted.deleted)
        held = self.store.get_memory(memory_id, access=self.access)
        self.assertIsNotNone(held)
        self.assertTrue(held.held)
        self.assertEqual(held.status, MemoryStatus.HELD)
        counts_before_hard_delete = self._creation_counts(memory_id)
        history_before_hard_delete = self._count(
            "memory_history", "memory_id = ?", (memory_id,)
        )

        with self.assertRaisesRegex(ValueError, "under legal hold"):
            self.store.delete_memory(memory_id, hard_delete=True, access=self.access)
        self.assertEqual(self._creation_counts(memory_id), counts_before_hard_delete)
        self.assertEqual(
            self._count("memory_history", "memory_id = ?", (memory_id,)),
            history_before_hard_delete,
        )

        self.assertTrue(self.store.release_legal_hold("hold-delete", self.scope.tenant_id))
        released = self.store.get_memory(memory_id, access=self.access)
        self.assertIsNotNone(released)
        self.assertFalse(released.held)
        self.assertEqual(released.status, MemoryStatus.DELETED)
        visible = self.store.search_memories(
            SearchRequest(query="Shared context", scope=self.scope),
            access=self.access,
        )
        self.assertNotIn(memory_id, {result.memory.memory_id for result in visible.results})

    def test_replacing_a_legal_hold_refreshes_old_and_new_targets(self) -> None:
        old_id = "context-old-hold-target"
        new_id = "context-new-hold-target"
        self.store.create_memory(self._payload(old_id), access=self.access)
        self.store.create_memory(self._payload(new_id), access=self.access)
        hold = LegalHold(
            hold_id="hold-replaced-target",
            tenant_id=self.scope.tenant_id,
            memory_ids=[old_id],
            reason="first target",
        )
        self.store.place_legal_hold(hold)
        self.store.place_legal_hold(
            hold.model_copy(update={"memory_ids": [new_id], "reason": "new target"})
        )

        old_memory = self.store.get_memory(old_id, access=self.access)
        new_memory = self.store.get_memory(new_id, access=self.access)
        self.assertIsNotNone(old_memory)
        self.assertIsNotNone(new_memory)
        self.assertFalse(old_memory.held)
        self.assertEqual(old_memory.status, MemoryStatus.ACTIVE)
        self.assertTrue(new_memory.held)
        self.assertEqual(new_memory.status, MemoryStatus.HELD)

    def test_scope_erase_preserves_held_memory(self) -> None:
        held_id = "context-held-erase"
        unheld_id = "context-unheld-erase"
        self.store.create_memory(self._payload(held_id), access=self.access)
        self.store.create_memory(self._payload(unheld_id), access=self.access)
        self.store.place_legal_hold(
            LegalHold(
                hold_id="hold-erase",
                tenant_id=self.scope.tenant_id,
                memory_ids=[held_id],
                reason="preserve scope evidence",
            )
        )

        erased = self.store.erase_scope(
            EraseRequest(
                tenant_id=self.scope.tenant_id,
                workspace_id=self.scope.workspace_id,
            ),
            access=self.access,
        )

        self.assertEqual(erased.deleted_memories, 1)
        self.assertIsNotNone(self.store.get_memory(held_id, access=self.access))
        self.assertIsNone(self.store.get_memory(unheld_id, access=self.access))

    def test_committed_hold_wins_against_concurrent_hard_delete(self) -> None:
        memory_id = "context-concurrent-hold-delete"
        self.store.create_memory(self._payload(memory_id), access=self.access)
        hold_store = ProvenaStore(self.temp_dir / "provena.db")
        hold_has_write_lock = Event()
        release_hold = Event()
        delete_started = Event()
        original_refresh = ProvenaStore._refresh_hold_state

        def gated_refresh(store: ProvenaStore, memory_ids: list[str]) -> None:
            if store is hold_store:
                hold_has_write_lock.set()
                if not release_hold.wait(timeout=5):
                    raise TimeoutError("test did not release the legal-hold transaction")
            original_refresh(store, memory_ids)

        def hard_delete() -> None:
            delete_started.set()
            self.store.delete_memory(memory_id, hard_delete=True, access=self.access)

        try:
            with patch.object(ProvenaStore, "_refresh_hold_state", new=gated_refresh):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    hold_future = executor.submit(
                        hold_store.place_legal_hold,
                        LegalHold(
                            hold_id="hold-concurrent-delete",
                            tenant_id=self.scope.tenant_id,
                            memory_ids=[memory_id],
                            reason="concurrent deletion fence",
                        ),
                    )
                    self.assertTrue(hold_has_write_lock.wait(timeout=5))
                    delete_future = executor.submit(hard_delete)
                    self.assertTrue(delete_started.wait(timeout=5))
                    release_hold.set()
                    hold_future.result(timeout=5)
                    with self.assertRaisesRegex(ValueError, "under legal hold"):
                        delete_future.result(timeout=5)
        finally:
            release_hold.set()
            hold_store.close()

        held = self.store.get_memory(memory_id, access=self.access)
        self.assertIsNotNone(held)
        self.assertTrue(held.held)

    def test_destructive_paths_use_atomic_unheld_returning_deletes(self) -> None:
        def capture_delete(operation) -> list[str]:
            statements: list[str] = []
            self.store.conn.set_trace_callback(statements.append)
            try:
                operation()
            finally:
                self.store.conn.set_trace_callback(None)
            deletes = [
                " ".join(statement.upper().split())
                for statement in statements
                if " ".join(statement.upper().split()).startswith("DELETE FROM MEMORIES WHERE")
            ]
            self.assertTrue(deletes)
            for statement in deletes:
                self.assertIn("AND HELD = 0", statement)
                self.assertIn("RETURNING MEMORY_ID", statement)
            return deletes

        hard_id = "context-sql-hard-delete"
        self.store.create_memory(self._payload(hard_id), access=self.access)
        capture_delete(
            lambda: self.store.delete_memory(hard_id, hard_delete=True, access=self.access)
        )

        erase_id = "context-sql-scope-erase"
        self.store.create_memory(self._payload(erase_id), access=self.access)
        capture_delete(
            lambda: self.store.erase_scope(
                EraseRequest(
                    tenant_id=self.scope.tenant_id,
                    workspace_id=self.scope.workspace_id,
                ),
                access=self.access,
            )
        )

        rtbf_id = "context-sql-rtbf"
        self.store.create_memory(self._payload(rtbf_id), access=self.access)
        capture_delete(lambda: self.store.rtbf(RTBFRequest(tenant_id=self.scope.tenant_id)))

        retention_id = "context-sql-retention"
        self.store.create_memory(self._payload(retention_id), access=self.access)
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE memories SET created_at = ? WHERE memory_id = ?",
                ("2020-01-01T00:00:00+00:00", retention_id),
            )
        self.store.save_retention_policy(
            RetentionPolicy(
                policy_id="retention-atomic-delete",
                tenant_id=self.scope.tenant_id,
                max_age_days=1,
                action="delete_hard",
            )
        )
        capture_delete(lambda: self.store.enforce_retention(self.scope.tenant_id))

    def test_rtbf_and_hard_retention_preserve_held_memories(self) -> None:
        rtbf_held_id = "context-held-rtbf"
        rtbf_unheld_id = "context-unheld-rtbf"
        for memory_id in (rtbf_held_id, rtbf_unheld_id):
            self.store.create_memory(self._payload(memory_id), access=self.access)
        self.store.place_legal_hold(
            LegalHold(
                hold_id="hold-rtbf",
                tenant_id=self.scope.tenant_id,
                memory_ids=[rtbf_held_id],
                reason="preserve RTBF evidence",
            )
        )

        erased = self.store.rtbf(RTBFRequest(tenant_id=self.scope.tenant_id))

        self.assertEqual(erased.deleted_memories, 1)
        self.assertEqual(erased.held_memories, [rtbf_held_id])
        self.assertIsNotNone(self.store.get_memory(rtbf_held_id, access=self.access))
        self.assertIsNone(self.store.get_memory(rtbf_unheld_id, access=self.access))

        retention_held_id = "context-held-retention"
        retention_unheld_id = "context-unheld-retention"
        for memory_id in (retention_held_id, retention_unheld_id):
            self.store.create_memory(self._payload(memory_id), access=self.access)
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE memories SET created_at = ? WHERE memory_id IN (?, ?)",
                (
                    "2020-01-01T00:00:00+00:00",
                    retention_held_id,
                    retention_unheld_id,
                ),
            )
        self.store.place_legal_hold(
            LegalHold(
                hold_id="hold-retention",
                tenant_id=self.scope.tenant_id,
                memory_ids=[retention_held_id],
                reason="preserve retention evidence",
            )
        )
        self.store.save_retention_policy(
            RetentionPolicy(
                policy_id="retention-held-fence",
                tenant_id=self.scope.tenant_id,
                max_age_days=1,
                action="delete_hard",
            )
        )

        retained = self.store.enforce_retention(self.scope.tenant_id)

        self.assertEqual(retained.expired_memory_ids, [retention_unheld_id])
        self.assertIsNotNone(self.store.get_memory(retention_held_id, access=self.access))
        self.assertIsNone(self.store.get_memory(retention_unheld_id, access=self.access))

    def test_supersession_requires_target_write_without_side_effects(self) -> None:
        principal_b = AccessContext(
            tenant_id=self.scope.tenant_id,
            role="editor",
            principal_id="principal-b",
        )
        target_id = "context-principal-b"
        target = self._payload(target_id).model_copy(
            update={
                "acl": [
                    ACLEntry(
                        principal_id="principal-b",
                        principal_type="user",
                        permissions=list(ACLPermission),
                    ),
                    ACLEntry(
                        principal_id=self.access.principal_id or "",
                        principal_type="user",
                        permissions=[ACLPermission.READ],
                    ),
                ]
            }
        )
        self.store.create_memory(target, access=principal_b)
        relation_count = self._relation_count()
        target_counts = self._creation_counts(target_id)

        create_payload = self._payload("context-unauthorized-create").model_copy(
            update={"supersedes_memory_id": target_id}
        )
        with self.assertRaisesRegex(ValueError, "write access required for supersession target"):
            self.store.create_memory(create_payload, access=self.access)
        self.assertEqual(self._creation_counts(create_payload.memory_id or ""), (0, 0, 0))

        source_id = "context-unauthorized-update"
        self.store.create_memory(self._payload(source_id), access=self.access)
        source_before = self._creation_counts(source_id)
        source_audits = self._count("audit_log", "memory_id = ?", (source_id,))
        source_history = self._count("memory_history", "memory_id = ?", (source_id,))
        with self.assertRaisesRegex(ValueError, "write access required for supersession target"):
            self.store.update_memory(
                source_id,
                MemoryUpdate(
                    content="must roll back",
                    supersedes_memory_id=target_id,
                ),
                access=self.access,
            )

        source = self.store.get_memory(source_id, access=self.access)
        target_after = self.store.get_memory(target_id, access=principal_b)
        self.assertIsNotNone(source)
        self.assertIsNotNone(target_after)
        self.assertEqual(source.content, "Shared context body")
        self.assertEqual(target_after.status, MemoryStatus.ACTIVE)
        self.assertEqual(self._creation_counts(source_id), source_before)
        self.assertEqual(
            self._count("audit_log", "memory_id = ?", (source_id,)),
            source_audits,
        )
        self.assertEqual(
            self._count("memory_history", "memory_id = ?", (source_id,)),
            source_history,
        )
        self.assertEqual(self._creation_counts(target_id), target_counts)
        self.assertEqual(self._relation_count(), relation_count)

    def test_supersession_rejects_self_scope_and_non_active_states(self) -> None:
        self_payload = self._payload("context-self").model_copy(
            update={"supersedes_memory_id": "context-self"}
        )
        with self.assertRaisesRegex(ValueError, "cannot supersede itself"):
            self.store.create_memory(self_payload, access=self.access)
        self.assertEqual(self._creation_counts("context-self"), (0, 0, 0))

        other_scope = self.scope.model_copy(update={"workspace_id": "workspace-other"})
        cross_target = self._payload("context-cross-target").model_copy(
            update={"scope": other_scope}
        )
        self.store.create_memory(cross_target, access=self.access)
        cross_replacement = self._payload("context-cross-replacement").model_copy(
            update={"supersedes_memory_id": cross_target.memory_id}
        )
        with self.assertRaisesRegex(ValueError, "same scope"):
            self.store.create_memory(cross_replacement, access=self.access)
        self.assertEqual(
            self._creation_counts(cross_replacement.memory_id or ""),
            (0, 0, 0),
        )

        deleted_id = "context-deleted-source"
        self.store.create_memory(self._payload(deleted_id), access=self.access)
        self.store.delete_memory(deleted_id, access=self.access)
        replacement = self._payload("context-replacement")
        replacement = replacement.model_copy(update={"supersedes_memory_id": deleted_id})
        relation_count = self._relation_count()
        with self.assertRaisesRegex(ValueError, "target must be active"):
            self.store.create_memory(replacement, access=self.access)
        self.assertEqual(self._creation_counts(replacement.memory_id or ""), (0, 0, 0))
        tombstone = self.store.get_memory(deleted_id, access=self.access)
        self.assertIsNotNone(tombstone)
        self.assertEqual(tombstone.status, MemoryStatus.DELETED)
        self.assertEqual(self._relation_count(), relation_count)

        source_id = "context-deleted-source-update"
        active_target_id = "context-active-target"
        self.store.create_memory(self._payload(source_id), access=self.access)
        self.store.create_memory(self._payload(active_target_id), access=self.access)
        self.store.delete_memory(source_id, access=self.access)
        target_before = self._creation_counts(active_target_id)
        relation_count = self._relation_count()
        with self.assertRaisesRegex(ValueError, "source must be active"):
            self.store.update_memory(
                source_id,
                MemoryUpdate(
                    content="must not persist",
                    supersedes_memory_id=active_target_id,
                ),
                access=self.access,
            )
        source = self.store.get_memory(source_id, access=self.access)
        target = self.store.get_memory(active_target_id, access=self.access)
        self.assertIsNotNone(source)
        self.assertIsNotNone(target)
        self.assertEqual(source.status, MemoryStatus.DELETED)
        self.assertNotEqual(source.content, "must not persist")
        self.assertEqual(target.status, MemoryStatus.ACTIVE)
        self.assertEqual(self._creation_counts(active_target_id), target_before)
        self.assertEqual(self._relation_count(), relation_count)

        held_target_id = "context-held-target"
        self.store.create_memory(self._payload(held_target_id), access=self.access)
        self.store.place_legal_hold(
            LegalHold(
                hold_id="hold-supersession-target",
                tenant_id=self.scope.tenant_id,
                memory_ids=[held_target_id],
                reason="freeze target",
            )
        )
        held_replacement = self._payload("context-held-target-replacement").model_copy(
            update={"supersedes_memory_id": held_target_id}
        )
        with self.assertRaisesRegex(ValueError, "target must be active"):
            self.store.create_memory(held_replacement, access=self.access)
        self.assertEqual(
            self._creation_counts(held_replacement.memory_id or ""),
            (0, 0, 0),
        )

        held_source_id = "context-held-source"
        held_source_target_id = "context-held-source-target"
        self.store.create_memory(self._payload(held_source_id), access=self.access)
        self.store.create_memory(self._payload(held_source_target_id), access=self.access)
        self.store.place_legal_hold(
            LegalHold(
                hold_id="hold-supersession-source",
                tenant_id=self.scope.tenant_id,
                memory_ids=[held_source_id],
                reason="freeze source",
            )
        )
        with self.assertRaisesRegex(ValueError, "source must be active"):
            self.store.update_memory(
                held_source_id,
                MemoryUpdate(supersedes_memory_id=held_source_target_id),
                access=self.access,
            )
        held_source_target = self.store.get_memory(
            held_source_target_id,
            access=self.access,
        )
        self.assertIsNotNone(held_source_target)
        self.assertEqual(held_source_target.status, MemoryStatus.ACTIVE)

        valid_create_target_id = "context-valid-create-target"
        valid_replacement_id = "context-valid-create-replacement"
        self.store.create_memory(self._payload(valid_create_target_id), access=self.access)
        relation_count = self._relation_count()
        valid_replacement = self.store.create_memory(
            self._payload(valid_replacement_id).model_copy(
                update={"supersedes_memory_id": valid_create_target_id}
            ),
            access=self.access,
        )
        valid_create_target = self.store.get_memory(valid_create_target_id, access=self.access)
        self.assertTrue(valid_replacement.created)
        self.assertIsNotNone(valid_create_target)
        self.assertEqual(valid_create_target.status, MemoryStatus.SUPERSEDED)
        self.assertEqual(self._relation_count(), relation_count + 1)

        valid_update_source_id = "context-valid-update-source"
        valid_update_target_id = "context-valid-update-target"
        self.store.create_memory(self._payload(valid_update_source_id), access=self.access)
        self.store.create_memory(self._payload(valid_update_target_id), access=self.access)
        relation_count = self._relation_count()
        valid_update = self.store.update_memory(
            valid_update_source_id,
            MemoryUpdate(
                content="Authorized replacement",
                supersedes_memory_id=valid_update_target_id,
            ),
            access=self.access,
        )
        valid_update_target = self.store.get_memory(valid_update_target_id, access=self.access)
        self.assertEqual(valid_update.memory.content, "Authorized replacement")
        self.assertIsNotNone(valid_update_target)
        self.assertEqual(valid_update_target.status, MemoryStatus.SUPERSEDED)
        self.assertEqual(self._relation_count(), relation_count + 1)


class ServiceAuthenticationTests(unittest.TestCase):
    ENV_KEYS = (
        "PROVENA_DB_PATH",
        "PROVENA_ENVIRONMENT",
        "PROVENA_SERVICE_TOKEN",
        "PROVENA_GATEWAY_SERVICE_TOKEN",
        "PROVENA_SERVICE_TENANT_ID",
        "PROVENA_SERVICE_PRINCIPAL_ID",
        "PROVENA_SERVICE_ROLE",
        "PROVENA_SERVICE_IDENTITIES",
        "PROVENA_ALLOW_UNAUTHENTICATED_LOCAL",
    )

    def setUp(self) -> None:
        self.original_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        self.temp_dir = Path(tempfile.mkdtemp(prefix="provena-auth-"))
        os.environ.update(
            {
                "PROVENA_DB_PATH": str(self.temp_dir / "provena.db"),
                "PROVENA_ENVIRONMENT": "production",
                "PROVENA_SERVICE_TOKEN": "test-service-secret",
                "PROVENA_SERVICE_TENANT_ID": "tenant-neverzero",
                "PROVENA_SERVICE_PRINCIPAL_ID": "neverzero-service",
                "PROVENA_SERVICE_ROLE": "editor",
                "PROVENA_ALLOW_UNAUTHENTICATED_LOCAL": "false",
            }
        )
        self.client = self._reload_app()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._reload_modules()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @staticmethod
    def _reload_modules():
        import app.config
        import app.main

        app.config.get_settings.cache_clear()
        importlib.reload(app.config)
        return importlib.reload(app.main)

    def _reload_app(self) -> TestClient:
        app_main = self._reload_modules()
        return TestClient(app_main.create_app())

    @staticmethod
    def _payload(tenant_id: str = "tenant-neverzero") -> dict:
        return {
            "memory_id": "context-authenticated",
            "kind": "fact",
            "scope": {"tenant_id": tenant_id, "workspace_id": "workspace-shared"},
            "content": "Authenticated shared context",
        }

    def test_production_data_plane_rejects_missing_and_invalid_bearer_tokens(self) -> None:
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.post("/v1/memories", json=self._payload()).status_code, 401)
        invalid = self.client.post(
            "/v1/memories",
            json=self._payload(),
            headers={"Authorization": "Bearer wrong-secret"},
        )
        self.assertEqual(invalid.status_code, 401)

    def test_valid_token_uses_configured_identity_and_cannot_escape_tenant(self) -> None:
        headers = {
            "Authorization": "Bearer test-service-secret",
            "X-Provena-Tenant-Id": "spoofed-tenant",
            "X-Provena-Role": "superadmin",
            "X-Provena-Principal-Id": "spoofed-principal",
        }
        created = self.client.post("/v1/memories", json=self._payload(), headers=headers)
        self.assertEqual(created.status_code, 200)
        acl = created.json()["memory"]["acl"]
        self.assertEqual([entry["principal_id"] for entry in acl], ["neverzero-service"])

        cross_tenant = self.client.post(
            "/v1/memories",
            json=self._payload("other-tenant"),
            headers=headers,
        )
        self.assertEqual(cross_tenant.status_code, 403)

    def test_gateway_transport_token_uses_only_authoritative_identity_headers(self) -> None:
        self.client.__exit__(None, None, None)
        os.environ["PROVENA_GATEWAY_SERVICE_TOKEN"] = "gateway-internal-transport-secret"
        self.client = self._reload_app()
        self.client.__enter__()

        headers = {
            "Authorization": "Bearer gateway-internal-transport-secret",
            "X-Provena-Tenant-Id": "tenant-gateway",
            "X-Provena-Role": "editor",
            "X-Provena-Key-Id": "external-key-a",
            "X-Provena-Principal-Id": "gateway-principal-a",
            "X-Provena-Groups": "engineering,agents",
        }
        created = self.client.post(
            "/v1/memories",
            json=self._payload("tenant-gateway") | {"memory_id": "context-gateway"},
            headers=headers,
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(
            [entry["principal_id"] for entry in created.json()["memory"]["acl"]],
            ["gateway-principal-a"],
        )

        missing_identity = self.client.post(
            "/v1/memories",
            json=self._payload("tenant-gateway") | {"memory_id": "context-gateway-missing"},
            headers={"Authorization": "Bearer gateway-internal-transport-secret"},
        )
        self.assertEqual(missing_identity.status_code, 401)
        self.assertEqual(
            missing_identity.json()["detail"],
            "valid gateway identity headers required",
        )

    def test_tenant_owned_id_conflict_returns_409_without_reassignment(self) -> None:
        self.client.__exit__(None, None, None)
        os.environ["PROVENA_GATEWAY_SERVICE_TOKEN"] = "gateway-superadmin-secret"
        self.client = self._reload_app()
        self.client.__enter__()
        headers = {
            "Authorization": "Bearer gateway-superadmin-secret",
            "X-Provena-Role": "superadmin",
            "X-Provena-Key-Id": "gateway-admin-key",
            "X-Provena-Principal-Id": "gateway-admin",
        }
        connector = {
            "connector_id": "shared-api-connector",
            "tenant_id": "tenant-foreign",
            "provider": "custom",
            "display_name": "Foreign connector",
            "auth_type": "api_key",
        }

        created = self.client.post(
            "/v1/integrations/connectors",
            json=connector,
            headers=headers,
        )
        conflict = self.client.post(
            "/v1/integrations/connectors",
            json=connector
            | {"tenant_id": "tenant-neverzero", "display_name": "Stolen connector"},
            headers=headers,
        )
        preserved = self.client.get(
            "/v1/integrations/connectors/shared-api-connector",
            params={"tenant_id": "tenant-foreign"},
            headers=headers,
        )

        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(
            conflict.json()["detail"],
            "resource write conflict",
        )
        self.assertEqual(preserved.status_code, 200, preserved.text)
        self.assertEqual(preserved.json()["tenant_id"], "tenant-foreign")
        self.assertEqual(preserved.json()["display_name"], "Foreign connector")

    def test_viewer_service_identity_cannot_create_memories(self) -> None:
        self.client.__exit__(None, None, None)
        os.environ["PROVENA_SERVICE_ROLE"] = "viewer"
        self.client = self._reload_app()
        self.client.__enter__()

        response = self.client.post(
            "/v1/memories",
            json=self._payload(),
            headers={"Authorization": "Bearer test-service-secret"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "write access required")

    def test_non_local_settings_fail_closed_without_service_credentials(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                environment="production",
                service_token=None,
                service_tenant_id="",
                allow_unauthenticated_local=False,
            )
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                environment="production",
                service_token="reused-token",
                service_tenant_id="tenant-a",
                gateway_service_token="reused-token",
                allow_unauthenticated_local=False,
            )
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                environment="production",
                service_token="configured-token",
                service_tenant_id="",
                allow_unauthenticated_local=False,
            )

    def test_hashed_service_registry_is_tenant_bound_and_supports_multiple_identities(self) -> None:
        self.client.__exit__(None, None, None)
        token_a = "tenant-a-high-entropy-service-token"
        token_b = "tenant-b-high-entropy-service-token"
        os.environ.pop("PROVENA_SERVICE_TOKEN", None)
        os.environ.pop("PROVENA_SERVICE_TENANT_ID", None)
        os.environ["PROVENA_SERVICE_IDENTITIES"] = json.dumps([
            {
                "token_sha256": hashlib.sha256(token_a.encode()).hexdigest(),
                "tenant_id": "tenant-a",
                "principal_id": "neverzero-a",
                "role": "editor",
            },
            {
                "token_sha256": hashlib.sha256(token_b.encode()).hexdigest(),
                "tenant_id": "tenant-b",
                "principal_id": "neverzero-b",
                "role": "editor",
            },
        ])
        self.client = self._reload_app()
        self.client.__enter__()

        payload_a = self._payload("tenant-a") | {"memory_id": "context-tenant-a"}
        created = self.client.post(
            "/v1/memories",
            json=payload_a,
            headers={
                "Authorization": f"Bearer {token_a}",
                "X-Provena-Tenant-Id": "tenant-b",
            },
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["memory"]["scope"]["tenant_id"], "tenant-a")

        wrong_tenant = self.client.post(
            "/v1/memories",
            json=self._payload("tenant-b") | {"memory_id": "context-cross-tenant"},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        self.assertEqual(wrong_tenant.status_code, 403)
        tenant_b = self.client.post(
            "/v1/memories",
            json=self._payload("tenant-b") | {"memory_id": "context-tenant-b"},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        self.assertEqual(tenant_b.status_code, 200)
        invalid = self.client.post(
            "/v1/memories",
            json=self._payload("tenant-a") | {"memory_id": "context-invalid"},
            headers={"Authorization": "Bearer invalid"},
        )
        self.assertEqual(invalid.status_code, 401)
