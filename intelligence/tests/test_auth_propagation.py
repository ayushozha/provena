"""Request-scoped gateway identity propagation stays ephemeral and store-only."""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any, Callable
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import IntelligenceSettings
from app.embeddings import EmbeddingManager
from app.extract_facts import ExtractedFact
from app.main import create_app
from app.model_router import ModelRouter
from app.models import (
    CompactResponse,
    ProjectOverview,
    ReadSearchRequest,
    ReadSearchResponse,
    WriteRequest,
    WriteResponse,
)
from app.overview_generator import OverviewGenerator
from app.read_pipeline import ReadPipeline
from app.write_pipeline import WritePipeline, WritePipelineError

SENTINEL = "auth-propagation-raw-bearer-sentinel"
ACCESS_HEADERS = {
    "Authorization": f"Bearer {SENTINEL}",
    "X-Provena-Tenant-Id": "tenant-a",
    "X-Provena-Role": "editor",
    "X-Provena-Key-Id": "key-a",
    "X-Provena-Principal-Id": "principal-a",
    "X-Provena-Groups": "engineering,platform",
}
EXTRA_HEADERS = {
    "Cookie": "session=must-not-forward",
    "Traceparent": "00-secret-trace",
    "X-Forwarded-For": "203.0.113.1",
    "X-Arbitrary-Secret": "must-not-forward",
}
SCOPE = {"tenant_id": "tenant-a", "project_id": "project-a"}


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        payload: dict[str, Any] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecordingClient:
    calls: list[dict[str, Any]] = []
    responder: Callable[[str, str, dict[str, Any] | None], _Response]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def post(
        self,
        url: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> _Response:
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        return type(self).responder("POST", url, json)

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> _Response:
        self.calls.append({"method": "GET", "url": url, "json": None, "headers": headers})
        return type(self).responder("GET", url, None)

    @classmethod
    def reset(
        cls,
        responder: Callable[[str, str, dict[str, Any] | None], _Response],
    ) -> None:
        cls.calls = []
        cls.responder = responder


def _assert_ephemeral(test: unittest.TestCase, value: Any) -> None:
    test.assertNotIn(SENTINEL, json.dumps(value, default=str))


class TestIngressAuthentication(unittest.TestCase):
    @staticmethod
    def _production_settings(**overrides: Any) -> IntelligenceSettings:
        values = {
            "environment": "production",
            "allow_unauthenticated_local": False,
            "gateway_service_token": "gateway-secret",
            "service_token": "queue-secret",
            "service_tenant_id": "tenant-queue",
            "service_principal_id": "queue-principal",
            "service_role": "editor",
        }
        values.update(overrides)
        return IntelligenceSettings(**values)

    @staticmethod
    def _gateway_headers(role: str = "editor") -> dict[str, str]:
        return {
            "Authorization": "Bearer gateway-secret",
            "X-Provena-Tenant-Id": "tenant-a",
            "X-Provena-Role": role,
            "X-Provena-Key-Id": "key-a",
            "X-Provena-Principal-Id": "principal-a",
        }

    def test_non_local_configuration_fails_without_an_ingress_credential(self) -> None:
        with self.assertRaisesRegex(ValidationError, "GATEWAY_SERVICE_TOKEN"):
            IntelligenceSettings(
                environment="production",
                allow_unauthenticated_local=False,
                gateway_service_token=None,
                service_token=None,
            )
        with self.assertRaisesRegex(ValidationError, "must differ"):
            self._production_settings(
                gateway_service_token="reused-secret",
                service_token="reused-secret",
            )

    def test_production_rejects_missing_invalid_and_incomplete_credentials(self) -> None:
        with TestClient(create_app(self._production_settings())) as client:
            for headers in (
                {},
                {"Authorization": "Bearer wrong-secret"},
                {"Authorization": "Bearer gateway-secret"},
            ):
                response = client.post(
                    "/v1/pipeline/write",
                    content=b"body must not be parsed",
                    headers=headers,
                )
                self.assertEqual(response.status_code, 401, response.text)

    def test_gateway_role_is_authenticated_before_write_authorization(self) -> None:
        class FakeWritePipeline:
            async def process(self, body: Any, access_headers: dict[str, str] | None = None):
                return WriteResponse(created=True, memory={"memory_id": "authenticated"})

        with TestClient(create_app(self._production_settings())) as client:
            client.app.state.write_pipeline = FakeWritePipeline()
            denied = client.post(
                "/v1/pipeline/write",
                json={"content": "viewer cannot write", "scope": SCOPE},
                headers=self._gateway_headers("viewer"),
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            allowed = client.post(
                "/v1/pipeline/write",
                json={"content": "editor can write", "scope": SCOPE},
                headers=self._gateway_headers(),
            )
            self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_queue_service_token_is_bound_to_its_configured_identity(self) -> None:
        settings = self._production_settings()
        base = {
            "Authorization": "Bearer queue-secret",
            "X-Provena-Tenant-Id": "tenant-queue",
            "X-Provena-Role": "editor",
            "X-Provena-Key-Id": "queue-service",
            "X-Provena-Principal-Id": "queue-principal",
        }
        with TestClient(create_app(settings)) as client:
            mismatch = client.post(
                "/v1/model/route",
                json={},
                headers={**base, "X-Provena-Tenant-Id": "tenant-other"},
            )
            self.assertEqual(mismatch.status_code, 403, mismatch.text)
            accepted = client.post("/v1/model/route", json={}, headers=base)
            self.assertEqual(accepted.status_code, 200, accepted.text)


class TestIngressAllowlist(unittest.TestCase):
    def test_all_request_scoped_routes_receive_only_the_fixed_allowlist(self) -> None:
        captured: list[dict[str, str] | None] = []

        class FakeWritePipeline:
            async def process(self, body: Any, access_headers: dict[str, str] | None = None):
                captured.append(access_headers)
                return WriteResponse(created=True, memory={"memory_id": "memory-route"})

            async def _compact_memories(self, **kwargs: Any):
                captured.append(kwargs.get("access_headers"))
                return CompactResponse(compacted_memory={"content": "compact"})

            async def _detect_conflicts(self, **kwargs: Any):
                captured.append(kwargs.get("access_headers"))
                return []

        class FakeReadPipeline:
            async def search(self, body: Any, access_headers: dict[str, str] | None = None):
                captured.append(access_headers)
                return ReadSearchResponse()

        class FakeOverviewGenerator:
            async def generate(self, **kwargs: Any):
                captured.append(kwargs.get("access_headers"))
                return ProjectOverview(tenant_id="tenant-a", project_id="project-a")

        response_texts: list[str] = []
        headers = {**ACCESS_HEADERS, **EXTRA_HEADERS}
        with TestClient(create_app()) as client:
            client.app.state.write_pipeline = FakeWritePipeline()
            client.app.state.read_pipeline = FakeReadPipeline()
            client.app.state.overview_generator = FakeOverviewGenerator()
            requests = [
                ("/v1/pipeline/write", {"content": "route write", "scope": SCOPE}),
                ("/v1/batch-write", [{"content": "route batch", "scope": SCOPE}]),
                ("/v1/pipeline/search", {"query": "route search", "scope": SCOPE}),
                ("/v1/pipeline/compact", {"memory_ids": ["m1", "m2"], "scope": SCOPE}),
                ("/v1/pipeline/conflicts", {"content": "route conflict", "scope": SCOPE}),
                ("/v1/pipeline/overview", {"scope": SCOPE}),
            ]
            for path, payload in requests:
                response = client.post(path, json=payload, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                response_texts.append(response.text)

        self.assertEqual(len(captured), 6)
        self.assertTrue(all(item == ACCESS_HEADERS for item in captured))
        _assert_ephemeral(self, response_texts)


class TestStoreOnlyPropagation(unittest.TestCase):
    def test_search_forwards_headers_to_store_but_not_orchestration_or_state(self) -> None:
        def responder(method: str, url: str, payload: dict[str, Any] | None) -> _Response:
            if url.endswith("/trigger/lookup"):
                return _Response(payload={"hits": []})
            if url.endswith("/v1/memories/search"):
                return _Response(payload={"results": []})
            if url.endswith("/budget"):
                return _Response(payload={"selected_memory_ids": [], "memory_tokens": 0})
            return _Response(status_code=404)

        _RecordingClient.reset(responder)
        pipeline = ReadPipeline(
            EmbeddingManager(),
            ModelRouter(),
            store_url="http://store.test",
            orchestration_url="http://orchestration.test",
        )
        with patch("app.read_pipeline.httpx.AsyncClient", _RecordingClient):
            result = asyncio.run(
                pipeline.search(
                    request=ReadSearchRequest(
                        query="authorization propagation",
                        scope=SCOPE,
                    ),
                    access_headers=ACCESS_HEADERS,
                )
            )

        store_calls = [call for call in _RecordingClient.calls if "store.test" in call["url"]]
        other_calls = [call for call in _RecordingClient.calls if "store.test" not in call["url"]]
        self.assertEqual([call["headers"] for call in store_calls], [ACCESS_HEADERS])
        self.assertTrue(other_calls)
        self.assertTrue(all(call["headers"] is None for call in other_calls))
        _assert_ephemeral(self, result.model_dump())
        _assert_ephemeral(self, vars(pipeline))

    def test_write_conflict_preserves_explicit_id_and_headers(self) -> None:
        def responder(method: str, url: str, payload: dict[str, Any] | None) -> _Response:
            if url.endswith("/v1/memories/search"):
                return _Response(payload={"results": []})
            if url.endswith("/v1/memories"):
                return _Response(status_code=409, text="UNIQUE constraint")
            return _Response(status_code=404)

        class OneFactExtractor:
            async def extract(self, content: str, title: str = "") -> list[ExtractedFact]:
                return [ExtractedFact(content=content, title=title)]

        _RecordingClient.reset(responder)
        pipeline = WritePipeline(
            EmbeddingManager(),
            ModelRouter(),
            store_url="http://store.test",
            orchestration_url="http://orchestration.test",
            fact_extractor=OneFactExtractor(),  # type: ignore[arg-type]
        )
        request = WriteRequest(
            memory_id="deterministic-memory",
            content="One request-scoped authorization fact.",
            scope=SCOPE,
        )
        with patch("app.write_pipeline.httpx.AsyncClient", _RecordingClient):
            with self.assertRaises(WritePipelineError) as error:
                asyncio.run(pipeline.process(request, access_headers=ACCESS_HEADERS))

        store_calls = [call for call in _RecordingClient.calls if "store.test" in call["url"]]
        writes = [call for call in store_calls if call["url"].endswith("/v1/memories")]
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(len(store_calls), 2)
        self.assertTrue(all(call["headers"] == ACCESS_HEADERS for call in store_calls))
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["json"]["memory_id"], "deterministic-memory")
        _assert_ephemeral(self, vars(pipeline))

    def test_compaction_headers_stop_before_the_model_call(self) -> None:
        def responder(method: str, url: str, payload: dict[str, Any] | None) -> _Response:
            memory_id = url.rsplit("/", 1)[-1]
            return _Response(payload={"memory_id": memory_id, "content": f"body {memory_id}"})

        class FakeLLM:
            enabled = True

            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def chat_json(self, **kwargs: Any) -> dict[str, str]:
                self.calls.append(kwargs)
                return {"summary": "compacted body", "title": "Compacted"}

        llm = FakeLLM()
        _RecordingClient.reset(responder)
        pipeline = WritePipeline(
            EmbeddingManager(),
            ModelRouter(),
            store_url="http://store.test",
            llm=llm,  # type: ignore[arg-type]
        )
        with patch("app.write_pipeline.httpx.AsyncClient", _RecordingClient):
            result = asyncio.run(
                pipeline._compact_memories(
                    ["memory-1", "memory-2"],
                    SCOPE,
                    access_headers=ACCESS_HEADERS,
                )
            )

        self.assertEqual(len(_RecordingClient.calls), 2)
        self.assertTrue(all(call["method"] == "GET" for call in _RecordingClient.calls))
        self.assertTrue(all(call["headers"] == ACCESS_HEADERS for call in _RecordingClient.calls))
        _assert_ephemeral(self, llm.calls)
        _assert_ephemeral(self, result.model_dump())

    def test_overview_forwards_headers_to_store_search_and_snapshot_only(self) -> None:
        def responder(method: str, url: str, payload: dict[str, Any] | None) -> _Response:
            if url.endswith("/v1/memories/search"):
                return _Response(
                    payload={
                        "results": [
                            {
                                "memory": {
                                    "memory_id": "memory-overview",
                                    "kind": "decision",
                                    "title": "Keep store authoritative",
                                    "entity_keys": ["Provena"],
                                }
                            }
                        ]
                    }
                )
            return _Response(payload={"ok": True})

        _RecordingClient.reset(responder)
        generator = OverviewGenerator(store_url="http://store.test")
        pipeline = WritePipeline(
            EmbeddingManager(),
            ModelRouter(),
            overview_generator=generator,
            store_url="http://store.test",
            orchestration_url="http://orchestration.test",
        )
        with (
            patch("app.overview_generator.httpx.AsyncClient", _RecordingClient),
            patch("app.write_pipeline.httpx.AsyncClient", _RecordingClient),
        ):
            updated = asyncio.run(
                pipeline._update_overview(SCOPE, access_headers=ACCESS_HEADERS)
            )

        self.assertTrue(updated)
        store_calls = [call for call in _RecordingClient.calls if "store.test" in call["url"]]
        orchestration_calls = [
            call for call in _RecordingClient.calls if "orchestration.test" in call["url"]
        ]
        self.assertEqual(len(store_calls), 2)
        self.assertTrue(all(call["headers"] == ACCESS_HEADERS for call in store_calls))
        self.assertEqual(len(orchestration_calls), 1)
        self.assertIsNone(orchestration_calls[0]["headers"])
        _assert_ephemeral(self, vars(generator))
        _assert_ephemeral(self, vars(pipeline))


if __name__ == "__main__":
    unittest.main()
