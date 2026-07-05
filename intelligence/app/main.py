"""FastAPI application for the Provena intelligence layer."""

from __future__ import annotations

import base64
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request

logger = logging.getLogger(__name__)

from app.config import settings
from app.embeddings import EmbeddingManager
from app.extract_facts import FactExtractor
from app.llm import LLMClient
from app.model_router import ModelRouter
from app.capture import CaptureEngine, CaptureSignal, HttpStoreClient
from app.models import (
    CaptureRequest,
    CaptureResponse,
    CaptureResultItem,
    CompactRequest,
    ModelTier,
    ReadSearchRequest,
    WriteRequest,
)
from app.overview_generator import OverviewGenerator
from app.read_pipeline import ReadPipeline
from app.write_pipeline import WritePipeline, WritePipelineError


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise shared components in app.state."""
    embedding_manager = EmbeddingManager(
        provider=settings.embedding_provider,
        model_id=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key.get_secret_value(),
    )
    model_router = ModelRouter.from_settings(settings)
    llm_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
    if not model_router.enabled and (llm_key or settings.llm_providers):
        logger.warning(
            "LLM endpoint/key configured but no model selected; set "
            "PROVENA_INTEL_LLM_MODEL or PROVENA_INTEL_LLM_PROVIDERS. "
            "LLM stages are running on deterministic heuristics."
        )
    overview_generator = OverviewGenerator(store_url=settings.pipeline_url)
    llm = LLMClient(model_router)
    fact_extractor = FactExtractor(model_router)
    write_pipeline = WritePipeline(
        embedding_manager=embedding_manager,
        model_router=model_router,
        overview_generator=overview_generator,
        store_url=settings.pipeline_url,
        orchestration_url=settings.orchestration_url,
        fact_extractor=fact_extractor,
        llm=llm,
    )
    read_pipeline = ReadPipeline(
        embedding_manager=embedding_manager,
        model_router=model_router,
        store_url=settings.pipeline_url,
        orchestration_url=settings.orchestration_url,
        llm=llm,
    )

    app.state.embedding_manager = embedding_manager
    app.state.model_router = model_router
    app.state.write_pipeline = write_pipeline
    app.state.read_pipeline = read_pipeline
    app.state.overview_generator = overview_generator
    app.state.capture_engine = CaptureEngine(HttpStoreClient(store_url=settings.pipeline_url))

    yield


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title="Provena Intelligence",
        version="0.1.0",
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    def require_write(request: Request) -> None:
        role = (request.headers.get("X-Provena-Role") or "").strip().lower()
        if role and role not in {"editor", "admin", "superadmin"}:
            raise HTTPException(status_code=403, detail="write access required")

    # ------------------------------------------------------------------
    # Living-memory capture
    # ------------------------------------------------------------------

    @app.post("/v1/capture/process", response_model=CaptureResponse)
    async def capture_process(request: Request, body: CaptureRequest) -> CaptureResponse:
        require_write(request)
        engine: CaptureEngine = request.app.state.capture_engine
        refs: list[dict[str, Any]] = []
        for source in body.source_references:
            if hasattr(source, "model_dump"):
                refs.append(source.model_dump())
            elif isinstance(source, dict):
                refs.append(source)
            elif isinstance(source, str):
                refs.append({"source_type": "uri", "source_id": source, "uri": source})
        signal = CaptureSignal(
            text=body.text,
            signal_type=body.signal_type,
            scope=body.scope,
            source_references=refs,
            metadata=body.metadata,
            error_signature=body.error_signature,
        )
        results = await engine.process(signal)
        return CaptureResponse(
            results=[
                CaptureResultItem(
                    created=r.created,
                    kind=r.kind,
                    memory_id=r.memory_id,
                    superseded_id=r.superseded_id,
                    reason=r.reason,
                )
                for r in results
            ]
        )

    # ------------------------------------------------------------------
    # Write pipeline
    # ------------------------------------------------------------------

    @app.post("/v1/pipeline/write")
    async def pipeline_write(request: Request, body: WriteRequest):
        require_write(request)
        wp: WritePipeline = request.app.state.write_pipeline
        try:
            result = await wp.process(body)
        except WritePipelineError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return result.model_dump()

    @app.post("/v1/batch-write")
    async def batch_write(request: Request, body: list[dict[str, Any]]):
        require_write(request)
        wp: WritePipeline = request.app.state.write_pipeline
        results: list[dict[str, Any]] = []
        for item in body:
            payload = item
            encoded_payload = item.get("payload") or item.get("Payload")
            if encoded_payload:
                try:
                    decoded = base64.b64decode(encoded_payload)
                    payload = json.loads(decoded.decode("utf-8"))
                except Exception:
                    payload = {}
            write_request = WriteRequest(**payload)
            try:
                result = await wp.process(write_request)
            except WritePipelineError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
            results.append(result.model_dump())
        return {"results": results}

    # ------------------------------------------------------------------
    # Search pipeline
    # ------------------------------------------------------------------

    @app.post("/v1/pipeline/search")
    async def pipeline_search(request: Request, body: ReadSearchRequest):
        rp: ReadPipeline = request.app.state.read_pipeline
        access_headers = {
            name: value
            for name, value in {
                "X-Provena-Tenant-Id": request.headers.get("X-Provena-Tenant-Id"),
                "X-Provena-Role": request.headers.get("X-Provena-Role"),
                "X-Provena-Key-Id": request.headers.get("X-Provena-Key-Id"),
                "X-Provena-Principal-Id": request.headers.get("X-Provena-Principal-Id"),
                "X-Provena-Groups": request.headers.get("X-Provena-Groups"),
            }.items()
            if value
        }
        result = await rp.search(body, access_headers=access_headers)
        return result.model_dump()

    # ------------------------------------------------------------------
    # Compact
    # ------------------------------------------------------------------

    @app.post("/v1/pipeline/compact")
    async def pipeline_compact(request: Request, body: CompactRequest):
        wp: WritePipeline = request.app.state.write_pipeline
        result = await wp._compact_memories(
            memory_ids=body.memory_ids,
            scope=body.scope,
            tier=body.tier,
        )
        return result.model_dump()

    # ------------------------------------------------------------------
    # Conflicts
    # ------------------------------------------------------------------

    @app.post("/v1/pipeline/conflicts")
    async def pipeline_conflicts(request: Request, body: dict[str, Any]):
        wp: WritePipeline = request.app.state.write_pipeline
        conflicts = await wp._detect_conflicts(
            content=body.get("content") or "",
            scope=body.get("scope") or {},
            entity_keys=body.get("entity_keys") or [],
            query_embedding=body.get("query_embedding"),
        )
        return {"conflicts": conflicts}

    # ------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------

    @app.post("/v1/pipeline/overview")
    async def pipeline_overview(request: Request, body: dict[str, Any]):
        og: OverviewGenerator = request.app.state.overview_generator
        result = await og.generate(scope=body.get("scope") or {})
        return result.model_dump()

    # ------------------------------------------------------------------
    # Model routing
    # ------------------------------------------------------------------

    @app.post("/v1/model/route")
    async def model_route(request: Request, body: dict[str, Any]):
        mr: ModelRouter = request.app.state.model_router
        task = body.get("task") or "classify"
        tier_str = body.get("tier") or "balanced"
        try:
            tier = ModelTier(tier_str)
        except ValueError:
            tier = ModelTier.BALANCED
        routed = mr.route(task=task, tier=tier)
        if routed is None:
            return {"enabled": mr.enabled, "routed": None}
        return {
            "enabled": mr.enabled,
            "provider": routed.provider,
            "model": routed.model,
            "tier": routed.tier.value,
        }

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    @app.post("/v1/embeddings/generate")
    async def embeddings_generate(request: Request, body: dict[str, Any]):
        em: EmbeddingManager = request.app.state.embedding_manager
        text = body.get("text", "")
        if isinstance(text, list):
            vectors = em.generate_batch(text)
            return {"vectors": vectors, "dimensions": em.dimensions}
        vector = em.generate(text)
        return {"vector": vector, "dimensions": em.dimensions}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.listen_port,
        reload=True,
    )
