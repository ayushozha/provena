from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable

from pydantic import BaseModel, Field

from app.connector_worker import (
    ConnectorExecutionOutcome,
    ConnectorExecutionService,
    ConnectorRunContext,
    ConnectorWorker,
)
from app.models import ConnectorConfig, ConnectorProvider, ConnectorStatus, SyncJob, SyncJobStatus, SyncJobType, utc_now

if TYPE_CHECKING:
    from app.store import ProvenaStore


class ConnectorScheduleConfig(BaseModel):
    enabled: bool = False
    cadence_seconds: int | None = None
    job_type: SyncJobType = SyncJobType.FULL


class ScheduledConnectorSkip(BaseModel):
    connector_id: str
    tenant_id: str
    reason: str


class ScheduledConnectorTick(BaseModel):
    evaluated_at: datetime
    scheduled: list[ConnectorExecutionOutcome] = Field(default_factory=list)
    skipped: list[ScheduledConnectorSkip] = Field(default_factory=list)


class ConnectorSchedulerService:
    def __init__(
        self,
        store: "ProvenaStore",
        execution: ConnectorExecutionService | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.execution = execution or ConnectorExecutionService(store)
        self.clock = clock
        self._provider_workers: dict[str, ConnectorWorker] = {}

    def register_worker(self, provider: ConnectorProvider | str, worker: ConnectorWorker) -> None:
        key = provider.value if isinstance(provider, ConnectorProvider) else str(provider)
        self._provider_workers[key] = worker

    def worker_for(self, provider: ConnectorProvider | str) -> ConnectorWorker:
        key = provider.value if isinstance(provider, ConnectorProvider) else str(provider)
        worker = self._provider_workers.get(key)
        if worker is None:
            raise KeyError(key)
        return worker

    def tick(
        self,
        *,
        tenant_id: str | None = None,
        provider: str | None = None,
        evaluated_at: datetime | None = None,
    ) -> ScheduledConnectorTick:
        tick_at = evaluated_at or self.clock()
        connectors = (
            self.store.list_connectors(tenant_id, provider)
            if tenant_id
            else self.store.list_all_connectors(provider)
        )
        scheduled: list[ConnectorExecutionOutcome] = []
        skipped: list[ScheduledConnectorSkip] = []

        for connector in connectors:
            schedule = self._schedule_for(connector)
            reason = self._skip_reason(connector, schedule, tick_at)
            if reason is not None:
                skipped.append(
                    ScheduledConnectorSkip(
                        connector_id=connector.connector_id,
                        tenant_id=connector.tenant_id,
                        reason=reason,
                    )
                )
                continue

            latest_job = self._latest_job(connector)
            context = ConnectorRunContext(
                connector=connector,
                job_id=f"sync-scheduled-{connector.connector_id}-{uuid.uuid4().hex[:12]}",
                job_type=schedule.job_type,
                cursor=latest_job.cursor if latest_job is not None else None,
                trigger="scheduled",
                started_at=tick_at,
                metadata={
                    "scheduler": schedule.model_dump(mode="json"),
                    "scheduled_tick_at": tick_at.isoformat(),
                },
            )
            try:
                worker = self.worker_for(connector.provider)
            except KeyError:
                skipped.append(
                    ScheduledConnectorSkip(
                        connector_id=connector.connector_id,
                        tenant_id=connector.tenant_id,
                        reason="provider_not_implemented",
                    )
                )
                continue
            scheduled.append(self.execution.execute(worker, context))

        return ScheduledConnectorTick(
            evaluated_at=tick_at,
            scheduled=scheduled,
            skipped=skipped,
        )

    def _schedule_for(self, connector: ConnectorConfig) -> ConnectorScheduleConfig:
        return ConnectorScheduleConfig.model_validate(connector.metadata.get("scheduler") or {})

    def _skip_reason(
        self,
        connector: ConnectorConfig,
        schedule: ConnectorScheduleConfig,
        evaluated_at: datetime,
    ) -> str | None:
        if connector.status != ConnectorStatus.ACTIVE:
            return "connector_inactive"
        if not schedule.enabled:
            return "schedule_disabled"
        if schedule.cadence_seconds is None or schedule.cadence_seconds <= 0:
            return "missing_cadence"

        latest_job = self._latest_job(connector)
        if latest_job is None:
            return None
        if latest_job.status in {SyncJobStatus.QUEUED, SyncJobStatus.RUNNING}:
            return "job_in_progress"

        reference_at = latest_job.finished_at or latest_job.started_at or latest_job.created_at
        if reference_at + timedelta(seconds=schedule.cadence_seconds) > evaluated_at:
            return "not_due"
        return None

    def _latest_job(self, connector: ConnectorConfig) -> SyncJob | None:
        jobs = self.store.list_sync_jobs(connector.connector_id, connector.tenant_id)
        return jobs[0] if jobs else None
