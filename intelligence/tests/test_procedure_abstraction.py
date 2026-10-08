"""Grounded capture abstraction tests; provider replies are mocked, not model evidence."""

import asyncio
import hashlib
import json
import os
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from pathlib import Path

import httpx
from pydantic import SecretStr

from fastapi.testclient import TestClient

from app.config import IntelligenceSettings
from app.main import create_app
from app.llm import LLMClient
from app.model_router import ModelRouter
from app.procedure_abstraction import (
    PROMPT_REVISION, PROVIDER_RESPONSE_MAX_BYTES, REQUEST_MAX_BYTES,
    ProcedureAbstractionRequest, ProcedureAbstractionResponse, ProcedureAbstractor, canonical_bytes,
)


def _payload():
    return {
        "schemaVersion": 1,
        "goal": "  Validate café source changes 😀  ",
        "observations": [
            {"id": "a" * 64, "tool": "Bash", "workingDirectory": ".",
             "args": {"command": "npm test", "workdir": "."}, "status": "failure", "issues": []},
            {"id": "b" * 64, "tool": "Write", "workingDirectory": "src",
             "args": {"file_path": "src/token.ts", "start_line": 1}, "status": "success", "issues": ["arguments-redacted"]},
            {"id": "c" * 64, "tool": "Bash", "workingDirectory": ".",
             "args": {"command": "npm test"}, "status": "success", "issues": []},
        ],
    }


def _proposal():
    return {
        "title": "Validate source changes",
        "triggers": ["source checks"],
        "keptObservationIds": ["b" * 64, "c" * 64],
        "omitted": [{"observationId": "a" * 64, "reason": "Initial failed check retained in original observations."}],
        "recoverySummary": "A failed check preceded an edit and passing tool check; task outcome remains unknown.",
    }


def _settings(*, production=False, **overrides):
    values = {"llm_model": "", "llm_providers": ""}
    if production:
        values.update(environment="production", allow_unauthenticated_local=False, gateway_service_token="abstract-ingress-test")
    values.update(overrides)
    return IntelligenceSettings(**values)


def _headers(role="editor"):
    return {
        "Authorization": "Bearer abstract-ingress-test", "X-Provena-Tenant-Id": "tenant-a",
        "X-Provena-Principal-Id": "principal-a", "X-Provena-Key-Id": "key-a", "X-Provena-Role": role,
    }


