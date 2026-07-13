from contextlib import asynccontextmanager
from hashlib import sha256
from secrets import compare_digest

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.connector_scheduler import ConnectorSchedulerService
from app.connector_worker import ConnectorExecutionService
from app.models import (
    ConnectorConfig,
    ConnectorSourceBatch,
    ConnectorSourceRecord,
    DeleteResponse,
    EraseRequest,
    EraseResponse,
    HealthResponse,
    IntegrationCoverageSummary,
    LegalHold,
    MemoryCreate,
    MemoryFeedbackCreate,
    MemoryFeedbackRecord,
    MemoryHistoryEvent,
    MemoryRecord,
    MemoryUpdate,
    MemoryWriteResult,
    PrincipalMapping,
    PrincipalMappingBatch,
    ProjectSnapshot,
    RelationWrite,
    RetentionEnforcementRequest,
    RTBFRequest,
    RTBFResponse,
    RetentionEnforcementResponse,
    RetentionPolicy,
    SearchRequest,
    SearchResponse,
    SourcePermissionBatch,
    SourcePermissionGrant,
    SyncJob,
    TenantIntegrityStatus,
)
from app.store import AccessContext, TenantOwnershipError, TenantParentNotFoundError
from app.store_factory import create_store


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = create_store(settings)
        app.state.store = store
        app.state.connector_execution = ConnectorExecutionService(store)
        app.state.connector_scheduler = ConnectorSchedulerService(
            store,
            app.state.connector_execution,
        )
        try:
            yield
        finally:
            store.close()

    app = FastAPI(
        title="Provena",
        version="0.1.0",
        summary="Provenance-first memory service",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def require_tenant_integrity_readiness(request: Request, call_next):
        if request.url.path.startswith("/v1/"):
            integrity = request.app.state.store.tenant_integrity_status()
            if not integrity["ready"]:
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": "store tenant-integrity migration requires operator attention"
                    },
                )
        return await call_next(request)

    @app.exception_handler(TenantOwnershipError)
    async def tenant_ownership_conflict(
        _request: Request,
        _exc: TenantOwnershipError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": "resource identifier is already owned by another tenant"},
        )

    @app.exception_handler(TenantParentNotFoundError)
    async def tenant_parent_not_found(
        _request: Request,
        exc: TenantParentNotFoundError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"detail": f"{exc.resource} not found"},
        )

    def extract_access(request: Request) -> AccessContext | None:
        configured_token = (
            settings.service_token.get_secret_value().strip()
            if settings.service_token
            else ""
        )
        gateway_token = (
            settings.gateway_service_token.get_secret_value().strip()
            if settings.gateway_service_token
            else ""
        )
        if configured_token or gateway_token or settings.service_identities:
            scheme, _, credential = request.headers.get("Authorization", "").partition(" ")
            presented = credential.strip()
            if scheme.lower() != "bearer" or not presented:
                raise HTTPException(status_code=401, detail="valid service bearer token required")
            presented_digest = sha256(presented.encode("utf-8")).hexdigest()
            matched_identity = None
            for identity in settings.service_identities:
                if compare_digest(presented_digest, identity.token_sha256):
                    matched_identity = identity
            legacy_match = bool(configured_token) and compare_digest(presented, configured_token)
            gateway_match = bool(gateway_token) and compare_digest(presented, gateway_token)
            if matched_identity is None and not legacy_match and not gateway_match:
                raise HTTPException(status_code=401, detail="valid service bearer token required")
            if gateway_match:
                role = request.headers.get("X-Provena-Role", "").strip().lower()
                tenant_id = request.headers.get("X-Provena-Tenant-Id", "").strip()
                principal_id = request.headers.get("X-Provena-Principal-Id", "").strip()
                key_id = request.headers.get("X-Provena-Key-Id", "").strip()
                if (
                    role not in {"viewer", "editor", "admin", "superadmin"}
                    or not principal_id
                    or not key_id
                    or (role != "superadmin" and not tenant_id)
                ):
                    raise HTTPException(
                        status_code=401,
                        detail="valid gateway identity headers required",
                    )
                return AccessContext(
                    tenant_id=tenant_id or None,
                    role=role,
                    principal_id=principal_id,
                    key_id=key_id,
                    groups=[
                        group.strip()
                        for group in request.headers.get("X-Provena-Groups", "").split(",")
                        if group.strip()
                    ],
                )
            if matched_identity is not None:
                return AccessContext(
                    tenant_id=matched_identity.tenant_id,
                    role=matched_identity.role,
                    principal_id=matched_identity.principal_id,
                    key_id=f"service-registry:{matched_identity.token_sha256[:12]}",
                    groups=["provena-service"],
                )
            return AccessContext(
                tenant_id=settings.service_tenant_id,
                role=settings.service_role,
                principal_id=settings.service_principal_id,
                key_id="service-token",
                groups=["provena-service"],
            )

        if not settings.local_auth_bypass_enabled:
            raise HTTPException(status_code=401, detail="service authentication required")

        tenant_id = request.headers.get("X-Provena-Tenant-Id")
        role = request.headers.get("X-Provena-Role")
        principal_id = request.headers.get("X-Provena-Principal-Id")
        key_id = request.headers.get("X-Provena-Key-Id")
        groups = [
            group.strip()
            for group in request.headers.get("X-Provena-Groups", "").split(",")
            if group.strip()
        ]
        if not any([tenant_id, role, principal_id, key_id, groups]):
            return None
        return AccessContext(
            tenant_id=tenant_id,
            role=role or "viewer",
            principal_id=principal_id,
            key_id=key_id,
            groups=groups,
        )

    def require_tenant(access: AccessContext | None, tenant_id: str) -> None:
        if access is not None and access.role != "superadmin" and access.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="tenant mismatch")

    def require_write(access: AccessContext | None) -> None:
        if access is not None and access.role not in {"editor", "admin", "superadmin"}:
            raise HTTPException(status_code=403, detail="write access required")

    def require_admin(access: AccessContext | None) -> None:
        if access is None:
            raise HTTPException(status_code=401, detail="authentication required")
        if access.principal_id is None:
            raise HTTPException(status_code=401, detail="authentication required")
        if access.role not in {"admin", "superadmin"}:
            raise HTTPException(status_code=403, detail="admin access required")

    def _memory_value_error(exc: ValueError) -> HTTPException:
        detail = str(exc)
        if detail == "memory not found":
            status = 404
        elif detail == "memory already exists but is not accessible":
            return HTTPException(status_code=404, detail="memory not found")
        elif (
            "duplicate" in detail.lower()
            or "already exists" in detail.lower()
            or "tombstoned" in detail.lower()
        ):
            status = 409
        else:
            status = 403
        return HTTPException(status_code=status, detail=detail)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(
            service=settings.service_name,
            environment=settings.environment,
            status="ok",
        )

    @app.get(
        "/readyz",
        response_model=TenantIntegrityStatus,
        responses={503: {"model": TenantIntegrityStatus, "description": "Store not ready"}},
    )
    async def readyz(request: Request, response: Response) -> TenantIntegrityStatus:
        integrity = request.app.state.store.tenant_integrity_status()
        response.status_code = 200 if integrity["ready"] else 503
        return TenantIntegrityStatus(**integrity)

    @app.post("/v1/memories", response_model=MemoryWriteResult)
    async def create_memory(payload: MemoryCreate, request: Request) -> MemoryWriteResult:
        access = extract_access(request)
        require_write(access)
        require_tenant(access, payload.scope.tenant_id)
        try:
            return request.app.state.store.create_memory(payload, access=access)
        except ValueError as exc:
            raise _memory_value_error(exc) from exc

    @app.get("/v1/memories/{memory_id}", response_model=MemoryRecord)
    async def get_memory(memory_id: str, request: Request) -> MemoryRecord:
        memory = request.app.state.store.get_memory(
            memory_id,
            access=extract_access(request),
            enforce_source_grants=True,
        )
        if not memory:
            raise HTTPException(status_code=404, detail="memory not found")
        return memory

    @app.post("/v1/memories/search", response_model=SearchResponse)
    async def search_memories(payload: SearchRequest, request: Request) -> SearchResponse:
        access = extract_access(request)
        require_tenant(access, payload.scope.tenant_id)
        effective_limit = min(payload.limit, settings.max_limit)
        if effective_limit != payload.limit:
            payload = payload.model_copy(update={"limit": effective_limit})
        return request.app.state.store.search_memories(payload, access=access)

    @app.post("/v1/memories/relations", status_code=204)
    async def create_relation(payload: RelationWrite, request: Request) -> None:
        access = extract_access(request)
        require_write(access)
        require_tenant(access, payload.scope.tenant_id)
        try:
            request.app.state.store.write_relation(payload, access=access)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.delete("/v1/memories/{memory_id}", response_model=DeleteResponse)
    async def delete_memory(
        memory_id: str,
        request: Request,
        hard_delete: bool = Query(default=False),
    ) -> DeleteResponse:
        access = extract_access(request)
        require_write(access)
        existing = request.app.state.store.get_memory(memory_id, access=access)
        if not existing:
            raise HTTPException(status_code=404, detail="memory not found")
        try:
            return request.app.state.store.delete_memory(memory_id, hard_delete=hard_delete, access=access)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.put("/v1/memories/{memory_id}", response_model=MemoryWriteResult)
    async def update_memory(memory_id: str, payload: MemoryUpdate, request: Request) -> MemoryWriteResult:
        access = extract_access(request)
        require_write(access)
        try:
            return request.app.state.store.update_memory(memory_id, payload, access=access)
        except ValueError as exc:
            raise _memory_value_error(exc) from exc

    @app.get("/v1/memories/{memory_id}/history", response_model=list[MemoryHistoryEvent])
    async def memory_history(
        memory_id: str,
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[MemoryHistoryEvent]:
        access = extract_access(request)
        memory = request.app.state.store.get_memory(
            memory_id,
            access=access,
            enforce_source_grants=True,
        )
        if not memory:
            raise HTTPException(status_code=404, detail="memory not found")
        return request.app.state.store.list_memory_history(memory_id, limit=limit)

    @app.post("/v1/memories/{memory_id}/feedback", response_model=MemoryFeedbackRecord)
    async def add_memory_feedback(memory_id: str, payload: MemoryFeedbackCreate, request: Request) -> MemoryFeedbackRecord:
        access = extract_access(request)
        require_write(access)
        try:
            return request.app.state.store.add_memory_feedback(memory_id, payload, access=access)
        except ValueError as exc:
            raise _memory_value_error(exc) from exc

    @app.get("/v1/memories/{memory_id}/feedback", response_model=list[MemoryFeedbackRecord])
    async def list_memory_feedback(
        memory_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[MemoryFeedbackRecord]:
        try:
            return request.app.state.store.list_memory_feedback(memory_id, access=extract_access(request), limit=limit)
        except ValueError as exc:
            raise _memory_value_error(exc) from exc

    @app.post("/v1/admin/erase", response_model=EraseResponse)
    async def erase_memories(payload: EraseRequest, request: Request) -> EraseResponse:
        access = extract_access(request)
        require_admin(access)
        require_tenant(access, payload.tenant_id)
        return request.app.state.store.erase_scope(payload, access=access)

    @app.post("/v1/project-snapshots", response_model=ProjectSnapshot)
    async def create_project_snapshot(payload: ProjectSnapshot, request: Request) -> ProjectSnapshot:
        access = extract_access(request)
        require_admin(access)
        require_tenant(access, payload.tenant_id)
        return request.app.state.store.upsert_project_snapshot(payload)

    @app.get("/v1/project-snapshots/latest", response_model=ProjectSnapshot)
    async def get_latest_project_snapshot(
        request: Request,
        tenant_id: str = Query(...),
        project_id: str = Query(...),
    ) -> ProjectSnapshot:
        access = extract_access(request)
        if access is not None and access.role != "superadmin" and access.tenant_id not in {None, tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        snapshot = app.state.store.get_latest_project_snapshot(tenant_id, project_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        return snapshot

    @app.post("/v1/integrations/connectors", response_model=ConnectorConfig)
    async def save_connector(payload: ConnectorConfig, request: Request) -> ConnectorConfig:
        access = extract_access(request)
        require_admin(access)
        if access is not None and access.role != "superadmin" and access.tenant_id not in {None, payload.tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        return request.app.state.store.save_connector(payload)

    @app.get("/v1/integrations/connectors", response_model=list[ConnectorConfig])
    async def list_connectors(
        request: Request,
        tenant_id: str = Query(...),
        provider: str | None = Query(default=None),
    ) -> list[ConnectorConfig]:
        access = extract_access(request)
        require_admin(access)
        if access is not None and access.role != "superadmin" and access.tenant_id not in {None, tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        return request.app.state.store.list_connectors(tenant_id, provider)

    @app.get("/v1/integrations/connectors/{connector_id}", response_model=ConnectorConfig)
    async def get_connector(connector_id: str, request: Request, tenant_id: str = Query(...)) -> ConnectorConfig:
        access = extract_access(request)
        require_admin(access)
        if access is not None and access.role != "superadmin" and access.tenant_id not in {None, tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        connector = request.app.state.store.get_connector(connector_id, tenant_id)
        if connector is None:
            raise HTTPException(status_code=404, detail="connector not found")
        return connector

    @app.post("/v1/integrations/connectors/{connector_id}/sources/batch", response_model=list[ConnectorSourceRecord])
    async def save_connector_sources(
        connector_id: str,
        payload: ConnectorSourceBatch,
        request: Request,
        tenant_id: str = Query(...),
    ) -> list[ConnectorSourceRecord]:
        access = extract_access(request)
        require_admin(access)
        if access is not None and access.role != "superadmin" and access.tenant_id not in {None, tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        return request.app.state.store.save_connector_sources(connector_id, tenant_id, payload)

    @app.get("/v1/integrations/connectors/{connector_id}/sources", response_model=list[ConnectorSourceRecord])
    async def list_connector_sources(connector_id: str, request: Request, tenant_id: str = Query(...)) -> list[ConnectorSourceRecord]:
        access = extract_access(request)
        require_admin(access)
        if access is not None and access.role != "superadmin" and access.tenant_id not in {None, tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        return request.app.state.store.list_connector_sources(connector_id, tenant_id)

    @app.post("/v1/integrations/connectors/{connector_id}/principal-mappings/batch", response_model=list[PrincipalMapping])
    async def save_principal_mappings(
        connector_id: str,
        payload: PrincipalMappingBatch,
        request: Request,
        tenant_id: str = Query(...),
    ) -> list[PrincipalMapping]:
        access = extract_access(request)
        require_admin(access)
        if access is not None and access.role != "superadmin" and access.tenant_id not in {None, tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        return request.app.state.store.save_principal_mappings(connector_id, tenant_id, payload)

    @app.get("/v1/integrations/connectors/{connector_id}/principal-mappings", response_model=list[PrincipalMapping])
    async def list_principal_mappings(connector_id: str, request: Request, tenant_id: str = Query(...)) -> list[PrincipalMapping]:
        access = extract_access(request)
        require_admin(access)
        if access is not None and access.role != "superadmin" and access.tenant_id not in {None, tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        return request.app.state.store.list_principal_mappings(connector_id, tenant_id)

    @app.post("/v1/integrations/connectors/{connector_id}/permissions/batch", response_model=list[SourcePermissionGrant])
    async def save_source_permissions(
        connector_id: str,
        payload: SourcePermissionBatch,
        request: Request,
        tenant_id: str = Query(...),
    ) -> list[SourcePermissionGrant]:
        access = extract_access(request)
        require_admin(access)
        if access is not None and access.role != "superadmin" and access.tenant_id not in {None, tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        return request.app.state.store.save_source_permission_grants(connector_id, tenant_id, payload)

    @app.get("/v1/integrations/connectors/{connector_id}/permissions", response_model=list[SourcePermissionGrant])
    async def list_source_permissions(
        connector_id: str,
        request: Request,
        tenant_id: str = Query(...),
        source_id: str | None = Query(default=None),
    ) -> list[SourcePermissionGrant]:
        access = extract_access(request)
        require_admin(access)
        if access is not None and access.role != "superadmin" and access.tenant_id not in {None, tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        return request.app.state.store.list_source_permission_grants(connector_id, tenant_id, source_id)

    @app.post("/v1/integrations/connectors/{connector_id}/sync-jobs", response_model=SyncJob)
    async def save_sync_job(
        connector_id: str,
        payload: SyncJob,
        request: Request,
        tenant_id: str = Query(...),
    ) -> SyncJob:
        access = extract_access(request)
        require_admin(access)
        if access is not None and access.role != "superadmin" and access.tenant_id not in {None, tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        return request.app.state.store.save_sync_job(connector_id, tenant_id, payload)

    @app.get("/v1/integrations/connectors/{connector_id}/sync-jobs", response_model=list[SyncJob])
    async def list_sync_jobs(connector_id: str, request: Request, tenant_id: str = Query(...)) -> list[SyncJob]:
        access = extract_access(request)
        require_admin(access)
        if access is not None and access.role != "superadmin" and access.tenant_id not in {None, tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        return request.app.state.store.list_sync_jobs(connector_id, tenant_id)

    @app.get("/v1/integrations/coverage", response_model=IntegrationCoverageSummary)
    async def integration_coverage(request: Request, tenant_id: str = Query(...)) -> IntegrationCoverageSummary:
        access = extract_access(request)
        require_admin(access)
        if access is not None and access.role != "superadmin" and access.tenant_id not in {None, tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        return request.app.state.store.integration_coverage(tenant_id)

    @app.post("/v1/admin/retention-policies", response_model=RetentionPolicy)
    async def save_retention_policy(payload: RetentionPolicy, request: Request) -> RetentionPolicy:
        access = extract_access(request)
        require_admin(access)
        require_tenant(access, payload.tenant_id)
        return request.app.state.store.save_retention_policy(payload)

    @app.get("/v1/admin/retention-policies", response_model=list[RetentionPolicy])
    async def list_retention_policies(request: Request, tenant_id: str | None = Query(default=None)) -> list[RetentionPolicy]:
        access = extract_access(request)
        require_admin(access)
        if tenant_id is None:
            if access is not None and access.role != "superadmin":
                raise HTTPException(status_code=403, detail="tenant_id is required for non-superadmin requests")
        elif access is not None and access.role != "superadmin" and access.tenant_id not in {None, tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        return request.app.state.store.list_retention_policies(tenant_id)

    @app.post("/v1/admin/legal-hold", response_model=LegalHold)
    async def place_legal_hold(payload: LegalHold, request: Request) -> LegalHold:
        access = extract_access(request)
        require_admin(access)
        require_tenant(access, payload.tenant_id)
        return request.app.state.store.place_legal_hold(payload)

    @app.delete("/v1/admin/legal-hold/{hold_id}")
    async def release_legal_hold(request: Request, hold_id: str, tenant_id: str = Query(default="")) -> dict[str, bool]:
        access = extract_access(request)
        require_admin(access)
        if access is not None and access.role != "superadmin":
            if not tenant_id:
                raise HTTPException(
                    status_code=403,
                    detail="tenant_id is required for non-superadmin requests",
                )
            require_tenant(access, tenant_id)
        released = app.state.store.release_legal_hold(
            hold_id,
            tenant_id=tenant_id or None,
        )
        if not released:
            raise HTTPException(status_code=404, detail="legal hold not found")
        return {"released": True}

    @app.post("/v1/admin/rtbf", response_model=RTBFResponse)
    async def right_to_be_forgotten(payload: RTBFRequest, request: Request) -> RTBFResponse:
        access = extract_access(request)
        require_admin(access)
        if access is not None and access.role != "superadmin" and access.tenant_id not in {None, payload.tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        return request.app.state.store.rtbf(payload)

    @app.post("/v1/admin/retention/enforce", response_model=RetentionEnforcementResponse)
    async def enforce_retention(payload: RetentionEnforcementRequest, request: Request) -> RetentionEnforcementResponse:
        access = extract_access(request)
        require_admin(access)
        if payload.tenant_id is None:
            if access is not None and access.role != "superadmin":
                raise HTTPException(status_code=403, detail="tenant_id is required for non-superadmin requests")
        elif access is not None and access.role != "superadmin" and access.tenant_id not in {None, payload.tenant_id}:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        return request.app.state.store.enforce_retention(payload.tenant_id)

    def openapi_schema() -> dict[str, object]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
        )
        security_schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
        security_schemes["ProvenaServiceBearer"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "Tenant-bound Provena service token.",
        }
        for path, path_item in schema["paths"].items():
            if not path.startswith("/v1/"):
                continue
            for operation in path_item.values():
                if isinstance(operation, dict) and "responses" in operation:
                    operation["security"] = [{"ProvenaServiceBearer": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = openapi_schema

    return app


app = create_app()
