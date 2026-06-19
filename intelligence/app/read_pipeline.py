"""Read pipeline - trigger lookup, hybrid retrieve, rerank, cite, budget."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app.embeddings import EmbeddingManager
from app.llm import LLMClient
from app.model_router import ModelRouter
from app.models import (
    Citation,
    ContradictionPair,
    ModelTier,
    ReadSearchRequest,
    ReadSearchResponse,
    ScoredMemory,
    TokenUsage,
    TriggerHit,
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _with_combined(memory: ScoredMemory, combined: float) -> ScoredMemory:
    """Copy a ScoredMemory with a new combined score (all else preserved)."""
    return ScoredMemory(
        memory_id=memory.memory_id,
        content=memory.content,
        title=memory.title,
        memory=memory.memory,
        reasons=memory.reasons,
        related_memories=memory.related_memories,
        vector_score=memory.vector_score,
        fts_score=memory.fts_score,
        combined=combined,
    )


class ReadPipeline:
    """Orchestrates the full read/search path."""

    def __init__(
        self,
        embedding_manager: EmbeddingManager,
        model_router: ModelRouter,
        store_url: str = "http://localhost:8000",
        orchestration_url: str = "http://localhost:50051",
        llm: LLMClient | None = None,
    ) -> None:
        self.embedding_manager = embedding_manager
        self.model_router = model_router
        self.store_url = store_url
        self.orchestration_url = orchestration_url
        self.llm = llm or LLMClient(model_router)
        self.min_combined_score = float(os.environ.get("PROVENA_ABSTAIN_MIN_SCORE", "0.15"))

    async def search(
        self,
        request: ReadSearchRequest,
        access_headers: dict[str, str] | None = None,
    ) -> ReadSearchResponse:
        trigger_hits = await self._trigger_lookup(request.query, request.scope)
        candidates = await self._hybrid_retrieve(
            request.query,
            request.scope,
            request.limit,
            query_embedding=request.query_embedding or None,
            access_headers=access_headers,
        )
        ranked = await self._rerank(request.query, candidates)
        if ranked and ranked[0].combined < self.min_combined_score:
            return ReadSearchResponse(
                results=[],
                token_usage=TokenUsage(
                    operation="abstain_low_evidence",
                    tenant_id=request.scope.get("tenant_id", ""),
                ),
                budget_remaining=request.max_tokens,
            )
        contradictions = await self._detect_contradictions(ranked)
        trimmed, tokens_used = await self._apply_context_budget(ranked, request.max_tokens)
        citations = self._package_citations([self._citation_payload(memory) for memory in trimmed])

        results: list[dict[str, Any]] = []
        for memory in trimmed:
            payload = memory.memory or {
                "memory_id": memory.memory_id,
                "title": memory.title,
                "content": memory.content,
            }
            results.append(
                {
                    "memory": payload,
                    "score": memory.combined,
                    "reasons": memory.reasons,
                    "related_memories": memory.related_memories,
                    "citations": [c.model_dump() for c in citations if c.memory_id == memory.memory_id],
                    "trigger_hits": [h.model_dump() for h in trigger_hits if h.memory_id == memory.memory_id],
                    "contradictions": [
                        pair.model_dump()
                        for pair in contradictions
                        if memory.memory_id in {pair.memory_id_a, pair.memory_id_b}
                    ],
                }
            )

        return ReadSearchResponse(
            results=results,
            token_usage=TokenUsage(
                operation="search",
                input_tokens=tokens_used,
                tenant_id=request.scope.get("tenant_id", ""),
            ),
            budget_remaining=max(0, request.max_tokens - tokens_used),
        )

    async def _trigger_lookup(self, query: str, scope: dict[str, Any]) -> list[TriggerHit]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{self.orchestration_url}/trigger/lookup",
                    json={
                        "user_message": query,
                        "tenant_id": scope.get("tenant_id", ""),
                    },
                )
                if response.status_code < 400:
                    data = response.json()
                    return [TriggerHit(**hit) for hit in data.get("hits", [])]
        except Exception:
            pass
        return []

    async def _hybrid_retrieve(
        self,
        query: str,
        scope: dict[str, Any],
        limit: int = 20,
        vector_weight: float = 0.45,
        query_embedding: list[float] | None = None,
        access_headers: dict[str, str] | None = None,
    ) -> list[ScoredMemory]:
        resolved_embedding = query_embedding or self.embedding_manager.generate(query)
        fts_results = await self._fts_search(
            query,
            scope,
            limit,
            resolved_embedding,
            access_headers=access_headers,
        )
        if not fts_results:
            return []

        embeddings = {
            item.memory_id: item.memory.get("embedding", [])
            for item in fts_results
            if item.memory.get("embedding")
        }
        vector_scores = {
            item.memory_id: item.vector_score
            for item in self._vector_search(resolved_embedding, embeddings, limit, fts_results)
        }

        merged: list[ScoredMemory] = []
        for item in fts_results:
            vector_score = vector_scores.get(item.memory_id, 0.0)
            combined = item.fts_score * (1 - vector_weight) + vector_score * vector_weight
            merged.append(
                ScoredMemory(
                    memory_id=item.memory_id,
                    content=item.content,
                    title=item.title,
                    memory=item.memory,
                    reasons=item.reasons,
                    related_memories=item.related_memories,
                    fts_score=item.fts_score,
                    vector_score=vector_score,
                    combined=combined,
                )
            )
        merged.sort(key=lambda item: item.combined, reverse=True)
        return merged[:limit]

    async def _fts_search(
        self,
        query: str,
        scope: dict[str, Any],
        limit: int,
        query_embedding: list[float],
        access_headers: dict[str, str] | None = None,
    ) -> list[ScoredMemory]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self.store_url}/v1/memories/search",
                    json={
                        "query": query,
                        "scope": scope,
                        "limit": limit,
                        "query_embedding": query_embedding,
                        "include_relations": True,
                    },
                    headers=access_headers,
                )
                if response.status_code >= 400:
                    return []
                data = response.json()
                results: list[ScoredMemory] = []
                for item in data.get("results", []):
                    memory = item.get("memory", {})
                    results.append(
                        ScoredMemory(
                            memory_id=memory.get("memory_id", ""),
                            content=memory.get("content", ""),
                            title=memory.get("title", ""),
                            memory=memory,
                            reasons=item.get("reasons", []),
                            related_memories=item.get("related_memories", []),
                            fts_score=float(item.get("score", 0.0)),
                            combined=float(item.get("score", 0.0)),
                        )
                    )
                return results
        except Exception:
            return []

    def _vector_search(
        self,
        query_embedding: list[float],
        stored_embeddings: dict[str, list[float]],
        limit: int,
        records: list[ScoredMemory],
    ) -> list[ScoredMemory]:
        record_map = {record.memory_id: record for record in records}
        scored: list[ScoredMemory] = []
        for memory_id, embedding in stored_embeddings.items():
            similarity = self.embedding_manager.cosine_similarity(query_embedding, embedding)
            record = record_map.get(memory_id)
            scored.append(
                ScoredMemory(
                    memory_id=memory_id,
                    content=record.content if record else "",
                    title=record.title if record else "",
                    memory=record.memory if record else {},
                    reasons=record.reasons if record else [],
                    related_memories=record.related_memories if record else [],
                    vector_score=similarity,
                    combined=similarity,
                )
            )
        scored.sort(key=lambda item: item.vector_score, reverse=True)
        return scored[:limit]

    async def _rerank(self, query: str, candidates: list[ScoredMemory]) -> list[ScoredMemory]:
        """Reorder candidates by relevance to the query. Uses the LLM to score
        relevance when configured; otherwise falls back to term-overlap."""
        if self.llm.enabled and candidates:
            llm_ranked = await self._llm_rerank(query, candidates)
            if llm_ranked is not None:
                return llm_ranked
        return self._rerank_heuristic(query, candidates)

    async def _llm_rerank(
        self, query: str, candidates: list[ScoredMemory]
    ) -> list[ScoredMemory] | None:
        # Ask the model to score each candidate 0..1 for relevance to the query.
        items = [{"id": m.memory_id, "content": m.content[:500]} for m in candidates]
        payload = await self.llm.chat_json(
            task="rerank",
            tier=ModelTier.BALANCED,
            system="You score how relevant each memory is to a search query. Reply with JSON only.",
            user=(
                f"Query: {query}\n\nMemories (JSON): {json.dumps(items)}\n\n"
                'Return JSON {"scores": [{"id": "<memory id>", "score": <0..1>}]} '
                "with one entry per memory, score = relevance to the query."
            ),
        )
        scores = payload.get("scores") if isinstance(payload, dict) else None
        if not isinstance(scores, list):
            return None
        by_id = {
            str(s.get("id")): float(s.get("score"))
            for s in scores
            if isinstance(s, dict) and s.get("id") is not None and _is_number(s.get("score"))
        }
        if not by_id:
            return None
        # Blend the LLM relevance into the retrieval score so vector/FTS signal
        # still counts; missing ids keep their original combined score.
        reranked = [
            _with_combined(m, m.combined + by_id.get(m.memory_id, 0.0) * 0.5) for m in candidates
        ]
        reranked.sort(key=lambda item: item.combined, reverse=True)
        return reranked

    def _rerank_heuristic(self, query: str, candidates: list[ScoredMemory]) -> list[ScoredMemory]:
        query_terms = set(query.lower().split())
        reranked: list[ScoredMemory] = []
        for memory in candidates:
            content_terms = set(memory.content.lower().split())
            overlap = len(query_terms & content_terms) / len(query_terms) if query_terms else 0.0
            combined = memory.combined + overlap * 0.3
            reranked.append(
                ScoredMemory(
                    memory_id=memory.memory_id,
                    content=memory.content,
                    title=memory.title,
                    memory=memory.memory,
                    reasons=memory.reasons,
                    related_memories=memory.related_memories,
                    vector_score=memory.vector_score,
                    fts_score=memory.fts_score,
                    combined=combined,
                )
            )
        reranked.sort(key=lambda item: item.combined, reverse=True)
        return reranked

    async def _detect_contradictions(
        self, memories: list[ScoredMemory]
    ) -> list[ContradictionPair]:
        """Find contradicting memory pairs. Uses the LLM to judge semantic
        contradiction when configured; otherwise falls back to the negation
        keyword heuristic."""
        if self.llm.enabled and len(memories) > 1:
            llm_pairs = await self._llm_detect_contradictions(memories)
            if llm_pairs is not None:
                return llm_pairs
        return self._detect_contradictions_heuristic(memories)

    async def _llm_detect_contradictions(
        self, memories: list[ScoredMemory]
    ) -> list[ContradictionPair] | None:
        items = [{"id": m.memory_id, "content": m.content[:500]} for m in memories]
        valid_ids = {m.memory_id for m in memories}
        payload = await self.llm.chat_json(
            task="classify",
            tier=ModelTier.BALANCED,
            system="You detect factual contradictions between memories. Reply with JSON only.",
            user=(
                f"Memories (JSON): {json.dumps(items)}\n\n"
                'Return JSON {"contradictions": [{"id_a": "...", "id_b": "...", '
                '"description": "...", "severity": "low|medium|high"}]} listing only '
                "pairs that genuinely contradict each other (a fact and its negation, "
                "incompatible values). Return an empty list if none."
            ),
        )
        raw = payload.get("contradictions") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return None
        pairs: list[ContradictionPair] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            id_a, id_b = str(item.get("id_a", "")), str(item.get("id_b", ""))
            # Only trust ids the model was actually given.
            if id_a not in valid_ids or id_b not in valid_ids or id_a == id_b:
                continue
            severity = item.get("severity")
            pairs.append(
                ContradictionPair(
                    memory_id_a=id_a,
                    memory_id_b=id_b,
                    description=str(item.get("description", "")).strip() or "Contradiction detected",
                    severity=severity if severity in {"low", "medium", "high"} else "medium",
                )
            )
        return pairs

    def _detect_contradictions_heuristic(
        self, memories: list[ScoredMemory]
    ) -> list[ContradictionPair]:
        negation_words = {"not", "never", "no", "don't", "doesn't", "isn't", "wasn't", "aren't", "won't", "can't", "cannot"}
        pairs: list[ContradictionPair] = []
        for index in range(len(memories)):
            for other_index in range(index + 1, len(memories)):
                first = set(memories[index].content.lower().split())
                second = set(memories[other_index].content.lower().split())
                overlap = (first & second) - negation_words
                first_neg = first & negation_words
                second_neg = second & negation_words
                if overlap and ((first_neg and not second_neg) or (second_neg and not first_neg)):
                    pairs.append(
                        ContradictionPair(
                            memory_id_a=memories[index].memory_id,
                            memory_id_b=memories[other_index].memory_id,
                            description=f"Potential negation conflict on shared terms: {list(overlap)[:5]}",
                            severity="medium",
                        )
                    )
        return pairs

    def _citation_payload(self, memory: ScoredMemory) -> dict[str, Any]:
        payload = dict(memory.memory)
        payload["combined"] = memory.combined
        payload.setdefault("memory_id", memory.memory_id)
        payload.setdefault("title", memory.title)
        payload.setdefault("content", memory.content)
        return payload

    def _package_citations(self, memories: list[dict[str, Any]]) -> list[Citation]:
        citations: list[Citation] = []
        for memory in memories:
            source_references = memory.get("source_references", [])
            citations.append(
                Citation(
                    memory_id=memory.get("memory_id", ""),
                    title=memory.get("title", ""),
                    excerpt=memory.get("content", "")[:200],
                    source_references=source_references if isinstance(source_references, list) else [],
                    confidence=float(memory.get("combined", 0.0)),
                )
            )
        return citations

    async def _apply_context_budget(
        self,
        memories: list[ScoredMemory],
        max_tokens: int,
    ) -> tuple[list[ScoredMemory], int]:
        candidates = [
            {
                "memory_id": memory.memory_id,
                "token_count": max(1, len(memory.content) // 4),
                "score": memory.combined,
                "importance": float(memory.memory.get("importance", 0.5)),
            }
            for memory in memories
        ]
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{self.orchestration_url}/budget",
                    json={
                        "candidates": candidates,
                        "total_budget": max_tokens,
                        "system_tokens": 0,
                        "user_tokens": 0,
                    },
                )
                if response.status_code < 400:
                    data = response.json()
                    selected = set(data.get("selected_memory_ids", []))
                    trimmed = [memory for memory in memories if memory.memory_id in selected]
                    return trimmed, int(data.get("memory_tokens", 0))
        except Exception:
            pass

        result: list[ScoredMemory] = []
        token_count = 0
        for memory in memories:
            estimated = max(1, len(memory.content) // 4)
            if token_count + estimated > max_tokens:
                break
            result.append(memory)
            token_count += estimated
        return result, token_count