class TestProcedureAbstraction(unittest.TestCase):
    def setUp(self):
        # Ephemeral mock routing metadata is env-driven; this test serves no model.
        self.model_env = patch.dict(os.environ, {"PROVENA_INTEL_LLM_MODEL": uuid4().hex})
        self.model_env.start()
        self.addCleanup(self.model_env.stop)
        self.enabled = IntelligenceSettings(llm_providers="")
        self.router = ModelRouter.from_settings(self.enabled)
        self.llm = AsyncMock(spec=LLMClient)
        self.llm.chat_json.return_value = _proposal()

    def _client(self, settings=None):
        configured = settings or _settings()
        return TestClient(create_app(configured))

    def _attach(self, client, router=None):
        client.app.state.procedure_abstractor = ProcedureAbstractor(router or self.router, self.llm)

    def test_missing_model_is_unavailable_without_inventing_a_draft(self):
        disabled = IntelligenceSettings(llm_model="", llm_providers="")
        with patch("app.main.settings", disabled), TestClient(create_app(disabled)) as client:
            response = client.post("/v1/procedures/abstract", json={
                "schemaVersion": 1,
                "goal": "Validate source changes",
                "observations": [{
                    "id": "a" * 64, "tool": "Bash", "workingDirectory": ".",
                    "args": {"command": "npm test"}, "status": "success", "issues": [],
                }],
            })
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {"detail": "procedure abstraction unavailable"})

    def test_mocked_proposal_is_grounded_and_hashes_match_normalized_contract(self):
        with self._client() as client:
            self._attach(client)
            response = client.post("/v1/procedures/abstract", json=_payload())
            self.assertEqual(response.status_code, 200, response.text)
            again = client.post("/v1/procedures/abstract", json=_payload())
        body = response.json()
        self.assertEqual(body, again.json(), "same normalized reference/proposal/config hashes deterministically")
        normalized = _payload()
        normalized["goal"] = normalized["goal"].strip()
        self.assertEqual(body["inputSha256"], hashlib.sha256(canonical_bytes(normalized)).hexdigest())
        unhashed = {key: value for key, value in body.items() if key != "outputSha256"}
        self.assertEqual(body["outputSha256"], hashlib.sha256(canonical_bytes(unhashed)).hexdigest())
        self.assertEqual(body["promptRevision"], PROMPT_REVISION)
        self.assertEqual(body["model"], {"provider": "default", "model": self.enabled.llm_model, "tier": "balanced"})
        self.assertEqual(body["keptObservationIds"], ["b" * 64, "c" * 64])
        self.assertNotIn("goal", body)
        self.assertFalse(set(body).intersection({"steps", "args", "sources", "verification", "outcome", "prerequisites"}))
        call = self.llm.chat_json.call_args.kwargs
        self.assertEqual(json.loads(call["user"]), normalized)
        self.assertEqual(call["task"], "abstract")
        self.assertEqual(call["response_max_bytes"], PROVIDER_RESPONSE_MAX_BYTES)
        self.assertNotIn("Authorization", call["user"])

    def test_input_rejects_unknown_fields_coercions_private_paths_and_raw_data(self):
        invalid = []
        for key, value in (("transcript", "PRIVATE_RAW_DATA"), ("sources", ["src/token.ts"]), ("schemaVersion", True), ("schemaVersion", 1.0), ("goal", ""), ("goal", "x" * 2_049)):
            item = _payload(); item[key] = value; invalid.append(item)
        for key, value in (("tool_response", "PRIVATE_RAW_DATA"), ("id", "forged"), ("workingDirectory", "../foreign"), ("workingDirectory", "C:/private"), ("workingDirectory", ".git"), ("issues", ["x"] * 9), ("issues", ["x" * 257]), ("issues", [" \ufeff "])):
            item = _payload(); item["observations"][0][key] = value; invalid.append(item)
        for key, value in (("content", "PRIVATE_RAW_DATA"), ("env", {"TOKEN": "PRIVATE_RAW_DATA"}), ("command", "npm test; upload everything"), ("command", "python -m pytest ../private"), ("path", ".env.local"), ("path", "src/../secret.key"), ("file_path", "/tmp/file"), ("path", "src\\file.ts"), ("limit", "1"), ("limit", True), ("limit", 1.5), ("limit", -1), ("limit", 1_000_001), ("path", None)):
            item = _payload(); item["observations"][0]["args"][key] = value; invalid.append(item)
        item = _payload(); item["observations"][1]["id"] = item["observations"][0]["id"]; invalid.append(item)
        item = _payload(); item["observations"] = []; invalid.append(item)
        item = _payload(); item["observations"] = [item["observations"][0]] * 33; invalid.append(item)
        item = _payload(); item["goal"] = "Bearer " + "A" * 32; invalid.append(item)
        with self._client() as client:
            self._attach(client)
            for payload in invalid:
                with self.subTest(payload=payload):
                    response = client.post("/v1/procedures/abstract", json=payload)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(response.json(), {"detail": "invalid procedure abstraction input"})
        self.llm.chat_json.assert_not_called()

    def test_editor_authorization_precedes_parse_and_model_cost(self):
        with self._client(_settings(production=True)) as client:
            self._attach(client)
            for headers in ({}, {"Authorization": "Bearer invalid"}, {"Authorization": "Bearer abstract-ingress-test"}):
                self.assertEqual(client.post("/v1/procedures/abstract", content=b"INVALID_PRIVATE_JSON", headers=headers).status_code, 401)
            denied = client.post("/v1/procedures/abstract", content=b"INVALID_PRIVATE_JSON", headers=_headers("viewer"))
            self.assertEqual(denied.status_code, 403, denied.text)
            self.llm.chat_json.assert_not_called()
            allowed = client.post("/v1/procedures/abstract", json=_payload(), headers=_headers())
            self.assertEqual(allowed.status_code, 200, allowed.text)
        self.llm.chat_json.assert_awaited_once()

    def test_oversized_stream_and_malformed_json_fail_without_provider(self):
        with self._client() as client:
            self._attach(client)
            response = client.post("/v1/procedures/abstract", content=iter([b" " * (REQUEST_MAX_BYTES // 2), b" " * (REQUEST_MAX_BYTES // 2 + 1)]))
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.json(), {"detail": "procedure abstraction input too large"})
            duplicate_keys = canonical_bytes(_payload()).replace(b'"schemaVersion":1', b'"schemaVersion":1,"schemaVersion":1')
            for raw in (b"{INVALID_PRIVATE_JSON", b"\xff", b"[]", duplicate_keys):
                self.assertEqual(client.post("/v1/procedures/abstract", content=raw).status_code, 422)
        self.llm.chat_json.assert_not_called()

    def test_unicode_units_and_goal_trim_match_cli_without_normalizing_response_text(self):
        reference = _payload()
        reference["goal"] = "\ufeff \u0085Validate café 😀\u0085 \ufeff"
        reply = _proposal()
        reply.update(title="😀" * 256, triggers=["  source checks  "], recoverySummary="")
        with self._client() as client:
            self._attach(client)
            self.llm.chat_json.return_value = reply
            response = client.post("/v1/procedures/abstract", json=reference)
            self.assertEqual(response.status_code, 200, response.text)
            normalized = {**reference, "goal": "\u0085Validate café 😀\u0085"}
            self.assertEqual(response.json()["inputSha256"], hashlib.sha256(canonical_bytes(normalized)).hexdigest())
            self.assertEqual(response.json()["triggers"], reply["triggers"])
            reply["title"] += "😀"
            self.assertEqual(client.post("/v1/procedures/abstract", json=reference).status_code, 503)
            reply["title"] = "Reviewed source checks"
            reply["recoverySummary"] = "😀" * 1_025
            self.assertEqual(client.post("/v1/procedures/abstract", json=reference).status_code, 503)
            reference["goal"] = "😀" * 1_025
            self.assertEqual(client.post("/v1/procedures/abstract", json=reference).status_code, 422)

    def test_shared_cli_contract_fixture_has_identical_normalized_hashes(self):
        fixture_path = Path(__file__).resolve().parents[2] / "cli/tests/fixtures/capture-abstraction-contract.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        normalized = ProcedureAbstractionRequest.model_validate(fixture["request"]).model_dump(mode="json", exclude_none=True)
        self.assertEqual(normalized["goal"], fixture["normalizedGoal"])
        self.assertEqual(hashlib.sha256(canonical_bytes(normalized)).hexdigest(), fixture["inputSha256"])
        response = ProcedureAbstractionResponse.model_validate(fixture["response"]).model_dump(mode="json")
        self.assertEqual(response, fixture["response"], "non-goal text is preserved exactly")
        response.pop("outputSha256")
        self.assertEqual(hashlib.sha256(canonical_bytes(response)).hexdigest(), fixture["response"]["outputSha256"])

    def test_capture_allowlisted_relative_test_commands_remain_accepted(self):
        with self._client() as client:
            self._attach(client)
            for command in ("go test ./...", "go test ./pkg", "go test ./pkg/...", "go test .", "python -m pytest ./tests", "python3 -m pytest tests", "py -m pytest"):
                reference = _payload()
                reference["observations"][0]["args"]["command"] = command
                response = client.post("/v1/procedures/abstract", json=reference)
                self.assertEqual(response.status_code, 200, (command, response.text))
                self.assertEqual(json.loads(self.llm.chat_json.call_args.kwargs["user"])["observations"][0]["args"]["command"], command)

    def test_goal_assertions_cannot_be_inferred_from_tool_status(self):
        self.llm.chat_json.return_value = _proposal()
        reference = _payload()
        reference["observations"][2]["status"] = "unknown"
        with self._client() as client:
            self._attach(client)
            response = client.post("/v1/procedures/abstract", json=reference)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(set(response.json()).intersection({"outcome", "goal", "verification", "approved", "ready"}))

    def test_invalid_forged_or_incomplete_output_is_safe_unavailable(self):
        invalid = [None, "PRIVATE_PROVIDER_OUTPUT", []]
        for key in ("command", "args", "steps", "paths", "sources", "prerequisites", "goal", "outcome", "verification", "model", "inputSha256", "schemaVersion"):
            item = _proposal(); item[key] = "PRIVATE_PROVIDER_OUTPUT"; invalid.append(item)
        for key, value in (("title", "x" * 513), ("triggers", ["x"] * 9), ("triggers", ["x" * 257]), ("recoverySummary", "x" * 2_049), ("keptObservationIds", []), ("keptObservationIds", ["d" * 64]), ("keptObservationIds", ["b" * 64] * 2), ("omitted", []), ("omitted", [{"observationId": "b" * 64, "reason": "overlaps kept"}]), ("omitted", [{"observationId": "a" * 64, "reason": "x" * 513}]), ("omitted", [{"observationId": "a" * 64, "reason": "omitted"}] * 2), ("recoverySummary", "Bearer " + "A" * 32)):
            item = _proposal(); item[key] = value; invalid.append(item)
        with self._client() as client:
            self._attach(client)
            for reply in invalid:
                self.llm.chat_json.return_value = reply
                response = client.post("/v1/procedures/abstract", json=_payload())
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json(), {"detail": "procedure abstraction unavailable"})
                self.assertNotIn("PRIVATE_PROVIDER_OUTPUT", response.text)
            self.llm.chat_json.side_effect = RuntimeError("PRIVATE_PROVIDER_OUTPUT")
            self.assertEqual(client.post("/v1/procedures/abstract", json=_payload()).json(), {"detail": "procedure abstraction unavailable"})

    def test_configured_provider_without_abstract_task_is_unavailable(self):
        registry = self.router._entries_from_json(json.dumps([{
            "name": "configured", "base_url": self.enabled.llm_base_url,
            "models": [{"model": self.enabled.llm_model, "tier": "balanced", "tasks": ["rerank"]}],
        }]))
        with self._client() as client:
            self._attach(client, ModelRouter(registry))
            self.assertEqual(client.post("/v1/procedures/abstract", json=_payload()).status_code, 503)
        self.llm.chat_json.assert_not_called()

    def test_exact_opaque_provider_credential_in_input_is_rejected_before_cost(self):
        credential = f'opaque"fragment\\{uuid4()}'
        settings = IntelligenceSettings(llm_providers="", llm_api_key=SecretStr(credential))
        reference = _payload()
        reference["goal"] = f"Review {credential}"
        with self._client() as client:
            self._attach(client, ModelRouter.from_settings(settings))
            response = client.post("/v1/procedures/abstract", json=reference)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {"detail": "procedure abstraction unavailable"})
        self.llm.chat_json.assert_not_called()

    def test_exact_provider_credential_in_configured_metadata_is_not_returned(self):
        credential = f'opaque"fragment\\{uuid4()}'
        settings = IntelligenceSettings(llm_providers="", llm_model=credential, llm_api_key=SecretStr(credential))
        with self._client() as client:
            self._attach(client, ModelRouter.from_settings(settings))
            response = client.post("/v1/procedures/abstract", json=_payload())
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {"detail": "procedure abstraction unavailable"})
        self.llm.chat_json.assert_not_called()


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class TestBoundedLLMResponse(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"PROVENA_INTEL_LLM_MODEL": uuid4().hex}):
            self.settings = IntelligenceSettings(llm_providers="")
        self.router = ModelRouter.from_settings(self.settings)

    def _call(self, stream, *, limit=80_000, status=200, headers=None):
        client_type = httpx.AsyncClient
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(status, stream=stream, headers=headers)

        def client(**kwargs):
            return client_type(transport=httpx.MockTransport(respond), **kwargs)

        with patch("app.llm.httpx.AsyncClient", side_effect=client):
            result = asyncio.run(LLMClient(self.router).chat_json(task="abstract", system="Reference only", user="{}", response_max_bytes=limit))
        self.assertTrue(stream.closed)
        self.assertEqual(json.loads(requests[0].content)["model"], self.settings.llm_model)
        return result, requests[0]

    def test_bounded_transport_parses_small_mocked_json(self):
        payload = json.dumps({"choices": [{"message": {"content": json.dumps(_proposal())}}]}).encode()
        reply, _ = self._call(_Chunks([payload[:20], payload[20:]]))
        self.assertEqual(reply, _proposal())

    def test_chunked_provider_body_stops_at_limit_without_materializing_remainder(self):
        stream = _Chunks([b" " * 40_001, b" " * 40_001, b"PRIVATE_PROVIDER_REMAINDER"])
        result, _ = self._call(stream)
        self.assertIsNone(result)
        self.assertEqual(stream.yielded, 2)

    def test_http_error_and_invalid_provider_json_return_none(self):
        result, _ = self._call(_Chunks([b"PRIVATE_PROVIDER_FAILURE"]), status=502)
        self.assertIsNone(result)
        result, _ = self._call(_Chunks([b"PRIVATE_PROVIDER_FAILURE"]))
        self.assertIsNone(result)

    def test_redirect_with_valid_proposal_body_is_refused_without_reading(self):
        payload = json.dumps({"choices": [{"message": {"content": json.dumps(_proposal())}}]}).encode()
        for status in (301, 302, 303, 307, 308):
            stream = _Chunks([payload])
            result, _ = self._call(stream, status=status, headers={"location": "https://invalid.example/redirect"})
            self.assertIsNone(result, status)
            self.assertEqual(stream.yielded, 0)

    def test_compressed_response_is_refused_without_decompression(self):
        stream = _Chunks([b"PRIVATE_COMPRESSED_RESPONSE"])
        result, request = self._call(stream, headers={"content-encoding": "gzip"})
        self.assertIsNone(result)
        self.assertEqual(stream.yielded, 0)
        self.assertEqual(request.headers["accept-encoding"], "identity")

    def test_bounded_transport_has_total_deadline_and_closes_slow_stream(self):
        class SlowChunks(_Chunks):
            async def __aiter__(self):
                await asyncio.sleep(0.05)
                yield b"PRIVATE_PROVIDER_REMAINDER"

        timeout = asyncio.timeout
        with patch("app.llm.asyncio.timeout", side_effect=lambda seconds: timeout(0.001), create=True):
            result, _ = self._call(SlowChunks([]))
        self.assertIsNone(result)

    def test_actual_shared_client_mocked_provider_cannot_reflect_escaped_opaque_key(self):
        credential = f'opaque"fragment\\{uuid4()}'
        settings = IntelligenceSettings(llm_providers="", llm_model=self.settings.llm_model, llm_api_key=SecretStr(credential))
        router = ModelRouter.from_settings(settings)
        reference = ProcedureAbstractionRequest.model_validate(_payload())
        proposal = {**_proposal(), "title": f"Review {credential}"}
        payload = json.dumps({"choices": [{"message": {"content": json.dumps(proposal)}}]}).encode()
        stream = _Chunks([payload])
        requests = []
        client_type = httpx.AsyncClient

        def respond(request):
            requests.append(request)
            return httpx.Response(200, stream=stream)

        def client(**kwargs):
            return client_type(transport=httpx.MockTransport(respond), **kwargs)

        with patch("app.llm.httpx.AsyncClient", side_effect=client):
            with self.assertRaisesRegex(RuntimeError, "^procedure abstraction unavailable$"):
                asyncio.run(ProcedureAbstractor(router, LLMClient(router)).abstract(reference))
        self.assertTrue(stream.closed)
        self.assertEqual(requests[0].headers["Authorization"], f"Bearer {credential}")
        self.assertNotIn(credential, json.loads(requests[0].content)["messages"][1]["content"])
