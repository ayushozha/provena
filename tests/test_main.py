import importlib
import hashlib
import json
import os
import shutil
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import rfc8785

from app.connector_worker import ConnectorRunContext, ConnectorRunResult
from app.models import (
    ConnectorAuthType,
    ConnectorConfig,
    ConnectorProvider,
    ConnectorStatus,
    RepositoryMemorySyncRequest,
    ScopeEnvelope,
    SyncMode,
    SyncJobStatus,
    SyncJobType,
)


class ProvenaApiTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = Path(__file__).resolve().parent.parent / ".tmp-e2e" / "test-db"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_root / f"test-{int(time.time() * 1000)}-{os.getpid()}"
        self.temp_dir.mkdir()
        db_path = self.temp_dir / "provena-test.db"
        os.environ["PROVENA_DB_PATH"] = str(db_path)

        import app.config
        import app.main

        app.config.get_settings.cache_clear()
        importlib.reload(app.config)
        importlib.reload(app.main)
        self.client = TestClient(app.main.create_app())
        self.context = self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.environ.pop("PROVENA_DB_PATH", None)

    def _admin_headers(self, tenant_id: str = "tenant-acme") -> dict[str, str]:
        return {
            "X-Provena-Tenant-Id": tenant_id,
            "X-Provena-Role": "admin",
            "X-Provena-Principal-Id": "principal-admin-1",
            "X-Provena-Groups": "admins,security",
        }

    def _viewer_headers(
        self,
        tenant_id: str = "tenant-acme",
        principal_id: str | None = "pm-1",
        groups: list[str] | None = None,
    ) -> dict[str, str]:
        headers = {
            "X-Provena-Tenant-Id": tenant_id,
            "X-Provena-Role": "viewer",
        }
        if principal_id is not None:
            headers["X-Provena-Principal-Id"] = principal_id
        if groups is not None:
            headers["X-Provena-Groups"] = ",".join(groups)
        return headers

    def _editor_headers(
        self,
        tenant_id: str = "tenant-acme",
        principal_id: str = "principal-editor-1",
    ) -> dict[str, str]:
        return {
            "X-Provena-Tenant-Id": tenant_id,
            "X-Provena-Role": "editor",
            "X-Provena-Principal-Id": principal_id,
        }

    def test_admin_endpoints_accept_authenticated_key_identity(self) -> None:
        response = self.client.get(
            "/v1/admin/retention-policies",
            params={"tenant_id": "tenant-acme"},
            headers={
                "X-Provena-Tenant-Id": "tenant-acme",
                "X-Provena-Role": "admin",
                "X-Provena-Key-Id": "key-admin-1",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)

    def test_tenant_admin_cannot_mutate_foreign_tenant_resources(self) -> None:
        acme_headers = self._admin_headers("tenant-acme")
        other_headers = self._admin_headers("tenant-other")

        snapshot = self.client.post(
            "/v1/project-snapshots",
            json={
                "tenant_id": "tenant-other",
                "project_id": "project-cross-tenant",
                "summary": "Must never be written by another tenant's admin.",
            },
            headers=acme_headers,
        )
        self.assertEqual(snapshot.status_code, 403, snapshot.text)

        retention = self.client.post(
            "/v1/admin/retention-policies",
            json={
                "policy_id": "policy-cross-tenant",
                "tenant_id": "tenant-other",
                "max_age_days": 1,
                "action": "delete_hard",
            },
            headers=acme_headers,
        )
        self.assertEqual(retention.status_code, 403, retention.text)

        blocked_hold = self.client.post(
            "/v1/admin/legal-hold",
            json={
                "hold_id": "hold-cross-tenant",
                "tenant_id": "tenant-other",
                "reason": "Must never be written by another tenant's admin.",
            },
            headers=acme_headers,
        )
        self.assertEqual(blocked_hold.status_code, 403, blocked_hold.text)

        protected_hold = self.client.post(
            "/v1/admin/legal-hold",
            json={
                "hold_id": "hold-owned-by-other",
                "tenant_id": "tenant-other",
                "reason": "Foreign tenant ownership proof.",
            },
            headers=other_headers,
        )
        self.assertEqual(protected_hold.status_code, 200, protected_hold.text)
        blocked_release = self.client.delete(
            "/v1/admin/legal-hold/hold-owned-by-other",
            params={"tenant_id": "tenant-other"},
            headers=acme_headers,
        )
        self.assertEqual(blocked_release.status_code, 403, blocked_release.text)

        store = self.client.app.state.store
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM project_snapshots WHERE tenant_id = ? AND project_id = ?",
                ("tenant-other", "project-cross-tenant"),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM retention_policies WHERE tenant_id = ? AND policy_id = ?",
                ("tenant-other", "policy-cross-tenant"),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM legal_holds WHERE tenant_id = ? AND hold_id = ?",
                ("tenant-other", "hold-cross-tenant"),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM legal_holds WHERE tenant_id = ? AND hold_id = ?",
                ("tenant-other", "hold-owned-by-other"),
            ).fetchone()[0],
            1,
        )

        valid_release = self.client.delete(
            "/v1/admin/legal-hold/hold-owned-by-other",
            params={"tenant_id": "tenant-other"},
            headers=other_headers,
        )
        self.assertEqual(valid_release.status_code, 200, valid_release.text)

    def _repository_event(
        self,
        event_id: str,
        *,
        kind: str = "fact",
        status: str = "active",
        body: str | None = None,
        created_at: str = "2026-07-13T10:00:00.000Z",
        supersedes: list[str] | None = None,
    ) -> dict[str, object]:
        explicit = kind in {"decision", "preference"}
        return {
            "schema_version": 1,
            "id": event_id,
            "kind": kind,
            "subject_type": "file",
            "title": f"Canonical {kind} {event_id}",
            "body": body or f"Canonical repository memory for {event_id}.",
            "structured_data": {"event": event_id, "nested": {"verified": True}},
            "status": status,
            "applies_to": ["src/example.ts"],
            "sources": [
                {
                    "path": "src/example.ts",
                    "symbol": "example",
                    "start_line": 3,
                    "end_line": 5,
                    "blob": "blob-123",
                    "commit": "commit-123",
                }
            ],
            "provenance": {
                "actor": "maintainer" if explicit else "repo-indexer",
                "method": "explicit" if explicit else "observed",
                "agent": "codex",
                "session_id": "session-123",
                "command": "provena remember" if explicit else "provena refresh",
            },
            "authority": "human" if explicit else "tool",
            "confidence": 0.9,
            "importance": 0.8,
            "sensitivity": "internal",
            "created_at": created_at,
            "updated_at": created_at,
            "supersedes": sorted(supersedes or []),
            "tags": ["canonical", kind],
            "triggers": [f"recall {event_id}"],
        }

    def _repository_ledger(self, events: list[dict[str, object]], *, blank_line: bool = False) -> str:
        ledger = "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
            for event in events
        )
        return ledger + ("\n" if blank_line else "")

    def _repository_memory_id(
        self,
        repository_id: str,
        event_id: str,
        *,
        tenant_id: str = "tenant-acme",
        project_id: str = "project-repo-brain",
    ) -> str:
        identity = f"repo-ledger\0{tenant_id}\0{project_id}\0{repository_id}\0{event_id}"
        return "repoevt_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _sync_repository(
        self,
        repository_id: str,
        ledger: str,
        *,
        fingerprint: str | None = None,
        headers: dict[str, str] | None = None,
        tenant_id: str = "tenant-acme",
        project_id: str = "project-repo-brain",
    ):
        raw = ledger.encode("utf-8")
        return self.client.post(
            f"/v1/repositories/{repository_id}/memory-events/sync",
            json={
                "schema_version": 1,
                "scope": {
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                },
                "ledger_path": ".provena/memory/events.jsonl",
                "memory_fingerprint": fingerprint or hashlib.sha256(raw).hexdigest(),
                "ledger_bytes": len(raw),
                "ledger": ledger,
            },
            headers=headers if headers is not None else {
                "X-Provena-Tenant-Id": tenant_id,
                "X-Provena-Role": "editor",
                "X-Provena-Principal-Id": "repo-sync-agent",
            },
        )

    def _register_connected_fixture(
        self,
        *,
        connector_id: str,
        tenant_id: str = "tenant-acme",
        sources: list[dict[str, object]],
        grants: list[dict[str, object]] | None = None,
        mappings: list[dict[str, object]] | None = None,
    ) -> None:
        headers = self._admin_headers(tenant_id)
        connector_response = self.client.post(
            "/v1/integrations/connectors",
            json={
                "connector_id": connector_id,
                "tenant_id": tenant_id,
                "provider": "slack",
                "display_name": f"{connector_id} workspace",
                "remote_workspace_id": f"remote-{connector_id}",
                "auth_type": "oauth",
                "sync_mode": "hybrid",
                "status": "active",
                "principal_sync_enabled": True,
                "acl_sync_enabled": True,
            },
            headers=headers,
        )
        self.assertEqual(connector_response.status_code, 200)

        sources_response = self.client.post(
            f"/v1/integrations/connectors/{connector_id}/sources/batch?tenant_id={tenant_id}",
            json={"sources": sources},
            headers=headers,
        )
        self.assertEqual(sources_response.status_code, 200)
        self.assertEqual(len(sources_response.json()), len(sources))

        if mappings:
            mappings_response = self.client.post(
                f"/v1/integrations/connectors/{connector_id}/principal-mappings/batch?tenant_id={tenant_id}",
                json={"mappings": mappings},
                headers=headers,
            )
            self.assertEqual(mappings_response.status_code, 200)
            self.assertEqual(len(mappings_response.json()), len(mappings))

        if grants:
            grants_response = self.client.post(
                f"/v1/integrations/connectors/{connector_id}/permissions/batch?tenant_id={tenant_id}",
                json={"grants": grants},
                headers=headers,
            )
            self.assertEqual(grants_response.status_code, 200)
            self.assertEqual(len(grants_response.json()), len(grants))

    def test_healthz(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_authenticated_context_without_tenant_is_rejected_but_headerless_local_mode_remains(self) -> None:
        for headers in (
            {"X-Provena-Principal-Id": "principal-without-tenant"},
            {"X-Provena-Key-Id": "key-without-tenant"},
            {"X-Provena-Groups": "group-without-tenant"},
            {
                "X-Provena-Role": "editor",
                "X-Provena-Principal-Id": "editor-without-tenant",
            },
        ):
            response = self.client.get("/v1/memories/not-present", headers=headers)
            self.assertEqual(response.status_code, 401, response.text)
            self.assertNotIn("not-present", response.text)

        local_create = self.client.post(
            "/v1/memories",
            json={
                "kind": "fact",
                "scope": {"tenant_id": "tenant-local", "project_id": "project-local"},
                "title": "Headerless local compatibility",
                "content": "The standalone local boundary remains usable without identity headers.",
            },
        )
        self.assertEqual(local_create.status_code, 200, local_create.text)

    def test_repository_sync_openapi_documents_auth_and_conflicts(self) -> None:
        schema = self.client.app.openapi()
        operation = schema["paths"]["/v1/repositories/{repository_id}/memory-events/sync"]["post"]
        self.assertEqual(
            operation["security"],
            [{"ProvenaServiceBearer": []}],
        )
        schemes = schema["components"]["securitySchemes"]
        self.assertEqual(schemes["ProvenaTenant"]["name"], "X-Provena-Tenant-Id")
        self.assertEqual(schemes["ProvenaPrincipal"]["name"], "X-Provena-Principal-Id")
        self.assertEqual(schemes["ProvenaKey"]["name"], "X-Provena-Key-Id")
        role_parameter = next(
            parameter
            for parameter in operation["parameters"]
            if parameter["name"] == "X-Provena-Role"
        )
        self.assertFalse(role_parameter["required"])
        self.assertEqual(
            role_parameter["schema"]["anyOf"][0]["enum"],
            ["editor", "admin", "superadmin"],
        )
        self.assertTrue({"200", "401", "403", "409", "422"}.issubset(operation["responses"]))
    def test_readyz_blocks_data_plane_when_legacy_integrity_audit_is_dirty(self) -> None:
        ready = self.client.get("/readyz")
        self.assertEqual(ready.status_code, 200)
        self.assertTrue(ready.json()["ready"])

        blocked = {
            "status": "blocked",
            "ready": False,
            "backend": "postgresql",
            "constraints_validated": False,
            "issues": {"connector_sources": 1},
        }
        store = self.client.app.state.store
        with patch.object(store, "tenant_integrity_status", return_value=blocked):
            readiness = self.client.get("/readyz")
            data_plane = self.client.get("/v1/integrations/catalog")
            liveness = self.client.get("/healthz")

        self.assertEqual(readiness.status_code, 503)
        self.assertEqual(data_plane.status_code, 503)
        self.assertNotIn("connector_sources", data_plane.text)
        self.assertEqual(liveness.status_code, 200)

    def test_openapi_applies_service_bearer_only_to_v1(self) -> None:
        schema = self.client.get("/openapi.json").json()
        security_scheme = schema["components"]["securitySchemes"]["ProvenaServiceBearer"]
        self.assertEqual(security_scheme["type"], "http")
        self.assertEqual(security_scheme["scheme"], "bearer")
        self.assertNotIn("security", schema["paths"]["/healthz"]["get"])
        for path, path_item in schema["paths"].items():
            if not path.startswith("/v1/"):
                continue
            for operation in path_item.values():
                if isinstance(operation, dict) and "responses" in operation:
                    self.assertEqual(
                        operation["security"],
                        [{"ProvenaServiceBearer": []}],
                        msg=f"missing service bearer requirement on {path}",
                    )

    def test_create_and_get_memory(self) -> None:
        payload = {
            "kind": "decision",
            "scope": {
                "tenant_id": "tenant-acme",
                "workspace_id": "ws-growth",
                "project_id": "proj-career-os",
                "user_id": "pm-1",
            },
            "title": "Use memory citations in agent answers",
            "content": "Every recall should expose provenance and a reason for retrieval.",
            "summary": "Citation-first memory policy",
            "entity_keys": ["memory-policy"],
            "tags": ["citations", "trust"],
            "source_references": [
                {
                    "source_type": "doc",
                    "source_id": "prd-42",
                    "title": "Memory PRD",
                    "excerpt": "Every answer must cite its source.",
                }
            ],
        }
        create_response = self.client.post("/v1/memories", json=payload)
        self.assertEqual(create_response.status_code, 200)
        created = create_response.json()
        self.assertTrue(created["created"])

        get_response = self.client.get(f"/v1/memories/{created['memory']['memory_id']}")
        self.assertEqual(get_response.status_code, 200)
        record = get_response.json()
        self.assertEqual(record["kind"], "decision")
        self.assertEqual(record["source_references"][0]["source_id"], "prd-42")

        duplicate_response = self.client.post("/v1/memories", json=payload)
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertFalse(duplicate_response.json()["created"])

    def test_create_and_update_transactions_roll_back_late_failures(self) -> None:
        store = self.client.app.state.store
        tables = (
            "memories",
            "memory_sources",
            "trigger_index",
            "memory_relations",
            "memories_fts",
            "audit_log",
            "memory_history",
        )

        def persistence_state() -> dict[str, list[tuple[object, ...]]]:
            return {
                table: [
                    tuple(row)
                    for row in store.conn.execute(
                        f"SELECT * FROM {table} ORDER BY 1"
                    ).fetchall()
                ]
                for table in tables
            }

        def cache_state() -> tuple[dict[str, int], dict[tuple[object, ...], str]]:
            return (
                dict(store.hot_cache._versions),
                {
                    key: json.dumps(value, sort_keys=True)
                    for key, (_expires_at, value) in store.hot_cache._entries.items()
                },
            )

        target = self.client.post(
            "/v1/memories",
            json={
                "kind": "decision",
                "scope": {"tenant_id": "tenant-atomic", "project_id": "project-atomic"},
                "title": "Atomic create target",
                "content": "A failed late create must not supersede this memory.",
            },
        )
        self.assertEqual(target.status_code, 200, target.text)
        target_id = target.json()["memory"]["memory_id"]
        create_state = persistence_state()
        create_cache = cache_state()
        original_vec_upsert = store._vec_upsert

        def fail_vec_upsert(*_args, **_kwargs) -> None:
            raise RuntimeError("late vector failure")

        store._vec_upsert = fail_vec_upsert
        try:
            with self.assertRaisesRegex(RuntimeError, "late vector failure"):
                self.client.post(
                    "/v1/memories",
                    json={
                        "memory_id": "memory-create-atomic-rollback",
                        "kind": "fact",
                        "scope": {
                            "tenant_id": "tenant-atomic",
                            "project_id": "project-atomic",
                        },
                        "title": "Must roll back",
                        "content": "Every durable projection must commit together.",
                        "source_references": [
                            {"source_type": "file", "source_id": "src/atomic.py"}
                        ],
                        "trigger_phrases": ["atomic rollback"],
                        "supersedes_memory_id": target_id,
                        "embedding": [0.25, 0.75],
                    },
                )
        finally:
            store._vec_upsert = original_vec_upsert

        self.assertEqual(persistence_state(), create_state)
        self.assertEqual(cache_state(), create_cache)
        self.assertEqual(
            store.conn.execute(
                "SELECT status FROM memories WHERE memory_id = ?",
                (target_id,),
            ).fetchone()["status"],
            "active",
        )

        update_source = self.client.post(
            "/v1/memories",
            json={
                "kind": "workflow",
                "scope": {"tenant_id": "tenant-atomic", "project_id": "project-atomic"},
                "title": "Atomic update source",
                "content": "A late update failure must restore the old projection.",
                "source_references": [
                    {"source_type": "file", "source_id": "src/before.py"}
                ],
                "trigger_phrases": ["before update"],
            },
        )
        self.assertEqual(update_source.status_code, 200, update_source.text)
        update_id = update_source.json()["memory"]["memory_id"]
        update_target = self.client.post(
            "/v1/memories",
            json={
                "kind": "fact",
                "scope": {"tenant_id": "tenant-atomic", "project_id": "project-atomic"},
                "title": "Atomic update target",
                "content": "A failed update must not supersede this target.",
            },
        )
        self.assertEqual(update_target.status_code, 200, update_target.text)
        update_target_id = update_target.json()["memory"]["memory_id"]
        update_state = persistence_state()
        update_cache = cache_state()
        original_insert_history = store._insert_history

        def fail_insert_history(*_args, **_kwargs) -> None:
            raise RuntimeError("late history failure")

        store._insert_history = fail_insert_history
        try:
            with self.assertRaisesRegex(RuntimeError, "late history failure"):
                self.client.put(
                    f"/v1/memories/{update_id}",
                    json={
                        "title": "Must not persist",
                        "source_references": [
                            {"source_type": "file", "source_id": "src/after.py"}
                        ],
                        "trigger_phrases": ["after update"],
                        "supersedes_memory_id": update_target_id,
                    },
                )
        finally:
            store._insert_history = original_insert_history

        self.assertEqual(persistence_state(), update_state)
        self.assertEqual(cache_state(), update_cache)
        self.assertEqual(
            store.conn.execute(
                "SELECT status FROM memories WHERE memory_id = ?",
                (update_target_id,),
            ).fetchone()["status"],
            "active",
        )

    def test_repository_procedures_require_dedicated_recall_after_store_sync(self) -> None:
        repository_id = "repository-procedure-recall-gate"
        events = []
        for index in range(6):
            event = self._repository_event(
                f"procedure-{index}", kind="workflow",
                body="PROCEDURE_ONLY_GUIDANCE validation session",
            )
            event["structured_data"] = {"procedure": {
                "schemaVersion": 1, "state": "candidate" if index == 0 else "approved",
                "episodeId": f"learn-{index}", "sessionId": "session-learning",
                "goal": "PROCEDURE_ONLY_GUIDANCE validation session",
                "triggers": ["validation session"], "prerequisites": [],
                "steps": [{"tool": "shell", "args": {"command": "PROCEDURE_STEP_SENTINEL", "unicode": "😀", "epsilon": 1e-7}}],
                "verification": [],
            }}
            if index == 0:
                event["created_at"] = event["updated_at"] = "2026-07-13T08:00:00.000Z"
            events.append(event)
        for outcome in ("success", "failure"):
            event = self._repository_event(
                f"outcome-{outcome}", kind="mistake" if outcome == "failure" else "fact",
                body="PROCEDURE_ONLY_GUIDANCE validation session",
            )
            event["structured_data"] = {"procedureOutcome": {"outcome": outcome}}
            events.append(event)
        ordinary = self._repository_event(
            "ordinary-validation", kind="invariant", body="Ordinary validation session guidance.",
            created_at="2026-07-13T09:00:00.000Z", supersedes=["procedure-0"],
        )
        events.append(ordinary)
        ledger = b"".join(rfc8785.dumps(event) + b"\n" for event in events).decode("utf-8")
        synced = self._sync_repository(repository_id, ledger)
        self.assertEqual(synced.status_code, 200, synced.text)
        headers = self._admin_headers()
        scope = {"tenant_id": "tenant-acme", "project_id": "project-repo-brain"}
        ordinary_id = self._repository_memory_id(repository_id, ordinary["id"])

        for event in events[:-1]:
            memory_id = self._repository_memory_id(repository_id, event["id"])
            raw = self.client.get(f"/v1/memories/{memory_id}", headers=headers)
            self.assertEqual(raw.status_code, 200, raw.text)
            self.assertEqual(raw.json()["metadata"]["provena_event"], event)

        for query in ("validation session", ""):
            searched = self.client.post("/v1/memories/search", headers=headers, json={
                "query": query, "scope": scope, "include_deleted": True,
                "include_relations": True, "limit": 1,
            })
            self.assertEqual(searched.status_code, 200, searched.text)
            results = searched.json()["results"]
            self.assertEqual([item["memory"]["memory_id"] for item in results], [ordinary_id])
            self.assertEqual(results[0]["related_memories"], [])
            self.assertNotIn("PROCEDURE_ONLY_GUIDANCE", searched.text)

        context_payload = {
            "query": "validation session", "scope": scope, "max_memories": 1,
        }
        context = self.client.post("/v1/agent/context", headers=headers, json=context_payload)
        self.assertEqual(context.status_code, 200, context.text)
        self.assertEqual([item["memory_id"] for item in context.json()["memories"]], [ordinary_id])
        self.assertNotIn("PROCEDURE_ONLY_GUIDANCE", context.text)
        viewer_context = self.client.post("/v1/agent/context", headers=self._viewer_headers(), json=context_payload)
        self.assertEqual(viewer_context.status_code, 200, viewer_context.text)
        self.assertNotIn("PROCEDURE_ONLY_GUIDANCE", viewer_context.text)
        foreign_context = self.client.post("/v1/agent/context", headers=self._viewer_headers(), json={
            **context_payload, "scope": {**scope, "tenant_id": "tenant-other"},
        })
        self.assertEqual(foreign_context.status_code, 403, foreign_context.text)
        for invalid in ({"max_memories": 0}, {"max_memories": 201}, {"max_characters": 127}, {"max_characters": 100_001}):
            rejected = self.client.post("/v1/agent/context", headers=headers, json={**context_payload, **invalid})
            self.assertEqual(rejected.status_code, 422, rejected.text)
        second = self._sync_repository(repository_id, ledger)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["no_op"], "the read gate must preserve lossless projection attestations")

    def test_repository_ledger_sync_is_lossless_idempotent_and_lifecycle_aware(self) -> None:
        repository_id = "repository-canonical-sync"
        kinds = ["fact", "decision", "workflow", "mistake", "preference", "handoff", "invariant"]
        events = [
            self._repository_event(f"event-{kind}", kind=kind)
            for kind in kinds
        ]
        ledger = self._repository_ledger(events, blank_line=True)
        expected_fingerprint = hashlib.sha256(ledger.encode("utf-8")).hexdigest()

        response = self._sync_repository(repository_id, ledger)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["ledger_fingerprint"], expected_fingerprint)
        self.assertEqual(result["received_events"], len(events))
        self.assertEqual(result["created_memories"], len(events))
        self.assertEqual(result["unchanged_memories"], 0)
        self.assertTrue(result["checkpoint_updated"])
        self.assertFalse(result["no_op"])
        self.assertGreaterEqual(result["duration_ms"], 0)
        self.assertEqual(result["duration_ms"], result["timings_ms"]["total"])

        store = self.client.app.state.store
        projection_rows = store.conn.execute(
            """
            SELECT event_id, event_fingerprint, memory_id
            FROM repo_memory_event_projections
            WHERE tenant_id = ? AND project_id = ? AND repository_id = ?
            ORDER BY event_id
            """,
            ("tenant-acme", "project-repo-brain", repository_id),
        ).fetchall()
        self.assertEqual(len(projection_rows), len(events))
        projected_kinds: set[str] = set()
        for row in projection_rows:
            expected_memory_id = self._repository_memory_id(repository_id, row["event_id"])
            self.assertEqual(row["memory_id"], expected_memory_id)
            memory_row = store.conn.execute(
                "SELECT kind, status, metadata_json, acl_json FROM memories WHERE memory_id = ?",
                (row["memory_id"],),
            ).fetchone()
            projected_kinds.add(memory_row["kind"])
            metadata = json.loads(memory_row["metadata_json"])
            source_event = next(event for event in events if event["id"] == row["event_id"])
            self.assertEqual(metadata["provena_event"], source_event)
            self.assertEqual(metadata["provena_event_fingerprint"], row["event_fingerprint"])
            self.assertEqual(memory_row["status"], source_event["status"])
            self.assertEqual(json.loads(memory_row["acl_json"]), [])
        self.assertEqual(projected_kinds, set(kinds))
        self.assertEqual(
            store.conn.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0],
            len(events) * 2,
        )
        self.assertEqual(
            store.conn.execute("SELECT COUNT(*) FROM memory_history").fetchone()[0],
            len(events),
        )

        replay = self._sync_repository(repository_id, ledger)
        self.assertEqual(replay.status_code, 200, replay.text)
        replay_result = replay.json()
        self.assertTrue(replay_result["no_op"])
        self.assertFalse(replay_result["checkpoint_updated"])
        self.assertEqual(replay_result["created_memories"], 0)
        self.assertEqual(replay_result["unchanged_memories"], len(events))
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM memory_history").fetchone()[0], len(events))
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0], len(events) * 2)

        replacement = self._repository_event(
            "event-fact-v2",
            body="Canonical repository memory now uses the v2 fact.",
            created_at="2026-07-13T11:00:00.000Z",
            supersedes=["event-fact"],
        )
        retracted = self._repository_event(
            "event-retracted",
            kind="handoff",
            status="retracted",
            body="This retracted handoff remains auditable but is not retrievable.",
            created_at="2026-07-13T11:30:00.000Z",
        )
        expanded_events = [*events, replacement, retracted]
        expanded_ledger = self._repository_ledger(expanded_events)
        expanded = self._sync_repository(repository_id, expanded_ledger)
        self.assertEqual(expanded.status_code, 200, expanded.text)
        expanded_result = expanded.json()
        self.assertEqual(expanded_result["created_memories"], 2)
        self.assertEqual(expanded_result["unchanged_memories"], len(events) - 1)
        self.assertEqual(expanded_result["repaired_memories"], 1)
        self.assertEqual(expanded_result["status_updates"], 1)
        self.assertEqual(expanded_result["created_relations"], 1)

        original_id = self._repository_memory_id(repository_id, "event-fact")
        replacement_id = self._repository_memory_id(repository_id, "event-fact-v2")
        retracted_id = self._repository_memory_id(repository_id, "event-retracted")
        original_row = store.conn.execute(
            "SELECT status, valid_to FROM memories WHERE memory_id = ?",
            (original_id,),
        ).fetchone()
        self.assertEqual(original_row["status"], "superseded")
        self.assertEqual(original_row["valid_to"], replacement["created_at"])
        self.assertEqual(
            store.conn.execute(
                """
                SELECT COUNT(*) FROM memory_relations
                WHERE from_memory_id = ? AND to_memory_id = ? AND relation = 'supersedes'
                """,
                (replacement_id, original_id),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            store.conn.execute("SELECT status FROM memories WHERE memory_id = ?", (retracted_id,)).fetchone()[0],
            "retracted",
        )
        immutable_update = self.client.put(
            f"/v1/memories/{replacement_id}",
            json={"content": "Generic CRUD must not rewrite a canonical event projection."},
            headers=self._admin_headers(),
        )
        self.assertEqual(immutable_update.status_code, 409)
        immutable_delete = self.client.delete(
            f"/v1/memories/{replacement_id}",
            params={"hard_delete": "true"},
            headers=self._admin_headers(),
        )
        self.assertEqual(immutable_delete.status_code, 409)
        search = self.client.post(
            "/v1/memories/search",
            json={
                "query": "Canonical repository memory",
                "scope": {"tenant_id": "tenant-acme", "project_id": "project-repo-brain"},
                "limit": 100,
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(search.status_code, 200)
        returned_ids = {item["memory"]["memory_id"] for item in search.json()["results"]}
        self.assertIn(replacement_id, returned_ids)
        self.assertNotIn(original_id, returned_ids)
        self.assertNotIn(retracted_id, returned_ids)
        peer_search = self.client.post(
            "/v1/memories/search",
            json={
                "query": "Canonical repository memory v2 fact",
                "scope": {"tenant_id": "tenant-acme", "project_id": "project-repo-brain"},
                "limit": 10,
            },
            headers={
                "X-Provena-Tenant-Id": "tenant-acme",
                "X-Provena-Role": "editor",
                "X-Provena-Principal-Id": "peer-coding-agent",
            },
        )
        self.assertEqual(peer_search.status_code, 200, peer_search.text)
        self.assertIn(
            replacement_id,
            {item["memory"]["memory_id"] for item in peer_search.json()["results"]},
        )

        old_branch = self._sync_repository(repository_id, ledger)
        self.assertEqual(old_branch.status_code, 200, old_branch.text)
        self.assertFalse(old_branch.json()["no_op"])
        self.assertEqual(old_branch.json()["created_memories"], 0)
        self.assertEqual(
            store.conn.execute("SELECT status FROM memories WHERE memory_id = ?", (original_id,)).fetchone()[0],
            "superseded",
            "an older branch snapshot must never reactivate a superseded event",
        )
        self.assertIsNotNone(
            store.conn.execute("SELECT memory_id FROM memories WHERE memory_id = ?", (replacement_id,)).fetchone(),
            "events absent from an older branch remain in the append-only projection",
        )

        changed_event = {**events[0], "body": "The same immutable event id now has different bytes."}
        conflict_ledger = self._repository_ledger([changed_event])
        conflict = self._sync_repository(repository_id, conflict_ledger)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        checkpoint = store.conn.execute(
            """
            SELECT ledger_fingerprint FROM repo_memory_sync_state
            WHERE tenant_id = ? AND project_id = ? AND repository_id = ?
            """,
            ("tenant-acme", "project-repo-brain", repository_id),
        ).fetchone()
        self.assertEqual(checkpoint["ledger_fingerprint"], hashlib.sha256(ledger.encode("utf-8")).hexdigest())

        mismatched = self._sync_repository(repository_id, ledger, fingerprint="0" * 64)
        self.assertEqual(mismatched.status_code, 422)
        wrong_tenant = self._sync_repository(
            repository_id,
            ledger,
            headers={
                "X-Provena-Tenant-Id": "tenant-other",
                "X-Provena-Role": "editor",
                "X-Provena-Principal-Id": "foreign-agent",
            },
        )
        self.assertEqual(wrong_tenant.status_code, 403)

        empty = self._sync_repository("repository-empty-ledger", "")
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json()["ledger_fingerprint"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(empty.json()["received_events"], 0)
        self.assertTrue(self._sync_repository("repository-empty-ledger", "").json()["no_op"])

    def test_repository_ledger_sync_rejects_invalid_batches_and_rolls_back_runtime_failures(self) -> None:
        repository_id = "repository-atomic-sync"
        valid = self._repository_event("event-valid")
        invalid = {**self._repository_event("event-invalid"), "unexpected": True}
        invalid_ledger = self._repository_ledger([valid, invalid])
        rejected = self._sync_repository(repository_id, invalid_ledger)
        self.assertEqual(rejected.status_code, 422, rejected.text)
        store = self.client.app.state.store
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM repo_memory_event_projections WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(
            store.conn.execute(
                "SELECT repository_id FROM repo_memory_sync_state WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
        )

        unknown_target = self._repository_event("event-unknown-target", supersedes=["event-missing"])
        missing = self._sync_repository(repository_id, self._repository_ledger([unknown_target]))
        self.assertEqual(missing.status_code, 422, missing.text)
        self.assertIn("unknown event id", missing.text)

        ledger = self._repository_ledger([valid])
        raw = ledger.encode("utf-8")
        payload = RepositoryMemorySyncRequest.model_validate(
            {
                "schema_version": 1,
                "scope": {"tenant_id": "tenant-acme", "project_id": "project-repo-brain"},
                "ledger_path": ".provena/memory/events.jsonl",
                "memory_fingerprint": hashlib.sha256(raw).hexdigest(),
                "ledger_bytes": len(raw),
                "ledger": ledger,
            }
        )
        original_insert_history = store._insert_history

        def fail_before_checkpoint(*args, **kwargs):
            raise RuntimeError("injected projection failure")

        store._insert_history = fail_before_checkpoint
        try:
            with self.assertRaisesRegex(RuntimeError, "injected projection failure"):
                store.sync_repository_memory_events(repository_id, payload)
        finally:
            store._insert_history = original_insert_history
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM repo_memory_event_projections WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM memories WHERE metadata_json LIKE ?",
                (f'%"provena_repository_id":"{repository_id}"%',),
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(
            store.conn.execute(
                "SELECT repository_id FROM repo_memory_sync_state WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
        )

        retry = self._sync_repository(repository_id, ledger)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["created_memories"], 1)

    def test_repository_sync_enforces_scope_causality_and_canonical_json_types(self) -> None:
        repository_id = "repository-causal-contract"
        event = self._repository_event("event-scoped")
        ledger = self._repository_ledger([event])
        first = self._sync_repository(
            repository_id,
            ledger,
            tenant_id="tenant-scope-a",
            project_id="project-scope-a",
        )
        second = self._sync_repository(
            repository_id,
            ledger,
            tenant_id="tenant-scope-b",
            project_id="project-scope-b",
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        rows = self.client.app.state.store.conn.execute(
            """
            SELECT tenant_id, project_id, memory_id
            FROM repo_memory_event_projections
            WHERE repository_id = ? AND event_id = ?
            ORDER BY tenant_id
            """,
            (repository_id, event["id"]),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["memory_id"], rows[1]["memory_id"])

        raw = ledger.encode("utf-8")
        unauthenticated = self.client.post(
            f"/v1/repositories/{repository_id}/memory-events/sync",
            json={
                "schema_version": 1,
                "scope": {"tenant_id": "tenant-scope-a", "project_id": "project-scope-a"},
                "ledger_path": ".provena/memory/events.jsonl",
                "memory_fingerprint": hashlib.sha256(raw).hexdigest(),
                "ledger_bytes": len(raw),
                "ledger": ledger,
            },
        )
        self.assertEqual(unauthenticated.status_code, 401)

        header_only_role = self.client.post(
            f"/v1/repositories/{repository_id}/memory-events/sync",
            json={
                "schema_version": 1,
                "scope": {"tenant_id": "tenant-scope-a", "project_id": "project-scope-a"},
                "ledger_path": ".provena/memory/events.jsonl",
                "memory_fingerprint": hashlib.sha256(raw).hexdigest(),
                "ledger_bytes": len(raw),
                "ledger": ledger,
            },
            headers={
                "X-Provena-Tenant-Id": "tenant-scope-a",
                "X-Provena-Role": "editor",
            },
        )
        self.assertEqual(header_only_role.status_code, 401)

        cross_tenant_erase = self.client.post(
            "/v1/admin/erase",
            json={"tenant_id": "tenant-scope-b", "project_id": "project-scope-b"},
            headers={
                "X-Provena-Tenant-Id": "tenant-scope-a",
                "X-Provena-Role": "admin",
                "X-Provena-Principal-Id": "tenant-scope-a-admin",
            },
        )
        self.assertEqual(cross_tenant_erase.status_code, 403)

        invalid_scope = self.client.post(
            f"/v1/repositories/{repository_id}/memory-events/sync",
            json={
                "schema_version": 1,
                "scope": {
                    "tenant_id": "tenant-scope-a",
                    "project_id": "project-scope-a",
                    "session_id": "not-part-of-repo-identity",
                },
                "ledger_path": ".provena/memory/events.jsonl",
                "memory_fingerprint": hashlib.sha256(raw).hexdigest(),
                "ledger_bytes": len(raw),
                "ledger": ledger,
            },
            headers={
                "X-Provena-Tenant-Id": "tenant-scope-a",
                "X-Provena-Role": "editor",
            },
        )
        self.assertEqual(invalid_scope.status_code, 422)

        unknown_scope_field = self.client.post(
            f"/v1/repositories/{repository_id}/memory-events/sync",
            json={
                "schema_version": 1,
                "scope": {
                    "tenant_id": "tenant-scope-a",
                    "project_id": "project-scope-a",
                    "unsupported_security_boundary": "must-not-be-ignored",
                },
                "ledger_path": ".provena/memory/events.jsonl",
                "memory_fingerprint": hashlib.sha256(raw).hexdigest(),
                "ledger_bytes": len(raw),
                "ledger": ledger,
            },
            headers={
                "X-Provena-Tenant-Id": "tenant-scope-a",
                "X-Provena-Role": "editor",
                "X-Provena-Principal-Id": "scope-contract-test",
            },
        )
        self.assertEqual(unknown_scope_field.status_code, 422)

        forward = self._repository_event(
            "event-forward-a",
            created_at="2026-07-13T11:00:00.000Z",
            supersedes=["event-forward-b"],
        )
        target = self._repository_event("event-forward-b")
        rejected_forward = self._sync_repository(
            "repository-forward-reference",
            self._repository_ledger([forward, target]),
        )
        self.assertEqual(rejected_forward.status_code, 422)
        self.assertIn("earlier ledger event", rejected_forward.text)

        human = self._repository_event("event-human", kind="decision")
        tool = self._repository_event(
            "event-tool",
            created_at="2026-07-13T11:00:00.000Z",
            supersedes=["event-human"],
        )
        rejected_authority = self._sync_repository(
            "repository-authority-downgrade",
            self._repository_ledger([human, tool]),
        )
        self.assertEqual(rejected_authority.status_code, 422)
        self.assertIn("cannot supersede human", rejected_authority.text)

        newer = self._repository_event(
            "event-newer-target",
            created_at="2026-07-13T12:00:00.000Z",
        )
        older = self._repository_event(
            "event-older-superseder",
            created_at="2026-07-13T11:00:00.000Z",
            supersedes=["event-newer-target"],
        )
        rejected_time = self._sync_repository(
            "repository-causal-time",
            self._repository_ledger([newer, older]),
        )
        self.assertEqual(rejected_time.status_code, 422)
        self.assertIn("cannot supersede newer", rejected_time.text)

        coerced = {**event, "id": "event-string-confidence", "confidence": "1"}
        self.assertEqual(
            self._sync_repository(
                "repository-strict-number",
                self._repository_ledger([coerced]),
            ).status_code,
            422,
        )
        bool_schema = {**event, "id": "event-bool-schema", "schema_version": True}
        self.assertEqual(
            self._sync_repository(
                "repository-strict-schema",
                self._repository_ledger([bool_schema]),
            ).status_code,
            422,
        )
        empty_tag = {**event, "id": "event-empty-tag", "tags": [""]}
        self.assertEqual(
            self._sync_repository(
                "repository-nonempty-lists",
                self._repository_ledger([empty_tag]),
            ).status_code,
            422,
        )
        empty_optional = {
            **event,
            "id": "event-empty-optional",
            "provenance": {**event["provenance"], "agent": ""},
        }
        self.assertEqual(
            self._sync_repository(
                "repository-nonempty-optionals",
                self._repository_ledger([empty_optional]),
            ).status_code,
            422,
        )
        escaped_path = {**event, "id": "event-escaped-path", "applies_to": ["../outside"]}
        self.assertEqual(
            self._sync_repository(
                "repository-safe-path",
                self._repository_ledger([escaped_path]),
            ).status_code,
            422,
        )
        root_scoped = {**event, "id": "event-root-scope", "applies_to": ["."]}
        root_scoped_response = self._sync_repository(
            "repository-root-scope",
            self._repository_ledger([root_scoped]),
        )
        self.assertEqual(root_scoped_response.status_code, 200, root_scoped_response.text)
        self.assertEqual(root_scoped_response.json()["received_events"], 1)
        root_source = {
            **event,
            "id": "event-root-source",
            "sources": [{**event["sources"][0], "path": "."}],
        }
        self.assertEqual(
            self._sync_repository(
                "repository-root-source",
                self._repository_ledger([root_source]),
            ).status_code,
            422,
        )
        nul_text = {**event, "id": "event-nul-text", "body": "unsafe\x00text"}
        self.assertEqual(
            self._sync_repository(
                "repository-portable-nul",
                self._repository_ledger([nul_text]),
            ).status_code,
            422,
        )
        nonfinite = {
            **event,
            "id": "event-nonfinite-json",
            "structured_data": {"not_json": float("nan")},
        }
        self.assertEqual(
            self._sync_repository(
                "repository-finite-json",
                self._repository_ledger([nonfinite]),
            ).status_code,
            422,
        )
        secret = {
            **event,
            "id": "event-secret-material",
            "body": f"accidental credential ghp_{'A' * 24}",
        }
        self.assertEqual(
            self._sync_repository(
                "repository-secret-guard",
                self._repository_ledger([secret]),
            ).status_code,
            422,
        )
        surrogate = {**event, "id": "event-surrogate-text", "body": "\ud800"}
        surrogate_ledger = (
            json.dumps(surrogate, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            + "\n"
        )
        self.assertEqual(
            self._sync_repository(
                "repository-portable-surrogate",
                surrogate_ledger,
            ).status_code,
            422,
        )
        shallow_record = json.dumps(
            {**event, "id": "event-too-deep", "structured_data": {}},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        deep_value = '{"x":' * 1_500 + "0" + "}" * 1_500
        deep_ledger = shallow_record.replace(
            '"structured_data":{}',
            f'"structured_data":{deep_value}',
        ) + "\n"
        deep_response = self._sync_repository(
            "repository-depth-limit",
            deep_ledger,
        )
        self.assertEqual(deep_response.status_code, 422, deep_response.text)
        unicode_event = self._repository_event(
            "event-unicode-separator",
        )
        unicode_event["structured_data"] = {
            "separator": "Unicode line separator \u2028 remains inside one JSONL record."
        }
        unicode_response = self._sync_repository(
            "repository-unicode-jsonl",
            self._repository_ledger([unicode_event]),
        )
        self.assertEqual(unicode_response.status_code, 200, unicode_response.text)

        multiline_event = self._repository_event("event-multiline-body")
        multiline_event["body"] = "First source-attested line.\nSecond source-attested line."
        multiline_response = self._sync_repository(
            "repository-multiline-body",
            self._repository_ledger([multiline_event]),
        )
        self.assertEqual(multiline_response.status_code, 200, multiline_response.text)
        multiline_id = self._repository_memory_id(
            "repository-multiline-body",
            "event-multiline-body",
        )
        multiline_row = self.client.app.state.store.conn.execute(
            "SELECT content FROM memories WHERE memory_id = ?",
            (multiline_id,),
        ).fetchone()
        self.assertEqual(multiline_row["content"], multiline_event["body"])

        utf16_ordered = self._repository_event("event-utf16-order")
        utf16_ordered["tags"] = ["canonical", "fact", "😀", "！"]
        utf16_response = self._sync_repository(
            "repository-utf16-order",
            self._repository_ledger([utf16_ordered]),
        )
        self.assertEqual(utf16_response.status_code, 200, utf16_response.text)

        retracted = self._repository_event(
            "event-hidden-related",
            status="retracted",
            body="Retracted nested content must never leak through relations.",
        )
        active = self._repository_event(
            "event-active-related",
            body="Active successor used to inspect nested relation filtering.",
            created_at="2026-07-13T11:00:00.000Z",
            supersedes=["event-hidden-related"],
        )
        relation_sync = self._sync_repository(
            "repository-related-filter",
            self._repository_ledger([retracted, active]),
        )
        self.assertEqual(relation_sync.status_code, 200, relation_sync.text)
        related_search = self.client.post(
            "/v1/memories/search",
            json={
                "query": "Active successor nested relation filtering",
                "scope": {"tenant_id": "tenant-acme", "project_id": "project-repo-brain"},
                "limit": 10,
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(related_search.status_code, 200, related_search.text)
        nested_ids = {
            related["memory"]["memory_id"]
            for result in related_search.json()["results"]
            for related in result["related_memories"]
        }
        self.assertNotIn(
            self._repository_memory_id("repository-related-filter", "event-hidden-related"),
            nested_ids,
        )

    def test_repository_sync_enforces_jcs_auth_and_projection_ownership(self) -> None:
        numeric = self._repository_event("event-jcs-number")
        numeric["structured_data"] = {"rate": 1e-7, "2": "two", "10": "ten"}
        numeric_ledger = rfc8785.dumps(numeric).decode("utf-8") + "\n"
        self.assertIn('"structured_data":{"10":"ten","2":"two","rate":1e-7}', numeric_ledger)
        accepted = self._sync_repository("repository-jcs-number", numeric_ledger)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(
            accepted.json()["events_fingerprint"],
            hashlib.sha256(numeric_ledger.encode("utf-8")).hexdigest(),
        )

        python_number_spelling = (
            json.dumps(numeric, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        )
        self.assertIn("1e-07", python_number_spelling)
        non_jcs = self._sync_repository("repository-non-jcs-number", python_number_spelling)
        self.assertEqual(non_jcs.status_code, 422, non_jcs.text)

        body_token = '"body":' + rfc8785.dumps(numeric["body"]).decode("utf-8")
        duplicate_key_ledger = numeric_ledger.replace(
            body_token,
            f'"body":"accidental credential ghp_{"A" * 24}",{body_token}',
        )
        duplicate = self._sync_repository("repository-duplicate-key", duplicate_key_ledger)
        self.assertEqual(duplicate.status_code, 422, duplicate.text)
        self.assertIn("duplicate JSON object keys", duplicate.text)

        unsafe_integer_ledger = numeric_ledger.replace("1e-7", "9007199254740992")
        unsafe_integer = self._sync_repository(
            "repository-unsafe-integer",
            unsafe_integer_ledger,
        )
        self.assertEqual(unsafe_integer.status_code, 422, unsafe_integer.text)

        negative_zero = self._sync_repository(
            "repository-negative-zero",
            numeric_ledger.replace("1e-7", "-0"),
        )
        self.assertEqual(negative_zero.status_code, 422, negative_zero.text)

        invalid_scope = self._sync_repository(
            "repository-invalid-scope",
            self._repository_ledger([self._repository_event("event-invalid-scope")]),
            tenant_id="tenant\x00hidden",
            headers=self._admin_headers(),
        )
        self.assertEqual(invalid_scope.status_code, 422, invalid_scope.text)

        blank_identity = self._sync_repository(
            "repository-blank-identity",
            self._repository_ledger([self._repository_event("event-blank-identity")]),
            headers={
                "X-Provena-Tenant-Id": "tenant-acme",
                "X-Provena-Role": "editor",
                "X-Provena-Principal-Id": "   ",
            },
        )
        self.assertEqual(blank_identity.status_code, 401, blank_identity.text)
        invalid_role = self._sync_repository(
            "repository-invalid-role",
            self._repository_ledger([self._repository_event("event-invalid-role")]),
            headers={
                "X-Provena-Tenant-Id": "tenant-acme",
                "X-Provena-Role": "owner",
                "X-Provena-Principal-Id": "principal-1",
            },
        )
        self.assertEqual(invalid_role.status_code, 422, invalid_role.text)

        repository_id = "repository-owned-projection"
        event = self._repository_event("event-owned-projection")
        synced = self._sync_repository(repository_id, self._repository_ledger([event]))
        self.assertEqual(synced.status_code, 200, synced.text)
        repository_memory_id = self._repository_memory_id(repository_id, event["id"])
        with self.client.app.state.store.conn:
            self.client.app.state.store.conn.execute(
                "DELETE FROM repo_memory_event_projections WHERE memory_id = ?",
                (repository_memory_id,),
            )
        ordinary_payload = {
            "kind": "fact",
            "scope": {"tenant_id": "tenant-acme", "project_id": "project-repo-brain"},
            "title": "Ordinary memory",
            "content": "Generic writes must not mutate source-attested projections.",
        }
        forged_create = self.client.post(
            "/v1/memories",
            json={**ordinary_payload, "supersedes_memory_id": repository_memory_id},
            headers=self._admin_headers(),
        )
        self.assertEqual(forged_create.status_code, 409, forged_create.text)

        ordinary = self.client.post(
            "/v1/memories",
            json=ordinary_payload,
            headers=self._admin_headers(),
        )
        self.assertEqual(ordinary.status_code, 200, ordinary.text)
        ordinary_id = ordinary.json()["memory"]["memory_id"]
        forged_update = self.client.put(
            f"/v1/memories/{ordinary_id}",
            json={"supersedes_memory_id": repository_memory_id},
            headers=self._admin_headers(),
        )
        self.assertEqual(forged_update.status_code, 409, forged_update.text)
        forged_relation = self.client.post(
            "/v1/memories/relations",
            json={
                "from_memory_id": ordinary_id,
                "to_memory_id": repository_memory_id,
                "relation": "supersedes",
                "scope": {"tenant_id": "tenant-acme", "project_id": "project-repo-brain"},
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(forged_relation.status_code, 409, forged_relation.text)
        projection = self.client.app.state.store.conn.execute(
            "SELECT status FROM memories WHERE memory_id = ?",
            (repository_memory_id,),
        ).fetchone()
        self.assertEqual(projection["status"], "active")

    def test_future_dated_supersession_is_hidden_from_normal_search(self) -> None:
        repository_id = "repository-future-supersession"
        original = self._repository_event(
            "event-future-original",
            body="Future supersession sentinel must not remain normal guidance.",
        )
        successor = self._repository_event(
            "event-future-successor",
            body="Future replacement is not valid yet.",
            created_at="2099-01-01T00:00:00.000Z",
            supersedes=[original["id"]],
        )
        synced = self._sync_repository(
            repository_id,
            self._repository_ledger([original, successor]),
        )
        self.assertEqual(synced.status_code, 200, synced.text)
        query = {
            "query": "Future supersession sentinel",
            "scope": {"tenant_id": "tenant-acme", "project_id": "project-repo-brain"},
            "limit": 10,
        }
        current = self.client.post(
            "/v1/memories/search",
            json=query,
            headers=self._admin_headers(),
        )
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json()["results"], [])
        audit = self.client.post(
            "/v1/memories/search",
            json={**query, "include_deleted": True},
            headers=self._admin_headers(),
        )
        self.assertEqual(audit.status_code, 200, audit.text)
        self.assertIn(
            self._repository_memory_id(repository_id, original["id"]),
            {item["memory"]["memory_id"] for item in audit.json()["results"]},
        )

    def test_repository_sync_repairs_drift_and_prevents_governance_resurrection(self) -> None:
        repository_id = "repository-governed-replay"
        original = self._repository_event("event-governed-original")
        successor = self._repository_event(
            "event-governed-successor",
            kind="decision",
            created_at="2026-07-13T11:00:00.000Z",
            supersedes=["event-governed-original"],
        )
        ledger = self._repository_ledger([original, successor])
        initial = self._sync_repository(repository_id, ledger)
        self.assertEqual(initial.status_code, 200, initial.text)
        store = self.client.app.state.store
        original_id = self._repository_memory_id(repository_id, original["id"])
        successor_id = self._repository_memory_id(repository_id, successor["id"])

        with store.conn:
            store.conn.execute(
                "UPDATE memories SET content = ?, status = ? WHERE memory_id = ?",
                ("drifted content", "deleted", successor_id),
            )
            store.conn.execute(
                """
                DELETE FROM memory_relations
                WHERE from_memory_id = ? AND to_memory_id = ? AND relation = 'supersedes'
                """,
                (successor_id, original_id),
            )
        repaired = self._sync_repository(repository_id, ledger)
        self.assertEqual(repaired.status_code, 200, repaired.text)
        repaired_result = repaired.json()
        self.assertFalse(repaired_result["no_op"])
        self.assertEqual(repaired_result["repaired_memories"], 1)
        self.assertEqual(repaired_result["created_relations"], 1)
        successor_row = store.conn.execute(
            "SELECT content, status FROM memories WHERE memory_id = ?",
            (successor_id,),
        ).fetchone()
        self.assertEqual(successor_row["content"], successor["body"])
        self.assertEqual(successor_row["status"], "active")

        with store.conn:
            store.conn.execute(
                "UPDATE memories SET workspace_id = ? WHERE memory_id = ?",
                ("corrupt-workspace", successor_id),
            )
            store.conn.execute(
                "UPDATE trigger_index SET tenant_id = ? WHERE memory_id = ?",
                ("corrupt-tenant", successor_id),
            )
            store.conn.execute(
                "DELETE FROM memories_fts WHERE memory_id = ?",
                (successor_id,),
            )
            store.conn.execute(
                """
                UPDATE memory_relations
                SET tenant_id = ?, project_id = ?
                WHERE from_memory_id = ? AND to_memory_id = ? AND relation = 'supersedes'
                """,
                ("corrupt-tenant", "corrupt-project", successor_id, original_id),
            )
        scope_repaired = self._sync_repository(repository_id, ledger)
        self.assertEqual(scope_repaired.status_code, 200, scope_repaired.text)
        self.assertEqual(scope_repaired.json()["repaired_memories"], 1)
        self.assertEqual(scope_repaired.json()["repaired_relations"], 1)
        repaired_memory_scope = store.conn.execute(
            """
            SELECT tenant_id, workspace_id, project_id, user_id, agent_id, session_id
            FROM memories WHERE memory_id = ?
            """,
            (successor_id,),
        ).fetchone()
        self.assertEqual(
            tuple(repaired_memory_scope),
            ("tenant-acme", None, "project-repo-brain", None, None, None),
        )
        repaired_relation_scope = store.conn.execute(
            """
            SELECT tenant_id, workspace_id, project_id, user_id, agent_id, session_id
            FROM memory_relations
            WHERE from_memory_id = ? AND to_memory_id = ? AND relation = 'supersedes'
            """,
            (successor_id, original_id),
        ).fetchone()
        self.assertEqual(
            tuple(repaired_relation_scope),
            ("tenant-acme", None, "project-repo-brain", None, None, None),
        )
        self.assertEqual(
            {
                row["tenant_id"]
                for row in store.conn.execute(
                    "SELECT tenant_id FROM trigger_index WHERE memory_id = ?",
                    (successor_id,),
                ).fetchall()
            },
            {"tenant-acme"},
        )
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM memories_fts WHERE memory_id = ?",
                (successor_id,),
            ).fetchone()[0],
            1,
        )

        with store.conn:
            store.conn.execute(
                "DELETE FROM repo_memory_event_projections WHERE memory_id = ?",
                (successor_id,),
            )
            store.conn.execute(
                """
                UPDATE memories
                SET kind = ?, title = ?, content = ?, workspace_id = ?, project_id = ?
                WHERE memory_id = ?
                """,
                (
                    "preference",
                    "drifted title",
                    "drifted content after mapping loss",
                    "corrupt-workspace",
                    "corrupt-project",
                    successor_id,
                ),
            )
        mapping_repaired = self._sync_repository(repository_id, ledger)
        self.assertEqual(mapping_repaired.status_code, 200, mapping_repaired.text)
        self.assertFalse(mapping_repaired.json()["no_op"])
        self.assertEqual(mapping_repaired.json()["repaired_memories"], 1)
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM repo_memory_event_projections WHERE memory_id = ?",
                (successor_id,),
            ).fetchone()[0],
            1,
        )
        restored_row = store.conn.execute(
            "SELECT kind, title, content, workspace_id, project_id FROM memories WHERE memory_id = ?",
            (successor_id,),
        ).fetchone()
        self.assertEqual(
            tuple(restored_row),
            ("decision", successor["title"], successor["body"], None, "project-repo-brain"),
        )

        retention = self.client.post(
            "/v1/admin/retention-policies",
            json={
                "policy_id": "repo-decision-retention",
                "tenant_id": "tenant-acme",
                "kind": "decision",
                "max_age_days": 0,
                "action": "delete_hard",
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(retention.status_code, 200, retention.text)
        enforced = self.client.post(
            "/v1/admin/retention/enforce",
            json={"tenant_id": "tenant-acme"},
            headers=self._admin_headers(),
        )
        self.assertEqual(enforced.status_code, 200, enforced.text)
        self.assertIn(successor_id, enforced.json()["expired_memory_ids"])
        self.assertIsNone(
            store.conn.execute(
                "SELECT memory_id FROM memories WHERE memory_id = ?",
                (successor_id,),
            ).fetchone()
        )
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM memories_fts WHERE memory_id = ?",
                (successor_id,),
            ).fetchone()[0],
            0,
            "retention must purge external FTS content, not only the memory row",
        )
        self.assertEqual(
            store.conn.execute(
                "SELECT status FROM memories WHERE memory_id = ?",
                (original_id,),
            ).fetchone()["status"],
            "superseded",
        )

        suppressed = self._sync_repository(repository_id, ledger)
        self.assertEqual(suppressed.status_code, 200, suppressed.text)
        suppressed_result = suppressed.json()
        self.assertEqual(suppressed_result["suppressed_events"], 1)
        self.assertEqual(suppressed_result["suppressed_relations"], 1)
        self.assertEqual(
            store.conn.execute(
                "SELECT status FROM memories WHERE memory_id = ?",
                (original_id,),
            ).fetchone()["status"],
            "superseded",
            "erasing a successor must not reactivate stale guidance",
        )
        self.assertIsNone(
            store.conn.execute(
                "SELECT memory_id FROM memories WHERE memory_id = ?",
                (successor_id,),
            ).fetchone(),
            "retention tombstones must prevent ledger replay resurrection",
        )

        cached_memory = self.client.post(
            "/v1/memories",
            json={
                "kind": "fact",
                "scope": {"tenant_id": "tenant-acme", "project_id": "project-repo-brain"},
                "title": "RTBF cache sentinel",
                "content": "Sensitive cached content must disappear immediately after RTBF.",
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(cached_memory.status_code, 200, cached_memory.text)
        cache_query = {
            "query": "Sensitive cached content RTBF",
            "scope": {"tenant_id": "tenant-acme", "project_id": "project-repo-brain"},
            "limit": 10,
        }
        cached_search = self.client.post(
            "/v1/memories/search",
            json=cache_query,
            headers=self._admin_headers(),
        )
        self.assertTrue(cached_search.json()["results"])
        in_memory_entries = getattr(store.hot_cache, "_entries")
        self.assertTrue(
            any(
                "Sensitive cached content" in json.dumps(payload)
                for _, payload in in_memory_entries.values()
            )
        )

        with store.conn:
            store.conn.execute(
                "UPDATE memories SET metadata_json = '{}' WHERE memory_id = ?",
                (original_id,),
            )
        forgotten = self.client.post(
            "/v1/admin/rtbf",
            json={"tenant_id": "tenant-acme"},
            headers=self._admin_headers(),
        )
        self.assertEqual(forgotten.status_code, 200, forgotten.text)
        self.assertGreaterEqual(forgotten.json()["deleted_memories"], 1)
        self.assertFalse(
            any(key[0] == "tenant-acme" for key in in_memory_entries),
            "RTBF must physically purge old tenant cache payloads",
        )
        replay = self._sync_repository(repository_id, ledger)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["suppressed_events"], 2)
        after_rtbf = self.client.post(
            "/v1/memories/search",
            json=cache_query,
            headers=self._admin_headers(),
        )
        self.assertEqual(after_rtbf.json()["results"], [])
        self.assertFalse(
            any(
                "Sensitive cached content" in json.dumps(payload)
                for _, payload in in_memory_entries.values()
            ),
            "new cache versions must not retain erased content",
        )
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM memories WHERE memory_id IN (?, ?)",
                (original_id, successor_id),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM memories_fts WHERE memory_id IN (?, ?)",
                (original_id, successor_id),
            ).fetchone()[0],
            0,
            "RTBF must purge all external FTS content",
        )

    def test_missing_projection_mapping_cannot_bypass_governance_tombstones(self) -> None:
        store = self.client.app.state.store
        for action in ("rtbf", "erase", "retention"):
            with self.subTest(action=action):
                tenant_id = f"tenant-mapping-{action}"
                project_id = f"project-mapping-{action}"
                repository_id = f"repository-mapping-{action}"
                event = self._repository_event(f"event-mapping-{action}")
                ledger = self._repository_ledger([event])
                synced = self._sync_repository(
                    repository_id,
                    ledger,
                    tenant_id=tenant_id,
                    project_id=project_id,
                )
                self.assertEqual(synced.status_code, 200, synced.text)
                memory_id = self._repository_memory_id(
                    repository_id,
                    event["id"],
                    tenant_id=tenant_id,
                    project_id=project_id,
                )
                with store.conn:
                    store.conn.execute(
                        "DELETE FROM repo_memory_event_projections WHERE memory_id = ?",
                        (memory_id,),
                    )

                headers = self._admin_headers(tenant_id)
                if action == "rtbf":
                    governed = self.client.post(
                        "/v1/admin/rtbf",
                        json={"tenant_id": tenant_id},
                        headers=headers,
                    )
                elif action == "erase":
                    governed = self.client.post(
                        "/v1/admin/erase",
                        json={"tenant_id": tenant_id, "project_id": project_id},
                        headers=headers,
                    )
                else:
                    policy = self.client.post(
                        "/v1/admin/retention-policies",
                        json={
                            "policy_id": f"policy-mapping-{action}",
                            "tenant_id": tenant_id,
                            "kind": "fact",
                            "max_age_days": 0,
                            "action": "delete_hard",
                        },
                        headers=headers,
                    )
                    self.assertEqual(policy.status_code, 200, policy.text)
                    governed = self.client.post(
                        "/v1/admin/retention/enforce",
                        json={"tenant_id": tenant_id},
                        headers=headers,
                    )
                self.assertEqual(governed.status_code, 200, governed.text)
                self.assertIsNone(
                    store.conn.execute(
                        "SELECT memory_id FROM memories WHERE memory_id = ?",
                        (memory_id,),
                    ).fetchone()
                )
                self.assertEqual(
                    store.conn.execute(
                        """
                        SELECT COUNT(*) FROM repo_memory_event_erasures
                        WHERE tenant_id = ? AND project_id = ?
                          AND repository_id = ? AND event_id = ?
                        """,
                        (tenant_id, project_id, repository_id, event["id"]),
                    ).fetchone()[0],
                    1,
                )
                replay = self._sync_repository(
                    repository_id,
                    ledger,
                    tenant_id=tenant_id,
                    project_id=project_id,
                )
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertEqual(replay.json()["suppressed_events"], 1)
                self.assertEqual(replay.json()["created_memories"], 0)

    def test_repository_sync_chunks_large_projection_lookups(self) -> None:
        events = [
            self._repository_event(f"event-bulk-{index:04d}")
            for index in range(1_050)
        ]
        response = self._sync_repository(
            "repository-large-ledger",
            self._repository_ledger(events),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["created_memories"], len(events))
        replay = self._sync_repository(
            "repository-large-ledger",
            self._repository_ledger(events),
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["no_op"])
        self.assertEqual(replay.json()["unchanged_memories"], len(events))

    def test_repository_sync_serializes_concurrent_first_writers(self) -> None:
        from app.store import ProvenaStore

        database = self.temp_dir / "repository-sync-race.db"
        stores = [ProvenaStore(database), ProvenaStore(database)]
        event = self._repository_event("event-concurrent")
        ledger = self._repository_ledger([event])
        raw = ledger.encode("utf-8")
        payload = RepositoryMemorySyncRequest(
            schema_version=1,
            scope={"tenant_id": "tenant-race", "project_id": "project-race"},
            ledger_path=".provena/memory/events.jsonl",
            memory_fingerprint=hashlib.sha256(raw).hexdigest(),
            ledger_bytes=len(raw),
            ledger=ledger,
        )
        barrier = threading.Barrier(2)

        def sync(store: ProvenaStore):
            barrier.wait()
            return store.sync_repository_memory_events("repository-race", payload)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(sync, stores))
        finally:
            for store in stores:
                store.close()
        self.assertEqual(sorted(result.created_memories for result in results), [0, 1])
        self.assertEqual(sum(result.no_op for result in results), 1)

    def test_search_prefers_exact_scope_and_latest_revision(self) -> None:
        broad = {
            "kind": "fact",
            "scope": {
                "tenant_id": "tenant-acme",
                "workspace_id": "ws-growth",
            },
            "title": "LinkedIn trust policy",
            "content": "LinkedIn rewards authentic PM storytelling with evidence.",
            "tags": ["linkedin"],
            "entity_keys": ["authenticity"],
        }
        broad_response = self.client.post("/v1/memories", json=broad)
        self.assertEqual(broad_response.status_code, 200)

        refined = {
            "kind": "fact",
            "scope": {
                "tenant_id": "tenant-acme",
                "workspace_id": "ws-growth",
                "user_id": "pm-1",
                "session_id": "session-7",
            },
            "title": "LinkedIn trust policy",
            "content": "LinkedIn rewards authentic PM storytelling with evidence and penalties for automation abuse.",
            "tags": ["linkedin", "policy"],
            "entity_keys": ["authenticity"],
        }
        refined_response = self.client.post("/v1/memories", json=refined)
        self.assertEqual(refined_response.status_code, 200)

        search_response = self.client.post(
            "/v1/memories/search",
            json={
                "query": "LinkedIn evidence automation",
                "scope": {
                    "tenant_id": "tenant-acme",
                    "workspace_id": "ws-growth",
                    "user_id": "pm-1",
                    "session_id": "session-7",
                },
                "limit": 5,
            },
        )
        self.assertEqual(search_response.status_code, 200)
        results = search_response.json()["results"]
        self.assertGreaterEqual(len(results), 2)
        self.assertEqual(results[0]["memory"]["scope"]["session_id"], "session-7")
        self.assertIn("exact session scope", results[0]["reasons"])

    def test_hard_delete_removes_memory(self) -> None:
        headers = self._admin_headers()
        created = self.client.post(
            "/v1/memories",
            json={
                "kind": "fact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Ephemeral",
                "content": "to be hard-deleted",
            },
            headers=headers,
        )
        mem_id = created.json()["memory"]["memory_id"]
        resp = self.client.delete(
            f"/v1/memories/{mem_id}", params={"hard_delete": "true"}, headers=headers
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["hard_delete"])
        self.assertEqual(
            self.client.get(f"/v1/memories/{mem_id}", headers=headers).status_code, 404
        )

    def test_duplicate_create_respects_soft_delete_tombstone(self) -> None:
        headers = self._admin_headers()
        payload = {
            "kind": "fact",
            "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
            "title": "Restorable repository fact",
            "content": "The generated index can safely restore this exact fact.",
            "tags": ["indexed"],
        }
        created = self.client.post("/v1/memories", json=payload, headers=headers)
        self.assertEqual(created.status_code, 200)
        memory_id = created.json()["memory"]["memory_id"]

        deleted = self.client.delete(f"/v1/memories/{memory_id}", headers=headers)
        self.assertEqual(deleted.status_code, 200)
        hidden = self.client.post(
            "/v1/memories/search",
            json={
                "query": "Restorable repository fact",
                "scope": payload["scope"],
                "limit": 5,
            },
            headers=headers,
        )
        self.assertEqual(hidden.json()["results"], [])

        repeated = self.client.post("/v1/memories", json=payload, headers=headers)
        self.assertEqual(repeated.status_code, 409)
        self.assertIn("tombstoned", repeated.text)

        visible = self.client.post(
            "/v1/memories/search",
            json={
                "query": "Restorable repository fact",
                "scope": payload["scope"],
                "limit": 5,
            },
            headers=headers,
        )
        self.assertEqual(visible.json()["results"], [])

        hard_deleted = self.client.delete(
            f"/v1/memories/{memory_id}",
            params={"hard_delete": "true"},
            headers=headers,
        )
        self.assertEqual(hard_deleted.status_code, 200)
        recreated = self.client.post("/v1/memories", json=payload, headers=headers)
        self.assertEqual(recreated.status_code, 200)
        self.assertTrue(recreated.json()["created"])
        self.assertNotEqual(recreated.json()["memory"]["memory_id"], memory_id)

    def test_generated_memory_fingerprint_tracks_source_identity(self) -> None:
        base = {
            "kind": "fact",
            "scope": {"tenant_id": "tenant-acme", "project_id": "repo-brain"},
            "title": "src/example.ts::run",
            "content": "export function run() { return true; }",
            "metadata": {"provena_generated_fingerprint": "a" * 64},
        }
        first = self.client.post("/v1/memories", json=base)
        self.assertEqual(first.status_code, 200)
        first_id = first.json()["memory"]["memory_id"]

        changed_content = {
            **base,
            "content": "export function run() { return false; }",
        }
        changed = self.client.post("/v1/memories", json=changed_content)
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(changed.json()["created"])
        self.assertNotEqual(changed.json()["memory"]["memory_id"], first_id)

        moved = {
            **base,
            "metadata": {"provena_generated_fingerprint": "b" * 64},
        }
        second = self.client.post("/v1/memories", json=moved)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["created"])
        self.assertNotEqual(second.json()["memory"]["memory_id"], first_id)

        duplicate = self.client.post("/v1/memories", json=moved)
        self.assertEqual(duplicate.status_code, 200)
        self.assertFalse(duplicate.json()["created"])
        self.assertEqual(
            duplicate.json()["memory"]["memory_id"],
            second.json()["memory"]["memory_id"],
        )

        unicode_scope = ScopeEnvelope(tenant_id="\u79df\u6237", project_id="\u4ed3\u5e93")
        generated_identity = "c" * 64
        expected_scope = json.dumps(
            unicode_scope.model_dump(exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        expected = hashlib.sha256(
            "|".join(
                [
                    expected_scope,
                    "artifact",
                    "generated title",
                    "generated content",
                    "provena-generated-v2",
                    generated_identity,
                ]
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            self.client.app.state.store._fingerprint(
                unicode_scope,
                "artifact",
                "Generated Title",
                "Generated Content",
                {"provena_generated_fingerprint": generated_identity},
            ),
            expected,
        )
        self.assertEqual(
            self.client.app.state.store._canonical_repository_json(
                {"\uff01": 2, "\U0001f600": 1}
            ),
            '{"\U0001f600":1,"\uff01":2}',
            "repository canonical JSON must use JavaScript UTF-16 key ordering",
        )

    def test_duplicate_create_cannot_cross_tenant_or_acl_boundary(self) -> None:
        owner_headers = self._admin_headers("tenant-acme")
        payload = {
            "kind": "fact",
            "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
            "title": "Tenant-private restorable fact",
            "content": "Only an authorized tenant principal may restore this fact.",
        }
        created = self.client.post("/v1/memories", json=payload, headers=owner_headers)
        self.assertEqual(created.status_code, 200)
        memory_id = created.json()["memory"]["memory_id"]

        active_duplicate = self.client.post(
            "/v1/memories",
            json=payload,
            headers={
                **self._viewer_headers("tenant-acme", "unauthorized-viewer"),
                "X-Provena-Role": "editor",
            },
        )
        self.assertEqual(active_duplicate.status_code, 404)
        self.assertNotIn(memory_id, active_duplicate.text)

        self.assertEqual(
            self.client.delete(f"/v1/memories/{memory_id}", headers=owner_headers).status_code,
            200,
        )
        cross_tenant_restore = self.client.post(
            "/v1/memories",
            json=payload,
            headers=self._admin_headers("tenant-other"),
        )
        self.assertEqual(cross_tenant_restore.status_code, 403)
        self.assertNotIn(memory_id, cross_tenant_restore.text)

        still_hidden = self.client.post(
            "/v1/memories/search",
            json={"query": payload["title"], "scope": payload["scope"], "limit": 5},
            headers=owner_headers,
        )
        self.assertEqual(still_hidden.status_code, 200)
        self.assertEqual(still_hidden.json()["results"], [])

        owner_replay = self.client.post("/v1/memories", json=payload, headers=owner_headers)
        self.assertEqual(owner_replay.status_code, 409)
        self.assertIn("tombstoned", owner_replay.text)

    def test_create_rejects_scope_tenant_mismatch(self) -> None:
        response = self.client.post(
            "/v1/memories",
            json={
                "kind": "fact",
                "scope": {"tenant_id": "tenant-other", "workspace_id": "ws-growth"},
                "title": "Cross-tenant create",
                "content": "Must not be created under a mismatched access tenant.",
            },
            headers=self._admin_headers("tenant-acme"),
        )
        self.assertEqual(response.status_code, 403)

    def test_update_history_and_feedback_roundtrip(self) -> None:
        headers = self._admin_headers()
        created = self.client.post(
            "/v1/memories",
            json={
                "kind": "fact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Original",
                "content": "Original content about onboarding.",
            },
            headers=headers,
        )
        self.assertEqual(created.status_code, 200)
        mem_id = created.json()["memory"]["memory_id"]

        updated = self.client.put(
            f"/v1/memories/{mem_id}",
            json={"title": "Revised", "content": "Revised content about onboarding."},
            headers=headers,
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["memory"]["title"], "Revised")

        hist = self.client.get(f"/v1/memories/{mem_id}/history", headers=headers)
        self.assertEqual(hist.status_code, 200)
        self.assertGreaterEqual(len(hist.json()), 1)
        self.assertIn("update", {row["event"] for row in hist.json()})

        fb = self.client.post(
            f"/v1/memories/{mem_id}/feedback",
            json={"feedback_type": "positive", "reason": "useful"},
            headers=headers,
        )
        self.assertEqual(fb.status_code, 200)

        fb_list = self.client.get(f"/v1/memories/{mem_id}/feedback", headers=headers)
        self.assertEqual(fb_list.status_code, 200)
        self.assertEqual(len(fb_list.json()), 1)
        self.assertEqual(fb_list.json()[0]["feedback_type"], "positive")

        # Unknown memory: mutating routes 404 rather than silently succeeding.
        self.assertEqual(
            self.client.put("/v1/memories/nope", json={"title": "x"}, headers=headers).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                "/v1/memories/nope/feedback",
                json={"feedback_type": "positive"},
                headers=headers,
            ).status_code,
            404,
        )

    def test_memory_history_denies_unauthorized_tenant(self) -> None:
        headers = self._admin_headers()
        created = self.client.post(
            "/v1/memories",
            json={
                "kind": "fact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Tenant scoped",
                "content": "History should not leak across tenants.",
            },
            headers=headers,
        )
        self.assertEqual(created.status_code, 200)
        mem_id = created.json()["memory"]["memory_id"]

        cross_tenant = self.client.get(
            f"/v1/memories/{mem_id}/history",
            headers=self._admin_headers("tenant-other"),
        )
        self.assertEqual(cross_tenant.status_code, 404)

    def test_search_includes_related_memories(self) -> None:
        primary = self.client.post(
            "/v1/memories",
            json={
                "kind": "decision",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Adopt provenance-first memory",
                "content": "Provena will store evidence-backed memory records.",
                "entity_keys": ["provena"],
                "tags": ["architecture"],
            },
        ).json()["memory"]["memory_id"]
        supporting = self.client.post(
            "/v1/memories",
            json={
                "kind": "artifact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Competitive analysis",
                "content": "Competitors rarely expose provenance and scoped recall together.",
                "entity_keys": ["provena"],
                "tags": ["research"],
            },
        ).json()["memory"]["memory_id"]

        relation_response = self.client.post(
            "/v1/memories/relations",
            json={
                "from_memory_id": primary,
                "to_memory_id": supporting,
                "relation": "supports",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
            },
        )
        self.assertEqual(relation_response.status_code, 204)

        search_response = self.client.post(
            "/v1/memories/search",
            json={
                "query": "provenance competitive analysis",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "limit": 5,
            },
        )
        self.assertEqual(search_response.status_code, 200)
        results = search_response.json()["results"]
        primary_result = next(
            result for result in results if result["memory"]["memory_id"] == primary
        )
        self.assertEqual(primary_result["related_memories"][0]["relation"], "supports")
        self.assertEqual(primary_result["related_memories"][0]["memory"]["memory_id"], supporting)

    def test_relation_scope_is_derived_from_both_memories_and_cannot_cross_tenants(self) -> None:
        scope = {
            "tenant_id": "tenant-acme",
            "workspace_id": "workspace-relations",
            "project_id": "project-relations",
            "user_id": "principal-admin-1",
            "agent_id": "agent-relations",
            "session_id": "session-relations",
        }

        def create_memory(title: str, memory_scope: dict[str, str], tenant_id: str) -> str:
            response = self.client.post(
                "/v1/memories",
                json={
                    "kind": "fact",
                    "scope": memory_scope,
                    "title": title,
                    "content": f"Relation scope fixture for {title}.",
                },
                headers=self._admin_headers(tenant_id),
            )
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()["memory"]["memory_id"]

        source_id = create_memory("Scoped source", scope, "tenant-acme")
        target_id = create_memory("Scoped target", scope, "tenant-acme")
        mismatched_targets = {
            field: create_memory(
                f"Mismatched {field} target",
                {**scope, field: f"different-{field}"},
                "tenant-acme",
            )
            for field in (
                "workspace_id",
                "project_id",
                "user_id",
                "agent_id",
                "session_id",
            )
        }
        foreign_id = create_memory(
            "Foreign target",
            {
                "tenant_id": "tenant-other",
                "workspace_id": "workspace-relations",
                "project_id": "project-relations",
                "user_id": "principal-admin-1",
            },
            "tenant-other",
        )
        store = self.client.app.state.store
        before = store.conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0]

        for field in scope:
            forged_scope = self.client.post(
                "/v1/memories/relations",
                json={
                    "from_memory_id": source_id,
                    "to_memory_id": target_id,
                    "relation": "supports",
                    "scope": {**scope, field: f"forged-{field}"},
                },
                headers=self._admin_headers("tenant-acme"),
            )
            self.assertEqual(forged_scope.status_code, 403, forged_scope.text)
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0],
                before,
            )

        for field, mismatched_target in mismatched_targets.items():
            mismatched_relation = self.client.post(
                "/v1/memories/relations",
                json={
                    "from_memory_id": source_id,
                    "to_memory_id": mismatched_target,
                    "relation": "supports",
                    "scope": scope,
                },
                headers=self._admin_headers("tenant-acme"),
            )
            self.assertEqual(
                mismatched_relation.status_code,
                403,
                f"{field}: {mismatched_relation.text}",
            )
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0],
                before,
            )

        cross_tenant = self.client.post(
            "/v1/memories/relations",
            json={
                "from_memory_id": source_id,
                "to_memory_id": foreign_id,
                "relation": "supports",
                "scope": scope,
            },
            headers={
                "X-Provena-Role": "superadmin",
                "X-Provena-Principal-Id": "platform-superadmin",
            },
        )
        self.assertEqual(cross_tenant.status_code, 403, cross_tenant.text)
        self.assertEqual(
            store.conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0],
            before,
        )

        valid = self.client.post(
            "/v1/memories/relations",
            json={
                "from_memory_id": source_id,
                "to_memory_id": target_id,
                "relation": "supports",
                "scope": scope,
            },
            headers=self._admin_headers("tenant-acme"),
        )
        self.assertEqual(valid.status_code, 204, valid.text)
        relation = store.conn.execute(
            """
            SELECT tenant_id, workspace_id, project_id, user_id, agent_id, session_id
            FROM memory_relations
            WHERE from_memory_id = ? AND to_memory_id = ?
            """,
            (source_id, target_id),
        ).fetchone()
        self.assertEqual(dict(relation), scope)

    def test_connected_source_user_grants_gate_search_and_direct_get(self) -> None:
        self._register_connected_fixture(
            connector_id="conn-retrieval-user",
            sources=[
                {
                    "source_id": "src-roadmap-user",
                    "connector_id": "conn-retrieval-user",
                    "tenant_id": "tenant-acme",
                    "remote_source_id": "C110",
                    "source_type": "channel",
                    "display_name": "#roadmap-user",
                },
                {
                    "source_id": "src-private-user",
                    "connector_id": "conn-retrieval-user",
                    "tenant_id": "tenant-acme",
                    "remote_source_id": "C111",
                    "source_type": "channel",
                    "display_name": "#private-user",
                },
            ],
            grants=[
                {
                    "grant_id": "grant-roadmap-user-view",
                    "source_id": "src-roadmap-user",
                    "connector_id": "conn-retrieval-user",
                    "tenant_id": "tenant-acme",
                    "principal_type": "user",
                    "principal_id": "pm-1",
                    "permission_level": "view",
                    "inherited": True,
                }
            ],
        )

        granted_memory = self.client.post(
            "/v1/memories",
            json={
                "kind": "artifact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Granted roadmap memory",
                "content": "Permission smoke roadmap notes should be visible to the granted principal.",
                "acl": [
                    {
                        "principal_id": "pm-1",
                        "principal_type": "user",
                        "permissions": ["read"],
                    }
                ],
                "source_references": [
                    {"source_type": "channel", "source_id": "src-roadmap-user"}
                ],
            },
            headers=self._admin_headers(),
        ).json()["memory"]["memory_id"]
        blocked_memory = self.client.post(
            "/v1/memories",
            json={
                "kind": "artifact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Blocked roadmap memory",
                "content": "Permission smoke roadmap notes should stay hidden without a matching grant.",
                "acl": [
                    {
                        "principal_id": "pm-1",
                        "principal_type": "user",
                        "permissions": ["read"],
                    }
                ],
                "source_references": [
                    {"source_type": "channel", "source_id": "src-private-user"}
                ],
            },
            headers=self._admin_headers(),
        ).json()["memory"]["memory_id"]

        granted_search = self.client.post(
            "/v1/memories/search",
            json={
                "query": "permission smoke roadmap",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "limit": 10,
            },
            headers=self._viewer_headers(principal_id="pm-1"),
        )
        self.assertEqual(granted_search.status_code, 200)
        granted_ids = {item["memory"]["memory_id"] for item in granted_search.json()["results"]}
        self.assertIn(granted_memory, granted_ids)
        self.assertNotIn(blocked_memory, granted_ids)

        denied_search = self.client.post(
            "/v1/memories/search",
            json={
                "query": "permission smoke roadmap",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "limit": 10,
            },
            headers=self._viewer_headers(principal_id="pm-2"),
        )
        self.assertEqual(denied_search.status_code, 200)
        self.assertEqual(len(denied_search.json()["results"]), 0)

        missing_principal_search = self.client.post(
            "/v1/memories/search",
            json={
                "query": "permission smoke roadmap",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "limit": 10,
            },
            headers=self._viewer_headers(principal_id=None),
        )
        self.assertEqual(missing_principal_search.status_code, 200)
        self.assertEqual(len(missing_principal_search.json()["results"]), 0)

        granted_get = self.client.get(
            f"/v1/memories/{granted_memory}",
            headers=self._viewer_headers(principal_id="pm-1"),
        )
        self.assertEqual(granted_get.status_code, 200)
        blocked_get = self.client.get(
            f"/v1/memories/{blocked_memory}",
            headers=self._viewer_headers(principal_id="pm-1"),
        )
        self.assertEqual(blocked_get.status_code, 404)
        blocked_duplicate = self.client.post(
            "/v1/memories",
            json={
                "kind": "artifact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Blocked roadmap memory",
                "content": "Permission smoke roadmap notes should stay hidden without a matching grant.",
                "source_references": [
                    {"source_type": "channel", "source_id": "src-private-user"}
                ],
            },
            headers={
                **self._viewer_headers(principal_id="pm-1"),
                "X-Provena-Role": "editor",
            },
        )
        self.assertEqual(blocked_duplicate.status_code, 404)
        self.assertNotIn(blocked_memory, blocked_duplicate.text)

    def test_connected_source_permission_levels_gate_every_memory_surface(self) -> None:
        connector_id = "conn-operation-permissions"
        source_id = "src-operation-permissions"
        restricted_source_id = "src-operation-restricted"
        principal_id = "connected-editor"
        editor_headers = self._editor_headers(principal_id=principal_id)
        viewer_headers = self._viewer_headers(principal_id=principal_id)
        self._register_connected_fixture(
            connector_id=connector_id,
            sources=[
                {
                    "source_id": source_id,
                    "connector_id": connector_id,
                    "tenant_id": "tenant-acme",
                    "remote_source_id": "C-OPERATION-PERMISSIONS",
                    "source_type": "channel",
                    "display_name": "#operation-permissions",
                },
                {
                    "source_id": restricted_source_id,
                    "connector_id": connector_id,
                    "tenant_id": "tenant-acme",
                    "remote_source_id": "C-OPERATION-RESTRICTED",
                    "source_type": "channel",
                    "display_name": "#operation-restricted",
                }
            ],
        )

        def persistence_state() -> dict[str, list[tuple[object, ...]]]:
            tables = (
                "memories",
                "memory_sources",
                "memories_fts",
                "memory_history",
                "memory_relations",
                "memory_feedback",
                "trigger_index",
                "audit_log",
            )
            return {
                table: [
                    tuple(row)
                    for row in self.client.app.state.store.conn.execute(
                        f"SELECT * FROM {table} ORDER BY 1"
                    ).fetchall()
                ]
                for table in tables
            }

        def assert_concealed(response, *identifiers: str) -> None:
            for identifier in identifiers:
                self.assertNotIn(identifier, response.text)

        connected_payload = {
            "kind": "artifact",
            "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
            "title": "Connected operation permission proof",
            "content": "Every public memory surface must preserve source permissions.",
            "acl": [
                {
                    "principal_id": principal_id,
                    "principal_type": "user",
                    "permissions": ["read", "write", "delete"],
                }
            ],
            "source_references": [
                {"source_type": "channel", "source_id": source_id}
            ],
        }
        first_write_state = persistence_state()
        for denied_headers in (viewer_headers, editor_headers):
            denied_create = self.client.post(
                "/v1/memories",
                json={
                    **connected_payload,
                    "title": f"Forbidden connected create for {denied_headers['X-Provena-Role']}",
                },
                headers=denied_headers,
            )
            self.assertEqual(denied_create.status_code, 403, denied_create.text)
            assert_concealed(denied_create, source_id, restricted_source_id)
            self.assertEqual(persistence_state(), first_write_state)
        connected_create = self.client.post(
            "/v1/memories",
            json=connected_payload,
            headers=self._admin_headers(),
        )
        self.assertEqual(connected_create.status_code, 200, connected_create.text)
        connected_memory = connected_create.json()["memory"]["memory_id"]
        supersession_target_create = self.client.post(
            "/v1/memories",
            json={
                **connected_payload,
                "title": "Connected supersession permission target",
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(
            supersession_target_create.status_code,
            200,
            supersession_target_create.text,
        )
        supersession_target = supersession_target_create.json()["memory"]["memory_id"]
        declassification_target_create = self.client.post(
            "/v1/memories",
            json={
                **connected_payload,
                "title": "Explicit connected declassification target",
                "acl": [
                    {
                        "principal_id": principal_id,
                        "principal_type": "user",
                        "permissions": ["read", "write", "share"],
                    }
                ],
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(
            declassification_target_create.status_code,
            200,
            declassification_target_create.text,
        )
        declassification_target = declassification_target_create.json()["memory"]["memory_id"]
        partial_declassification_target_create = self.client.post(
            "/v1/memories",
            json={
                **connected_payload,
                "title": "Partial connected declassification target",
                "acl": [
                    {
                        "principal_id": principal_id,
                        "principal_type": "user",
                        "permissions": ["read", "write", "share"],
                    }
                ],
                "source_references": [
                    {"source_type": "channel", "source_id": source_id},
                    {"source_type": "channel", "source_id": restricted_source_id},
                ],
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(
            partial_declassification_target_create.status_code,
            200,
            partial_declassification_target_create.text,
        )
        partial_declassification_target = partial_declassification_target_create.json()[
            "memory"
        ]["memory_id"]
        unconnected_create = self.client.post(
            "/v1/memories",
            json={
                "kind": "artifact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Unconnected local source compatibility",
                "content": "Local source references stay usable without a connector grant.",
                "source_references": [
                    {"source_type": "file", "source_id": "local-unconnected-source"}
                ],
            },
        )
        self.assertEqual(unconnected_create.status_code, 200, unconnected_create.text)
        relation_target = self.client.post(
            "/v1/memories",
            json={
                "kind": "fact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Relation target",
                "content": "A tenant-local relation target.",
            },
        ).json()["memory"]["memory_id"]
        relation_payload = {
            "from_memory_id": connected_memory,
            "to_memory_id": relation_target,
            "relation": "supports",
            "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
        }

        def set_permission(level: str, *, granted_source_id: str = source_id) -> None:
            response = self.client.post(
                f"/v1/integrations/connectors/{connector_id}/permissions/batch",
                params={"tenant_id": "tenant-acme"},
                json={
                    "grants": [
                        {
                            "grant_id": f"grant-operation-{granted_source_id}",
                            "source_id": granted_source_id,
                            "connector_id": connector_id,
                            "tenant_id": "tenant-acme",
                            "principal_type": "user",
                            "principal_id": principal_id,
                            "permission_level": level,
                            "inherited": False,
                        }
                    ]
                },
                headers=self._admin_headers(),
            )
            self.assertEqual(response.status_code, 200, response.text)

        def supersede_target(title: str, headers: dict[str, str]):
            return self.client.post(
                "/v1/memories",
                json={
                    "kind": "fact",
                    "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                    "title": title,
                    "content": "Causal writes must preserve connected-source authorization.",
                    "supersedes_memory_id": supersession_target,
                },
                headers=headers,
            )

        no_grant_state = persistence_state()
        no_grant_get = self.client.get(
            f"/v1/memories/{connected_memory}", headers=editor_headers
        )
        self.assertEqual(no_grant_get.status_code, 404, no_grant_get.text)
        no_grant_update = self.client.put(
            f"/v1/memories/{connected_memory}",
            json={"title": "Blocked without a source grant"},
            headers=editor_headers,
        )
        self.assertEqual(no_grant_update.status_code, 404, no_grant_update.text)
        no_grant_feedback_list = self.client.get(
            f"/v1/memories/{connected_memory}/feedback", headers=editor_headers
        )
        self.assertEqual(
            no_grant_feedback_list.status_code,
            404,
            no_grant_feedback_list.text,
        )
        no_grant_feedback = self.client.post(
            f"/v1/memories/{connected_memory}/feedback",
            json={"feedback_type": "positive"},
            headers=editor_headers,
        )
        self.assertEqual(no_grant_feedback.status_code, 404, no_grant_feedback.text)
        no_grant_relation = self.client.post(
            "/v1/memories/relations",
            json=relation_payload,
            headers=editor_headers,
        )
        self.assertEqual(no_grant_relation.status_code, 403, no_grant_relation.text)
        no_grant_delete = self.client.delete(
            f"/v1/memories/{connected_memory}", headers=editor_headers
        )
        self.assertEqual(no_grant_delete.status_code, 404, no_grant_delete.text)
        no_grant_supersession = supersede_target(
            "Blocked supersession without grant",
            editor_headers,
        )
        self.assertEqual(
            no_grant_supersession.status_code,
            403,
            no_grant_supersession.text,
        )
        for denied_response in (
            no_grant_get,
            no_grant_update,
            no_grant_feedback_list,
            no_grant_feedback,
            no_grant_relation,
            no_grant_delete,
            no_grant_supersession,
        ):
            assert_concealed(
                denied_response,
                connected_memory,
                supersession_target,
                source_id,
            )
        self.assertEqual(persistence_state(), no_grant_state)

        set_permission("view")
        self.assertEqual(
            self.client.get(
                f"/v1/memories/{connected_memory}", headers=editor_headers
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"/v1/memories/{connected_memory}/feedback", headers=editor_headers
            ).status_code,
            200,
        )
        view_state = persistence_state()
        view_feedback = self.client.post(
            f"/v1/memories/{connected_memory}/feedback",
            json={"feedback_type": "positive"},
            headers=editor_headers,
        )
        self.assertEqual(view_feedback.status_code, 404, view_feedback.text)
        view_update = self.client.put(
            f"/v1/memories/{connected_memory}",
            json={"title": "Blocked with view permission"},
            headers=editor_headers,
        )
        self.assertEqual(view_update.status_code, 404, view_update.text)
        view_relation = self.client.post(
            "/v1/memories/relations",
            json=relation_payload,
            headers=editor_headers,
        )
        self.assertEqual(view_relation.status_code, 403, view_relation.text)
        view_delete = self.client.delete(
            f"/v1/memories/{connected_memory}", headers=editor_headers
        )
        self.assertEqual(view_delete.status_code, 404, view_delete.text)
        view_supersession = supersede_target(
            "Blocked supersession with view",
            editor_headers,
        )
        self.assertEqual(view_supersession.status_code, 403, view_supersession.text)
        for denied_response in (
            view_feedback,
            view_update,
            view_relation,
            view_delete,
            view_supersession,
        ):
            assert_concealed(denied_response, connected_memory, supersession_target, source_id)
        self.assertEqual(persistence_state(), view_state)

        set_permission("comment")
        comment = self.client.post(
            f"/v1/memories/{connected_memory}/feedback",
            json={
                "feedback_type": "positive",
                "reason": "source-authorized comment",
                "principal_id": "spoofed-victim-principal",
            },
            headers=editor_headers,
        )
        self.assertEqual(comment.status_code, 200, comment.text)
        self.assertEqual(comment.json()["principal_id"], principal_id)
        comment_state = persistence_state()
        viewer_feedback = self.client.post(
            f"/v1/memories/{connected_memory}/feedback",
            json={"feedback_type": "negative", "reason": "viewer role is read-only"},
            headers=viewer_headers,
        )
        self.assertEqual(viewer_feedback.status_code, 404, viewer_feedback.text)
        comment_update = self.client.put(
            f"/v1/memories/{connected_memory}",
            json={"title": "Blocked with comment permission"},
            headers=editor_headers,
        )
        self.assertEqual(comment_update.status_code, 404, comment_update.text)
        comment_relation = self.client.post(
            "/v1/memories/relations",
            json=relation_payload,
            headers=editor_headers,
        )
        self.assertEqual(comment_relation.status_code, 403, comment_relation.text)
        comment_delete = self.client.delete(
            f"/v1/memories/{connected_memory}", headers=editor_headers
        )
        self.assertEqual(comment_delete.status_code, 404, comment_delete.text)
        comment_supersession = supersede_target(
            "Blocked supersession with comment",
            editor_headers,
        )
        self.assertEqual(
            comment_supersession.status_code,
            403,
            comment_supersession.text,
        )
        for denied_response in (
            viewer_feedback,
            comment_update,
            comment_relation,
            comment_delete,
            comment_supersession,
        ):
            assert_concealed(denied_response, connected_memory, supersession_target, source_id)
        self.assertEqual(persistence_state(), comment_state)

        set_permission("edit")
        edit_denied_state = persistence_state()
        viewer_create = self.client.post(
            "/v1/memories",
            json={
                **connected_payload,
                "title": "Viewer role cannot create connected memory",
            },
            headers=viewer_headers,
        )
        self.assertEqual(viewer_create.status_code, 403, viewer_create.text)
        viewer_update = self.client.put(
            f"/v1/memories/{connected_memory}",
            json={"title": "Viewer role must not write"},
            headers=viewer_headers,
        )
        self.assertEqual(viewer_update.status_code, 404, viewer_update.text)
        viewer_edit_feedback = self.client.post(
            f"/v1/memories/{connected_memory}/feedback",
            json={"feedback_type": "negative", "reason": "viewer remains read-only"},
            headers=viewer_headers,
        )
        self.assertEqual(
            viewer_edit_feedback.status_code,
            404,
            viewer_edit_feedback.text,
        )
        viewer_relation = self.client.post(
            "/v1/memories/relations",
            json=relation_payload,
            headers=viewer_headers,
        )
        self.assertEqual(viewer_relation.status_code, 403, viewer_relation.text)
        viewer_supersession = supersede_target(
            "Viewer role cannot supersede",
            viewer_headers,
        )
        self.assertEqual(
            viewer_supersession.status_code,
            403,
            viewer_supersession.text,
        )
        viewer_edit_delete = self.client.delete(
            f"/v1/memories/{connected_memory}", headers=viewer_headers
        )
        self.assertEqual(viewer_edit_delete.status_code, 404, viewer_edit_delete.text)
        edit_declassification = self.client.put(
            f"/v1/memories/{connected_memory}",
            json={"source_references": []},
            headers=editor_headers,
        )
        self.assertEqual(
            edit_declassification.status_code,
            404,
            edit_declassification.text,
        )
        edit_acl_widening = self.client.put(
            f"/v1/memories/{connected_memory}",
            json={"acl": []},
            headers=editor_headers,
        )
        self.assertEqual(edit_acl_widening.status_code, 404, edit_acl_widening.text)
        for denied_response in (
            viewer_create,
            viewer_update,
            viewer_edit_feedback,
            viewer_relation,
            viewer_supersession,
            viewer_edit_delete,
            edit_declassification,
            edit_acl_widening,
        ):
            assert_concealed(
                denied_response,
                connected_memory,
                supersession_target,
                source_id,
            )
        self.assertEqual(persistence_state(), edit_denied_state)

        authorized_create = self.client.post(
            "/v1/memories",
            json={
                **connected_payload,
                "title": "Source-authorized connected create",
            },
            headers=editor_headers,
        )
        self.assertEqual(authorized_create.status_code, 200, authorized_create.text)
        update = self.client.put(
            f"/v1/memories/{connected_memory}",
            json={"title": "Source-authorized update"},
            headers=editor_headers,
        )
        self.assertEqual(update.status_code, 200, update.text)
        relation = self.client.post(
            "/v1/memories/relations",
            json=relation_payload,
            headers=editor_headers,
        )
        self.assertEqual(relation.status_code, 204, relation.text)
        superseded = supersede_target("Source-authorized supersession", editor_headers)
        self.assertEqual(superseded.status_code, 200, superseded.text)
        edit_delete_state = persistence_state()
        cache_versions_before_delete = dict(
            self.client.app.state.store.hot_cache._versions
        )
        edit_delete = self.client.delete(
            f"/v1/memories/{connected_memory}", headers=editor_headers
        )
        self.assertEqual(edit_delete.status_code, 404, edit_delete.text)
        assert_concealed(edit_delete, connected_memory, source_id)
        self.assertEqual(persistence_state(), edit_delete_state)
        self.assertEqual(
            self.client.app.state.store.hot_cache._versions,
            cache_versions_before_delete,
        )

        transition_state = persistence_state()
        denied_transition = self.client.put(
            f"/v1/memories/{declassification_target}",
            json={
                "source_references": [
                    {"source_type": "channel", "source_id": restricted_source_id}
                ]
            },
            headers=editor_headers,
        )
        self.assertEqual(denied_transition.status_code, 404, denied_transition.text)
        assert_concealed(
            denied_transition,
            declassification_target,
            source_id,
            restricted_source_id,
        )
        self.assertEqual(persistence_state(), transition_state)

        set_permission("edit", granted_source_id=restricted_source_id)
        edit_transition_state = persistence_state()
        edit_transition = self.client.put(
            f"/v1/memories/{declassification_target}",
            json={
                "source_references": [
                    {"source_type": "channel", "source_id": restricted_source_id}
                ]
            },
            headers=editor_headers,
        )
        self.assertEqual(edit_transition.status_code, 404, edit_transition.text)
        assert_concealed(
            edit_transition,
            declassification_target,
            source_id,
            restricted_source_id,
        )
        self.assertEqual(persistence_state(), edit_transition_state)

        set_permission("owner")
        allowed_transition = self.client.put(
            f"/v1/memories/{declassification_target}",
            json={
                "source_references": [
                    {"source_type": "channel", "source_id": restricted_source_id}
                ]
            },
            headers=editor_headers,
        )
        self.assertEqual(allowed_transition.status_code, 200, allowed_transition.text)
        self.assertEqual(
            allowed_transition.json()["memory"]["source_references"][0]["source_id"],
            restricted_source_id,
        )

        set_permission("edit")
        partial_removal_state = persistence_state()
        partial_removal_without_removed_source_owner = self.client.put(
            f"/v1/memories/{partial_declassification_target}",
            json={
                "source_references": [
                    {"source_type": "channel", "source_id": source_id}
                ]
            },
            headers=editor_headers,
        )
        self.assertEqual(
            partial_removal_without_removed_source_owner.status_code,
            404,
            partial_removal_without_removed_source_owner.text,
        )
        assert_concealed(
            partial_removal_without_removed_source_owner,
            partial_declassification_target,
            source_id,
            restricted_source_id,
        )
        self.assertEqual(persistence_state(), partial_removal_state)

        set_permission("owner", granted_source_id=restricted_source_id)
        partial_removal = self.client.put(
            f"/v1/memories/{partial_declassification_target}",
            json={
                "source_references": [
                    {"source_type": "channel", "source_id": source_id}
                ]
            },
            headers=editor_headers,
        )
        self.assertEqual(partial_removal.status_code, 200, partial_removal.text)
        self.assertEqual(
            [
                reference["source_id"]
                for reference in partial_removal.json()["memory"]["source_references"]
            ],
            [source_id],
        )

        owner_transition = self.client.put(
            f"/v1/memories/{declassification_target}",
            json={
                "source_references": [
                    {"source_type": "channel", "source_id": source_id}
                ]
            },
            headers=editor_headers,
        )
        self.assertEqual(owner_transition.status_code, 200, owner_transition.text)
        self.assertEqual(
            owner_transition.json()["memory"]["source_references"][0]["source_id"],
            source_id,
        )
        set_permission("owner")
        owner_without_share_state = persistence_state()
        owner_without_share_declassification = self.client.put(
            f"/v1/memories/{connected_memory}",
            json={"source_references": []},
            headers=editor_headers,
        )
        self.assertEqual(
            owner_without_share_declassification.status_code,
            404,
            owner_without_share_declassification.text,
        )
        owner_without_share_acl_widening = self.client.put(
            f"/v1/memories/{connected_memory}",
            json={"acl": []},
            headers=editor_headers,
        )
        self.assertEqual(
            owner_without_share_acl_widening.status_code,
            404,
            owner_without_share_acl_widening.text,
        )
        for denied_response in (
            owner_without_share_declassification,
            owner_without_share_acl_widening,
        ):
            assert_concealed(denied_response, connected_memory, source_id)
        self.assertEqual(persistence_state(), owner_without_share_state)

        explicit_declassification = self.client.put(
            f"/v1/memories/{declassification_target}",
            json={"source_references": []},
            headers=editor_headers,
        )
        self.assertEqual(
            explicit_declassification.status_code,
            200,
            explicit_declassification.text,
        )
        self.assertEqual(
            explicit_declassification.json()["memory"]["source_references"],
            [],
        )
        owner_viewer_state = persistence_state()
        viewer_owner_delete = self.client.delete(
            f"/v1/memories/{connected_memory}", headers=viewer_headers
        )
        self.assertEqual(viewer_owner_delete.status_code, 404, viewer_owner_delete.text)
        assert_concealed(viewer_owner_delete, connected_memory, source_id)
        self.assertEqual(persistence_state(), owner_viewer_state)
        deleted = self.client.delete(
            f"/v1/memories/{connected_memory}", headers=editor_headers
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)

    def test_source_grant_revocation_invalidates_cached_authorization_results(self) -> None:
        connector_id = "conn-cache-revocation"
        source_id = "src-cache-revocation"
        grant_id = "grant-cache-revocation"
        self._register_connected_fixture(
            connector_id=connector_id,
            sources=[
                {
                    "source_id": source_id,
                    "connector_id": connector_id,
                    "tenant_id": "tenant-acme",
                    "remote_source_id": "C-CACHE",
                    "source_type": "channel",
                    "display_name": "#cache-revocation",
                }
            ],
            grants=[
                {
                    "grant_id": grant_id,
                    "source_id": source_id,
                    "connector_id": connector_id,
                    "tenant_id": "tenant-acme",
                    "principal_type": "user",
                    "principal_id": "pm-1",
                    "permission_level": "view",
                    "inherited": True,
                }
            ],
        )
        memory_id = self.client.post(
            "/v1/memories",
            json={
                "kind": "artifact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Cached connected grant",
                "content": "Revoked source permissions must invalidate cached search results.",
                "acl": [
                    {
                        "principal_id": "pm-1",
                        "principal_type": "user",
                        "permissions": ["read"],
                    }
                ],
                "source_references": [
                    {"source_type": "channel", "source_id": source_id}
                ],
            },
            headers=self._admin_headers(),
        ).json()["memory"]["memory_id"]
        search_payload = {
            "query": "revoked source permissions cached",
            "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
            "limit": 10,
        }
        before = self.client.post(
            "/v1/memories/search",
            json=search_payload,
            headers=self._viewer_headers(principal_id="pm-1"),
        )
        self.assertIn(
            memory_id,
            {item["memory"]["memory_id"] for item in before.json()["results"]},
        )

        reassigned = self.client.post(
            f"/v1/integrations/connectors/{connector_id}/permissions/batch?tenant_id=tenant-acme",
            json={
                "grants": [
                    {
                        "grant_id": grant_id,
                        "source_id": source_id,
                        "connector_id": connector_id,
                        "tenant_id": "tenant-acme",
                        "principal_type": "user",
                        "principal_id": "pm-2",
                        "permission_level": "view",
                        "inherited": True,
                    }
                ]
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(reassigned.status_code, 200, reassigned.text)
        after = self.client.post(
            "/v1/memories/search",
            json=search_payload,
            headers=self._viewer_headers(principal_id="pm-1"),
        )
        self.assertNotIn(
            memory_id,
            {item["memory"]["memory_id"] for item in after.json()["results"]},
        )

    def test_connected_source_group_grants_filter_mixed_and_related_results(self) -> None:
        self._register_connected_fixture(
            connector_id="conn-retrieval-group",
            sources=[
                {
                    "source_id": "src-leadership-group",
                    "connector_id": "conn-retrieval-group",
                    "tenant_id": "tenant-acme",
                    "remote_source_id": "C210",
                    "source_type": "channel",
                    "display_name": "#leadership-group",
                },
                {
                    "source_id": "src-mixed-denied",
                    "connector_id": "conn-retrieval-group",
                    "tenant_id": "tenant-acme",
                    "remote_source_id": "C211",
                    "source_type": "channel",
                    "display_name": "#mixed-denied",
                },
                {
                    "source_id": "src-related-denied",
                    "connector_id": "conn-retrieval-group",
                    "tenant_id": "tenant-acme",
                    "remote_source_id": "C212",
                    "source_type": "channel",
                    "display_name": "#related-denied",
                },
            ],
            grants=[
                {
                    "grant_id": "grant-leadership-edit",
                    "source_id": "src-leadership-group",
                    "connector_id": "conn-retrieval-group",
                    "tenant_id": "tenant-acme",
                    "principal_type": "group",
                    "principal_id": "leadership",
                    "permission_level": "edit",
                    "inherited": False,
                }
            ],
        )

        primary_memory = self.client.post(
            "/v1/memories",
            json={
                "kind": "artifact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Leadership planning brief",
                "content": "Leadership release planning brief should be visible to the leadership group.",
                "acl": [
                    {
                        "principal_id": "leadership",
                        "principal_type": "group",
                        "permissions": ["read"],
                    }
                ],
                "source_references": [
                    {"source_type": "channel", "source_id": "src-leadership-group"}
                ],
            },
            headers=self._admin_headers(),
        ).json()["memory"]["memory_id"]
        mixed_memory = self.client.post(
            "/v1/memories",
            json={
                "kind": "artifact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Mixed leadership brief",
                "content": "Leadership release planning brief should not leak partial source access.",
                "acl": [
                    {
                        "principal_id": "leadership",
                        "principal_type": "group",
                        "permissions": ["read"],
                    }
                ],
                "source_references": [
                    {"source_type": "channel", "source_id": "src-leadership-group"},
                    {"source_type": "channel", "source_id": "src-mixed-denied"},
                ],
            },
            headers=self._admin_headers(),
        ).json()["memory"]["memory_id"]
        hidden_related = self.client.post(
            "/v1/memories",
            json={
                "kind": "artifact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Hidden related brief",
                "content": "Leadership release planning brief follow-up should stay hidden.",
                "acl": [
                    {
                        "principal_id": "leadership",
                        "principal_type": "group",
                        "permissions": ["read"],
                    }
                ],
                "source_references": [
                    {"source_type": "channel", "source_id": "src-related-denied"}
                ],
            },
            headers=self._admin_headers(),
        ).json()["memory"]["memory_id"]

        relation_response = self.client.post(
            "/v1/memories/relations",
            json={
                "from_memory_id": primary_memory,
                "to_memory_id": hidden_related,
                "relation": "supports",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(relation_response.status_code, 204)

        leadership_search = self.client.post(
            "/v1/memories/search",
            json={
                "query": "leadership release planning brief",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "limit": 10,
            },
            headers=self._viewer_headers(principal_id="lead-1", groups=["leadership"]),
        )
        self.assertEqual(leadership_search.status_code, 200)
        leadership_results = leadership_search.json()["results"]
        leadership_ids = {item["memory"]["memory_id"] for item in leadership_results}
        self.assertIn(primary_memory, leadership_ids)
        self.assertNotIn(mixed_memory, leadership_ids)
        primary_result = next(
            item for item in leadership_results if item["memory"]["memory_id"] == primary_memory
        )
        self.assertEqual(primary_result["related_memories"], [])

        unrelated_group_search = self.client.post(
            "/v1/memories/search",
            json={
                "query": "leadership release planning brief",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "limit": 10,
            },
            headers=self._viewer_headers(principal_id="lead-1", groups=["product"]),
        )
        self.assertEqual(unrelated_group_search.status_code, 200)
        self.assertEqual(len(unrelated_group_search.json()["results"]), 0)

    def test_connected_source_grants_do_not_override_acl_or_hide_legacy_memories(self) -> None:
        self._register_connected_fixture(
            connector_id="conn-retrieval-acl",
            sources=[
                {
                    "source_id": "src-acl-gated",
                    "connector_id": "conn-retrieval-acl",
                    "tenant_id": "tenant-acme",
                    "remote_source_id": "C310",
                    "source_type": "channel",
                    "display_name": "#acl-gated",
                }
            ],
            grants=[
                {
                    "grant_id": "grant-acl-pm-1",
                    "source_id": "src-acl-gated",
                    "connector_id": "conn-retrieval-acl",
                    "tenant_id": "tenant-acme",
                    "principal_type": "user",
                    "principal_id": "pm-1",
                    "permission_level": "view",
                    "inherited": True,
                }
            ],
        )

        acl_blocked_memory = self.client.post(
            "/v1/memories",
            json={
                "kind": "decision",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "ACL continuity proof",
                "content": "Connected ACL continuity proof should stay hidden from pm-1.",
                "acl": [
                    {
                        "principal_id": "pm-2",
                        "principal_type": "user",
                        "permissions": ["read"],
                    }
                ],
                "source_references": [
                    {"source_type": "channel", "source_id": "src-acl-gated"}
                ],
            },
            headers=self._admin_headers(),
        ).json()["memory"]["memory_id"]
        legacy_memory = self.client.post(
            "/v1/memories",
            json={
                "kind": "decision",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Legacy continuity proof",
                "content": "Legacy ACL continuity proof should remain visible without connected grants.",
            },
        ).json()["memory"]["memory_id"]

        continuity_search = self.client.post(
            "/v1/memories/search",
            json={
                "query": "continuity proof",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "limit": 10,
            },
            headers=self._viewer_headers(principal_id="pm-1"),
        )
        self.assertEqual(continuity_search.status_code, 200)
        continuity_ids = {item["memory"]["memory_id"] for item in continuity_search.json()["results"]}
        self.assertIn(legacy_memory, continuity_ids)
        self.assertNotIn(acl_blocked_memory, continuity_ids)

        blocked_get = self.client.get(
            f"/v1/memories/{acl_blocked_memory}",
            headers=self._viewer_headers(principal_id="pm-1"),
        )
        self.assertEqual(blocked_get.status_code, 404)

    def test_deterministic_create_retry_honors_connected_source_grants(self) -> None:
        self._register_connected_fixture(
            connector_id="conn-deterministic-retry",
            sources=[
                {
                    "source_id": "src-deterministic-retry",
                    "connector_id": "conn-deterministic-retry",
                    "tenant_id": "tenant-acme",
                    "remote_source_id": "C320",
                    "source_type": "channel",
                    "display_name": "#deterministic-retry",
                }
            ],
            grants=[
                {
                    "grant_id": "grant-deterministic-owner",
                    "source_id": "src-deterministic-retry",
                    "connector_id": "conn-deterministic-retry",
                    "tenant_id": "tenant-acme",
                    "principal_type": "user",
                    "principal_id": "pm-2",
                    "permission_level": "view",
                    "inherited": False,
                }
            ],
        )
        payload = {
            "memory_id": "neverzero-context-source-gated",
            "kind": "artifact",
            "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
            "title": "Deterministic retry source grant",
            "content": "A retry must not reveal a connected source memory.",
            "source_references": [
                {"source_type": "channel", "source_id": "src-deterministic-retry"}
            ],
        }
        created = self.client.post("/v1/memories", json=payload, headers=self._admin_headers())
        self.assertEqual(created.status_code, 200)

        blocked_retry = self.client.post(
            "/v1/memories",
            json=payload,
            headers={
                "X-Provena-Tenant-Id": "tenant-acme",
                "X-Provena-Role": "editor",
                "X-Provena-Principal-Id": "pm-1",
            },
        )
        self.assertEqual(blocked_retry.status_code, 404)
        self.assertEqual(
            blocked_retry.json()["detail"],
            "memory not found",
        )

    def test_connected_source_mapping_and_grant_joins_stay_scoped(self) -> None:
        self._register_connected_fixture(
            connector_id="conn-retrieval-mapping",
            sources=[
                {
                    "source_id": "src-mapped-user",
                    "connector_id": "conn-retrieval-mapping",
                    "tenant_id": "tenant-acme",
                    "remote_source_id": "C410",
                    "source_type": "channel",
                    "display_name": "#mapped-user",
                }
            ],
            mappings=[
                {
                    "mapping_id": "map-remote-user",
                    "connector_id": "conn-retrieval-mapping",
                    "tenant_id": "tenant-acme",
                    "principal_type": "user",
                    "local_principal_id": "pm-remote",
                    "remote_principal_id": "U100",
                    "remote_name": "Remote PM",
                    "groups": ["remote-product"],
                    "last_synced_at": "2026-04-10T10:05:00Z",
                }
            ],
            grants=[
                {
                    "grant_id": "grant-remote-user-view",
                    "source_id": "src-mapped-user",
                    "connector_id": "conn-retrieval-mapping",
                    "tenant_id": "tenant-acme",
                    "principal_type": "user",
                    "principal_id": "U100",
                    "permission_level": "view",
                    "inherited": True,
                }
            ],
        )

        mapped_memory = self.client.post(
            "/v1/memories",
            json={
                "kind": "artifact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Mapped remote memory",
                "content": "Mapped remote grant proof should resolve through principal mappings.",
                "acl": [
                    {
                        "principal_id": "pm-remote",
                        "principal_type": "user",
                        "permissions": ["read"],
                    }
                ],
                "source_references": [
                    {"source_type": "channel", "source_id": "src-mapped-user"}
                ],
            },
            headers=self._admin_headers(),
        ).json()["memory"]["memory_id"]

        self._register_connected_fixture(
            connector_id="conn-wrong-connector",
            sources=[
                {
                    "source_id": "src-wrong-anchor",
                    "connector_id": "conn-wrong-connector",
                    "tenant_id": "tenant-acme",
                    "remote_source_id": "C411",
                    "source_type": "channel",
                    "display_name": "#wrong-anchor",
                }
            ],
        )
        wrong_connector_grant = self.client.post(
            "/v1/integrations/connectors/conn-wrong-connector/permissions/batch?tenant_id=tenant-acme",
            json={
                "grants": [
                    {
                        "grant_id": "grant-wrong-connector",
                        "source_id": "src-mapped-user",
                        "connector_id": "conn-wrong-connector",
                        "tenant_id": "tenant-acme",
                        "principal_type": "user",
                        "principal_id": "pm-wrong",
                        "permission_level": "view",
                        "inherited": True,
                    }
                ]
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(wrong_connector_grant.status_code, 404)
        self.assertEqual(wrong_connector_grant.json(), {"detail": "source not found"})

        self._register_connected_fixture(
            connector_id="conn-foreign-tenant",
            tenant_id="tenant-other",
            sources=[
                {
                    "source_id": "src-foreign-anchor",
                    "connector_id": "conn-foreign-tenant",
                    "tenant_id": "tenant-other",
                    "remote_source_id": "C510",
                    "source_type": "channel",
                    "display_name": "#foreign-anchor",
                }
            ],
        )
        foreign_grant = self.client.post(
            "/v1/integrations/connectors/conn-foreign-tenant/permissions/batch?tenant_id=tenant-other",
            json={
                "grants": [
                    {
                        "grant_id": "grant-foreign-tenant",
                        "source_id": "src-mapped-user",
                        "connector_id": "conn-foreign-tenant",
                        "tenant_id": "tenant-other",
                        "principal_type": "user",
                        "principal_id": "pm-foreign",
                        "permission_level": "view",
                        "inherited": True,
                    }
                ]
            },
            headers=self._admin_headers("tenant-other"),
        )
        self.assertEqual(foreign_grant.status_code, 404)
        self.assertEqual(foreign_grant.json(), {"detail": "source not found"})
        self.assertEqual(
            self.client.app.state.store.conn.execute(
                """
                SELECT COUNT(*) FROM source_permission_grants
                WHERE grant_id IN (?, ?)
                """,
                ("grant-wrong-connector", "grant-foreign-tenant"),
            ).fetchone()[0],
            0,
        )

        mapped_search = self.client.post(
            "/v1/memories/search",
            json={
                "query": "mapped remote grant proof",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "limit": 10,
            },
            headers=self._viewer_headers(principal_id="pm-remote"),
        )
        self.assertEqual(mapped_search.status_code, 200)
        mapped_ids = {item["memory"]["memory_id"] for item in mapped_search.json()["results"]}
        self.assertIn(mapped_memory, mapped_ids)

        wrong_connector_search = self.client.post(
            "/v1/memories/search",
            json={
                "query": "mapped remote grant proof",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "limit": 10,
            },
            headers=self._viewer_headers(principal_id="pm-wrong"),
        )
        self.assertEqual(wrong_connector_search.status_code, 200)
        self.assertEqual(len(wrong_connector_search.json()["results"]), 0)

        foreign_search = self.client.post(
            "/v1/memories/search",
            json={
                "query": "mapped remote grant proof",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "limit": 10,
            },
            headers=self._viewer_headers(principal_id="pm-foreign"),
        )
        self.assertEqual(foreign_search.status_code, 200)
        self.assertEqual(len(foreign_search.json()["results"]), 0)

    def test_erase_memories_by_user_scope(self) -> None:
        self.client.post(
            "/v1/memories",
            json={
                "kind": "preference",
                "scope": {
                    "tenant_id": "tenant-acme",
                    "workspace_id": "ws-growth",
                    "user_id": "pm-1",
                },
                "title": "Voice-first capture",
                "content": "The PM prefers recording spoken updates before structured summarization.",
            },
        )
        self.client.post(
            "/v1/memories",
            json={
                "kind": "preference",
                "scope": {
                    "tenant_id": "tenant-acme",
                    "workspace_id": "ws-growth",
                    "user_id": "pm-2",
                },
                "title": "Text-first capture",
                "content": "The PM prefers text documents.",
            },
        )

        erase_response = self.client.post(
            "/v1/admin/erase",
            json={
                "tenant_id": "tenant-acme",
                "workspace_id": "ws-growth",
                "user_id": "pm-1",
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(erase_response.status_code, 200)
        self.assertEqual(erase_response.json()["deleted_memories"], 1)

        search_response = self.client.post(
            "/v1/memories/search",
            json={
                "query": "prefers",
                "scope": {
                    "tenant_id": "tenant-acme",
                    "workspace_id": "ws-growth",
                    "user_id": "pm-1",
                },
            },
        )
        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(len(search_response.json()["results"]), 0)

    def test_deduplication_is_scope_aware(self) -> None:
        payload = {
            "kind": "fact",
            "title": "Shared content",
            "content": "Same content should persist independently across scopes.",
        }
        first = self.client.post(
            "/v1/memories",
            json={
                **payload,
                "scope": {
                    "tenant_id": "tenant-acme",
                    "workspace_id": "ws-growth",
                    "user_id": "pm-1",
                },
            },
        )
        second = self.client.post(
            "/v1/memories",
            json={
                **payload,
                "scope": {
                    "tenant_id": "tenant-acme",
                    "workspace_id": "ws-platform",
                    "user_id": "pm-2",
                },
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(first.json()["created"])
        self.assertTrue(second.json()["created"])
        self.assertNotEqual(
            first.json()["memory"]["memory_id"],
            second.json()["memory"]["memory_id"],
        )

    def test_scope_legal_hold_applies_to_future_writes_and_restores_status(self) -> None:
        hold_response = self.client.post(
            "/v1/admin/legal-hold",
            json={
                "hold_id": "hold-growth",
                "tenant_id": "tenant-acme",
                "scope": {
                    "workspace_id": "ws-growth",
                    "user_id": "pm-1",
                },
                "reason": "litigation freeze",
            },
            headers=self._admin_headers(),
        )
        self.assertEqual(hold_response.status_code, 200)

        created = self.client.post(
            "/v1/memories",
            json={
                "kind": "decision",
                "scope": {
                    "tenant_id": "tenant-acme",
                    "workspace_id": "ws-growth",
                    "user_id": "pm-1",
                },
                "title": "Held decision",
                "content": "This decision should be held immediately.",
            },
        ).json()["memory"]

        held = self.client.get(f"/v1/memories/{created['memory_id']}")
        self.assertEqual(held.status_code, 200)
        self.assertTrue(held.json()["held"])
        self.assertEqual(held.json()["status"], "held")

        release = self.client.delete(
            "/v1/admin/legal-hold/hold-growth?tenant_id=tenant-acme",
            headers=self._admin_headers(),
        )
        self.assertEqual(release.status_code, 200)

        released = self.client.get(f"/v1/memories/{created['memory_id']}")
        self.assertEqual(released.status_code, 200)
        self.assertFalse(released.json()["held"])
        self.assertEqual(released.json()["status"], "active")

    def test_tenant_admin_cannot_release_another_tenants_legal_hold(self) -> None:
        placed = self.client.post(
            "/v1/admin/legal-hold",
            json={
                "hold_id": "hold-tenant-bound",
                "tenant_id": "tenant-acme",
                "reason": "tenant boundary proof",
            },
            headers=self._admin_headers("tenant-acme"),
        )
        self.assertEqual(placed.status_code, 200)

        missing_tenant = self.client.delete(
            "/v1/admin/legal-hold/hold-tenant-bound",
            headers=self._admin_headers("tenant-other"),
        )
        self.assertEqual(missing_tenant.status_code, 403)

        cross_tenant = self.client.delete(
            "/v1/admin/legal-hold/hold-tenant-bound?tenant_id=tenant-acme",
            headers=self._admin_headers("tenant-other"),
        )
        self.assertEqual(cross_tenant.status_code, 403)

        released = self.client.delete(
            "/v1/admin/legal-hold/hold-tenant-bound?tenant_id=tenant-acme",
            headers=self._admin_headers("tenant-acme"),
        )
        self.assertEqual(released.status_code, 200)

    def test_integration_child_writes_hide_foreign_parents_and_do_not_reserve_ids(self) -> None:
        tenant_a = "tenant-parent-api-a"
        tenant_b = "tenant-parent-api-b"
        connector_a = "connector-parent-api-a"
        connector_b = "connector-parent-api-b"
        for connector_id, tenant_id in (
            (connector_a, tenant_a),
            (connector_b, tenant_b),
        ):
            created = self.client.post(
                "/v1/integrations/connectors",
                json={
                    "connector_id": connector_id,
                    "tenant_id": tenant_id,
                    "provider": "custom",
                    "display_name": f"{tenant_id} connector",
                    "auth_type": "api_key",
                },
                headers=self._admin_headers(tenant_id),
            )
            self.assertEqual(created.status_code, 200, created.text)

        foreign_source = {
            "source_id": "attacker-api-source",
            "connector_id": connector_a,
            "tenant_id": tenant_b,
            "remote_source_id": "reserved-api-remote",
            "source_type": "repository",
            "display_name": "Foreign API source",
        }
        foreign_source_response = self.client.post(
            f"/v1/integrations/connectors/{connector_a}/sources/batch?tenant_id={tenant_b}",
            json={"sources": [foreign_source]},
            headers=self._admin_headers(tenant_b),
        )
        self.assertEqual(foreign_source_response.status_code, 404)
        self.assertEqual(foreign_source_response.json(), {"detail": "connector not found"})

        missing_source_response = self.client.post(
            f"/v1/integrations/connectors/missing-connector/sources/batch?tenant_id={tenant_b}",
            json={"sources": [foreign_source]},
            headers=self._admin_headers(tenant_b),
        )
        self.assertEqual(missing_source_response.status_code, foreign_source_response.status_code)
        self.assertEqual(missing_source_response.json(), foreign_source_response.json())

        owner_source = {
            **foreign_source,
            "source_id": "owner-api-source",
            "tenant_id": tenant_a,
            "display_name": "Owner API source",
        }
        owner_source_response = self.client.post(
            f"/v1/integrations/connectors/{connector_a}/sources/batch?tenant_id={tenant_a}",
            json={"sources": [owner_source]},
            headers=self._admin_headers(tenant_a),
        )
        self.assertEqual(owner_source_response.status_code, 200, owner_source_response.text)
        self.assertEqual(owner_source_response.json()[0]["source_id"], "owner-api-source")

        foreign_mapping_response = self.client.post(
            f"/v1/integrations/connectors/{connector_a}/principal-mappings/batch?tenant_id={tenant_b}",
            json={
                "mappings": [
                    {
                        "mapping_id": "foreign-api-mapping",
                        "connector_id": connector_a,
                        "tenant_id": tenant_b,
                        "principal_type": "user",
                        "local_principal_id": "local-user",
                        "remote_principal_id": "remote-user",
                    }
                ]
            },
            headers=self._admin_headers(tenant_b),
        )
        self.assertEqual(foreign_mapping_response.status_code, 404)
        self.assertEqual(foreign_mapping_response.json(), {"detail": "connector not found"})

        foreign_job_response = self.client.post(
            f"/v1/integrations/connectors/{connector_a}/sync-jobs?tenant_id={tenant_b}",
            json={
                "job_id": "foreign-api-job",
                "connector_id": connector_a,
                "tenant_id": tenant_b,
                "job_type": "full",
            },
            headers=self._admin_headers(tenant_b),
        )
        self.assertEqual(foreign_job_response.status_code, 404)
        self.assertEqual(foreign_job_response.json(), {"detail": "connector not found"})

        foreign_grant_response = self.client.post(
            f"/v1/integrations/connectors/{connector_b}/permissions/batch?tenant_id={tenant_b}",
            json={
                "grants": [
                    {
                        "grant_id": "foreign-api-grant",
                        "source_id": owner_source["source_id"],
                        "connector_id": connector_b,
                        "tenant_id": tenant_b,
                        "principal_type": "user",
                        "principal_id": "tenant-b-user",
                        "permission_level": "view",
                    }
                ]
            },
            headers=self._admin_headers(tenant_b),
        )
        self.assertEqual(foreign_grant_response.status_code, 404)
        self.assertEqual(foreign_grant_response.json(), {"detail": "source not found"})

    def test_integration_plane_round_trip_and_coverage(self) -> None:
        headers = self._admin_headers()
        connector_payload = {
            "connector_id": "conn-slack-acme",
            "tenant_id": "tenant-acme",
            "provider": "slack",
            "display_name": "Slack workspace",
            "remote_workspace_id": "T123",
            "auth_type": "oauth",
            "sync_mode": "hybrid",
            "status": "active",
            "principal_sync_enabled": True,
            "acl_sync_enabled": True,
            "freshness_sla_seconds": 900,
            "metadata": {"workspace_name": "Acme Product"},
            "last_synced_at": "2026-04-10T10:00:00Z",
        }
        connector_response = self.client.post(
            "/v1/integrations/connectors",
            json=connector_payload,
            headers=headers,
        )
        self.assertEqual(connector_response.status_code, 200)
        self.assertEqual(connector_response.json()["provider"], "slack")

        list_connectors = self.client.get(
            "/v1/integrations/connectors?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(list_connectors.status_code, 200)
        self.assertEqual(len(list_connectors.json()), 1)

        get_connector = self.client.get(
            "/v1/integrations/connectors/conn-slack-acme?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(get_connector.status_code, 200)
        self.assertEqual(get_connector.json()["display_name"], "Slack workspace")

        sources_payload = {
            "sources": [
                {
                    "source_id": "src-roadmap",
                    "connector_id": "conn-slack-acme",
                    "tenant_id": "tenant-acme",
                    "remote_source_id": "C001",
                    "source_type": "channel",
                    "display_name": "#roadmap",
                    "path": "/channels/roadmap",
                    "status": "stale",
                    "metadata": {"topic": "product roadmap"},
                },
                {
                    "source_id": "src-releases",
                    "connector_id": "conn-slack-acme",
                    "tenant_id": "tenant-acme",
                    "remote_source_id": "C002",
                    "source_type": "channel",
                    "display_name": "#releases",
                    "path": "/channels/releases",
                    "status": "error",
                    "metadata": {"topic": "release operations"},
                },
            ]
        }
        save_sources = self.client.post(
            "/v1/integrations/connectors/conn-slack-acme/sources/batch?tenant_id=tenant-acme",
            json=sources_payload,
            headers=headers,
        )
        self.assertEqual(save_sources.status_code, 200)
        self.assertEqual(len(save_sources.json()), 2)

        mappings_payload = {
            "mappings": [
                {
                    "mapping_id": "map-pm-1",
                    "connector_id": "conn-slack-acme",
                    "tenant_id": "tenant-acme",
                    "principal_type": "user",
                    "local_principal_id": "pm-1",
                    "remote_principal_id": "U123",
                    "remote_name": "PM One",
                    "groups": ["product", "leadership"],
                    "last_synced_at": "2026-04-10T10:05:00Z",
                }
            ]
        }
        save_mappings = self.client.post(
            "/v1/integrations/connectors/conn-slack-acme/principal-mappings/batch?tenant_id=tenant-acme",
            json=mappings_payload,
            headers=headers,
        )
        self.assertEqual(save_mappings.status_code, 200)
        self.assertEqual(save_mappings.json()[0]["remote_principal_id"], "U123")

        grants_payload = {
            "grants": [
                {
                    "grant_id": "grant-roadmap-view",
                    "source_id": "src-roadmap",
                    "connector_id": "conn-slack-acme",
                    "tenant_id": "tenant-acme",
                    "principal_type": "user",
                    "principal_id": "pm-1",
                    "permission_level": "view",
                    "inherited": True,
                },
                {
                    "grant_id": "grant-releases-edit",
                    "source_id": "src-releases",
                    "connector_id": "conn-slack-acme",
                    "tenant_id": "tenant-acme",
                    "principal_type": "group",
                    "principal_id": "leadership",
                    "permission_level": "edit",
                    "inherited": False,
                },
            ]
        }
        save_grants = self.client.post(
            "/v1/integrations/connectors/conn-slack-acme/permissions/batch?tenant_id=tenant-acme",
            json=grants_payload,
            headers=headers,
        )
        self.assertEqual(save_grants.status_code, 200)
        self.assertEqual(len(save_grants.json()), 2)

        sync_job_payload = {
            "job_id": "sync-001",
            "connector_id": "conn-slack-acme",
            "tenant_id": "tenant-acme",
            "job_type": "acl_sync",
            "status": "running",
            "cursor": "cursor-42",
            "stats": {"sources_seen": 2, "grants_written": 2},
            "started_at": "2026-04-10T10:10:00Z",
        }
        save_job = self.client.post(
            "/v1/integrations/connectors/conn-slack-acme/sync-jobs?tenant_id=tenant-acme",
            json=sync_job_payload,
            headers=headers,
        )
        self.assertEqual(save_job.status_code, 200)
        self.assertEqual(save_job.json()["status"], "running")

        list_jobs = self.client.get(
            "/v1/integrations/connectors/conn-slack-acme/sync-jobs?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(list_jobs.status_code, 200)
        self.assertEqual(len(list_jobs.json()), 1)

        coverage = self.client.get(
            "/v1/integrations/coverage?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(coverage.status_code, 200)
        summary = coverage.json()
        self.assertEqual(summary["connectors_total"], 1)
        self.assertEqual(summary["connectors_healthy"], 1)
        self.assertEqual(summary["sources_total"], 2)
        self.assertEqual(summary["sources_stale"], 1)
        self.assertEqual(summary["sources_past_freshness_sla"], 0)
        self.assertEqual(summary["sources_error"], 1)
        self.assertEqual(summary["principals_mapped"], 1)
        self.assertEqual(summary["grants_total"], 2)
        self.assertEqual(summary["sync_jobs_running"], 1)
        self.assertEqual(summary["missing_foundations"], [])
        self.assertEqual(summary["last_synced_at"], "2026-04-10T10:00:00Z")

    def test_enterprise_global_ids_cannot_be_taken_over_across_tenants(self) -> None:
        owner_tenant = "tenant-enterprise-owner"
        attacker_tenant = "tenant-enterprise-attacker"
        owner_headers = self._admin_headers(owner_tenant)
        attacker_headers = self._admin_headers(attacker_tenant)
        owner_connector_id = "conn-enterprise-shared"
        peer_connector_id = "conn-enterprise-peer"
        attacker_connector_id = "conn-enterprise-attacker"
        owner_source_id = "src-enterprise-shared"
        peer_source_id = "src-enterprise-peer"
        attacker_source_id = "src-enterprise-attacker"

        def save_connector(
            connector_id: str,
            tenant_id: str,
            display_name: str,
            headers: dict[str, str],
        ):
            return self.client.post(
                "/v1/integrations/connectors",
                json={
                    "connector_id": connector_id,
                    "tenant_id": tenant_id,
                    "provider": "github",
                    "display_name": display_name,
                    "auth_type": "api_key",
                    "sync_mode": "manual",
                },
                headers=headers,
            )

        def save_source(
            *,
            endpoint_connector_id: str,
            tenant_id: str,
            source_id: str,
            payload_connector_id: str,
            display_name: str,
            headers: dict[str, str],
        ):
            return self.client.post(
                f"/v1/integrations/connectors/{endpoint_connector_id}/sources/batch",
                params={"tenant_id": tenant_id},
                json={
                    "sources": [
                        {
                            "source_id": source_id,
                            "connector_id": payload_connector_id,
                            "tenant_id": tenant_id,
                            "remote_source_id": f"remote-{source_id}",
                            "source_type": "repository",
                            "display_name": display_name,
                        }
                    ]
                },
                headers=headers,
            )

        def save_mapping(
            *,
            endpoint_connector_id: str,
            tenant_id: str,
            mapping_id: str,
            payload_connector_id: str,
            remote_name: str,
            headers: dict[str, str],
        ):
            return self.client.post(
                f"/v1/integrations/connectors/{endpoint_connector_id}/principal-mappings/batch",
                params={"tenant_id": tenant_id},
                json={
                    "mappings": [
                        {
                            "mapping_id": mapping_id,
                            "connector_id": payload_connector_id,
                            "tenant_id": tenant_id,
                            "principal_type": "user",
                            "local_principal_id": f"local-{mapping_id}",
                            "remote_principal_id": f"remote-{mapping_id}",
                            "remote_name": remote_name,
                        }
                    ]
                },
                headers=headers,
            )

        def save_grant(
            *,
            endpoint_connector_id: str,
            tenant_id: str,
            grant_id: str,
            source_id: str,
            payload_connector_id: str,
            permission_level: str,
            headers: dict[str, str],
        ):
            return self.client.post(
                f"/v1/integrations/connectors/{endpoint_connector_id}/permissions/batch",
                params={"tenant_id": tenant_id},
                json={
                    "grants": [
                        {
                            "grant_id": grant_id,
                            "source_id": source_id,
                            "connector_id": payload_connector_id,
                            "tenant_id": tenant_id,
                            "principal_type": "user",
                            "principal_id": "principal-enterprise",
                            "permission_level": permission_level,
                        }
                    ]
                },
                headers=headers,
            )

        def save_sync_job(
            *,
            endpoint_connector_id: str,
            tenant_id: str,
            job_id: str,
            payload_connector_id: str,
            status: str,
            headers: dict[str, str],
        ):
            return self.client.post(
                f"/v1/integrations/connectors/{endpoint_connector_id}/sync-jobs",
                params={"tenant_id": tenant_id},
                json={
                    "job_id": job_id,
                    "connector_id": payload_connector_id,
                    "tenant_id": tenant_id,
                    "job_type": "full",
                    "status": status,
                },
                headers=headers,
            )

        seed_responses = [
            save_connector(owner_connector_id, owner_tenant, "Owner connector", owner_headers),
            save_connector(peer_connector_id, owner_tenant, "Peer connector", owner_headers),
            save_connector(attacker_connector_id, attacker_tenant, "Attacker connector", attacker_headers),
            save_source(
                endpoint_connector_id=owner_connector_id,
                tenant_id=owner_tenant,
                source_id=owner_source_id,
                payload_connector_id=owner_connector_id,
                display_name="Owner source",
                headers=owner_headers,
            ),
            save_source(
                endpoint_connector_id=peer_connector_id,
                tenant_id=owner_tenant,
                source_id=peer_source_id,
                payload_connector_id=peer_connector_id,
                display_name="Peer source",
                headers=owner_headers,
            ),
            save_source(
                endpoint_connector_id=attacker_connector_id,
                tenant_id=attacker_tenant,
                source_id=attacker_source_id,
                payload_connector_id=attacker_connector_id,
                display_name="Attacker source",
                headers=attacker_headers,
            ),
            save_mapping(
                endpoint_connector_id=owner_connector_id,
                tenant_id=owner_tenant,
                mapping_id="map-enterprise-shared",
                payload_connector_id=owner_connector_id,
                remote_name="Owner mapping",
                headers=owner_headers,
            ),
            save_grant(
                endpoint_connector_id=owner_connector_id,
                tenant_id=owner_tenant,
                grant_id="grant-enterprise-shared",
                source_id=owner_source_id,
                payload_connector_id=owner_connector_id,
                permission_level="view",
                headers=owner_headers,
            ),
            save_sync_job(
                endpoint_connector_id=owner_connector_id,
                tenant_id=owner_tenant,
                job_id="job-enterprise-shared",
                payload_connector_id=owner_connector_id,
                status="running",
                headers=owner_headers,
            ),
            self.client.post(
                "/v1/admin/retention-policies",
                json={
                    "policy_id": "policy-enterprise-shared",
                    "tenant_id": owner_tenant,
                    "max_age_days": 365,
                    "action": "delete_soft",
                },
                headers=owner_headers,
            ),
            self.client.post(
                "/v1/admin/legal-hold",
                json={
                    "hold_id": "hold-enterprise-shared",
                    "tenant_id": owner_tenant,
                    "reason": "Owner legal hold",
                },
                headers=owner_headers,
            ),
        ]
        for response in seed_responses:
            self.assertEqual(response.status_code, 200, response.text)

        store = self.client.app.state.store
        protected_tables = (
            "connectors",
            "connector_sources",
            "principal_mappings",
            "source_permission_grants",
            "sync_jobs",
            "retention_policies",
            "legal_holds",
            "audit_log",
        )

        def persistence_state() -> dict[str, list[tuple[object, ...]]]:
            return {
                table: [
                    tuple(row)
                    for row in store.conn.execute(
                        f"SELECT * FROM {table} ORDER BY 1"
                    ).fetchall()
                ]
                for table in protected_tables
            }

        def cache_state() -> tuple[dict[str, int], dict[tuple[object, ...], tuple[float, str]]]:
            cache = store.hot_cache
            return (
                dict(cache._versions),
                {
                    key: (expires_at, json.dumps(value, sort_keys=True))
                    for key, (expires_at, value) in cache._entries.items()
                },
            )

        state_before_attacks = persistence_state()
        cache_before_attacks = cache_state()

        hostile_responses = [
            save_source(
                endpoint_connector_id=peer_connector_id,
                tenant_id=owner_tenant,
                source_id=owner_source_id,
                payload_connector_id=peer_connector_id,
                display_name="Reparented source",
                headers=owner_headers,
            ),
            save_mapping(
                endpoint_connector_id=peer_connector_id,
                tenant_id=owner_tenant,
                mapping_id="map-enterprise-shared",
                payload_connector_id=peer_connector_id,
                remote_name="Reparented mapping",
                headers=owner_headers,
            ),
            save_grant(
                endpoint_connector_id=peer_connector_id,
                tenant_id=owner_tenant,
                grant_id="grant-enterprise-shared",
                source_id=peer_source_id,
                payload_connector_id=peer_connector_id,
                permission_level="owner",
                headers=owner_headers,
            ),
            save_sync_job(
                endpoint_connector_id=peer_connector_id,
                tenant_id=owner_tenant,
                job_id="job-enterprise-shared",
                payload_connector_id=peer_connector_id,
                status="succeeded",
                headers=owner_headers,
            ),
            save_connector(owner_connector_id, attacker_tenant, "Stolen connector", attacker_headers),
            save_source(
                endpoint_connector_id=attacker_connector_id,
                tenant_id=attacker_tenant,
                source_id=owner_source_id,
                payload_connector_id=attacker_connector_id,
                display_name="Stolen source",
                headers=attacker_headers,
            ),
            save_source(
                endpoint_connector_id=owner_connector_id,
                tenant_id=attacker_tenant,
                source_id="src-under-foreign-connector",
                payload_connector_id=owner_connector_id,
                display_name="Foreign connector source",
                headers=attacker_headers,
            ),
            save_mapping(
                endpoint_connector_id=attacker_connector_id,
                tenant_id=attacker_tenant,
                mapping_id="map-enterprise-shared",
                payload_connector_id=attacker_connector_id,
                remote_name="Stolen mapping",
                headers=attacker_headers,
            ),
            save_mapping(
                endpoint_connector_id=owner_connector_id,
                tenant_id=attacker_tenant,
                mapping_id="map-under-foreign-connector",
                payload_connector_id=owner_connector_id,
                remote_name="Foreign connector mapping",
                headers=attacker_headers,
            ),
            save_grant(
                endpoint_connector_id=attacker_connector_id,
                tenant_id=attacker_tenant,
                grant_id="grant-enterprise-shared",
                source_id=attacker_source_id,
                payload_connector_id=attacker_connector_id,
                permission_level="view",
                headers=attacker_headers,
            ),
            save_grant(
                endpoint_connector_id=attacker_connector_id,
                tenant_id=attacker_tenant,
                grant_id="grant-on-foreign-source",
                source_id=owner_source_id,
                payload_connector_id=attacker_connector_id,
                permission_level="view",
                headers=attacker_headers,
            ),
            save_grant(
                endpoint_connector_id=owner_connector_id,
                tenant_id=attacker_tenant,
                grant_id="grant-under-foreign-connector",
                source_id=owner_source_id,
                payload_connector_id=owner_connector_id,
                permission_level="view",
                headers=attacker_headers,
            ),
            save_sync_job(
                endpoint_connector_id=attacker_connector_id,
                tenant_id=attacker_tenant,
                job_id="job-enterprise-shared",
                payload_connector_id=attacker_connector_id,
                status="queued",
                headers=attacker_headers,
            ),
            save_sync_job(
                endpoint_connector_id=owner_connector_id,
                tenant_id=attacker_tenant,
                job_id="job-under-foreign-connector",
                payload_connector_id=owner_connector_id,
                status="queued",
                headers=attacker_headers,
            ),
            self.client.post(
                "/v1/admin/retention-policies",
                json={
                    "policy_id": "policy-enterprise-shared",
                    "tenant_id": attacker_tenant,
                    "max_age_days": 1,
                    "action": "delete_hard",
                },
                headers=attacker_headers,
            ),
            self.client.post(
                "/v1/admin/legal-hold",
                json={
                    "hold_id": "hold-enterprise-shared",
                    "tenant_id": attacker_tenant,
                    "reason": "Stolen legal hold",
                },
                headers=attacker_headers,
            ),
        ]
        for response in hostile_responses:
            self.assertIn(response.status_code, {403, 404, 409}, response.text)
            self.assertNotIn(owner_tenant, response.text)

        self.assertEqual(persistence_state(), state_before_attacks)
        self.assertEqual(cache_state(), cache_before_attacks)
        owned_ids = (
            ("connectors", "connector_id", owner_connector_id),
            ("connector_sources", "source_id", owner_source_id),
            ("principal_mappings", "mapping_id", "map-enterprise-shared"),
            ("source_permission_grants", "grant_id", "grant-enterprise-shared"),
            ("sync_jobs", "job_id", "job-enterprise-shared"),
            ("retention_policies", "policy_id", "policy-enterprise-shared"),
            ("legal_holds", "hold_id", "hold-enterprise-shared"),
        )
        for table, id_column, record_id in owned_ids:
            row = store.conn.execute(
                f"SELECT tenant_id FROM {table} WHERE {id_column} = ?",
                (record_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["tenant_id"], owner_tenant)

        same_tenant_updates = [
            save_connector(owner_connector_id, owner_tenant, "Updated owner connector", owner_headers),
            save_source(
                endpoint_connector_id=owner_connector_id,
                tenant_id=owner_tenant,
                source_id=owner_source_id,
                payload_connector_id=owner_connector_id,
                display_name="Updated owner source",
                headers=owner_headers,
            ),
            save_mapping(
                endpoint_connector_id=owner_connector_id,
                tenant_id=owner_tenant,
                mapping_id="map-enterprise-shared",
                payload_connector_id=owner_connector_id,
                remote_name="Updated owner mapping",
                headers=owner_headers,
            ),
            save_grant(
                endpoint_connector_id=owner_connector_id,
                tenant_id=owner_tenant,
                grant_id="grant-enterprise-shared",
                source_id=owner_source_id,
                payload_connector_id=owner_connector_id,
                permission_level="owner",
                headers=owner_headers,
            ),
            save_sync_job(
                endpoint_connector_id=owner_connector_id,
                tenant_id=owner_tenant,
                job_id="job-enterprise-shared",
                payload_connector_id=owner_connector_id,
                status="succeeded",
                headers=owner_headers,
            ),
            self.client.post(
                "/v1/admin/retention-policies",
                json={
                    "policy_id": "policy-enterprise-shared",
                    "tenant_id": owner_tenant,
                    "max_age_days": 730,
                    "action": "delete_soft",
                },
                headers=owner_headers,
            ),
            self.client.post(
                "/v1/admin/legal-hold",
                json={
                    "hold_id": "hold-enterprise-shared",
                    "tenant_id": owner_tenant,
                    "reason": "Updated owner legal hold",
                },
                headers=owner_headers,
            ),
        ]
        for response in same_tenant_updates:
            self.assertEqual(response.status_code, 200, response.text)

        updated_values = (
            ("connectors", "connector_id", owner_connector_id, "display_name", "Updated owner connector"),
            ("connector_sources", "source_id", owner_source_id, "display_name", "Updated owner source"),
            ("principal_mappings", "mapping_id", "map-enterprise-shared", "remote_name", "Updated owner mapping"),
            ("source_permission_grants", "grant_id", "grant-enterprise-shared", "permission_level", "owner"),
            ("sync_jobs", "job_id", "job-enterprise-shared", "status", "succeeded"),
            ("retention_policies", "policy_id", "policy-enterprise-shared", "max_age_days", 730),
            ("legal_holds", "hold_id", "hold-enterprise-shared", "reason", "Updated owner legal hold"),
        )
        for table, id_column, record_id, value_column, expected in updated_values:
            row = store.conn.execute(
                f"SELECT tenant_id, {value_column} FROM {table} WHERE {id_column} = ?",
                (record_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["tenant_id"], owner_tenant)
            self.assertEqual(row[value_column], expected)

    def test_integration_coverage_distinguishes_stale_sources_from_sla_breaches(self) -> None:
        headers = self._admin_headers()
        connector_response = self.client.post(
            "/v1/integrations/connectors",
            json={
                "connector_id": "conn-coverage-freshness",
                "tenant_id": "tenant-acme",
                "provider": "slack",
                "display_name": "Coverage freshness connector",
                "remote_workspace_id": "T555",
                "auth_type": "oauth",
                "sync_mode": "hybrid",
                "status": "active",
                "principal_sync_enabled": True,
                "acl_sync_enabled": True,
                "freshness_sla_seconds": 900,
                "last_synced_at": "2026-04-15T12:00:00Z",
            },
            headers=headers,
        )
        self.assertEqual(connector_response.status_code, 200)

        save_sources = self.client.post(
            "/v1/integrations/connectors/conn-coverage-freshness/sources/batch?tenant_id=tenant-acme",
            json={
                "sources": [
                    {
                        "source_id": "src-stale-only",
                        "connector_id": "conn-coverage-freshness",
                        "tenant_id": "tenant-acme",
                        "remote_source_id": "C301",
                        "source_type": "channel",
                        "display_name": "#stale-only",
                        "status": "stale",
                        "last_synced_at": "2026-04-15T12:00:00Z",
                        "stale_after": "2099-04-15T12:15:00Z",
                    },
                    {
                        "source_id": "src-breached-deadline",
                        "connector_id": "conn-coverage-freshness",
                        "tenant_id": "tenant-acme",
                        "remote_source_id": "C302",
                        "source_type": "channel",
                        "display_name": "#breached-deadline",
                        "status": "indexed",
                        "last_synced_at": "2026-04-10T10:00:00Z",
                    },
                ]
            },
            headers=headers,
        )
        self.assertEqual(save_sources.status_code, 200)

        coverage = self.client.get(
            "/v1/integrations/coverage?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(coverage.status_code, 200)
        summary = coverage.json()
        self.assertEqual(summary["sources_total"], 2)
        self.assertEqual(summary["sources_stale"], 1)
        self.assertEqual(summary["sources_past_freshness_sla"], 1)
        self.assertEqual(summary["sources_error"], 0)

    def test_integration_coverage_keeps_freshness_breaches_tenant_scoped_and_null_safe(self) -> None:
        acme_headers = self._admin_headers("tenant-acme")
        other_headers = self._admin_headers("tenant-other")
        for connector, headers in (
            (
                {
                    "connector_id": "conn-coverage-acme",
                    "tenant_id": "tenant-acme",
                    "provider": "slack",
                    "display_name": "Acme coverage connector",
                    "auth_type": "oauth",
                    "sync_mode": "hybrid",
                    "status": "active",
                    "freshness_sla_seconds": 900,
                },
                acme_headers,
            ),
            (
                {
                    "connector_id": "conn-coverage-other",
                    "tenant_id": "tenant-other",
                    "provider": "slack",
                    "display_name": "Other coverage connector",
                    "auth_type": "oauth",
                    "sync_mode": "hybrid",
                    "status": "active",
                    "freshness_sla_seconds": 900,
                },
                other_headers,
            ),
        ):
            response = self.client.post(
                "/v1/integrations/connectors",
                json=connector,
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)

        acme_sources = self.client.post(
            "/v1/integrations/connectors/conn-coverage-acme/sources/batch?tenant_id=tenant-acme",
            json={
                "sources": [
                    {
                        "source_id": "src-acme-breached",
                        "connector_id": "conn-coverage-acme",
                        "tenant_id": "tenant-acme",
                        "remote_source_id": "C401",
                        "source_type": "channel",
                        "display_name": "#acme-breached",
                        "status": "indexed",
                        "last_synced_at": "2026-04-10T10:00:00Z",
                    },
                    {
                        "source_id": "src-acme-unknown-freshness",
                        "connector_id": "conn-coverage-acme",
                        "tenant_id": "tenant-acme",
                        "remote_source_id": "C402",
                        "source_type": "channel",
                        "display_name": "#acme-unknown-freshness",
                        "status": "indexed",
                    },
                ]
            },
            headers=acme_headers,
        )
        self.assertEqual(acme_sources.status_code, 200)

        other_sources = self.client.post(
            "/v1/integrations/connectors/conn-coverage-other/sources/batch?tenant_id=tenant-other",
            json={
                "sources": [
                    {
                        "source_id": "src-other-breached",
                        "connector_id": "conn-coverage-other",
                        "tenant_id": "tenant-other",
                        "remote_source_id": "C501",
                        "source_type": "channel",
                        "display_name": "#other-breached",
                        "status": "indexed",
                        "last_synced_at": "2026-04-10T10:00:00Z",
                    }
                ]
            },
            headers=other_headers,
        )
        self.assertEqual(other_sources.status_code, 200)

        acme_coverage = self.client.get(
            "/v1/integrations/coverage?tenant_id=tenant-acme",
            headers=acme_headers,
        )
        self.assertEqual(acme_coverage.status_code, 200)
        acme_summary = acme_coverage.json()
        self.assertEqual(acme_summary["sources_total"], 2)
        self.assertEqual(acme_summary["sources_past_freshness_sla"], 1)
        self.assertEqual(acme_summary["missing_foundations"], ["principal_mappings", "source_permission_grants"])

        other_coverage = self.client.get(
            "/v1/integrations/coverage?tenant_id=tenant-other",
            headers=other_headers,
        )
        self.assertEqual(other_coverage.status_code, 200)
        other_summary = other_coverage.json()
        self.assertEqual(other_summary["sources_total"], 1)
        self.assertEqual(other_summary["sources_past_freshness_sla"], 1)

    def test_connector_worker_contract_respects_optional_sync_dimensions(self) -> None:
        class DisabledDimensionsWorker:
            def run(self, context: ConnectorRunContext) -> ConnectorRunResult:
                return ConnectorRunResult(
                    cursor="cursor-next",
                    stats={"sources_seen": 1, "principal_candidates": 1, "permission_candidates": 1},
                    sources={
                        "sources": [
                            {
                                "source_id": "src-only",
                                "connector_id": context.connector.connector_id,
                                "tenant_id": context.connector.tenant_id,
                                "remote_source_id": "C100",
                                "source_type": "channel",
                                "display_name": "#only-source",
                                "status": "indexed",
                            }
                        ]
                    },
                    principal_mappings={
                        "mappings": [
                            {
                                "mapping_id": "map-skipped",
                                "connector_id": context.connector.connector_id,
                                "tenant_id": context.connector.tenant_id,
                                "principal_type": "user",
                                "local_principal_id": "pm-1",
                                "remote_principal_id": "U100",
                            }
                        ]
                    },
                    permissions={
                        "grants": [
                            {
                                "grant_id": "grant-skipped",
                                "source_id": "src-only",
                                "connector_id": context.connector.connector_id,
                                "tenant_id": context.connector.tenant_id,
                                "principal_type": "user",
                                "principal_id": "pm-1",
                                "permission_level": "view",
                            }
                        ]
                    },
                )

        connector = ConnectorConfig(
            connector_id="conn-worker-disabled",
            tenant_id="tenant-acme",
            provider=ConnectorProvider.SLACK,
            display_name="Slack worker disabled dims",
            auth_type=ConnectorAuthType.OAUTH,
            sync_mode=SyncMode.HYBRID,
            status=ConnectorStatus.ACTIVE,
            principal_sync_enabled=False,
            acl_sync_enabled=False,
        )
        headers = self._admin_headers()
        save_connector = self.client.post(
            "/v1/integrations/connectors",
            json=connector.model_dump(mode="json"),
            headers=headers,
        )
        self.assertEqual(save_connector.status_code, 200)

        context = ConnectorRunContext(
            connector=connector,
            job_id="sync-worker-disabled-001",
            job_type=SyncJobType.FULL,
            cursor="cursor-start",
        )
        outcome = self.client.app.state.connector_execution.execute(DisabledDimensionsWorker(), context)

        self.assertEqual(len(outcome.sources), 1)
        self.assertEqual(outcome.sources[0].source_id, "src-only")
        self.assertEqual(outcome.principal_mappings, [])
        self.assertEqual(outcome.permissions, [])
        self.assertEqual(outcome.sync_job.job_type, SyncJobType.FULL)
        self.assertEqual(outcome.sync_job.status, SyncJobStatus.SUCCEEDED)
        self.assertEqual(outcome.sync_job.cursor, "cursor-next")
        self.assertEqual(outcome.sync_job.stats["sources_written"], 1)
        self.assertEqual(outcome.sync_job.stats["principal_mappings_emitted"], 1)
        self.assertEqual(outcome.sync_job.stats["principal_mappings_written"], 0)
        self.assertEqual(outcome.sync_job.stats["permissions_emitted"], 1)
        self.assertEqual(outcome.sync_job.stats["permissions_written"], 0)
        self.assertEqual(outcome.coverage.sources_total, 1)
        self.assertEqual(outcome.coverage.principals_mapped, 0)
        self.assertEqual(outcome.coverage.grants_total, 0)

    def test_scheduler_skips_active_connector_with_no_registered_worker(self) -> None:
        headers = self._admin_headers()
        connector = {
            "connector_id": "conn-scheduled-slack",
            "tenant_id": "tenant-acme",
            "provider": "slack",
            "display_name": "Slack scheduled connector",
            "remote_workspace_id": "T901",
            "auth_type": "oauth",
            "sync_mode": "poll",
            "status": "active",
            "principal_sync_enabled": True,
            "acl_sync_enabled": True,
            "metadata": {
                "workspace_name": "Acme Product",
                "scheduler": {
                    "enabled": True,
                    "cadence_seconds": 900,
                    "job_type": "full",
                }
            },
        }
        save_connector = self.client.post(
            "/v1/integrations/connectors",
            json=connector,
            headers=headers,
        )
        self.assertEqual(save_connector.status_code, 200)

        scheduler = self.client.app.state.connector_scheduler
        # No first-party Slack worker ships, so none is registered.
        with self.assertRaises(KeyError):
            scheduler.worker_for(ConnectorProvider.SLACK)

        scheduled_at = datetime(2026, 4, 15, 12, 30, tzinfo=UTC)
        tick = scheduler.tick(
            tenant_id="tenant-acme",
            evaluated_at=scheduled_at,
        )

        # The connector is eligible (active + valid schedule) but has no worker,
        # so it must be skipped and nothing written to the ledger.
        self.assertEqual(len(tick.scheduled), 0)
        skips = {item.connector_id: item.reason for item in tick.skipped}
        self.assertEqual(skips["conn-scheduled-slack"], "provider_not_implemented")

        list_jobs = self.client.get(
            "/v1/integrations/connectors/conn-scheduled-slack/sync-jobs?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(list_jobs.status_code, 200)
        self.assertEqual(list_jobs.json(), [])

        list_sources = self.client.get(
            "/v1/integrations/connectors/conn-scheduled-slack/sources?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(list_sources.status_code, 200)
        self.assertEqual(list_sources.json(), [])

        connector_readback = self.client.get(
            "/v1/integrations/connectors/conn-scheduled-slack?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(connector_readback.status_code, 200)
        self.assertIsNone(connector_readback.json()["last_synced_at"])

        coverage = self.client.get(
            "/v1/integrations/coverage?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(coverage.status_code, 200)
        summary = coverage.json()
        self.assertEqual(summary["connectors_total"], 1)
        self.assertEqual(summary["sources_total"], 0)
        self.assertEqual(summary["principals_mapped"], 0)
        self.assertEqual(summary["grants_total"], 0)
        self.assertIsNone(summary["last_synced_at"])

    def test_scheduler_has_no_default_worker_and_worker_for_raises_on_unregistered(self) -> None:
        scheduler = self.client.app.state.connector_scheduler
        self.assertFalse(hasattr(scheduler, "default_worker"))
        with self.assertRaises(KeyError):
            scheduler.worker_for(ConnectorProvider.SLACK)
        with self.assertRaises(KeyError):
            scheduler.worker_for("notion")

    def test_connector_scheduler_skips_not_due_in_progress_and_ineligible_connectors(self) -> None:
        class FastSlackWorker:
            def run(self, context: ConnectorRunContext) -> ConnectorRunResult:
                return ConnectorRunResult(
                    status=SyncJobStatus.SUCCEEDED,
                    cursor="cursor-repeat-002",
                    started_at=context.started_at,
                    finished_at=context.started_at + timedelta(minutes=1),
                    stats={"scheduler": "fixed_cadence"},
                )

        headers = self._admin_headers()
        connectors = [
            {
                "connector_id": "conn-sched-repeat",
                "tenant_id": "tenant-acme",
                "provider": "slack",
                "display_name": "Slack repeat connector",
                "auth_type": "oauth",
                "sync_mode": "poll",
                "status": "active",
                "metadata": {"scheduler": {"enabled": True, "cadence_seconds": 900, "job_type": "full"}},
            },
            {
                "connector_id": "conn-sched-running",
                "tenant_id": "tenant-acme",
                "provider": "slack",
                "display_name": "Slack running connector",
                "auth_type": "oauth",
                "sync_mode": "poll",
                "status": "active",
                "metadata": {"scheduler": {"enabled": True, "cadence_seconds": 900, "job_type": "full"}},
            },
            {
                "connector_id": "conn-sched-paused",
                "tenant_id": "tenant-acme",
                "provider": "notion",
                "display_name": "Paused notion connector",
                "auth_type": "oauth",
                "sync_mode": "poll",
                "status": "paused",
                "metadata": {"scheduler": {"enabled": True, "cadence_seconds": 900, "job_type": "full"}},
            },
            {
                "connector_id": "conn-sched-missing",
                "tenant_id": "tenant-acme",
                "provider": "github",
                "display_name": "Missing cadence connector",
                "auth_type": "oauth",
                "sync_mode": "poll",
                "status": "active",
                "metadata": {"scheduler": {"enabled": True}},
            },
            {
                "connector_id": "conn-sched-disabled",
                "tenant_id": "tenant-acme",
                "provider": "custom",
                "display_name": "Disabled scheduler connector",
                "auth_type": "api_key",
                "sync_mode": "manual",
                "status": "active",
                "metadata": {"scheduler": {"enabled": False, "cadence_seconds": 900, "job_type": "full"}},
            },
            {
                "connector_id": "conn-sched-no-worker",
                "tenant_id": "tenant-acme",
                "provider": "salesforce",
                "display_name": "Salesforce connector without a worker",
                "auth_type": "oauth",
                "sync_mode": "poll",
                "status": "active",
                "metadata": {"scheduler": {"enabled": True, "cadence_seconds": 900, "job_type": "full"}},
            },
        ]
        for connector in connectors:
            response = self.client.post(
                "/v1/integrations/connectors",
                json=connector,
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)

        running_job = self.client.post(
            "/v1/integrations/connectors/conn-sched-running/sync-jobs?tenant_id=tenant-acme",
            json={
                "job_id": "sync-sched-running-001",
                "connector_id": "conn-sched-running",
                "tenant_id": "tenant-acme",
                "job_type": "full",
                "status": "running",
                "started_at": "2026-04-15T12:28:00Z",
            },
            headers=headers,
        )
        self.assertEqual(running_job.status_code, 200)

        self.client.app.state.connector_scheduler.register_worker("slack", FastSlackWorker())
        scheduled_at = datetime(2026, 4, 15, 12, 30, tzinfo=UTC)
        first_tick = self.client.app.state.connector_scheduler.tick(
            tenant_id="tenant-acme",
            evaluated_at=scheduled_at,
        )
        self.assertEqual(len(first_tick.scheduled), 1)
        self.assertEqual(first_tick.scheduled[0].connector.connector_id, "conn-sched-repeat")
        first_skips = {item.connector_id: item.reason for item in first_tick.skipped}
        self.assertEqual(first_skips["conn-sched-running"], "job_in_progress")
        self.assertEqual(first_skips["conn-sched-paused"], "connector_inactive")
        self.assertEqual(first_skips["conn-sched-missing"], "missing_cadence")
        self.assertEqual(first_skips["conn-sched-disabled"], "schedule_disabled")
        # Active + valid schedule but no registered worker: skipped, no ledger write.
        self.assertEqual(first_skips["conn-sched-no-worker"], "provider_not_implemented")
        no_worker_jobs = self.client.get(
            "/v1/integrations/connectors/conn-sched-no-worker/sync-jobs?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(no_worker_jobs.status_code, 200)
        self.assertEqual(no_worker_jobs.json(), [])
        no_worker_sources = self.client.get(
            "/v1/integrations/connectors/conn-sched-no-worker/sources?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(no_worker_sources.status_code, 200)
        self.assertEqual(no_worker_sources.json(), [])

        second_tick = self.client.app.state.connector_scheduler.tick(
            tenant_id="tenant-acme",
            evaluated_at=scheduled_at + timedelta(minutes=5),
        )
        self.assertEqual(len(second_tick.scheduled), 0)
        second_skips = {item.connector_id: item.reason for item in second_tick.skipped}
        self.assertEqual(second_skips["conn-sched-repeat"], "not_due")
        self.assertEqual(second_skips["conn-sched-running"], "job_in_progress")

        repeat_jobs = self.client.get(
            "/v1/integrations/connectors/conn-sched-repeat/sync-jobs?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(repeat_jobs.status_code, 200)
        self.assertEqual(len(repeat_jobs.json()), 1)

    def test_integration_plane_requires_admin_and_tenant_match(self) -> None:
        connector_payload = {
            "connector_id": "conn-notion-acme",
            "tenant_id": "tenant-acme",
            "provider": "notion",
            "display_name": "Notion workspace",
            "auth_type": "oauth",
            "sync_mode": "hybrid",
        }

        viewer_response = self.client.post(
            "/v1/integrations/connectors",
            json=connector_payload,
            headers={
                "X-Provena-Tenant-Id": "tenant-acme",
                "X-Provena-Role": "viewer",
                "X-Provena-Principal-Id": "viewer-1",
            },
        )
        self.assertEqual(viewer_response.status_code, 403)

        mismatch_response = self.client.post(
            "/v1/integrations/connectors",
            json=connector_payload,
            headers=self._admin_headers("tenant-other"),
        )
        self.assertEqual(mismatch_response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
