"""FastAPI application for the Provena intelligence layer."""

from __future__ import annotations

import base64
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

from app.config import IntelligenceSettings, settings
from app.embeddings import EmbeddingManager
from app.extract_facts import FactExtractor
from app.llm import LLMClient
from app.model_router import ModelRouter
from app.models import (
    CompactRequest,
    ModelTier,
    ReadSearchRequest,
    WriteRequest,
)
from app.overview_generator import OverviewGenerator
from app.read_pipeline import ReadPipeline
from app.store_auth import (
    authenticate_intelligence_request,
    require_intelligence_write,
    store_request_headers,
)
from app.write_pipeline import WritePipeline, WritePipelineError


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise shared components in app.state."""
    embedding_manager = EmbeddingManager(
        provider=settings.embedding_provider,
        model_id=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        base_url=settings.embedding_base_url,
        api_key=(
            settings.embedding_api_key.get_secret_value()
            if settings.embedding_api_key
            else ""
        ),
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

    yield


def create_app(auth_settings: IntelligenceSettings = settings) -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title="Provena Intelligence",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def authenticate_ingress(request: Request, call_next):
        if request.url.path.startswith("/v1/"):
            try:
                request.state.provena_role = authenticate_intelligence_request(
                    request, auth_settings
                )
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                    headers=exc.headers,
                )
        return await call_next(request)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Write pipeline
    # ------------------------------------------------------------------

    @app.post("/v1/pipeline/write")
    async def pipeline_write(request: Request, body: WriteRequest):
        require_intelligence_write(request)
        wp: WritePipeline = request.app.state.write_pipeline
        try:
            result = await wp.process(body, access_headers=store_request_headers(request))
        except WritePipelineError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return result.model_dump()

    @app.post("/v1/batch-write")
    async def batch_write(request: Request, body: list[dict[str, Any]]):
        require_intelligence_write(request)
        wp: WritePipeline = request.app.state.write_pipeline
        access_headers = store_request_headers(request)
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
                result = await wp.process(write_request, access_headers=access_headers)
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
        result = await rp.search(body, access_headers=store_request_headers(request))
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
            access_headers=store_request_headers(request),
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
            access_headers=store_request_headers(request),
        )
        return {"conflicts": conflicts}

    # ------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------

    @app.post("/v1/pipeline/overview")
    async def pipeline_overview(request: Request, body: dict[str, Any]):
        og: OverviewGenerator = request.app.state.overview_generator
        result = await og.generate(
            scope=body.get("scope") or {},
            access_headers=store_request_headers(request),
        )
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
