"""Store behaviour a loopback local-store gateway relies on.

The gateway rebuilds the X-Provena-* headers from the verified key, so these
tests send exactly what it would send for an editor key scoped to one tenant:
first through the explicit local bypass (no transport bearer, as the existing
API tests do), then through the deployed shape where the store refuses every
``/v1`` request that does not carry the gateway's private transport bearer.
"""

import importlib
import os
import shutil
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


def _key_headers(tenant_id: str, principal_id: str = "person-1") -> dict[str, str]:
    return {
        "X-Provena-Tenant-Id": tenant_id,
        "X-Provena-Role": "editor",
        "X-Provena-Principal-Id": principal_id,
        "X-Provena-Key-Id": "valcairn-local-memory",
    }


def _reload_app() -> TestClient:
    import app.config
    import app.main

    app.config.get_settings.cache_clear()
    importlib.reload(app.config)
    importlib.reload(app.main)
    return TestClient(app.main.create_app())


class LocalStoreModeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = Path(__file__).resolve().parent.parent / ".tmp-e2e" / "test-db"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_root / f"local-{int(time.time() * 1000)}-{os.getpid()}"
        self.temp_dir.mkdir()
        os.environ["PROVENA_DB_PATH"] = str(self.temp_dir / "provena-local-test.db")
        self.client = _reload_app()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.environ.pop("PROVENA_DB_PATH", None)

    def _note(self, tenant_id: str, content: str, title: str = "QA note", **extra: object) -> dict:
        return {
            "kind": "fact",
            "memory_layer": "user",
            "scope": {"tenant_id": tenant_id, "workspace_id": "ws-qa", "user_id": "user-qa"},
            "title": title,
            "content": content,
            "source_references": [{"source_type": "note", "source_id": "note:qa-1", "title": title}],
            **extra,
        }

    def test_editor_key_scope_is_enforced_by_the_store_not_the_client(self) -> None:
        mine = _key_headers("valcairn-local")
        other = _key_headers("valcairn-other", principal_id="person-2")
        created = self.client.post("/v1/memories", json=self._note("valcairn-local", "SYNTHETIC QA: the lab meets on Tuesdays."), headers=mine)
        self.assertEqual(created.status_code, 200, created.text)
        memory_id = created.json()["memory"]["memory_id"]

        # Naming another tenant's scope with my key: the write is refused.
        foreign_write = self.client.post("/v1/memories", json=self._note("valcairn-other", "SYNTHETIC QA: should never be written."), headers=mine)
        self.assertEqual(foreign_write.status_code, 403, foreign_write.text)

        # Another key naming my scope: the search is refused outright (tenant mismatch), and nothing is readable,
        # editable or deletable by id.
        search = {"query": "Tuesdays", "scope": {"tenant_id": "valcairn-local"}, "limit": 5}
        self.assertEqual(self.client.post("/v1/memories/search", json=search, headers=other).status_code, 403)
        self.assertEqual(self.client.get(f"/v1/memories/{memory_id}", headers=other).status_code, 404)
        self.assertEqual(self.client.get(f"/v1/memories/{memory_id}/history", headers=other).status_code, 404)
        self.assertEqual(self.client.put(f"/v1/memories/{memory_id}", json={"content": "x"}, headers=other).status_code, 404)
        self.assertEqual(self.client.delete(f"/v1/memories/{memory_id}", headers=other).status_code, 404)

        # The owner still finds it, and the result explains itself without a fabricated confidence.
        results = self.client.post("/v1/memories/search", json=search, headers=mine).json()["results"]
        self.assertEqual([r["memory"]["memory_id"] for r in results], [memory_id])
        self.assertIn("content match", results[0]["reasons"])

    def test_empty_query_lists_recent_memories_and_soft_delete_keeps_history(self) -> None:
        headers = _key_headers("valcairn-local")
        ids = []
        for n in range(3):
            res = self.client.post("/v1/memories", json=self._note("valcairn-local", f"SYNTHETIC QA note number {n}.", title=f"Note {n}"), headers=headers)
            self.assertEqual(res.status_code, 200, res.text)
            ids.append(res.json()["memory"]["memory_id"])

        listing = self.client.post("/v1/memories/search", json={"query": "", "scope": {"tenant_id": "valcairn-local"}, "limit": 100, "include_relations": False}, headers=headers)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual({r["memory"]["memory_id"] for r in listing.json()["results"]}, set(ids))

        deleted = self.client.delete(f"/v1/memories/{ids[0]}", headers=headers)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse(deleted.json()["hard_delete"])

        after = self.client.post("/v1/memories/search", json={"query": "note", "scope": {"tenant_id": "valcairn-local"}, "limit": 100}, headers=headers)
        self.assertEqual({r["memory"]["memory_id"] for r in after.json()["results"]}, set(ids[1:]))
        record = self.client.get(f"/v1/memories/{ids[0]}", headers=headers)
        self.assertEqual(record.status_code, 200, record.text)
        self.assertEqual(record.json()["status"], "deleted")
        # Same-second events are not ordered among themselves by the store, so compare the set.
        events = sorted(event["event"] for event in self.client.get(f"/v1/memories/{ids[0]}/history", headers=headers).json())
        self.assertEqual(events, ["add", "delete"])

    def test_superseding_marks_the_old_note_and_history_stays_attributed(self) -> None:
        headers = _key_headers("valcairn-local")
        first = self.client.post("/v1/memories", json=self._note("valcairn-local", "SYNTHETIC QA: the lab meets on Tuesdays."), headers=headers).json()["memory"]
        second = self.client.post("/v1/memories", json=self._note("valcairn-local", "SYNTHETIC QA: the lab now meets on Wednesdays.", supersedes_memory_id=first["memory_id"]), headers=headers)
        self.assertEqual(second.status_code, 200, second.text)

        old = self.client.get(f"/v1/memories/{first['memory_id']}", headers=headers).json()
        self.assertEqual(old["status"], "superseded")
        history = self.client.get(f"/v1/memories/{first['memory_id']}/history", headers=headers).json()
        self.assertEqual(history[-1]["event"], "add")
        self.assertEqual(history[-1]["actor_id"], "person-1")
        results = self.client.post("/v1/memories/search", json={"query": "lab meets", "scope": {"tenant_id": "valcairn-local"}, "limit": 5}, headers=headers).json()["results"]
        by_id = {r["memory"]["memory_id"]: r for r in results}
        self.assertEqual(by_id[second.json()["memory"]["memory_id"]]["memory"]["status"], "active")
        self.assertIn("superseded memory", by_id[first["memory_id"]]["reasons"])

    def test_a_bound_key_is_confined_to_its_workspace_and_person_by_the_store(self) -> None:
        # Same tenant, same principal, same role: only the bound scope differs. Nothing in sessionStorage or
        # a search filter is involved; the store itself must hold the line, including for legacy rows.
        unbound = _key_headers("valcairn-local")
        bound_a = {**unbound, "X-Provena-Scope-Workspace-Id": "ws-A", "X-Provena-Scope-User-Id": "user-1"}
        bound_b = {**unbound, "X-Provena-Scope-Workspace-Id": "ws-B", "X-Provena-Scope-User-Id": "user-1"}

        def note(workspace_id: str | None, content: str, **extra: object) -> dict:
            scope = {"tenant_id": "valcairn-local", "user_id": "user-1"}
            if workspace_id:
                scope["workspace_id"] = workspace_id
            return {"kind": "fact", "memory_layer": "user", "scope": scope, "title": "QA", "content": content, **extra}

        in_a = self.client.post("/v1/memories", json=note("ws-A", "SYNTHETIC QA: note in workspace A."), headers=bound_a)
        self.assertEqual(in_a.status_code, 200, in_a.text)
        id_a = in_a.json()["memory"]["memory_id"]
        legacy = self.client.post("/v1/memories", json=note(None, "SYNTHETIC QA: legacy note with no workspace."), headers=unbound)
        self.assertEqual(legacy.status_code, 200, legacy.text)
        id_legacy = legacy.json()["memory"]["memory_id"]

        # Creating outside the bound scope, even naming the key's own tenant and person, is refused.
        self.assertEqual(self.client.post("/v1/memories", json=note("ws-B", "SYNTHETIC QA: wrong workspace."), headers=bound_a).status_code, 403)
        self.assertEqual(self.client.post("/v1/memories", json=note(None, "SYNTHETIC QA: no workspace."), headers=bound_a).status_code, 403)

        # Direct-id access, history, update, delete and supersession from the other workspace all fail closed.
        for memory_id in (id_a, id_legacy):
            self.assertEqual(self.client.get(f"/v1/memories/{memory_id}", headers=bound_b).status_code, 404, memory_id)
            self.assertEqual(self.client.get(f"/v1/memories/{memory_id}/history", headers=bound_b).status_code, 404, memory_id)
            self.assertEqual(self.client.put(f"/v1/memories/{memory_id}", json={"content": "x"}, headers=bound_b).status_code, 404, memory_id)
            self.assertEqual(self.client.delete(f"/v1/memories/{memory_id}", headers=bound_b).status_code, 404, memory_id)
        successor = self.client.post("/v1/memories", json=note("ws-B", "SYNTHETIC QA: successor in B.", supersedes_memory_id=id_a), headers=bound_b)
        self.assertEqual(successor.status_code, 403, successor.text)
        self.assertEqual(self.client.get(f"/v1/memories/{id_a}", headers=bound_a).json()["status"], "active")

        # Searches from the other workspace find nothing, however the scope is named; the legacy row is invisible to both.
        for scope in ({"tenant_id": "valcairn-local"}, {"tenant_id": "valcairn-local", "workspace_id": "ws-A"}, {"tenant_id": "valcairn-local", "user_id": "user-1"}):
            found = self.client.post("/v1/memories/search", json={"query": "note", "scope": scope, "limit": 10}, headers=bound_b).json()["results"]
            self.assertEqual(found, [], scope)
        mine = self.client.post("/v1/memories/search", json={"query": "note", "scope": {"tenant_id": "valcairn-local"}, "limit": 10}, headers=bound_a).json()["results"]
        self.assertEqual([r["memory"]["memory_id"] for r in mine], [id_a])

        # The bound key still works normally inside its scope.
        self.assertEqual(self.client.get(f"/v1/memories/{id_a}", headers=bound_a).status_code, 200)
        self.assertEqual(self.client.put(f"/v1/memories/{id_a}", json={"content": "SYNTHETIC QA: note in workspace A, edited."}, headers=bound_a).status_code, 200)
        self.assertEqual(self.client.delete(f"/v1/memories/{id_a}", headers=bound_a).json()["deleted"], True)


