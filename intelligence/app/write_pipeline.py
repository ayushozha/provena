"""Write pipeline - classify, dedupe, embed, resolve, conflict-detect, store."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from app.embeddings import EmbeddingManager
from app.extract_facts import ExtractedFact, FactExtractor
from app.model_router import ModelRouter
from app.models import (
    ACLEntry,
    CompactResponse,
    PipelineTrace,
    ProjectOverview,
    SourceReference,
    TokenUsage,
    WriteRequest,
    WriteResponse,
)

# Polarity markers used to flag a candidate that overlaps the new content but
# negates it (e.g. "X is supported" vs "X is not supported").
_NEGATION_WORDS = frozenset(
    {"not", "never", "no", "don't", "doesn't", "isn't", "wasn't", "aren't", "won't", "can't", "cannot"}
)


class WritePipelineError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class WritePipeline:
    """Orchestrates the full write path for a new memory."""

    def __init__(
        self,
        embedding_manager: EmbeddingManager,
        model_router: ModelRouter,
        overview_generator: Any | None = None,
        store_url: str = "http://localhost:8000",
        orchestration_url: str = "http://localhost:50051",
        fact_extractor: FactExtractor | None = None,
    ) -> None:
        self.embedding_manager = embedding_manager
        self.model_router = model_router
        self.overview_generator = overview_generator
        self.store_url = store_url
        self.orchestration_url = orchestration_url
        self.fact_extractor = fact_extractor or FactExtractor(model_router)
        self._fingerprints: set[str] = set()

    async def process(self, request: WriteRequest) -> WriteResponse:
        start = time.monotonic()
        trace = PipelineTrace()

        kind = request.kind if request.kind not in {"", "auto"} else self._classify(request.content, request.title, request.tags)
        trace.classified_kind = kind

        facts = await self.fact_extractor.extract(request.content, request.title)
        primary: WriteResponse | None = None
        stored_count = 0
        fact_count = len(facts)

        for index, fact in enumerate(facts):
            fact_request = self._fact_request(request, fact, kind, index)
            result = await self._process_one(
                fact_request,
                trace,
                start,
                update_overview=index == len(facts) - 1,
                fact_count=fact_count,
            )
            if result.created:
                stored_count += 1
            if primary is None:
                primary = result
            elif result.created:
                primary.memory.setdefault("extracted_memory_ids", []).append(result.memory.get("memory_id"))

        if primary is None:
            trace.elapsed_ms = (time.monotonic() - start) * 1000
            return WriteResponse(created=False, memory={}, pipeline_trace=trace)

        primary.memory["extraction_count"] = len(facts)
        primary.memory["extraction_stored"] = stored_count
        trace.elapsed_ms = (time.monotonic() - start) * 1000
        primary.pipeline_trace = trace
        return primary

    async def _process_one(
        self,
        request: WriteRequest,
        trace: PipelineTrace,
        start: float,
        update_overview: bool,
        fact_count: int = 1,
    ) -> WriteResponse:
        fingerprint = self._fingerprint(request.content, request.scope)
        is_dup, dup_id = self._deduplicate(fingerprint)
        if is_dup:
            return WriteResponse(created=False, memory={"duplicate_of": dup_id}, pipeline_trace=trace)

        kind = request.kind if request.kind not in {"", "auto"} else self._classify(request.content, request.title, request.tags)
        embedding = self._generate_embedding(request.content)
        trace.embedding_model_used = self.embedding_manager.model_id
        trace.embedding_dimensions = self.embedding_manager.dimensions

        entities = self._resolve_entities(request.content, request.entity_keys)
        trace.resolved_entities = entities

        conflicts = await self._detect_conflicts(request.content, request.scope, entities, embedding)
        trace.detected_conflicts = conflicts

        memory = self._memory_payload(request, kind, embedding, entities, fact_count=fact_count)

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(f"{self.store_url}/v1/memories", json=memory)
                if response.status_code >= 400 and request.memory_id and "UNIQUE" in response.text:
                    memory["memory_id"] = str(uuid.uuid4())
                    response = await client.post(f"{self.store_url}/v1/memories", json=memory)
                if response.status_code >= 400:
                    raise WritePipelineError(response.status_code, response.text or "store rejected write")
                stored = response.json()
                created = bool(stored.get("created", True))
                memory = stored.get("memory", memory)
        except WritePipelineError:
            raise
        except Exception as exc:
            raise WritePipelineError(503, f"store unavailable: {exc}") from exc

        self._fingerprints.add(fingerprint)

        if created:
            await self._index_triggers(memory, request.scope)

        if update_overview:
            trace.overview_updated = await self._update_overview(request.scope)

        return WriteResponse(created=created, memory=memory, pipeline_trace=trace)

    def _fact_request(self, request: WriteRequest, fact: ExtractedFact, kind: str, index: int) -> WriteRequest:
        metadata = {
            **request.metadata,
            "extraction_source": "add_only",
            "extraction_index": index,
        }
        return request.model_copy(
            update={
                "content": fact.content,
                "title": fact.title or request.title,
                "kind": fact.kind if fact.kind not in {"", "auto"} else kind,
                "metadata": metadata,
            }
        )

    def _memory_payload(
        self,
        request: WriteRequest,
        kind: str,
        embedding: list[float],
        entities: list[str],
        fact_count: int = 1,
    ) -> dict[str, Any]:
        extraction_index = int(request.metadata.get("extraction_index", 0))
        memory_id = request.memory_id or str(uuid.uuid4())
        if fact_count > 1:
            memory_id = f"{memory_id}_f{extraction_index}"
        return {
            "memory_id": memory_id,
            "kind": kind,
            "scope": request.scope,
            "content": request.content,
            "title": request.title,
            "summary": request.summary,
            "entity_keys": entities,
            "tags": request.tags,
            "metadata": request.metadata,
            "importance": request.importance,
            "confidence": request.confidence,
            "strength": request.strength,
            "valid_from": request.valid_from.isoformat() if request.valid_from else None,
            "valid_to": request.valid_to.isoformat() if request.valid_to else None,
            "source_references": self._normalise_source_references(request.source_references),
            "supersedes_memory_id": request.supersedes_memory_id,
            "trigger_phrases": request.trigger_phrases,
            "acl": self._normalise_acl(request.acl),
            "embedding_model": self.embedding_manager.model_id,
            "embedding": embedding,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _classify(self, content: str, title: str, tags: list[str]) -> str:
        text = f"{title} {content} {' '.join(tags)}".lower()
        if any(kw in text for kw in ("decided", "decision", "chose", "chosen")):
            return "decision"
        if any(kw in text for kw in ("prefer", "like", "want", "favorite")):
            return "preference"
        if any(kw in text for kw in ("plan", "roadmap", "milestone", "sprint")):
            return "instruction"
        if any(kw in text for kw in ("bug", "issue", "error", "fix")):
            return "artifact"
        if any(kw in text for kw in ("learned", "found", "discovered", "realized")):
            return "fact"
        return "fact"

    def _fingerprint(self, content: str, scope: dict[str, Any]) -> str:
        normalised = content.strip().lower()
        scope_str = json.dumps(scope, sort_keys=True)
        return hashlib.sha256(f"{normalised}|{scope_str}".encode()).hexdigest()

    def _deduplicate(self, fingerprint: str) -> tuple[bool, str | None]:
        if fingerprint in self._fingerprints:
            return True, fingerprint
        return False, None

    def _generate_embedding(self, content: str) -> list[float]:
        return self.embedding_manager.generate(content)

    def _resolve_entities(self, content: str, entity_keys: list[str]) -> list[str]:
        pattern = r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b"
        found = re.findall(pattern, content)
        return list(dict.fromkeys(entity_keys + found))


    @staticmethod
    def _conflict_terms(text: str) -> set[str]:
        """Tokenise content for overlap checks; strip edge punctuation, keep contractions."""
        terms: set[str] = set()
        for word in text.lower().split():
            token = word.strip(".,!?;:\"()[]{}")
            if token:
                terms.add(token)
        return terms

    async def _detect_conflicts(
        self,
        content: str,
        scope: dict[str, Any],
        entity_keys: list[str],
        query_embedding: list[float] | None = None,
    ) -> list[str]:
        """Flag existing memories that overlap the new content but negate it.

        Candidates come from the store's hybrid search (FTS + vector KNN); the
        contradiction signal is shared non-negation terms with opposite
        polarity. Best-effort and advisory (surfaced in the pipeline trace): a
        store hiccup yields no conflicts rather than failing the write.
        """
        candidates = await self._conflict_candidates(content, scope, query_embedding)
        if not candidates:
            return []
        new_terms = self._conflict_terms(content)
        new_negated = bool(new_terms & _NEGATION_WORDS)
        conflicts: list[str] = []
        for item in candidates:
            memory = item.get("memory") or item
            cand_terms = self._conflict_terms(memory.get("content") or "")
            shared = (new_terms & cand_terms) - _NEGATION_WORDS
            if not shared:
                continue
            if new_negated != bool(cand_terms & _NEGATION_WORDS):
                conflicts.append(
                    f"memory {memory.get('memory_id', '?')} may conflict on: "
                    f"{', '.join(sorted(shared)[:5])}"
                )
        return conflicts

    async def _conflict_candidates(
        self,
        content: str,
        scope: dict[str, Any],
        query_embedding: list[float] | None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "query": content,
            "scope": scope,
            "limit": 10,
            "include_relations": False,
        }
        if query_embedding:
            payload["query_embedding"] = query_embedding
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(f"{self.store_url}/v1/memories/search", json=payload)
                if response.status_code >= 400:
                    return []
                return response.json().get("results", [])
        except Exception:
            return []

    async def _index_triggers(self, memory: dict[str, Any], scope: dict[str, Any]) -> bool:
        phrases = memory.get("trigger_phrases") or []
        if not phrases:
            return True
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{self.orchestration_url}/trigger/index",
                    json={
                        "memory_id": memory.get("memory_id"),
                        "phrases": phrases,
                        "tenant_id": scope.get("tenant_id", ""),
                    },
                )
                return response.status_code < 400
        except Exception:
            return False

    async def _update_overview(self, scope: dict[str, Any]) -> bool:
        if not self.overview_generator:
            return False
        tenant_id = scope.get("tenant_id", "")
        project_id = scope.get("project_id", "")
        if not tenant_id or not project_id:
            return False

        try:
            overview: ProjectOverview = await self.overview_generator.generate(scope)
            async with httpx.AsyncClient(timeout=5.0) as client:
                snapshot_response = await client.post(
                    f"{self.store_url}/v1/project-snapshots",
                    json={
                        "tenant_id": overview.tenant_id,
                        "project_id": overview.project_id,
                        "summary": overview.summary,
                        "entity_summary": {entity: 1 for entity in overview.key_entities},
                        "decision_log": overview.recent_decisions,
                        "memory_count": overview.active_memory_count,
                        "created_at": overview.generated_at.isoformat(),
                    },
                )
                cache_response = await client.post(
                    f"{self.orchestration_url}/overview/cache",
                    json={
                        "tenant_id": overview.tenant_id,
                        "project_id": overview.project_id,
                        "summary": overview.summary,
                        "key_entities": overview.key_entities,
                        "recent_decisions": overview.recent_decisions,
                        "active_memory_count": overview.active_memory_count,
                        "generated_at_epoch": int(overview.generated_at.timestamp()),
                        "ttl_seconds": 300,
                    },
                )
                return snapshot_response.status_code < 400 and cache_response.status_code < 400
        except Exception:
            return False

    def _normalise_source_references(
        self,
        source_references: list[SourceReference | str | dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalised: list[dict[str, Any]] = []
        for source in source_references:
            if isinstance(source, SourceReference):
                normalised.append(source.model_dump())
            elif isinstance(source, str):
                normalised.append({"source_type": "uri", "source_id": source, "uri": source})
            elif isinstance(source, dict) and source.get("source_type") and source.get("source_id"):
                normalised.append(SourceReference(**source).model_dump())
        return normalised

    def _normalise_acl(self, acl: list[ACLEntry] | dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(acl, dict):
            return []
        return [
            entry.model_dump() if isinstance(entry, ACLEntry) else ACLEntry(**entry).model_dump()
            for entry in acl
        ]

    async def _compact_memories(
        self,
        memory_ids: list[str],
        scope: dict[str, Any],
        tier: str = "balanced",
    ) -> CompactResponse:
        return CompactResponse(
            compacted_memory={"scope": scope, "source_ids": memory_ids},
            superseded_ids=memory_ids,
            token_usage=TokenUsage(operation="compact"),
        )