"""Unit tests for the Provena intelligence layer.

Run with:  python -m pytest tests/
"""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from app.embeddings import EmbeddingManager
from app.model_router import ModelRouter
from app.models import ModelTier, ReadSearchRequest, ScoredMemory, WriteRequest
from app.extract_facts import ExtractedFact, FactExtractor
from app.read_pipeline import ReadPipeline
from app.write_pipeline import WritePipeline, WritePipelineError
from app.overview_generator import OverviewGenerator
from eval.retrieval_quality import precision_at_k, recall_at_k, mrr


def _router(
    model: str = "test-model",
    base_url: str = "http://llm.test/v1",
    api_key: str = "dummy-key",
    tier: str = "balanced",
    tasks: list[str] | None = None,
) -> ModelRouter:
    """An enabled ModelRouter with one configured provider/model."""
    providers = [
        {
            "name": "test",
            "base_url": base_url,
            "api_key": api_key,
            "models": [
                {
                    "model": model,
                    "tier": tier,
                    "tasks": tasks or ["classify", "summarize", "extract", "rerank", "compact"],
                }
            ],
        }
    ]
    return ModelRouter.from_settings(SimpleNamespace(llm_providers=json.dumps(providers)))


class TestClassification(unittest.TestCase):
    """Test WritePipeline._classify heuristics."""

    def setUp(self) -> None:
        em = EmbeddingManager()
        mr = ModelRouter()
        self.wp = WritePipeline(em, mr)

    def test_classify_decision(self) -> None:
        result = self.wp._classify("We decided to use PostgreSQL", "", [])
        self.assertEqual(result, "decision")

    def test_classify_fact(self) -> None:
        result = self.wp._classify("I learned that Python is great", "", [])
        self.assertEqual(result, "fact")

    def test_classify_preference(self) -> None:
        result = self.wp._classify("I prefer dark mode over light", "", [])
        self.assertEqual(result, "preference")

    def test_classify_default(self) -> None:
        result = self.wp._classify("The sky is blue today", "", [])
        self.assertEqual(result, "fact")


class TestFingerprint(unittest.TestCase):
    def setUp(self) -> None:
        self.wp = WritePipeline(EmbeddingManager(), ModelRouter())

    def test_valid_generated_fingerprint_binds_source_identity_and_content(self) -> None:
        scope = {"tenant_id": "tenant-a", "project_id": "repo-a"}
        metadata = {"provena_generated_fingerprint": "a" * 64}

        self.assertNotEqual(
            self.wp._fingerprint("original source", scope, metadata),
            self.wp._fingerprint("renamed source", scope, metadata),
        )
        self.assertEqual(
            self.wp._fingerprint("original source", scope, metadata),
            self.wp._fingerprint("original source", scope, metadata),
        )

    def test_invalid_generated_fingerprint_falls_back_to_content(self) -> None:
        scope = {"tenant_id": "tenant-a", "project_id": "repo-a"}
        metadata = {"provena_generated_fingerprint": "not-a-sha256"}

        self.assertNotEqual(
            self.wp._fingerprint("original source", scope, metadata),
            self.wp._fingerprint("renamed source", scope, metadata),
        )


class TestEmbeddings(unittest.TestCase):
    """Test EmbeddingManager."""

    def setUp(self) -> None:
        self.em = EmbeddingManager(provider="local", dimensions=384)

    def test_embedding_deterministic(self) -> None:
        v1 = self.em.generate("hello world")
        v2 = self.em.generate("hello world")
        self.assertEqual(v1, v2)

    def test_embedding_dimensions(self) -> None:
        vec = self.em.generate("test input")
        self.assertEqual(len(vec), 384)

    def test_cosine_similarity_identical(self) -> None:
        vec = self.em.generate("same text")
        sim = self.em.cosine_similarity(vec, vec)
        self.assertAlmostEqual(sim, 1.0, places=5)

    def test_cosine_similarity_orthogonal(self) -> None:
        # Two very different texts should have low similarity
        v1 = self.em.generate("quantum physics thermodynamics entropy")
        v2 = self.em.generate("chocolate cake recipe baking dessert")
        sim = self.em.cosine_similarity(v1, v2)
        # Pseudo-embeddings won't be truly orthogonal, but should differ
        self.assertLess(abs(sim), 0.5)


