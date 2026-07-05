"""In-memory mock store matching Contract B shapes for offline evals."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

_SECRET_PATTERNS = (
    re.compile(r"sk-[a-zA-Z0-9-]{8,19}"),
    re.compile(r"ghp_[a-zA-Z0-9_]{8,19}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA )?PRIVATE KEY-----"),
)


def redact_secrets(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


@dataclass
class MockMemory:
    memory_id: str
    kind: str
    content: str
    summary: str
    scope: dict[str, Any]
    status: str = "active"
    supersedes_memory_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


class MockStore:
    """Minimal store for eval scorecards without a live server."""

    def __init__(self) -> None:
        self.memories: dict[str, MockMemory] = {}

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = redact_secrets(str(payload.get("content") or ""))
        memory_id = str(payload.get("memory_id") or uuid.uuid4())
        superseded = payload.get("supersedes_memory_id")
        if superseded and superseded in self.memories:
            self.memories[superseded].status = "superseded"

        memory = MockMemory(
            memory_id=memory_id,
            kind=str(payload.get("kind") or "fact"),
            content=content,
            summary=str(payload.get("summary") or content[:200]),
            scope=dict(payload.get("scope") or {}),
            supersedes_memory_id=superseded,
            metadata=dict(payload.get("metadata") or {}),
            tags=[str(t) for t in (payload.get("tags") or [])],
        )
        self.memories[memory_id] = memory
        return {"created": True, "memory": self._as_dict(memory)}

    def search(
        self,
        query: str,
        *,
        kinds: list[str] | None = None,
        max_characters: int | None = None,
    ) -> list[dict[str, Any]]:
        words = {w.strip(".,?").lower() for w in query.split() if len(w.strip(".,?")) > 2}
        scored: list[tuple[float, MockMemory]] = []
        for memory in self.memories.values():
            if memory.status != "active":
                continue
            if kinds and memory.kind not in kinds:
                continue
            path = str(memory.metadata.get("path") or "").lower()
            hay = f"{memory.content} {memory.summary} {' '.join(memory.tags)} {path}".lower()
            hay_words = {w.strip(".,?/") for w in hay.split()}
            overlap = len(words & hay_words)
            if overlap == 0 and query.lower() not in hay:
                continue
            score = float(overlap)
            if path:
                path_hits = sum(1 for w in words if w in path)
                score += path_hits * 2.0
            if memory.kind == "mistake":
                score += 0.5
            scored.append((score, memory))
        scored.sort(key=lambda item: item[0], reverse=True)

        results: list[dict[str, Any]] = []
        chars = 0
        for score, memory in scored:
            body = self._as_dict(memory)
            excerpt = body["content"][:200]
            if max_characters is not None:
                if chars + len(excerpt) > max_characters:
                    break
                chars += len(excerpt)
            results.append(
                {
                    "memory": body,
                    "score": score,
                    "reasons": [f"score={score}"],
                    "related_memories": [],
                }
            )
        return results

    def agent_context(self, task: str, *, max_characters: int = 4000) -> dict[str, Any]:
        hits = self.search(task, max_characters=max_characters)
        lines = []
        citations = []
        for item in hits:
            memory = item["memory"]
            lines.append(f"[{memory['kind']}] {memory.get('summary') or memory['content']}")
            citations.append({"memory_id": memory["memory_id"], "excerpt": memory["content"][:120]})
        context = "\n".join(lines)
        return {
            "context": context,
            "token_estimate": max(1, len(context) // 4),
            "citations": citations,
            "reasons_by_memory": {item["memory"]["memory_id"]: item["reasons"] for item in hits},
        }

    @staticmethod
    def _as_dict(memory: MockMemory) -> dict[str, Any]:
        return {
            "memory_id": memory.memory_id,
            "kind": memory.kind,
            "status": memory.status,
            "content": memory.content,
            "summary": memory.summary,
            "scope": memory.scope,
            "metadata": memory.metadata,
            "tags": memory.tags,
            "supersedes_memory_id": memory.supersedes_memory_id,
        }