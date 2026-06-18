import importlib
import os
import shutil
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.connector_worker import ConnectorRunContext, ConnectorRunResult
from app.models import (
    ConnectorAuthType,
    ConnectorConfig,
    ConnectorProvider,
    ConnectorStatus,
    SyncMode,
    SyncJobStatus,
    SyncJobType,
)
from app.slack_connector import SlackConnectorStubWorker


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
        broad_id = broad_response.json()["memory"]["memory_id"]

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
            "supersedes_memory_id": broad_id,
        }
        self.client.post("/v1/memories", json=refined)

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
                "source_references": [
                    {"source_type": "channel", "source_id": "src-roadmap-user"}
                ],
            },
        ).json()["memory"]["memory_id"]
        blocked_memory = self.client.post(
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
                "source_references": [
                    {"source_type": "channel", "source_id": "src-leadership-group"}
                ],
            },
        ).json()["memory"]["memory_id"]
        mixed_memory = self.client.post(
            "/v1/memories",
            json={
                "kind": "artifact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Mixed leadership brief",
                "content": "Leadership release planning brief should not leak partial source access.",
                "source_references": [
                    {"source_type": "channel", "source_id": "src-leadership-group"},
                    {"source_type": "channel", "source_id": "src-mixed-denied"},
                ],
            },
        ).json()["memory"]["memory_id"]
        hidden_related = self.client.post(
            "/v1/memories",
            json={
                "kind": "artifact",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
                "title": "Hidden related brief",
                "content": "Leadership release planning brief follow-up should stay hidden.",
                "source_references": [
                    {"source_type": "channel", "source_id": "src-related-denied"}
                ],
            },
        ).json()["memory"]["memory_id"]

        relation_response = self.client.post(
            "/v1/memories/relations",
            json={
                "from_memory_id": primary_memory,
                "to_memory_id": hidden_related,
                "relation": "supports",
                "scope": {"tenant_id": "tenant-acme", "workspace_id": "ws-growth"},
            },
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
                "source_references": [
                    {"source_type": "channel", "source_id": "src-mapped-user"}
                ],
            },
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
        self.assertEqual(wrong_connector_grant.status_code, 200)

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
        self.assertEqual(foreign_grant.status_code, 200)

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
            "/v1/admin/legal-hold/hold-growth?tenant_id=tenant-acme"
        )
        self.assertEqual(release.status_code, 200)

        released = self.client.get(f"/v1/memories/{created['memory_id']}")
        self.assertEqual(released.status_code, 200)
        self.assertFalse(released.json()["held"])
        self.assertEqual(released.json()["status"], "active")

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

    def test_shipped_slack_stub_execution_flows_through_store_and_integration_routes(self) -> None:
        headers = self._admin_headers()
        connector = {
            "connector_id": "conn-worker-slack",
            "tenant_id": "tenant-acme",
            "provider": "slack",
            "display_name": "Slack worker connector",
            "remote_workspace_id": "T987",
            "auth_type": "oauth",
            "sync_mode": "hybrid",
            "status": "active",
            "principal_sync_enabled": True,
            "acl_sync_enabled": True,
            "freshness_sla_seconds": 900,
            "last_synced_at": "2026-04-15T11:50:00Z",
        }
        save_connector = self.client.post(
            "/v1/integrations/connectors",
            json=connector,
            headers=headers,
        )
        self.assertEqual(save_connector.status_code, 200)

        context = ConnectorRunContext(
            connector=ConnectorConfig.model_validate(connector),
            job_id="sync-worker-slack-001",
            job_type=SyncJobType.ACL_SYNC,
            cursor="cursor-slack-001",
        )
        outcome = self.client.app.state.connector_execution.execute(SlackConnectorStubWorker(), context)
        self.assertEqual(outcome.sync_job.status, SyncJobStatus.SUCCEEDED)
        self.assertEqual(outcome.sync_job.job_type, SyncJobType.ACL_SYNC)
        self.assertEqual(outcome.sync_job.cursor, "slack:T987:channels:v1")
        self.assertEqual(outcome.sync_job.stats["sources_written"], 2)
        self.assertEqual(outcome.sync_job.stats["principal_mappings_written"], 1)
        self.assertEqual(outcome.sync_job.stats["permissions_written"], 2)
        self.assertEqual(outcome.sync_job.stats["provider"], "slack")
        self.assertTrue(outcome.sync_job.stats["stub"])
        self.assertEqual(outcome.sync_job.stats["workspace_name"], "Slack worker connector")

        list_connectors = self.client.get(
            "/v1/integrations/connectors?tenant_id=tenant-acme&provider=slack",
            headers=headers,
        )
        self.assertEqual(list_connectors.status_code, 200)
        self.assertEqual(len(list_connectors.json()), 1)

        list_sources = self.client.get(
            "/v1/integrations/connectors/conn-worker-slack/sources?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(list_sources.status_code, 200)
        sources = list_sources.json()
        self.assertEqual(len(sources), 2)
        sources_by_id = {source["source_id"]: source for source in sources}
        roadmap_source = sources_by_id["conn-worker-slack-roadmap"]
        release_source = sources_by_id["conn-worker-slack-release-ops"]
        self.assertEqual(roadmap_source["source_type"], "channel")
        self.assertEqual(roadmap_source["path"], "/channels/roadmap")
        self.assertEqual(roadmap_source["metadata"]["provider"], "slack")
        self.assertTrue(roadmap_source["metadata"]["stub"])
        self.assertEqual(roadmap_source["metadata"]["remote_workspace_id"], "T987")
        self.assertEqual(release_source["path"], "/channels/release-ops")

        list_mappings = self.client.get(
            "/v1/integrations/connectors/conn-worker-slack/principal-mappings?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(list_mappings.status_code, 200)
        self.assertEqual(list_mappings.json()[0]["remote_principal_id"], "U201")
        self.assertEqual(list_mappings.json()[0]["remote_name"], "Slack worker connector PM")

        roadmap_permissions = self.client.get(
            "/v1/integrations/connectors/conn-worker-slack/permissions"
            "?tenant_id=tenant-acme&source_id=conn-worker-slack-roadmap",
            headers=headers,
        )
        self.assertEqual(roadmap_permissions.status_code, 200)
        self.assertEqual(len(roadmap_permissions.json()), 1)
        self.assertEqual(
            roadmap_permissions.json()[0]["grant_id"],
            "conn-worker-slack-roadmap-view",
        )

        list_jobs = self.client.get(
            "/v1/integrations/connectors/conn-worker-slack/sync-jobs?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(list_jobs.status_code, 200)
        self.assertEqual(len(list_jobs.json()), 1)
        self.assertEqual(list_jobs.json()[0]["job_id"], "sync-worker-slack-001")
        self.assertEqual(list_jobs.json()[0]["status"], "succeeded")
        self.assertEqual(list_jobs.json()[0]["stats"]["provider"], "slack")
        self.assertNotIn("scheduled_only", list_jobs.json()[0]["stats"])

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
        self.assertEqual(summary["sources_error"], 0)
        self.assertEqual(summary["principals_mapped"], 1)
        self.assertEqual(summary["grants_total"], 2)
        self.assertEqual(summary["sync_jobs_running"], 0)
        self.assertEqual(summary["missing_foundations"], [])

    def test_shipped_slack_stub_is_registered_for_scheduler_routing_and_readbacks(self) -> None:
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

        self.assertIsInstance(
            self.client.app.state.connector_scheduler.worker_for(ConnectorProvider.SLACK),
            SlackConnectorStubWorker,
        )
        scheduled_at = datetime(2026, 4, 15, 12, 30, tzinfo=UTC)
        tick = self.client.app.state.connector_scheduler.tick(
            tenant_id="tenant-acme",
            evaluated_at=scheduled_at,
        )

        self.assertEqual(len(tick.scheduled), 1)
        self.assertEqual(len(tick.skipped), 0)
        outcome = tick.scheduled[0]
        self.assertEqual(outcome.sync_job.connector_id, "conn-scheduled-slack")
        self.assertEqual(outcome.sync_job.status, SyncJobStatus.SUCCEEDED)
        self.assertEqual(outcome.sync_job.job_type, SyncJobType.FULL)
        self.assertEqual(outcome.sync_job.cursor, "slack:T901:channels:v1")
        self.assertEqual(outcome.sync_job.stats["provider"], "slack")
        self.assertEqual(outcome.sync_job.stats["scheduler"], "fixed_cadence")
        self.assertEqual(outcome.sync_job.stats["sources_written"], 2)
        self.assertEqual(outcome.sync_job.stats["principal_mappings_written"], 1)
        self.assertEqual(outcome.sync_job.stats["permissions_written"], 2)
        self.assertNotIn("scheduled_only", outcome.sync_job.stats)

        connector_readback = self.client.get(
            "/v1/integrations/connectors/conn-scheduled-slack?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(connector_readback.status_code, 200)
        self.assertEqual(
            connector_readback.json()["last_synced_at"],
            "2026-04-15T12:31:00Z",
        )

        list_jobs = self.client.get(
            "/v1/integrations/connectors/conn-scheduled-slack/sync-jobs?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(list_jobs.status_code, 200)
        jobs = list_jobs.json()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "succeeded")
        self.assertEqual(jobs[0]["cursor"], "slack:T901:channels:v1")
        self.assertEqual(jobs[0]["stats"]["provider"], "slack")

        list_sources = self.client.get(
            "/v1/integrations/connectors/conn-scheduled-slack/sources?tenant_id=tenant-acme",
            headers=headers,
        )
        self.assertEqual(list_sources.status_code, 200)
        self.assertEqual(len(list_sources.json()), 2)
        scheduled_sources = {source["source_id"]: source for source in list_sources.json()}
        self.assertEqual(
            scheduled_sources["conn-scheduled-slack-roadmap"]["path"],
            "/channels/roadmap",
        )

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
        self.assertEqual(summary["principals_mapped"], 1)
        self.assertEqual(summary["grants_total"], 2)
        self.assertEqual(summary["sync_jobs_running"], 0)
        self.assertEqual(summary["last_synced_at"], "2026-04-15T12:31:00Z")
        self.assertEqual(summary["missing_foundations"], [])

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