class TestOpenAIEmbeddingProvider(unittest.TestCase):
    """The openai-compatible provider speaks the OpenAI embeddings shape."""

    def _manager(self, api_key: str = "test-key") -> EmbeddingManager:
        return EmbeddingManager(
            provider="openai",
            model_id=f"test-{self.__class__.__name__}",
            dimensions=3,
            base_url="https://endpoint.example/v1",
            api_key=api_key,
        )

    @staticmethod
    def _fake_response(payload: dict):
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            @staticmethod
            def json() -> dict:
                return payload

        return FakeResponse()

    def test_request_shape_and_parse(self) -> None:
        captured: dict = {}

        def fake_post(client_self, url, json=None, headers=None, timeout=None):
            captured.update(url=url, json=json, headers=headers)
            return self._fake_response({"data": [{"embedding": [0.1, 0.2, 0.3]}]})

        with patch("app.embeddings.httpx.Client.post", fake_post):
            vec = self._manager().generate("hello")

        self.assertEqual(vec, [0.1, 0.2, 0.3])
        self.assertEqual(captured["url"], "https://endpoint.example/v1/embeddings")
        self.assertEqual(
            captured["json"],
            {"model": f"test-{self.__class__.__name__}", "input": "hello"},
        )
        self.assertEqual(captured["headers"], {"Authorization": "Bearer test-key"})

    def test_no_auth_header_without_key(self) -> None:
        captured: dict = {}

        def fake_post(client_self, url, json=None, headers=None, timeout=None):
            captured["headers"] = headers
            return self._fake_response({"data": [{"embedding": [1.0]}]})

        with patch("app.embeddings.httpx.Client.post", fake_post):
            self._manager(api_key="").generate("x")
        self.assertIsNone(captured["headers"])

    def test_empty_vector_raises(self) -> None:
        def fake_post(client_self, url, json=None, headers=None, timeout=None):
            return self._fake_response({"data": []})

        with patch("app.embeddings.httpx.Client.post", fake_post):
            with self.assertRaises(RuntimeError):
                self._manager().generate("x")


