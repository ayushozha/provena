from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.models import (
    ConnectorConfig,
    ConnectorSourceBatch,
    ConnectorSourceRecord,
    IntegrationCoverageSummary,
    PrincipalMapping,
    PrincipalMappingBatch,
    SourcePermissionBatch,
    SourcePermissionGrant,
    SyncJob,
    SyncJobStatus,
    SyncJobType,
    utc_now,
)

if TYPE_CHECKING:
    from app.store import ProvenaStore


class ConnectorRunContext(BaseModel):
    connector: ConnectorConfig
    job_id: str
    job_type: SyncJobType = SyncJobType.FULL
    cursor: str | None = None
    trigger: str = "manual"
    started_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectorRunResult(BaseModel):
    sources: ConnectorSourceBatch = Field(default_factory=ConnectorSourceBatch)
    principal_mappings: PrincipalMappingBatch = Field(default_factory=PrincipalMappingBatch)
    permissions: SourcePermissionBatch = Field(default_factory=SourcePermissionBatch)
    status: SyncJobStatus = SyncJobStatus.SUCCEEDED
    job_type: SyncJobType | None = None
    cursor: str | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def to_sync_job(self, context: ConnectorRunContext) -> SyncJob:
        finished_at = self.finished_at
        if finished_at is None and self.status in {SyncJobStatus.SUCCEEDED, SyncJobStatus.FAILED}:
            finished_at = utc_now()
        return SyncJob(
            job_id=context.job_id,
            connector_id=context.connector.connector_id,
            tenant_id=context.connector.tenant_id,
            job_type=self.job_type or context.job_type,
            status=self.status,
            cursor=self.cursor if self.cursor is not None else context.cursor,
            stats=dict(self.stats),
            error_message=self.error_message,
            started_at=self.started_at or context.started_at,
            finished_at=finished_at,
        )


class ConnectorExecutionOutcome(BaseModel):
    connector: ConnectorConfig
    sync_job: SyncJob
    sources: list[ConnectorSourceRecord] = Field(default_factory=list)
    principal_mappings: list[PrincipalMapping] = Field(default_factory=list)
    permissions: list[SourcePermissionGrant] = Field(default_factory=list)
    coverage: IntegrationCoverageSummary


@runtime_checkable
class ConnectorWorker(Protocol):
    def run(self, context: ConnectorRunContext) -> ConnectorRunResult: ...


class ConnectorExecutionService:
    def __init__(self, store: "ProvenaStore") -> None:
        self.store = store

    def execute(self, worker: ConnectorWorker, context: ConnectorRunContext) -> ConnectorExecutionOutcome:
        result = worker.run(context)
        sources = result.sources
        principal_mappings = (
            result.principal_mappings if context.connector.principal_sync_enabled else PrincipalMappingBatch()
        )
        permissions = result.permissions if context.connector.acl_sync_enabled else SourcePermissionBatch()

        self._validate_sources(context, sources)
        self._validate_mappings(context, principal_mappings)
        self._validate_permissions(context, permissions)

        connector_id = context.connector.connector_id
        tenant_id = context.connector.tenant_id

        saved_sources = (
            self.store.save_connector_sources(connector_id, tenant_id, sources) if sources.sources else []
        )
        saved_mappings = (
            self.store.save_principal_mappings(connector_id, tenant_id, principal_mappings)
            if principal_mappings.mappings
            else []
        )
        saved_permissions = (
            self.store.save_source_permission_grants(connector_id, tenant_id, permissions)
            if permissions.grants
            else []
        )

        sync_job = result.to_sync_job(context)
        sync_job = sync_job.model_copy(
            update={
                "stats": {
                    **sync_job.stats,
                    "sources_emitted": len(result.sources.sources),
                    "principal_mappings_emitted": len(result.principal_mappings.mappings),
                    "permissions_emitted": len(result.permissions.grants),
                    "sources_written": len(saved_sources),
                    "principal_mappings_written": len(saved_mappings),
                    "permissions_written": len(saved_permissions),
                }
            }
        )
        saved_job = self.store.save_sync_job(connector_id, tenant_id, sync_job)
        saved_connector = context.connector
        if saved_job.status == SyncJobStatus.SUCCEEDED:
            synced_at = saved_job.finished_at or saved_job.started_at or utc_now()
            saved_connector = self.store.save_connector(
                context.connector.model_copy(
                    update={
                        "last_synced_at": synced_at,
                        "updated_at": synced_at,
                    }
                )
            )
        coverage = self.store.integration_coverage(tenant_id)
        return ConnectorExecutionOutcome(
            connector=saved_connector,
            sync_job=saved_job,
            sources=saved_sources,
            principal_mappings=saved_mappings,
            permissions=saved_permissions,
            coverage=coverage,
        )

    def _validate_sources(self, context: ConnectorRunContext, batch: ConnectorSourceBatch) -> None:
        for source in batch.sources:
            self._validate_scope(
                record_kind="source",
                record_id=source.source_id,
                record_connector_id=source.connector_id,
                record_tenant_id=source.tenant_id,
                context=context,
            )

    def _validate_mappings(self, context: ConnectorRunContext, batch: PrincipalMappingBatch) -> None:
        for mapping in batch.mappings:
            self._validate_scope(
                record_kind="principal mapping",
                record_id=mapping.mapping_id,
                record_connector_id=mapping.connector_id,
                record_tenant_id=mapping.tenant_id,
                context=context,
            )

    def _validate_permissions(self, context: ConnectorRunContext, batch: SourcePermissionBatch) -> None:
        for grant in batch.grants:
            self._validate_scope(
                record_kind="permission grant",
                record_id=grant.grant_id,
                record_connector_id=grant.connector_id,
                record_tenant_id=grant.tenant_id,
                context=context,
            )

    def _validate_scope(
        self,
        *,
        record_kind: str,
        record_id: str,
        record_connector_id: str,
        record_tenant_id: str,
        context: ConnectorRunContext,
    ) -> None:
        expected_connector_id = context.connector.connector_id
        expected_tenant_id = context.connector.tenant_id
        if record_connector_id != expected_connector_id:
            raise ValueError(
                f"{record_kind} {record_id} has connector_id {record_connector_id!r}, expected {expected_connector_id!r}"
            )
        if record_tenant_id != expected_tenant_id:
            raise ValueError(
                f"{record_kind} {record_id} has tenant_id {record_tenant_id!r}, expected {expected_tenant_id!r}"
            )
