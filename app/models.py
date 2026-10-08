from datetime import UTC, datetime
from enum import Enum
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_repository_json_value(value: Any, path: str = "event") -> None:
    if isinstance(value, str):
        if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError(f"{path} contains text that cannot be persisted portably")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} must contain only finite JSON numbers")
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_repository_json_value(key, f"{path}.key")
            _validate_repository_json_value(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_repository_json_value(item, f"{path}[{index}]")


_REPOSITORY_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bsk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{16,}\b",
        r"\bsk_[A-Za-z0-9_-]{16,}\b",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
        r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
        r"\bglpat-[A-Za-z0-9_-]{16,}\b",
        r"\bsk_live_[A-Za-z0-9]{16,}\b",
        r"\bAIza[A-Za-z0-9_-]{30,}\b",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s/@]+@",
        r"\b(?:password|passwd|token|secret|api[_-]?key)[\"']?\s*[:=]\s*[\"']?[^\s\"',}]{8,}",
    )
)


def _assert_no_repository_secret_material(value: Any) -> None:
    serialized = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if any(pattern.search(serialized) for pattern in _REPOSITORY_SECRET_PATTERNS):
        raise ValueError(
            "repository memory appears to contain a credential or private key"
        )


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
    WORKFLOW = "workflow"
    MISTAKE = "mistake"
    HANDOFF = "handoff"
    INVARIANT = "invariant"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DELETED = "deleted"
    HELD = "held"
    RETRACTED = "retracted"


class RelationKind(str, Enum):
    RELATED_TO = "related_to"
    SUPPORTS = "supports"
    DERIVED_FROM = "derived_from"
    DEFINED_IN = "defined_in"
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


class RepositoryMemoryEventKind(str, Enum):
    FACT = "fact"
    DECISION = "decision"
    WORKFLOW = "workflow"
    MISTAKE = "mistake"
    PREFERENCE = "preference"
    HANDOFF = "handoff"
    INVARIANT = "invariant"


class RepositoryMemorySubjectType(str, Enum):
    REPO = "repo"
    FILE = "file"
    SYMBOL = "symbol"
    COMMAND = "command"
    TEST = "test"
    API = "api"
    ARCHITECTURE = "architecture"
    TASK = "task"


class RepositoryMemoryEventStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class RepositoryMemoryAuthority(str, Enum):
    HUMAN = "human"
    AGENT = "agent"
    TOOL = "tool"
    SYSTEM = "system"


class RepositoryMemorySensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


MAX_REPOSITORY_LEDGER_BYTES = 64 * 1024 * 1024


def _validate_repository_relative_path(
    value: str,
    field: str,
    *,
    allow_repository_root: bool = False,
) -> None:
    normalized = value.replace("\\", "/")
    if allow_repository_root and normalized == ".":
        return
    if normalized != value or normalized.startswith("/") or normalized == ".":
        raise ValueError(f"{field} must be a normalized repository-relative file path")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError(f"{field} must stay inside the repository")
    if len(normalized) >= 2 and normalized[1] == ":":
        raise ValueError(f"{field} must be repository-relative")


def _utf16_sort_key(value: str) -> bytes:
    """Match JavaScript's locale-independent UTF-16 code-unit ordering."""
    return value.encode("utf-16-be", errors="surrogatepass")


def _canonical_repository_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _trimmed_repository_text(value: str) -> str:
    """Match the ledger writer's requiredText(): trim boundaries only."""
    return value.strip()


class RepositoryMemorySource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    symbol: str | None = Field(default=None, max_length=50_000)
    start_line: int | None = Field(default=None, ge=1, strict=True)
    end_line: int | None = Field(default=None, ge=1, strict=True)
    blob: str | None = Field(default=None, max_length=50_000)
    commit: str | None = Field(default=None, max_length=50_000)

    @model_validator(mode="after")
    def validate_repository_source(self) -> "RepositoryMemorySource":
        _validate_repository_relative_path(self.path, "source path")
        for name in ("symbol", "blob", "commit"):
            value = getattr(self, name)
            if value is not None and (
                not value or value != _canonical_repository_text(value)
            ):
                raise ValueError(f"source {name} must be a canonical trimmed string")
        if self.start_line is not None and self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("source end_line must not precede start_line")
        return self


class RepositoryMemoryProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str = Field(min_length=1, max_length=512)
    method: Literal["explicit", "observed", "imported"]
    agent: str | None = Field(default=None, max_length=50_000)
    session_id: str | None = Field(default=None, max_length=50_000)
    command: str | None = Field(default=None, max_length=8192)

    @model_validator(mode="after")
    def validate_canonical_provenance(self) -> "RepositoryMemoryProvenance":
        if self.actor != _trimmed_repository_text(self.actor):
            raise ValueError("provenance.actor must be a canonical trimmed string")
        for name in ("agent", "session_id", "command"):
            value = getattr(self, name)
            if value is not None and (
                not value or value != _canonical_repository_text(value)
            ):
                raise ValueError(f"provenance.{name} must be a canonical trimmed string")
        return self


class RepositoryMemoryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
    kind: RepositoryMemoryEventKind
    subject_type: RepositoryMemorySubjectType
    title: str = Field(min_length=1, max_length=512)
    body: str = Field(min_length=1, max_length=50_000)
    structured_data: dict[str, Any] = Field(default_factory=dict)
    status: RepositoryMemoryEventStatus
    applies_to: list[str] = Field(default_factory=list, max_length=256)
    sources: list[RepositoryMemorySource] = Field(default_factory=list, max_length=256)
    provenance: RepositoryMemoryProvenance
    authority: RepositoryMemoryAuthority
    confidence: float = Field(ge=0.0, le=1.0, strict=True)
    importance: float = Field(ge=0.0, le=1.0, strict=True)
    sensitivity: RepositoryMemorySensitivity
    created_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
    updated_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
    supersedes: list[str] = Field(default_factory=list, max_length=256)
    tags: list[str] = Field(default_factory=list, max_length=256)
    triggers: list[str] = Field(default_factory=list, max_length=256)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_strict_schema_version(cls, value: Any) -> Any:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def validate_canonical_event(self) -> "RepositoryMemoryEvent":
        portable_event = self.model_dump(mode="python")
        _validate_repository_json_value(portable_event)
        _assert_no_repository_secret_material(portable_event)
        created_at = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        updated_at = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
        if updated_at < created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.id in self.supersedes:
            raise ValueError("a memory event cannot supersede itself")
        if self.sensitivity not in {
            RepositoryMemorySensitivity.PUBLIC,
            RepositoryMemorySensitivity.INTERNAL,
        }:
            raise ValueError(
                "confidential and restricted events cannot originate from the Git-tracked repository ledger"
            )
        if (
            self.title != _trimmed_repository_text(self.title)
            or self.body != _trimmed_repository_text(self.body)
        ):
            raise ValueError("title and body must be canonical boundary-trimmed strings")
        if len(json.dumps(self.structured_data, ensure_ascii=False, separators=(",", ":"))) > 100_000:
            raise ValueError("structured_data must not exceed 100000 serialized characters")
        for name, values in {
            "applies_to": self.applies_to,
            "supersedes": self.supersedes,
            "tags": self.tags,
            "triggers": self.triggers,
        }.items():
            if any(
                not value or value != _canonical_repository_text(value)
                for value in values
            ):
                raise ValueError(f"{name} entries must be canonical non-empty strings")
            if values != sorted(set(values), key=_utf16_sort_key):
                raise ValueError(f"{name} must be sorted and contain no duplicates")
        for path in self.applies_to:
            _validate_repository_relative_path(
                path,
                "applies_to path",
                allow_repository_root=True,
            )
        expected_sources = sorted(
            self.sources,
            key=lambda source: (
                _utf16_sort_key(source.path),
                source.start_line or 0,
                _utf16_sort_key(source.symbol or ""),
            ),
        )
        if self.sources != expected_sources:
            raise ValueError("sources must use canonical repository path and span ordering")
        has_rationale = "rationale" in self.structured_data
        if (self.kind in {RepositoryMemoryEventKind.DECISION, RepositoryMemoryEventKind.PREFERENCE} or has_rationale) and self.provenance.method != "explicit":
            raise ValueError("decision, preference, and rationale events must be explicit")
        if self.kind in {RepositoryMemoryEventKind.DECISION, RepositoryMemoryEventKind.PREFERENCE} and self.authority not in {RepositoryMemoryAuthority.HUMAN, RepositoryMemoryAuthority.AGENT}:
            raise ValueError("decision and preference events require human or agent authority")
        return self


class RepositoryMemorySyncScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=512)
    project_id: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_canonical_scope(self) -> "RepositoryMemorySyncScope":
        if self.tenant_id != self.tenant_id.strip() or self.project_id != self.project_id.strip():
            raise ValueError("repository sync scope identifiers must be boundary-trimmed")
        for value in (self.tenant_id, self.project_id):
            if any(
                ord(character) < 0x20
                or 0x7F <= ord(character) <= 0x9F
                or 0xD800 <= ord(character) <= 0xDFFF
                for character in value
            ):
                raise ValueError(
                    "repository sync scope identifiers must not contain control characters or surrogates"
                )
        return self


class RepositoryMemorySyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    scope: RepositoryMemorySyncScope
    ledger_path: Literal[".provena/memory/events.jsonl"]
    memory_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    ledger_bytes: int = Field(ge=0, le=MAX_REPOSITORY_LEDGER_BYTES, strict=True)
    ledger: str = Field(max_length=MAX_REPOSITORY_LEDGER_BYTES)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_strict_schema_version(cls, value: Any) -> Any:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def validate_repository_scope(self) -> "RepositoryMemorySyncRequest":
        return self


class RepositoryMemorySyncTimings(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    validation: float = Field(alias="validate", ge=0.0)
    write: float = Field(ge=0.0)
    relations: float = Field(ge=0.0)
    total: float = Field(ge=0.0)


class RepositoryMemorySyncResponse(BaseModel):
    schema_version: Literal[1] = 1
    repository_id: str
    ledger_fingerprint: str
    events_fingerprint: str
    received_events: int = Field(ge=0)
    created_memories: int = Field(ge=0)
    unchanged_memories: int = Field(ge=0)
    repaired_memories: int = Field(default=0, ge=0)
    suppressed_events: int = Field(default=0, ge=0)
    status_updates: int = Field(ge=0)
    created_relations: int = Field(ge=0)
    repaired_relations: int = Field(default=0, ge=0)
    unchanged_relations: int = Field(ge=0)
    suppressed_relations: int = Field(default=0, ge=0)
    checkpoint_updated: bool
    no_op: bool
    duration_ms: float = Field(ge=0.0)
    timings_ms: RepositoryMemorySyncTimings


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


class TenantIntegrityStatus(BaseModel):
    status: str
    ready: bool
    backend: str
    constraints_validated: bool
    issues: dict[str, int] = Field(default_factory=dict)


class ConnectorProvider(str, Enum):
    # Membership here only validates the provider field on connector records;
    # it does NOT imply a shipped first-party sync worker for that provider.
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
    max_memories: int = Field(default=10, ge=1, le=200)
    max_characters: int = Field(default=4000, ge=128, le=100_000)
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
    fts_rank: float | None = None
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
    memories_retracted: int = 0
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
