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


class MemoryLayer(str, Enum):
    CONVERSATION = "conversation"
    SESSION = "session"
    USER = "user"
    AGENT = "agent"
    ORGANIZATION = "organization"


class MemoryFeedbackType(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    CORRECTION = "correction"
    PIN = "pin"


class MemoryHistoryEventType(str, Enum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    FEEDBACK = "feedback"


class MemoryCreate(BaseModel):
    memory_id: str | None = None
    kind: MemoryKind
    memory_layer: MemoryLayer | None = None
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
    expires_at: datetime | None = None
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
    memory_layer: MemoryLayer = MemoryLayer.USER
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
    expires_at: datetime | None = None
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
    memory_layers: list[MemoryLayer] = Field(default_factory=list)
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


class MemoryUpdate(BaseModel):
    kind: MemoryKind | None = None
    memory_layer: MemoryLayer | None = None
    title: str | None = None
    content: str | None = None
    summary: str | None = None
    entity_keys: list[str] | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    importance: float | None = None
    confidence: float | None = None
    strength: float | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    expires_at: datetime | None = None
    embedding_model: str | None = None
    embedding: list[float] | None = None
    acl: list[ACLEntry] | None = None
    source_references: list[SourceReference] | None = None
    trigger_phrases: list[str] | None = None
    supersedes_memory_id: str | None = None


class MemoryHistoryEvent(BaseModel):
    history_id: str
    memory_id: str
    event: MemoryHistoryEventType
    actor_id: str | None = None
    old_memory: dict[str, Any] = Field(default_factory=dict)
    new_memory: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class MemoryFeedbackCreate(BaseModel):
    feedback_id: str | None = None
    feedback_type: MemoryFeedbackType
    principal_id: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryFeedbackRecord(BaseModel):
    feedback_id: str
    memory_id: str
    tenant_id: str
    feedback_type: MemoryFeedbackType
    principal_id: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class MemoryFeedbackSummary(BaseModel):
    positive: int = 0
    negative: int = 0
    correction: int = 0
    pin: int = 0
    latest_feedback_at: datetime | None = None
    net_score: float = 0.0


class AuditEvent(BaseModel):
    audit_id: str
    action: str
    memory_id: str | None = None
    actor_id: str | None = None
    tenant_id: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ConnectedSourceInspection(BaseModel):
    source_id: str
    connector_id: str
    source_type: str
    display_name: str
    status: SourceSyncStatus | None = None
    grant_count: int = 0
    last_synced_at: datetime | None = None
    stale_after: datetime | None = None
    freshness_deadline: datetime | None = None


class MemoryStorageState(BaseModel):
    fingerprint: str
    embedding_dimensions: int = 0
    source_reference_count: int = 0
    connected_source_count: int = 0
    trigger_phrase_count: int = 0
    relation_count: int = 0
    audit_event_count: int = 0
    fts_indexed: bool = False
    trigger_indexed: bool = False


class MemoryInspectionResponse(BaseModel):
    memory: MemoryRecord
    storage: MemoryStorageState
    connected_sources: list[ConnectedSourceInspection] = Field(default_factory=list)
    related_memories: list[RelatedMemory] = Field(default_factory=list)
    audit_trail: list[AuditEvent] = Field(default_factory=list)
    history: list[MemoryHistoryEvent] = Field(default_factory=list)
    feedback_summary: MemoryFeedbackSummary


class AgentContextRequest(BaseModel):
    query: str
    scope: ScopeEnvelope
    kinds: list[MemoryKind] = Field(default_factory=list)
    memory_layers: list[MemoryLayer] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    entity_keys: list[str] = Field(default_factory=list)
    max_memories: int = 10
    max_characters: int = 4000
    include_citations: bool = True


class AgentContextMemory(BaseModel):
    memory_id: str
    title: str | None = None
    kind: MemoryKind
    memory_layer: MemoryLayer
    content: str
    score: float
    reasons: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    updated_at: datetime


class AgentContextResponse(BaseModel):
    query: str
    scope: ScopeEnvelope
    context: str
    token_estimate: int = 0
    memories: list[AgentContextMemory] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


class TemporalGraphRequest(BaseModel):
    scope: ScopeEnvelope
    query: str | None = None
    seed_memory_ids: list[str] = Field(default_factory=list)
    memory_layers: list[MemoryLayer] = Field(default_factory=list)
    include_expired: bool = False
    depth: int = 1
    limit: int = Field(default=50, ge=1, le=500)


class TemporalGraphNode(BaseModel):
    memory: MemoryRecord
    observed_at: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    expires_at: datetime | None = None
    layer: MemoryLayer


class TemporalGraphEdge(BaseModel):
    from_memory_id: str
    to_memory_id: str
    relation: RelationKind
    created_at: datetime


class TemporalGraphResponse(BaseModel):
    scope: ScopeEnvelope
    query: str | None = None
    seed_memory_ids: list[str] = Field(default_factory=list)
    nodes: list[TemporalGraphNode] = Field(default_factory=list)
    edges: list[TemporalGraphEdge] = Field(default_factory=list)


class SearchExplainCandidate(BaseModel):
    memory: MemoryRecord
    score: float
    reasons: list[str] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)
    fts_rank: int | None = None
    rank: int | None = None


class SearchExplainResponse(BaseModel):
    query: str
    query_terms: list[str] = Field(default_factory=list)
    candidate_strategy: str
    total_candidates: int = 0
    returned: list[SearchExplainCandidate] = Field(default_factory=list)
    not_returned: list[SearchExplainCandidate] = Field(default_factory=list)
    filtered_out: list[SearchExplainCandidate] = Field(default_factory=list)


class StorageTierOverview(BaseModel):
    memories_total: int = 0
    memories_active: int = 0
    memories_superseded: int = 0
    memories_deleted: int = 0
    memories_held: int = 0
    conversation_layer_total: int = 0
    session_layer_total: int = 0
    user_layer_total: int = 0
    agent_layer_total: int = 0
    organization_layer_total: int = 0
    source_references_total: int = 0
    connected_sources_total: int = 0
    relations_total: int = 0
    trigger_phrases_total: int = 0
    entity_registry_total: int = 0
    token_usage_total: int = 0
    evidence_cache_total: int = 0
    replication_state_total: int = 0
    embedding_models_total: int = 0
    fts_indexed_memories: int = 0
    vectorized_memories: int = 0
    project_snapshots_total: int = 0
    audit_events_total: int = 0
    history_events_total: int = 0
    feedback_total: int = 0
    connectors_total: int = 0
    principal_mappings_total: int = 0
    permission_grants_total: int = 0
    sync_jobs_total: int = 0


class MemoryArchitectureOverview(BaseModel):
    tenant_id: str
    generated_at: datetime
    storage: StorageTierOverview
    coverage: IntegrationCoverageSummary


class OnboardingStep(BaseModel):
    step_id: str
    label: str
    status: str
    detail: str
    required: bool = True


class OnboardingStatus(BaseModel):
    tenant_id: str
    generated_at: datetime
    ready: bool = False
    cache_backend: str
    steps: list[OnboardingStep] = Field(default_factory=list)
    recommended_next_actions: list[str] = Field(default_factory=list)


class ProductionEvidenceMetric(BaseModel):
    name: str
    value: Any
    unit: str | None = None
    status: str
    detail: str | None = None


class ProductionUseCaseEvidence(BaseModel):
    name: str
    readiness: str
    detail: str | None = None


class ProductionEvidenceReport(BaseModel):
    tenant_id: str
    generated_at: datetime
    cache_backend: str
    scale_level: str
    metrics: list[ProductionEvidenceMetric] = Field(default_factory=list)
    use_cases: list[ProductionUseCaseEvidence] = Field(default_factory=list)
    benchmark_command: str
    gaps: list[str] = Field(default_factory=list)
