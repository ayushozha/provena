from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.connector_worker import ConnectorRunContext, ConnectorRunResult
from app.models import (
    ConnectorSourceBatch,
    ConnectorSourceRecord,
    PermissionLevel,
    PrincipalMapping,
    PrincipalMappingBatch,
    SourcePermissionBatch,
    SourcePermissionGrant,
    SourceSyncStatus,
    SyncJobStatus,
)


class SlackConnectorStubWorker:
    def run(self, context: ConnectorRunContext) -> ConnectorRunResult:
        started_at = context.started_at
        finished_at = started_at + timedelta(minutes=1)
        workspace_name = self._workspace_name(context)
        remote_workspace_id = context.connector.remote_workspace_id or "slack-workspace"
        sources = self._build_sources(context, workspace_name, remote_workspace_id, started_at, finished_at)
        mappings = self._build_principal_mappings(context, workspace_name, finished_at)
        permissions = self._build_permissions(context, sources)

        stats: dict[str, Any] = {
            "provider": "slack",
            "stub": True,
            "workspace_name": workspace_name,
            "remote_workspace_id": remote_workspace_id,
            "sources_seen": len(sources),
            "mappings_seen": len(mappings),
            "grants_seen": len(permissions),
            "inventory_kinds": sorted({source.source_type for source in sources}),
            "trigger": context.trigger,
        }
        if "scheduler" in context.metadata:
            scheduler = context.metadata.get("scheduler") or {}
            stats["scheduler"] = "fixed_cadence"
            if isinstance(scheduler, dict) and "cadence_seconds" in scheduler:
                stats["cadence_seconds"] = scheduler["cadence_seconds"]

        return ConnectorRunResult(
            status=SyncJobStatus.SUCCEEDED,
            cursor=f"slack:{remote_workspace_id}:channels:v1",
            started_at=started_at,
            finished_at=finished_at,
            stats=stats,
            sources=ConnectorSourceBatch(sources=sources),
            principal_mappings=PrincipalMappingBatch(mappings=mappings),
            permissions=SourcePermissionBatch(grants=permissions),
        )

    def _build_sources(
        self,
        context: ConnectorRunContext,
        workspace_name: str,
        remote_workspace_id: str,
        started_at,
        finished_at,
    ) -> list[ConnectorSourceRecord]:
        freshness = max(context.connector.freshness_sla_seconds, 60)
        stale_synced_at = started_at - timedelta(seconds=freshness * 2)
        stale_after = stale_synced_at + timedelta(seconds=freshness)
        channel_metadata = {
            "provider": "slack",
            "stub": True,
            "workspace_name": workspace_name,
            "remote_workspace_id": remote_workspace_id,
        }
        return [
            ConnectorSourceRecord(
                source_id=f"{context.connector.connector_id}-roadmap",
                connector_id=context.connector.connector_id,
                tenant_id=context.connector.tenant_id,
                remote_source_id="C201",
                source_type="channel",
                display_name="#roadmap",
                path="/channels/roadmap",
                status=SourceSyncStatus.STALE,
                last_synced_at=stale_synced_at,
                stale_after=stale_after,
                metadata={
                    **channel_metadata,
                    "channel_name": "roadmap",
                    "conversation_type": "public_channel",
                    "topic": "Product roadmap decisions",
                },
            ),
            ConnectorSourceRecord(
                source_id=f"{context.connector.connector_id}-release-ops",
                connector_id=context.connector.connector_id,
                tenant_id=context.connector.tenant_id,
                remote_source_id="C202",
                source_type="channel",
                display_name="#release-ops",
                path="/channels/release-ops",
                status=SourceSyncStatus.INDEXED,
                last_synced_at=finished_at,
                stale_after=finished_at + timedelta(seconds=freshness),
                metadata={
                    **channel_metadata,
                    "channel_name": "release-ops",
                    "conversation_type": "public_channel",
                    "topic": "Release readiness and incidents",
                },
            ),
        ]

    def _build_principal_mappings(
        self,
        context: ConnectorRunContext,
        workspace_name: str,
        finished_at,
    ) -> list[PrincipalMapping]:
        return [
            PrincipalMapping(
                mapping_id=f"{context.connector.connector_id}-pm-1",
                connector_id=context.connector.connector_id,
                tenant_id=context.connector.tenant_id,
                principal_type="user",
                local_principal_id="pm-1",
                remote_principal_id="U201",
                remote_name=f"{workspace_name} PM",
                groups=["product", "slack-admins"],
                last_synced_at=finished_at,
                updated_at=finished_at,
            )
        ]

    def _build_permissions(
        self,
        context: ConnectorRunContext,
        sources: list[ConnectorSourceRecord],
    ) -> list[SourcePermissionGrant]:
        roadmap_source, release_source = sources
        return [
            SourcePermissionGrant(
                grant_id=f"{context.connector.connector_id}-roadmap-view",
                source_id=roadmap_source.source_id,
                connector_id=context.connector.connector_id,
                tenant_id=context.connector.tenant_id,
                principal_type="user",
                principal_id="pm-1",
                permission_level=PermissionLevel.VIEW,
            ),
            SourcePermissionGrant(
                grant_id=f"{context.connector.connector_id}-release-edit",
                source_id=release_source.source_id,
                connector_id=context.connector.connector_id,
                tenant_id=context.connector.tenant_id,
                principal_type="group",
                principal_id="leadership",
                permission_level=PermissionLevel.EDIT,
                inherited=False,
            ),
        ]

    def _workspace_name(self, context: ConnectorRunContext) -> str:
        workspace_name = context.connector.metadata.get("workspace_name")
        if isinstance(workspace_name, str) and workspace_name.strip():
            return workspace_name.strip()
        return context.connector.display_name