class PrivateStoreBoundaryTests(unittest.TestCase):
    """With PROVENA_ALLOWED_HOSTS set, the trusted-header store answers only its exact hosts and never a browser."""

    def setUp(self) -> None:
        temp_root = Path(__file__).resolve().parent.parent / ".tmp-e2e" / "test-db"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_root / f"private-{int(time.time() * 1000)}-{os.getpid()}"
        self.temp_dir.mkdir()
        os.environ["PROVENA_DB_PATH"] = str(self.temp_dir / "provena-private-test.db")
        os.environ["PROVENA_ALLOWED_HOSTS"] = "127.0.0.1:8700, localhost:8700"
        self.client = _reload_app()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.environ.pop("PROVENA_DB_PATH", None)
        os.environ.pop("PROVENA_ALLOWED_HOSTS", None)
        import app.config

        app.config.get_settings.cache_clear()

    def test_wrong_host_and_any_origin_are_refused_before_identity_is_read(self) -> None:
        payload = {"kind": "fact", "scope": {"tenant_id": "valcairn-local"}, "content": "SYNTHETIC QA: must not be written."}
        forged = _key_headers("valcairn-local")
        for host in ("evil.example", "127.0.0.1", "127.0.0.1:8701", "rebound.example:8700"):
            response = self.client.post("/v1/memories", json=payload, headers={**forged, "Host": host})
            self.assertEqual(response.status_code, 421, host)
        for origin in ("http://localhost:3000", "null", "http://127.0.0.1:8700"):
            response = self.client.post("/v1/memories", json=payload, headers={**forged, "Host": "127.0.0.1:8700", "Origin": origin})
            self.assertEqual(response.status_code, 403, origin)
        self.assertEqual(self.client.get("/healthz", headers={"Host": "localhost:8700", "Origin": "http://localhost:3000"}).status_code, 403)
        self.assertEqual(self.client.get("/readyz", headers={"Host": "evil.example:8700"}).status_code, 421)
        listing = self.client.post("/v1/memories/search", json={"query": "written", "scope": {"tenant_id": "valcairn-local"}}, headers={**forged, "Host": "127.0.0.1:8700"})
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["results"], [])

    def test_the_gateway_path_still_works_with_the_same_configuration(self) -> None:
        # What the gateway sends: an allowed Host, verified identity headers, no Origin.
        headers = {**_key_headers("valcairn-local"), "X-Provena-Scope-Workspace-Id": "ws-A", "X-Provena-Scope-User-Id": "user-1", "Host": "127.0.0.1:8700"}
        payload = {"kind": "fact", "memory_layer": "user", "scope": {"tenant_id": "valcairn-local", "workspace_id": "ws-A", "user_id": "user-1"}, "content": "SYNTHETIC QA: through the gateway."}
        created = self.client.post("/v1/memories", json=payload, headers=headers)
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(self.client.get("/healthz", headers={"Host": "LOCALHOST:8700"}).status_code, 200)


