"""Unit tests for the Provena intelligence layer.

Run with:  python -m pytest tests/
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import httpx
from app.embeddings import EmbeddingManager
from app.model_router import ModelRouter
from app.models import ModelTier, ReadSearchRequest, ScoredMemory, WriteRequest
from app.extract_facts import ExtractedFact, FactExtractor
from app.read_pipeline import ReadPipeline
from app.write_pipeline import WritePipeline, WritePipelineError
from app.overview_generator import OverviewGenerator
from eval.retrieval_quality import precision_at_k, recall_at_k, mrr


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


class TestEmbeddings(unittest.TestCase):
    """Test EmbeddingManager."""

    def setUp(self) -> None:
        self.em = EmbeddingManager(provider="local", model_id="local-minilm", dimensions=384)

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
            model_id="nomic-embed-text",
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
        self.assertEqual(captured["json"], {"model": "nomic-embed-text", "input": "hello"})
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
                posted.append(kwargs.get("json", {}))
                return DummyResponse()

        with patch("app.write_pipeline.httpx.AsyncClient", return_value=DummyClient()):
            result = asyncio.run(wp.process(request))

        self.assertTrue(result.created)
        self.assertGreater(len(posted), 1)
        self.assertTrue(all(item.get("metadata", {}).get("extraction_source") == "add_only" for item in posted))

    def test_mocked_llm_extraction(self) -> None:
        extractor = FactExtractor(ModelRouter(), llm_api_key="test-key")

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


class TestModelRouter(unittest.TestCase):
    """Test ModelRouter.route."""

    def setUp(self) -> None:
        self.mr = ModelRouter()

    def test_model_router_selects_cheapest(self) -> None:
        result = self.mr.route("classify", ModelTier.FAST)
        # local-classify has cost=0, cheapest in FAST tier for classify
        self.assertEqual(result.model_id, "local-classify")

    def test_model_router_fallback_tier(self) -> None:
        # "compact" is not available in FAST tier, should fall back
        result = self.mr.route("compact", ModelTier.FAST)
        # Should find a model with compact capability in BALANCED or QUALITY
        self.assertIn("compact", result.capabilities)
        self.assertNotEqual(result.tier, ModelTier.FAST)


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
        pairs = self.rp._detect_contradictions(memories)
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

        async def _mock_fetch(scope):
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


if __name__ == "__main__":
    unittest.main()