class _FakeResp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Async-context-manager stub capturing the request it receives."""

    def __init__(self, resp: _FakeResp, capture: dict) -> None:
        self._resp = resp
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, headers=None, json=None):  # noqa: A002 - mirror httpx kwarg
        self._capture["url"] = url
        self._capture["headers"] = headers
        self._capture["json"] = json
        return self._resp


def _openai_payload(content: str = "", reasoning: str | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return {"choices": [{"message": message}]}


class TestLlmExtraction(unittest.TestCase):
    """The LLM extraction path: router-selected model + OpenAI-compatible call."""

    def _run_with(self, payload, capture, model="test-extractor", base_url="http://llm.test/v1", api_key="dummy-key"):
        extractor = FactExtractor(_router(model=model, base_url=base_url, api_key=api_key))
        client = _FakeAsyncClient(_FakeResp(payload), capture)
        with patch("app.extract_facts.httpx.AsyncClient", return_value=client):
            return asyncio.run(extractor.extract("User: I love Postgres and Rust.", "t"))

    def test_sends_configured_model(self) -> None:
        capture: dict = {}
        facts = self._run_with(
            _openai_payload('{"facts":[{"content":"User likes Postgres"}]}'), capture, model="my-extractor"
        )
        # The wire model is the operator-configured served model, never a placeholder id.
        self.assertEqual(capture["json"]["model"], "my-extractor")
        self.assertTrue(capture["url"].endswith("/chat/completions"))
        self.assertEqual(capture["headers"]["Authorization"], "Bearer dummy-key")
        self.assertEqual([f.content for f in facts], ["User likes Postgres"])

    def test_routes_to_provider_base_url(self) -> None:
        capture: dict = {}
        self._run_with(
            _openai_payload('{"facts":[{"content":"x is a fact here"}]}'),
            capture,
            base_url="http://other.test/v1",
            model="qwen3",
        )
        self.assertEqual(capture["json"]["model"], "qwen3")
        self.assertTrue(capture["url"].startswith("http://other.test/v1"))

    def test_reads_reasoning_field_when_content_empty(self) -> None:
        # Reasoning models return content="" with output in `reasoning`.
        capture: dict = {}
        facts = self._run_with(
            _openai_payload(content="", reasoning='{"facts":[{"content":"User uses Rust daily"}]}'),
            capture,
        )
        self.assertEqual([f.content for f in facts], ["User uses Rust daily"])

    def test_no_provider_falls_back_to_local_without_network(self) -> None:
        extractor = FactExtractor(ModelRouter())
        self.assertFalse(extractor.llm_enabled)
        with patch("app.extract_facts.httpx.AsyncClient", side_effect=AssertionError("should not call LLM")):
            facts = asyncio.run(extractor.extract("User: I love Postgres.", "t"))
        self.assertTrue(facts)  # produced by the deterministic local splitter


class TestLlmStages(unittest.TestCase):
    """LLM-backed rerank / contradiction / compaction with mocked chat_json."""

    def _read_pipeline(self, chat_return):
        from app.llm import LLMClient
        from app.read_pipeline import ReadPipeline

        llm = LLMClient(_router())  # enabled
        llm.chat_json = AsyncMock(return_value=chat_return)
        return ReadPipeline(EmbeddingManager(), ModelRouter(), llm=llm)

    def test_llm_rerank_reorders_by_score(self) -> None:
        rp = self._read_pipeline({"scores": [{"id": "a", "score": 0.0}, {"id": "b", "score": 1.0}]})
        candidates = [
            ScoredMemory(memory_id="a", content="alpha", title="", combined=0.5),
            ScoredMemory(memory_id="b", content="beta", title="", combined=0.4),
        ]
        ranked = asyncio.run(rp._rerank("q", candidates))
        # b's LLM score (1.0 * 0.5) lifts it above a despite lower retrieval score
        self.assertEqual([m.memory_id for m in ranked], ["b", "a"])

    def test_llm_rerank_accepts_stringified_scores(self) -> None:
        # Local models often emit scores as strings ("0.0"/"1.0").
        rp = self._read_pipeline({"scores": [{"id": "a", "score": "0.0"}, {"id": "b", "score": "1.0"}]})
        candidates = [
            ScoredMemory(memory_id="a", content="alpha", title="", combined=0.5),
            ScoredMemory(memory_id="b", content="beta", title="", combined=0.4),
        ]
        ranked = asyncio.run(rp._rerank("q", candidates))
        self.assertEqual([m.memory_id for m in ranked], ["b", "a"])

    def test_llm_rerank_falls_back_when_unparseable(self) -> None:
        rp = self._read_pipeline(None)  # chat_json returned None
        candidates = [ScoredMemory(memory_id="a", content="alpha beta", title="", combined=0.5)]
        ranked = asyncio.run(rp._rerank("alpha", candidates))
        self.assertEqual(len(ranked), 1)  # heuristic path still returns results

    def test_llm_contradictions_filters_unknown_ids(self) -> None:
        rp = self._read_pipeline(
            {"contradictions": [
                {"id_a": "m1", "id_b": "m2", "description": "conflict", "severity": "high"},
                {"id_a": "m1", "id_b": "ghost", "description": "bad", "severity": "low"},
            ]}
        )
        memories = [
            ScoredMemory(memory_id="m1", content="X is true", title=""),
            ScoredMemory(memory_id="m2", content="X is false", title=""),
        ]
        pairs = asyncio.run(rp._detect_contradictions(memories))
        self.assertEqual(len(pairs), 1)  # the ghost-id pair is dropped
        self.assertEqual((pairs[0].memory_id_a, pairs[0].memory_id_b), ("m1", "m2"))
        self.assertEqual(pairs[0].severity, "high")

    def test_llm_compaction_summarizes_fetched_bodies(self) -> None:
        from app.llm import LLMClient
        from app.write_pipeline import WritePipeline

        llm = LLMClient(_router())
        llm.chat_json = AsyncMock(return_value={"summary": "Karan likes Rust and Postgres.", "title": "Prefs"})
        wp = WritePipeline(EmbeddingManager(), ModelRouter(), llm=llm)
        wp._fetch_memory_bodies = AsyncMock(return_value={"m1": "likes Rust", "m2": "likes Postgres"})
        resp = asyncio.run(wp._compact_memories(["m1", "m2"], {"tenant_id": "t"}))
        self.assertEqual(resp.compacted_memory["content"], "Karan likes Rust and Postgres.")
        self.assertEqual(sorted(resp.superseded_ids), ["m1", "m2"])

    def test_compaction_noop_supersedes_nothing_without_llm(self) -> None:
        from app.write_pipeline import WritePipeline

        wp = WritePipeline(EmbeddingManager(), ModelRouter())  # no key → disabled
        resp = asyncio.run(wp._compact_memories(["m1", "m2"], {"tenant_id": "t"}))
        # Safe fallback: never claim to supersede sources we didn't actually fold in.
        self.assertEqual(resp.superseded_ids, [])


class TestLlmClientParsing(unittest.TestCase):
    def test_parse_json_variants(self) -> None:
        from app.llm import LLMClient

        self.assertEqual(LLMClient._parse_json('{"a": 1}'), {"a": 1})
        self.assertEqual(LLMClient._parse_json('```json\n{"a": 2}\n```'), {"a": 2})
        self.assertEqual(LLMClient._parse_json('noise {"a": 3} trailing'), {"a": 3})
        self.assertEqual(LLMClient._parse_json("[1, 2, 3]"), [1, 2, 3])
        self.assertIsNone(LLMClient._parse_json("not json at all"))
        self.assertIsNone(LLMClient._parse_json(""))


class TestEntityResolution(unittest.TestCase):
    """Test WritePipeline._resolve_entities."""

    def setUp(self) -> None:
        em = EmbeddingManager()
        mr = ModelRouter()
        self.wp = WritePipeline(em, mr)

    def test_entity_resolution(self) -> None:
        content = "John Smith met Apple CEO Tim Cook at the conference"
        entities = self.wp._resolve_entities(content, [])
        # Should extract multi-word capitalized phrases
        self.assertIn("John Smith", entities)
        self.assertIn("Tim Cook", entities)


class TestFactExtraction(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = FactExtractor(ModelRouter())

    def test_local_extract_splits_role_lines(self) -> None:
        content = "user: I moved to Seattle in 2024.\nassistant: Seattle is a great city."
        facts = asyncio.run(self.extractor.extract(content))
        self.assertGreaterEqual(len(facts), 2)
        self.assertTrue(any("Seattle" in fact.content for fact in facts))

    def test_extract_disabled_returns_single_chunk(self) -> None:
        import os

        os.environ["PROVENA_EXTRACT_DISABLED"] = "true"
        try:
            extractor = FactExtractor(ModelRouter())
            facts = asyncio.run(extractor.extract("user: one\nassistant: two"))
            self.assertEqual(len(facts), 1)
            self.assertIn("user:", facts[0].content)
        finally:
            os.environ.pop("PROVENA_EXTRACT_DISABLED", None)


class TestWriteExtraction(unittest.TestCase):
    def test_write_splits_into_multiple_store_posts(self) -> None:
        wp = WritePipeline(EmbeddingManager(), ModelRouter())
        request = WriteRequest(
            kind="episode",
            scope={"tenant_id": "tenant-a"},
            content="user: I prefer dark mode.\nassistant: Noted your preference.",
        )

        posted: list[dict] = []

        class DummyResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"created": True, "memory": {"memory_id": f"mem-{len(posted)}"}}

        class DummyClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *args, **kwargs):
                url = args[0] if args else ""
                if url.endswith("/v1/memories/search"):
                    return DummyResponse()  # conflict-detection probe; no candidates
                posted.append(kwargs.get("json", {}))
                return DummyResponse()

        with patch("app.write_pipeline.httpx.AsyncClient", return_value=DummyClient()):
            result = asyncio.run(wp.process(request))

        self.assertTrue(result.created)
        self.assertGreater(len(posted), 1)
        self.assertTrue(all(item.get("metadata", {}).get("extraction_source") == "add_only" for item in posted))

    def test_mocked_llm_extraction(self) -> None:
        extractor = FactExtractor(_router())

        async def _mock_llm(content: str, title: str = ""):
            return [
                ExtractedFact(content="User works at Acme Corp", title="Job"),
                ExtractedFact(content="User lives in Austin", title="Location"),
            ]

        extractor._llm_extract = _mock_llm  # type: ignore[method-assign]
        wp = WritePipeline(EmbeddingManager(), ModelRouter(), fact_extractor=extractor)
        request = WriteRequest(kind="fact", scope={"tenant_id": "tenant-a"}, content="long blob")

        posted: list[dict] = []

        class DummyResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"created": True, "memory": {"memory_id": f"mem-{len(posted)}"}}

        class DummyClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *args, **kwargs):
                url = args[0] if args else ""
                if url.endswith("/v1/memories/search"):
                    return DummyResponse()  # conflict-detection probe; no candidates
                posted.append(kwargs.get("json", {}))
                return DummyResponse()

        with patch("app.write_pipeline.httpx.AsyncClient", return_value=DummyClient()):
            result = asyncio.run(wp.process(request))

        self.assertEqual(len(posted), 2)
        self.assertEqual(result.memory.get("extraction_count"), 2)


class TestWriteFailures(unittest.TestCase):
    def setUp(self) -> None:
        self.wp = WritePipeline(EmbeddingManager(), ModelRouter())

    def test_store_failure_is_not_acknowledged(self) -> None:
        request = WriteRequest(kind="fact", scope={"tenant_id": "tenant-a"}, content="test content")

        class DummyResponse:
            status_code = 422
            text = "validation failed"

        class DummyClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *args, **kwargs):
                return DummyResponse()

        with patch("app.write_pipeline.httpx.AsyncClient", return_value=DummyClient()):
            with self.assertRaises(WritePipelineError) as error:
                asyncio.run(self.wp.process(request))

        self.assertEqual(error.exception.status_code, 422)

    def test_failed_write_does_not_poison_dedupe_cache(self) -> None:
        request = WriteRequest(kind="fact", scope={"tenant_id": "tenant-a"}, content="retryable content")

        class FailingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *args, **kwargs):
                raise httpx.ConnectError("store offline")

        class SuccessClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *args, **kwargs):
                class DummyResponse:
                    status_code = 200

                    @staticmethod
                    def json():
                        return {"created": True, "memory": {"memory_id": "mem-1"}}

                return DummyResponse()

        with patch("app.write_pipeline.httpx.AsyncClient", return_value=FailingClient()):
            with self.assertRaises(WritePipelineError):
                asyncio.run(self.wp.process(request))

        with patch("app.write_pipeline.httpx.AsyncClient", return_value=SuccessClient()):
            result = asyncio.run(self.wp.process(request))

        self.assertTrue(result.created)
        self.assertEqual(result.memory["memory_id"], "mem-1")


class TestAccessHeaderPropagation(unittest.TestCase):
    caller_headers = {
        "X-Provena-Tenant-Id": "tenant-caller",
        "X-Provena-Role": "editor",
        "X-Provena-Key-Id": "key-caller",
        "X-Provena-Principal-Id": "principal-caller",
        "X-Provena-Groups": "engineering,security",
    }
    service_headers = {
        "X-Provena-Tenant-Id": "tenant-service",
        "X-Provena-Role": "superadmin",
        "X-Provena-Key-Id": "key-service",
        "X-Provena-Principal-Id": "principal-service",
    }

    class Response:
        status_code = 200
        text = ""

        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def json(self) -> dict:
            return self.payload

    class Client:
        def __init__(self, requests: list[tuple[str, str, dict[str, str]]]) -> None:
            self.requests = requests

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(
            self,
            url,
            json=None,
            headers=None,
        ):  # noqa: A002 - mirrors httpx
            self.requests.append(("POST", url, dict(headers or {})))
            if url.endswith("/v1/memories/search"):
                return TestAccessHeaderPropagation.Response({"results": []})
            return TestAccessHeaderPropagation.Response(
                {"created": True, "memory": {"memory_id": "memory-access"}}
            )

        async def get(self, url, headers=None):
            self.requests.append(("GET", url, dict(headers or {})))
            return TestAccessHeaderPropagation.Response({"content": "memory body"})

    def test_write_search_and_store_use_caller_headers_without_service_claims(
        self,
    ) -> None:
        requests: list[tuple[str, str, dict[str, str]]] = []
        pipeline = WritePipeline(
            EmbeddingManager(),
            ModelRouter(),
            service_headers=self.service_headers,
        )
        request = WriteRequest(
            kind="fact",
            scope={"tenant_id": "tenant-caller"},
            content="Caller-scoped memory",
        )

        with patch(
            "app.write_pipeline.httpx.AsyncClient",
            return_value=self.Client(requests),
        ):
            result = asyncio.run(
                pipeline.process(request, access_headers=self.caller_headers)
            )

        self.assertTrue(result.created)
        self.assertEqual(
            {url.rsplit("/", 1)[-1] for _, url, _ in requests},
            {"search", "memories"},
        )
        self.assertTrue(requests)
        self.assertTrue(
            all(headers == self.caller_headers for _, _, headers in requests)
        )

    def test_internal_write_and_compaction_fetch_fall_back_to_service_headers(
        self,
    ) -> None:
        requests: list[tuple[str, str, dict[str, str]]] = []
        pipeline = WritePipeline(
            EmbeddingManager(),
            ModelRouter(),
            service_headers=self.service_headers,
        )
        request = WriteRequest(
            kind="fact",
            scope={"tenant_id": "tenant-service"},
            content="Service-scoped memory",
        )

        with patch(
            "app.write_pipeline.httpx.AsyncClient",
            return_value=self.Client(requests),
        ):
            asyncio.run(pipeline.process(request))
            bodies = asyncio.run(pipeline._fetch_memory_bodies(["m1", "m2"]))

        self.assertEqual(bodies, {"m1": "memory body", "m2": "memory body"})
        self.assertTrue(requests)
        self.assertTrue(
            all(headers == self.service_headers for _, _, headers in requests)
        )

    def test_overview_prefers_caller_headers_and_uses_service_fallback(self) -> None:
        caller_requests: list[tuple[str, str, dict[str, str]]] = []
        service_requests: list[tuple[str, str, dict[str, str]]] = []
        generator = OverviewGenerator(
            store_url="http://store.test",
            service_headers=self.service_headers,
        )

        with patch(
            "app.overview_generator.httpx.AsyncClient",
            return_value=self.Client(caller_requests),
        ):
            asyncio.run(
                generator.generate(
                    {"tenant_id": "tenant-caller"},
                    access_headers=self.caller_headers,
                )
            )
        with patch(
            "app.overview_generator.httpx.AsyncClient",
            return_value=self.Client(service_requests),
        ):
            asyncio.run(generator.generate({"tenant_id": "tenant-service"}))

        self.assertEqual(caller_requests[0][2], self.caller_headers)
        self.assertEqual(service_requests[0][2], self.service_headers)


class TestModelRouter(unittest.TestCase):
    """Test ModelRouter.route over a configured multi-provider registry."""

    def setUp(self) -> None:
        # Two providers across tiers/costs to exercise real routing.
        providers = [
            {
                "name": "cheap-local",
                "base_url": "http://local.test/v1",
                "api_key": "",
                "models": [
                    {"model": "local-fast", "tier": "fast", "tasks": ["classify"], "cost_per_1k_input": 0.0},
                ],
            },
            {
                "name": "cloud",
                "base_url": "http://cloud.test/v1",
                "api_key": "sk-test",
                "models": [
                    {"model": "cloud-cheap", "tier": "balanced", "tasks": ["classify", "compact"], "cost_per_1k_input": 0.001},
                    {"model": "cloud-pricey", "tier": "balanced", "tasks": ["classify", "compact"], "cost_per_1k_input": 0.01},
                    {"model": "cloud-quality", "tier": "quality", "tasks": ["compact"], "cost_per_1k_input": 0.02},
                ],
            },
        ]
        self.mr = ModelRouter.from_settings(SimpleNamespace(llm_providers=json.dumps(providers)))

    def test_selects_cheapest_at_tier(self) -> None:
        routed = self.mr.route("classify", ModelTier.BALANCED)
        # Two balanced models support classify; the cheaper wins, with its provider creds.
        self.assertEqual(routed.model, "cloud-cheap")
        self.assertEqual(routed.base_url, "http://cloud.test/v1")
        self.assertEqual(routed.api_key, "sk-test")

    def test_routes_fast_tier_to_local(self) -> None:
        routed = self.mr.route("classify", ModelTier.FAST)
        self.assertEqual(routed.model, "local-fast")
        self.assertEqual(routed.base_url, "http://local.test/v1")
        self.assertEqual(routed.api_key, "")

    def test_tier_fallback_when_exact_missing(self) -> None:
        # "compact" has no FAST model; fall back outward to a configured tier.
        routed = self.mr.route("compact", ModelTier.FAST)
        self.assertIsNotNone(routed)
        self.assertNotEqual(routed.tier, ModelTier.FAST)
        self.assertEqual(routed.model, "cloud-cheap")  # cheapest balanced compact

    def test_unconfigured_task_returns_none(self) -> None:
        # No provider declares "rerank" -> not routable -> caller uses heuristic.
        self.assertIsNone(self.mr.route("rerank", ModelTier.BALANCED))

    def test_disabled_router_routes_nothing(self) -> None:
        self.assertFalse(ModelRouter().enabled)
        self.assertIsNone(ModelRouter().route("classify"))


class TestRetrievalMetrics(unittest.TestCase):
    """Test retrieval quality metrics."""

    def test_precision_at_k(self) -> None:
        retrieved = ["a", "b", "c", "d", "e"]
        relevant = ["a", "c", "f"]
        p = precision_at_k(retrieved, relevant, 5)
        self.assertAlmostEqual(p, 2 / 5)

    def test_recall_at_k(self) -> None:
        retrieved = ["a", "b", "c", "d", "e"]
        relevant = ["a", "c", "f"]
        r = recall_at_k(retrieved, relevant, 5)
        self.assertAlmostEqual(r, 2 / 3)

    def test_mrr(self) -> None:
        # First relevant at position 3 (0-indexed: 2)
        retrieved = ["x", "y", "a", "b"]
        relevant = ["a", "b"]
        result = mrr(retrieved, relevant)
        self.assertAlmostEqual(result, 1 / 3)


class TestAbstentionGate(unittest.TestCase):
    """Test ReadPipeline abstention when evidence scores are too low."""

    def setUp(self) -> None:
        em = EmbeddingManager()
        mr = ModelRouter()
        self.rp = ReadPipeline(em, mr)
        self.rp.min_combined_score = 0.15

    def test_high_score_query_returns_hits(self) -> None:
        candidates = [
            ScoredMemory(
                memory_id="m1",
                content="PostgreSQL is the primary database",
                title="Database",
                combined=0.85,
                fts_score=0.8,
                vector_score=0.9,
            ),
        ]

        async def _mock_retrieve(*args, **kwargs):
            return candidates

        with patch.object(self.rp, "_hybrid_retrieve", side_effect=_mock_retrieve):
            with patch.object(self.rp, "_trigger_lookup", return_value=[]):
                response = asyncio.run(
                    self.rp.search(
                        ReadSearchRequest(
                            query="PostgreSQL database",
                            scope={"tenant_id": "t1"},
                            limit=5,
                        )
                    )
                )

        self.assertGreater(len(response.results), 0)
        self.assertNotEqual(response.token_usage.operation, "abstain_low_evidence")

    def test_noise_query_abstains(self) -> None:
        candidates = [
            ScoredMemory(
                memory_id="m1",
                content="unrelated noise fragment",
                title="Noise",
                combined=0.05,
                fts_score=0.04,
                vector_score=0.06,
            ),
        ]

        async def _mock_retrieve(*args, **kwargs):
            return candidates

        with patch.object(self.rp, "_hybrid_retrieve", side_effect=_mock_retrieve):
            with patch.object(self.rp, "_trigger_lookup", return_value=[]):
                response = asyncio.run(
                    self.rp.search(
                        ReadSearchRequest(
                            query="xyzzy quantum flarn",
                            scope={"tenant_id": "t1"},
                            limit=5,
                        )
                    )
                )

        self.assertEqual(response.results, [])
        self.assertEqual(response.token_usage.operation, "abstain_low_evidence")


class TestCitationPackaging(unittest.TestCase):
    """Test ReadPipeline._package_citations."""

    def setUp(self) -> None:
        em = EmbeddingManager()
        mr = ModelRouter()
        self.rp = ReadPipeline(em, mr)

    def test_citation_packaging(self) -> None:
        memories = [
            {
                "memory_id": "m1",
                "title": "Test Memory",
                "content": "Some content about testing",
                "source_references": ["https://example.com"],
                "combined": 0.85,
            },
            {
                "memory_id": "m2",
                "title": "Another Memory",
                "content": "More content",
                "source_references": [],
                "combined": 0.7,
            },
        ]
        citations = self.rp._package_citations(memories)
        self.assertEqual(len(citations), 2)
        self.assertEqual(citations[0].memory_id, "m1")
        self.assertEqual(citations[0].source_references, ["https://example.com"])
        self.assertAlmostEqual(citations[0].confidence, 0.85)


class TestContradictionDetection(unittest.TestCase):
    """Test ReadPipeline._detect_contradictions."""

    def setUp(self) -> None:
        em = EmbeddingManager()
        mr = ModelRouter()
        self.rp = ReadPipeline(em, mr)

    def test_contradiction_detection(self) -> None:
        memories = [
            ScoredMemory(
                memory_id="m1",
                content="The project uses Python for backend development",
                title="Tech Stack",
            ),
            ScoredMemory(
                memory_id="m2",
                content="The project does not use Python for backend development",
                title="Tech Stack Update",
            ),
        ]
        # No LLM key configured in this pipeline → exercises the heuristic path.
        pairs = asyncio.run(self.rp._detect_contradictions(memories))
        self.assertGreater(len(pairs), 0)
        self.assertEqual(pairs[0].memory_id_a, "m1")
        self.assertEqual(pairs[0].memory_id_b, "m2")


class TestOverviewGeneration(unittest.TestCase):
    """Test OverviewGenerator with mock store data."""

    def test_overview_generation_sync(self) -> None:
        generator = OverviewGenerator(store_url="http://localhost:8000")

        # Mock the internal fetch to return canned data
        mock_memories = [
            {"kind": "decision", "title": "Use PostgreSQL", "entity_keys": ["PostgreSQL", "Database"]},
            {"kind": "decision", "title": "Adopt Rust", "entity_keys": ["Rust", "Backend"]},
            {"kind": "fact", "title": "Team size is 5", "entity_keys": ["Team"]},
            {"kind": "fact", "title": "Sprint length is 2 weeks", "entity_keys": ["Sprint", "Team"]},
            {"kind": "preference", "title": "Dark mode preferred", "entity_keys": ["UI"]},
        ]

        async def _mock_fetch(scope, access_headers=None):
            return mock_memories

        generator._fetch_recent_memories = _mock_fetch  # type: ignore[assignment]

        overview = asyncio.run(
            generator.generate({"tenant_id": "t1", "project_id": "p1"})
        )

        self.assertEqual(overview.active_memory_count, 5)
        self.assertIn("Use PostgreSQL", overview.recent_decisions)
        self.assertIn("Adopt Rust", overview.recent_decisions)
        self.assertIn("Team", overview.key_entities)
        self.assertEqual(overview.tenant_id, "t1")
        self.assertEqual(overview.project_id, "p1")


class TestWriteConflictDetection(unittest.TestCase):
    """WritePipeline._detect_conflicts flags opposite-polarity overlap."""

    @staticmethod
    def _client_returning(search_results: list[dict]):
        class Resp:
            def __init__(self, data: dict) -> None:
                self.status_code = 200
                self.text = ""
                self._data = data

            def json(self) -> dict:
                return self._data

        class DummyClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, json=None, **kwargs):
                if url.endswith("/v1/memories/search"):
                    return Resp({"results": search_results})
                if url.endswith("/v1/memories"):
                    return Resp({"created": True, "memory": {"memory_id": "m-new"}})
                return Resp({})

        return DummyClient

    def _run(self, search_results: list[dict]) -> list[str]:
        wp = WritePipeline(EmbeddingManager(), ModelRouter())
        request = WriteRequest(
            kind="fact",
            scope={"tenant_id": "t"},
            content="Postgres is the primary database",
        )
        client = self._client_returning(search_results)
        with patch("app.write_pipeline.httpx.AsyncClient", return_value=client()):
            result = asyncio.run(wp.process(request))
        return result.pipeline_trace.detected_conflicts

    def test_negation_conflict_is_flagged(self) -> None:
        conflicts = self._run(
            [{"memory": {"memory_id": "m-old", "content": "Postgres is not the primary database"}}]
        )
        self.assertTrue(conflicts)
        self.assertIn("m-old", conflicts[0])

    def test_compatible_memory_is_not_flagged(self) -> None:
        conflicts = self._run(
            [{"memory": {"memory_id": "m-old", "content": "Postgres is the primary database"}}]
        )
        self.assertEqual(conflicts, [])


if __name__ == "__main__":
    unittest.main()
