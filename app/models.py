from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class MemoryKind(str, Enum):
    FACT = "fact"
    EPISODE = "episode"
    ARTIFACT = "artifact"
    DECISION = "decision"
    RELATION = "relation"
    PREFERENCE = "preference"
    INSTRUCTION = "instruction"
    METRIC = "metric"
    STAKEHOLDER = "stakeholder"
    SOURCE = "source"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DELETED = "deleted"
    HELD = "held"


class RelationKind(str, Enum):
    RELATED_TO = "related_to"
    SUPPORTS = "supports"
    DERIVED_FROM = "derived_from"
    SUPERSEDES = "supersedes"
    CONFLICTS_WITH = "conflicts_with"


class ScopeEnvelope(BaseModel):
    tenant_id: str
    workspace_id: str | None = None
    project_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None


class SourceReference(BaseModel):
    source_type: str
    source_id: str
    uri: str | None = None
    title: str | None = None
    excerpt: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ACLPermission(str, Enum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    SHARE = "share"
    ADMIN = "admin"


class ACLEntry(BaseModel):
    principal_id: str
    principal_type: str
    permissions: list[ACLPermission] = Field(default_factory=list)


class MemoryCreate(BaseModel):
    memory_id: str | None = None
    kind: MemoryKind
    scope: ScopeEnvelope
    content: str
    title: str | None = None
    summary: str | None = None
    entity_keys: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    strength: float = Field(default=0.7, ge=0.0, le=1.0)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_references: list[SourceReference] = Field(default_factory=list)
    supersedes_memory_id: str | None = None
    trigger_phrases: list[str] = Field(default_factory=list)
    acl: list[ACLEntry] = Field(default_factory=list)
    embedding_model: str | None = None
    embedding: list[float] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_time_window(self) -> "MemoryCreate":
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to must be greater than or equal to valid_from")
        return self


class MemoryRecord(BaseModel):
    memory_id: str
    fingerprint: str
    kind: MemoryKind
    status: MemoryStatus
    scope: ScopeEnvelope
    title: str | None = None
    content: str
    summary: str | None = None
    entity_keys: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    importance: float
    confidence: float
    strength: float
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_verified_at: datetime | None = None
    source_references: list[SourceReference] = Field(default_factory=list)
    acl: list[ACLEntry] = Field(default_factory=list)
    trigger_phrases: list[str] = Field(default_factory=list)
    embedding_model: str | None = None
    embedding: list[float] = Field(default_factory=list)
    held: bool = False
    hold_reason: str | None = None
    hold_until: datetime | None = None


class MemoryWriteResult(BaseModel):
    created: bool
    memory: MemoryRecord


class SearchRequest(BaseModel):
    query: str
    scope: ScopeEnvelope
    kinds: list[MemoryKind] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    entity_keys: list[str] = Field(default_factory=list)
    query_embedding: list[float] = Field(default_factory=list)
    include_relations: bool = True
    include_deleted: bool = False
    limit: int = Field(default=10, ge=1, le=100)


class RelatedMemory(BaseModel):
    relation: RelationKind
    memory: MemoryRecord


class SearchResult(BaseModel):
    memory: MemoryRecord
    score: float
    reasons: list[str] = Field(default_factory=list)
    related_memories: list[RelatedMemory] = Field(default_factory=list)


class SearchResponse(BaseModel):
    results: list[SearchResult]


class RelationWrite(BaseModel):
    from_memory_id: str
    to_memory_id: str
    relation: RelationKind
    scope: ScopeEnvelope


class DeleteResponse(BaseModel):
    memory_id: str
    deleted: bool
    hard_delete: bool


class EraseRequest(BaseModel):
    tenant_id: str
    workspace_id: str | None = None
    project_id: str | None = None
    user_id: str | None = None

    @model_validator(mode="after")
    def validate_selector(self) -> "EraseRequest":
        if not any([self.workspace_id, self.project_id, self.user_id]):
            raise ValueError("erase requires at least one secondary scope selector")
        return self


class EraseResponse(BaseModel):
    deleted_memories: int
    deleted_sources: int
    deleted_relations: int


class HealthResponse(BaseModel):
    service: str
    environment: str
    status: str


class ConnectorProvider(str, Enum):
    SLACK = "slack"
    GOOGLE_DRIVE = "google_drive"
    SHAREPOINT = "sharepoint"
    NOTION = "notion"
    CONFLUENCE = "confluence"
    JIRA = "jira"
    GITHUB = "github"
    SALESFORCE = "salesforce"
    CUSTOM = "custom"


class ConnectorStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"


class ConnectorAuthType(str, Enum):
    OAUTH = "oauth"
    API_KEY = "api_key"
    SERVICE_ACCOUNT = "service_account"
    WEBHOOK = "webhook"


class SyncMode(str, Enum):
    POLL = "poll"
    WEBHOOK = "webhook"
    HYBRID = "hybrid"
    MANUAL = "manual"


class SourceSyncStatus(str, Enum):
    INDEXED = "indexed"
    PENDING = "pending"
    STALE = "stale"
    ERROR = "error"


class SyncJobType(str, Enum):
    FULL = "full"
    INCREMENTAL = "incremental"
    ACL_SYNC = "acl_sync"
    PRINCIPAL_SYNC = "principal_sync"


class SyncJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PermissionLevel(str, Enum):
    VIEW = "view"
    COMMENT = "comment"
    EDIT = "edit"
    OWNER = "owner"


class ConnectorConfig(BaseModel):
    connector_id: str
    tenant_id: str
    provider: ConnectorProvider
    display_name: str
    remote_workspace_id: str | None = None
    auth_type: ConnectorAuthType
    sync_mode: SyncMode = SyncMode.HYBRID
    status: ConnectorStatus = ConnectorStatus.ACTIVE
    principal_sync_enabled: bool = True
    acl_sync_enabled: bool = True
    freshness_sla_seconds: int = 3600
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_synced_at: datetime | None = None
    last_webhook_at: datetime | None = None


class ConnectorSourceRecord(BaseModel):
    source_id: str
    connector_id: str
    tenant_id: str
    remote_source_id: str
    source_type: str
    display_name: str
    path: str | None = None
    status: SourceSyncStatus = SourceSyncStatus.INDEXED
    last_synced_at: datetime | None = None
    stale_after: datetime | None = None
    acl_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ConnectorSourceBatch(BaseModel):
    sources: list[ConnectorSourceRecord] = Field(default_factory=list)


class PrincipalMapping(BaseModel):
    mapping_id: str
    connector_id: str
    tenant_id: str
    principal_type: str
    local_principal_id: str
    remote_principal_id: str
    remote_name: str | None = None
    groups: list[str] = Field(default_factory=list)
    last_synced_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PrincipalMappingBatch(BaseModel):
    mappings: list[PrincipalMapping] = Field(default_factory=list)


class SourcePermissionGrant(BaseModel):
    grant_id: str
    source_id: str
    connector_id: str
    tenant_id: str
    principal_type: str
    principal_id: str
    permission_level: PermissionLevel
    inherited: bool = True
    remote_permission_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class SourcePermissionBatch(BaseModel):
    grants: list[SourcePermissionGrant] = Field(default_factory=list)


class SyncJob(BaseModel):
    job_id: str
    connector_id: str
    tenant_id: str
    job_type: SyncJobType
    status: SyncJobStatus = SyncJobStatus.QUEUED
    cursor: str | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class IntegrationCoverageSummary(BaseModel):
    tenant_id: str
    connectors_total: int = 0
    connectors_healthy: int = 0
    sources_total: int = 0
    sources_stale: int = 0
    sources_past_freshness_sla: int = Field(
        default=0,
        description="Number of sources whose freshness deadline has already passed.",
    )
    sources_error: int = 0
    principals_mapped: int = 0
    grants_total: int = 0
    sync_jobs_running: int = 0
    last_synced_at: datetime | None = None
    missing_foundations: list[str] = Field(default_factory=list)


class ProjectSnapshot(BaseModel):
    tenant_id: str
    project_id: str
    summary: str
    entity_summary: dict[str, int] = Field(default_factory=dict)
    decision_log: list[str] = Field(default_factory=list)
    memory_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)


class RetentionPolicy(BaseModel):
    policy_id: str
    tenant_id: str
    kind: str | None = None
    max_age_days: int = 365
    action: str = "delete_soft"
    created_at: datetime = Field(default_factory=utc_now)


class LegalHold(BaseModel):
    hold_id: str
    tenant_id: str
    memory_ids: list[str] = Field(default_factory=list)
    scope: dict[str, Any] | None = None
    reason: str
    hold_until: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class RTBFRequest(BaseModel):
    tenant_id: str


class RTBFResponse(BaseModel):
    deleted_memories: int
    held_memories: list[str] = Field(default_factory=list)


class RetentionEnforcementRequest(BaseModel):
    tenant_id: str | None = None


class RetentionEnforcementResponse(BaseModel):
    expired_memory_ids: list[str] = Field(default_factory=list)
