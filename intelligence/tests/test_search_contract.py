"""Regression coverage for the hosted retrieval request and context budget."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.embeddings import EmbeddingManager
from app.model_router import ModelRouter
from app.models import ModelTier, ReadSearchRequest, ScoredMemory, WriteRequest
from app.read_pipeline import ReadPipeline
from app.write_pipeline import WritePipeline, WritePipelineError
from app.overview_generator import OverviewGenerator


def pipeline():
    return ReadPipeline(EmbeddingManager(), ModelRouter())


def test_search_preserves_filters_and_requested_model_tier():
    reader = pipeline()
    request = ReadSearchRequest(
        query="authentication", scope={"tenant_id": "tenant-a"},
        kinds=["workflow"], tags=["reviewed"], entity_keys=["session"],
        include_deleted=True, include_relations=False, model_tier=ModelTier.QUALITY,
    )
    caller = {"Authorization": "Bearer test-caller"}
    with (
        patch.object(reader, "_trigger_lookup", new=AsyncMock(return_value=[])),
        patch.object(reader, "_hybrid_retrieve", new=AsyncMock(return_value=[])) as retrieve,
        patch.object(reader, "_rerank", new=AsyncMock(return_value=[])) as rerank,
        patch.object(reader, "_detect_contradictions", new=AsyncMock(return_value=[])) as conflicts,
        patch.object(reader, "_apply_context_budget", new=AsyncMock(return_value=([], 0))),
    ):
        asyncio.run(reader.search(request, access_headers=caller))
    assert retrieve.call_args.kwargs["access_headers"] == caller
    assert retrieve.call_args.kwargs["filters"] == {
        "kinds": ["workflow"], "tags": ["reviewed"], "entity_keys": ["session"],
        "include_deleted": True, "include_relations": False,
    }
    assert rerank.call_args.kwargs["tier"] == ModelTier.QUALITY
    assert conflicts.call_args.kwargs["tier"] == ModelTier.QUALITY


def test_filters_reach_store_without_service_identity_substitution():
    requests = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            requests.append((url, json, headers))
            return type("Response", (), {"status_code": 200, "json": lambda self: {"results": []}})()

    filters = {"kinds": ["decision"], "include_deleted": False, "include_relations": False}
    with patch("app.read_pipeline.httpx.AsyncClient", return_value=Client()):
        asyncio.run(pipeline()._fts_search("auth", {"tenant_id": "tenant-a"}, 3, [1.0],
                                            access_headers={}, filters=filters))
    assert requests[0][1] == {
        "query": "auth", "scope": {"tenant_id": "tenant-a"}, "limit": 3,
        "query_embedding": [1.0], **filters,
    }
    assert requests[0][2] == {}


def test_fallback_budget_skips_oversized_memory_and_keeps_later_fit():
    reader = pipeline()
    memories = [
        ScoredMemory(memory_id="large", title="large", content="x" * 400, combined=1),
        ScoredMemory(memory_id="small", title="small", content="use auth", combined=0.9),
    ]
    with patch("app.read_pipeline.httpx.AsyncClient", side_effect=OSError("offline")):
        selected, tokens = asyncio.run(reader._apply_context_budget(memories, 8))
    assert [memory.memory_id for memory in selected] == ["small"]
    assert tokens <= 8


def test_budget_rounds_up_partial_tokens_and_rechecks_orchestration_selection():
    memory = ScoredMemory(memory_id="partial", title="partial", content="abcde", combined=1)
    reader = pipeline()
    client = AsyncMock()
    client.post.return_value.status_code = 200
    client.post.return_value.json = lambda: {"selected_memory_ids": ["partial"], "memory_tokens": 1}
    client.__aenter__.return_value = client
    with patch("app.read_pipeline.httpx.AsyncClient", return_value=client):
        selected, tokens = asyncio.run(reader._apply_context_budget([memory], 1))
    assert selected == [] and tokens == 0
    assert client.post.call_args.kwargs["json"]["candidates"][0]["token_count"] == 2


def test_generic_search_excludes_procedure_and_outcome_projection_guidance():
    def record(identifier, structured):
        return {"memory": {"memory_id": identifier, "content": "session validation", "metadata": {
            "provena_event": {"structured_data": structured},
        }}, "score": 1}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value.status_code = 200
    client.post.return_value.json = lambda: {"results": [
        record("candidate", {"procedure": {"state": "candidate"}}),
        record("approved", {"procedure": {"state": "approved"}}),
        record("outcome", {"procedureOutcome": {"outcome": "success"}}),
        record("malformed-reserved", {"procedure": None}),
        {**record("ordinary", {}), "related_memories": [
            record("related-candidate", {"procedure": {"state": "candidate"}})["memory"],
            record("related-outcome", {"procedureOutcome": {"outcome": "success"}})["memory"],
            record("related-ordinary", {})["memory"],
        ]},
    ]}
    with patch("app.read_pipeline.httpx.AsyncClient", return_value=client):
        results = asyncio.run(pipeline()._fts_search("session", {"tenant_id": "tenant-a"}, 10, []))
    assert [memory.memory_id for memory in results] == ["ordinary"]
    assert [memory["memory_id"] for memory in results[0].related_memories] == ["related-ordinary"]


@pytest.mark.parametrize("network_error", [False, True])
def test_write_failure_preserves_status_without_exposing_upstream_diagnostics(network_error):
    writer = WritePipeline(EmbeddingManager(), ModelRouter())
    request = WriteRequest(memory_id="error-test", kind="fact", scope={"tenant_id": "tenant-a"},
                           content="diagnostic test", extraction_mode="none")
    sentinel = "PRIVATE_UPSTREAM_SENTINEL credential diagnostics"
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value.status_code = 500
    client.post.return_value.text = sentinel
    if network_error:
        client.post.side_effect = OSError(sentinel)
    with (
        patch.object(writer, "_detect_conflicts", new=AsyncMock(return_value=[])),
        patch("app.write_pipeline.httpx.AsyncClient", return_value=client),
    ):
        with pytest.raises(WritePipelineError) as captured:
            asyncio.run(writer.process(request))
    assert captured.value.status_code == (503 if network_error else 500)
    assert captured.value.detail == ("store unavailable" if network_error else "store rejected write")
    assert sentinel not in str(captured.value)


def test_explicit_empty_caller_context_never_inherits_service_headers():
    service = {"Authorization": "Bearer test-service"}
    writer = WritePipeline(EmbeddingManager(), ModelRouter(), service_headers=service)
    overview = OverviewGenerator(service_headers=service)
    assert writer._access_headers(None) == service
    assert writer._access_headers({}) == {}
    client = AsyncMock()
    client.post.return_value.status_code = 200
    client.post.return_value.json = lambda: {"results": []}
    client.__aenter__.return_value = client
    with patch("app.overview_generator.httpx.AsyncClient", return_value=client):
        asyncio.run(overview._fetch_recent_memories({"tenant_id": "tenant-a"}, access_headers={}))
        assert client.post.call_args.kwargs["headers"] == {}
        asyncio.run(overview._fetch_recent_memories({"tenant_id": "tenant-a"}))
        assert client.post.call_args.kwargs["headers"] == service


@pytest.mark.parametrize("values", [
    {"limit": 0}, {"limit": 201}, {"max_tokens": 0}, {"model_tier": "unsupported"},
])
def test_invalid_search_bounds_fail_at_api_validation(values):
    with pytest.raises(ValidationError):
        ReadSearchRequest(query="auth", scope={"tenant_id": "tenant-a"}, **values)
