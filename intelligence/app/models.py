"""Pydantic models for the Provena intelligence layer."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ModelTier(str, enum.Enum):
    FAST = "fast"
    BALANCED = "balanced"
    QUALITY = "quality"


# ---------------------------------------------------------------------------
# Pipeline trace
# ---------------------------------------------------------------------------

class PipelineTrace(BaseModel):
    classified_kind: str = ""
    deduplicated: bool = False
    embedding_model_used: str = ""
    embedding_dimensions: int = 0
    resolved_entities: list[str] = Field(default_factory=list)
    detected_conflicts: list[str] = Field(default_factory=list)
    overview_updated: bool = False
    elapsed_ms: float = 0.0


# ---------------------------------------------------------------------------
# Embedding info
# ---------------------------------------------------------------------------

class EmbeddingModelInfo(BaseModel):
    model_id: str
    provider: str
    dimensions: int
    max_tokens: int = 512
    version: str = "1.0"
    is_default: bool = False


# ---------------------------------------------------------------------------
# Token usage / budgeting
# ---------------------------------------------------------------------------

class TokenUsage(BaseModel):
    tenant_id: str = ""
    operation: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    cost_usd: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SourceReference(BaseModel):
    source_type: str
    source_id: str
    uri: str | None = None
    title: str | None = None
    excerpt: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ACLEntry(BaseModel):
    principal_id: str
    principal_type: str
    permissions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Write pipeline
# ---------------------------------------------------------------------------

class WriteRequest(BaseModel):
    kind: str = "fact"
    memory_id: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    content: str
    title: str = ""
    summary: str = ""
    entity_keys: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    importance: float = 0.5
    confidence: float = 0.7
    strength: float = 0.7
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_references: list[SourceReference | str | dict[str, Any]] = Field(default_factory=list)
    supersedes_memory_id: str | None = None
    trigger_phrases: list[str] = Field(default_factory=list)
    acl: list[ACLEntry] | dict[str, Any] = Field(default_factory=list)


class WriteResponse(BaseModel):
    created: bool = False
    memory: dict[str, Any] = Field(default_factory=dict)
    pipeline_trace: PipelineTrace = Field(default_factory=PipelineTrace)


# ---------------------------------------------------------------------------
# Living-memory capture
# ---------------------------------------------------------------------------

class CaptureRequest(BaseModel):
    text: str
    signal_type: str = "message"
    scope: dict[str, Any] = Field(default_factory=dict)
    source_references: list[SourceReference | str | dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    error_signature: str = ""


class CaptureResultItem(BaseModel):
    created: bool
    kind: str
    memory_id: str | None = None
    superseded_id: str | None = None
    reason: str = ""


class CaptureResponse(BaseModel):
    results: list[CaptureResultItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Read / Search pipeline
# ---------------------------------------------------------------------------

class ReadSearchRequest(BaseModel):
    query: str
    scope: dict[str, Any] = Field(default_factory=dict)
    kinds: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    entity_keys: list[str] = Field(default_factory=list)
    query_embedding: list[float] = Field(default_factory=list)
    include_relations: bool = False
    include_deleted: bool = False
    limit: int = 20
    max_tokens: int = 4096
    model_tier: str = "balanced"


class ReadSearchResponse(BaseModel):
    results: list[dict[str, Any]] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    budget_remaining: int = 0


class ScoredMemory(BaseModel):
    memory_id: str
    content: str = ""
    title: str = ""
    memory: dict[str, Any] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    related_memories: list[dict[str, Any]] = Field(default_factory=list)
    vector_score: float = 0.0
    fts_score: float = 0.0
    combined: float = 0.0


class Citation(BaseModel):
    memory_id: str
    title: str = ""
    excerpt: str = ""
    source_references: list[dict[str, Any] | str] = Field(default_factory=list)
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# Compact pipeline
# ---------------------------------------------------------------------------

class CompactRequest(BaseModel):
    scope: dict[str, Any] = Field(default_factory=dict)
    memory_ids: list[str] = Field(default_factory=list)
    tier: str = "balanced"


class CompactResponse(BaseModel):
    compacted_memory: dict[str, Any] = Field(default_factory=dict)
    superseded_ids: list[str] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)


# ---------------------------------------------------------------------------
# Conflict / contradiction
# ---------------------------------------------------------------------------

class ConflictPair(BaseModel):
    memory_id_a: str
    memory_id_b: str
    description: str = ""
    resolution: str = ""
    confidence: float = 0.0


class ContradictionPair(BaseModel):
    memory_id_a: str
    memory_id_b: str
    description: str = ""
    severity: str = "low"


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

class ProjectOverview(BaseModel):
    tenant_id: str = ""
    project_id: str = ""
    summary: str = ""
    key_entities: list[str] = Field(default_factory=list)
    recent_decisions: list[str] = Field(default_factory=list)
    active_memory_count: int = 0
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------

class TriggerHit(BaseModel):
    phrase: str
    memory_id: str
    match_score: float = 0.0