class GatewayCredentialLocalStoreTests(unittest.TestCase):
    """The deployed shape: the local bypass is off, every /v1 request needs the gateway's transport bearer, and the
    identity and bound-scope headers behind that bearer are what the store confines the key to."""

    ENV_KEYS = (
        "PROVENA_DB_PATH",
        "PROVENA_ENVIRONMENT",
        "PROVENA_ALLOW_UNAUTHENTICATED_LOCAL",
        "PROVENA_GATEWAY_SERVICE_TOKEN",
        "PROVENA_ALLOWED_HOSTS",
    )
    TRANSPORT_TOKEN = "-".join(("qa", "gateway", "transport", "bearer"))

    def setUp(self) -> None:
        self.original_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        temp_root = Path(__file__).resolve().parent.parent / ".tmp-e2e" / "test-db"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_root / f"credential-{int(time.time() * 1000)}-{os.getpid()}"
        self.temp_dir.mkdir()
        os.environ.update(
            {
                "PROVENA_DB_PATH": str(self.temp_dir / "provena-credential-test.db"),
                "PROVENA_ENVIRONMENT": "local",
                "PROVENA_ALLOW_UNAUTHENTICATED_LOCAL": "false",
                "PROVENA_GATEWAY_SERVICE_TOKEN": self.TRANSPORT_TOKEN,
                "PROVENA_ALLOWED_HOSTS": "127.0.0.1:8700",
            }
        )
        self.client = _reload_app()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _reload_app()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _gateway_headers(self, workspace_id: str, principal_id: str = "person-1") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.TRANSPORT_TOKEN}",
            "Host": "127.0.0.1:8700",
            **_key_headers("valcairn-local", principal_id),
            "X-Provena-Scope-Workspace-Id": workspace_id,
            "X-Provena-Scope-User-Id": "user-1",
        }

    @staticmethod
    def _note(workspace_id: str, content: str, **extra: object) -> dict:
        return {
            "kind": "fact",
            "memory_layer": "user",
            "scope": {"tenant_id": "valcairn-local", "workspace_id": workspace_id, "user_id": "user-1"},
            "title": "QA",
            "content": content,
            **extra,
        }

    def test_identity_headers_count_for_nothing_without_the_gateway_bearer(self) -> None:
        payload = self._note("ws-A", "SYNTHETIC QA: must not be written.")
        forged = {k: v for k, v in self._gateway_headers("ws-A").items() if k != "Authorization"}
        self.assertEqual(self.client.post("/v1/memories", json=payload, headers=forged).status_code, 401)
        self.assertEqual(self.client.post("/v1/memories", json=payload, headers={**forged, "Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/v1/memories/search", json={"query": "", "scope": {"tenant_id": "valcairn-local"}}, headers=forged).status_code, 401)
        # The bearer alone is not an identity either: the gateway must name the key behind it.
        self.assertEqual(self.client.post("/v1/memories", json=payload, headers={"Authorization": f"Bearer {self.TRANSPORT_TOKEN}", "Host": "127.0.0.1:8700"}).status_code, 401)
        self.assertEqual(self.client.get("/healthz", headers={"Host": "127.0.0.1:8700"}).status_code, 200)

    def test_bound_scope_behind_the_bearer_is_confined_by_the_store(self) -> None:
        bound_a, bound_b = self._gateway_headers("ws-A"), self._gateway_headers("ws-B")
        in_a = self.client.post("/v1/memories", json=self._note("ws-A", "SYNTHETIC QA: note in workspace A."), headers=bound_a)
        self.assertEqual(in_a.status_code, 200, in_a.text)
        id_a = in_a.json()["memory"]["memory_id"]
        self.assertEqual([entry["principal_id"] for entry in in_a.json()["memory"]["acl"]], ["person-1"])

        self.assertEqual(self.client.post("/v1/memories", json=self._note("ws-B", "SYNTHETIC QA: wrong workspace."), headers=bound_a).status_code, 403)
        self.assertEqual(self.client.get(f"/v1/memories/{id_a}", headers=bound_b).status_code, 404)
        self.assertEqual(self.client.get(f"/v1/memories/{id_a}/history", headers=bound_b).status_code, 404)
        self.assertEqual(self.client.put(f"/v1/memories/{id_a}", json={"content": "x"}, headers=bound_b).status_code, 404)
        self.assertEqual(self.client.delete(f"/v1/memories/{id_a}", headers=bound_b).status_code, 404)
        successor = self.client.post("/v1/memories", json=self._note("ws-B", "SYNTHETIC QA: successor in B.", supersedes_memory_id=id_a), headers=bound_b)
        self.assertEqual(successor.status_code, 403, successor.text)
        for scope in ({"tenant_id": "valcairn-local"}, {"tenant_id": "valcairn-local", "workspace_id": "ws-A"}):
            self.assertEqual(self.client.post("/v1/memories/search", json={"query": "note", "scope": scope, "limit": 10}, headers=bound_b).json()["results"], [], scope)

        mine = self.client.post("/v1/memories/search", json={"query": "note", "scope": {"tenant_id": "valcairn-local"}, "limit": 10}, headers=bound_a).json()["results"]
        self.assertEqual([r["memory"]["memory_id"] for r in mine], [id_a])
        self.assertEqual(self.client.get(f"/v1/memories/{id_a}/history", headers=bound_a).json()[-1]["actor_id"], "person-1")
        deleted = self.client.delete(f"/v1/memories/{id_a}", headers=bound_a)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse(deleted.json()["hard_delete"])
        self.assertEqual(self.client.get(f"/v1/memories/{id_a}", headers=bound_a).json()["status"], "deleted")

    def test_browser_and_wrong_host_are_refused_even_with_the_bearer(self) -> None:
        headers = self._gateway_headers("ws-A")
        payload = self._note("ws-A", "SYNTHETIC QA: must not be written.")
        self.assertEqual(self.client.post("/v1/memories", json=payload, headers={**headers, "Origin": "http://localhost:3000"}).status_code, 403)
        self.assertEqual(self.client.post("/v1/memories", json=payload, headers={**headers, "Host": "localhost:8700"}).status_code, 421)
        self.assertEqual(self.client.post("/v1/memories/search", json={"query": "written", "scope": {"tenant_id": "valcairn-local"}}, headers=headers).json()["results"], [])
