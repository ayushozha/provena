"""Workflow/mistake capture — turn agent signals into durable store memories."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

_CORRECTION_MARKERS = (
    "use ",
    "not ",
    "always ",
    "never ",
    "we always",
    "we never",
    "instead of",
    "correction:",
    "actually,",
    "don't use",
    "do not use",
    "prefer ",
)
_PREFERENCE_MARKERS = ("prefer", "like better", "favorite", "rather than")
_DECISION_MARKERS = ("decided", "decision:", "chose", "going with", "we will")
_HANDOFF_MARKERS = ("handoff", "hand off", "passing to", "next owner", "context for")


class StoreClient(Protocol):
    async def search(self, query: str, scope: dict[str, Any], *, kind: str | None = None) -> list[dict[str, Any]]:
        ...

    async def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...


@dataclass
class CaptureSignal:
    """Normalized input from hooks, transcripts, or git events."""

    text: str
    signal_type: str = "message"
    scope: dict[str, Any] = field(default_factory=dict)
    source_references: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    error_signature: str = ""


@dataclass
class CaptureResult:
    created: bool
    kind: str
    memory_id: str | None = None
    superseded_id: str | None = None
    reason: str = ""


class HttpStoreClient:
    """Thin HTTP client for Contract B store writes."""

    def __init__(self, store_url: str, timeout: float = 10.0) -> None:
        self.store_url = store_url.rstrip("/")
        self.timeout = timeout

    async def search(
        self,
        query: str,
        scope: dict[str, Any],
        *,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"query": query, "scope": scope, "limit": 10}
        if kind:
            payload["kinds"] = [kind]
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.store_url}/v1/memories/search", json=payload)
            if response.status_code >= 400:
                return []
            return [item.get("memory") or item for item in response.json().get("results", [])]

    async def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.store_url}/v1/memories", json=payload)
            response.raise_for_status()
            body = response.json()
            memory = body.get("memory") or payload
            return {"created": bool(body.get("created", True)), "memory": memory}


class CaptureEngine:
    """Turn corrections, failures, decisions, and handoffs into store memories."""

    def __init__(self, store: StoreClient) -> None:
        self.store = store
        self._failure_counts: dict[str, int] = {}
        self._mistake_written: set[str] = set()
        self._session_fingerprints: set[str] = set()

    async def process(self, signal: CaptureSignal) -> list[CaptureResult]:
        text = signal.text.strip()
        if not text:
            return []

        if signal.signal_type == "tool_failure" or signal.error_signature:
            result = await self._capture_repeated_failure(signal)
            return [result] if result else []

        if self._is_handoff(text):
            return [await self._write_memory(signal, "handoff", self._handoff_summary(text))]
        if self._is_decision(text):
            return [await self._write_memory(signal, "decision", self._decision_summary(text))]
        if self._is_correction(text):
            kind = "preference" if self._is_preference(text) else "workflow"
            return [await self._write_memory(signal, kind, self._workflow_summary(text))]
        return []

    async def capture_correction(self, signal: CaptureSignal) -> CaptureResult:
        kind = "preference" if self._is_preference(signal.text) else "workflow"
        return await self._write_memory(signal, kind, self._workflow_summary(signal.text))

    async def capture_decision(self, signal: CaptureSignal) -> CaptureResult:
        return await self._write_memory(signal, "decision", self._decision_summary(signal.text))

    async def capture_handoff(self, signal: CaptureSignal) -> CaptureResult:
        return await self._write_memory(signal, "handoff", self._handoff_summary(signal.text))

    async def capture_failure(self, signal: CaptureSignal) -> CaptureResult | None:
        return await self._capture_repeated_failure(signal)

    async def _capture_repeated_failure(self, signal: CaptureSignal) -> CaptureResult | None:
        signature = signal.error_signature or self._failure_signature(signal.text)
        if not signature:
            return None

        count = self._failure_counts.get(signature, 0) + 1
        self._failure_counts[signature] = count
        if count < 2 or signature in self._mistake_written:
            return CaptureResult(created=False, kind="mistake", reason="awaiting_second_occurrence")

        mistake_signal = CaptureSignal(
            text=signal.text,
            signal_type="mistake",
            scope=signal.scope,
            source_references=signal.source_references,
            metadata={**signal.metadata, "failure_signature": signature, "occurrence_count": count},
            error_signature=signature,
        )
        result = await self._write_memory(
            mistake_signal,
            "mistake",
            self._mistake_summary(signal.text, signature),
        )
        if result.created:
            self._mistake_written.add(signature)
        return result

    async def _write_memory(
        self,
        signal: CaptureSignal,
        kind: str,
        summary: str,
    ) -> CaptureResult:
        fingerprint = self._content_fingerprint(kind, summary, signal.scope)
        if fingerprint in self._session_fingerprints:
            return CaptureResult(created=False, kind=kind, reason="session_duplicate")

        superseded_id = await self._find_supersede_target(kind, summary, signal.scope)
        memory_id = str(uuid.uuid4())
        payload = {
            "memory_id": memory_id,
            "kind": kind,
            "scope": signal.scope,
            "content": signal.text.strip(),
            "title": summary[:120],
            "summary": summary,
            "source_references": signal.source_references,
            "metadata": signal.metadata,
            "supersedes_memory_id": superseded_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        stored = await self.store.write(payload)
        created = bool(stored.get("created", True))
        memory = stored.get("memory") or payload
        if created:
            self._session_fingerprints.add(fingerprint)
        return CaptureResult(
            created=created,
            kind=kind,
            memory_id=str(memory.get("memory_id") or memory_id),
            superseded_id=superseded_id,
            reason="stored" if created else "store_deduped",
        )

    async def _find_supersede_target(
        self,
        kind: str,
        summary: str,
        scope: dict[str, Any],
    ) -> str | None:
        candidates = await self.store.search(summary, scope, kind=kind)
        if not candidates:
            candidates = await self.store.search(kind, scope, kind=kind)
        summary_terms = {t for t in summary.lower().split() if len(t) > 3}
        best_id: str | None = None
        best_overlap = 0
        for memory in candidates:
            existing = (memory.get("summary") or memory.get("content") or "").lower()
            if not existing:
                continue
            if summary.lower() in existing or existing in summary.lower():
                mid = memory.get("memory_id")
                return str(mid) if mid else None
            existing_terms = {t for t in existing.split() if len(t) > 3}
            overlap = len(summary_terms & existing_terms)
            if overlap > best_overlap:
                best_overlap = overlap
                mid = memory.get("memory_id")
                best_id = str(mid) if mid else None
        return best_id if best_overlap >= 2 else None

    @staticmethod
    def _content_fingerprint(kind: str, summary: str, scope: dict[str, Any]) -> str:
        raw = f"{kind}|{summary.strip().lower()}|{json.dumps(scope, sort_keys=True)}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _failure_signature(text: str) -> str:
        normalised = re.sub(r"\s+", " ", text.strip().lower())
        if not normalised:
            return ""
        return hashlib.sha256(normalised.encode()).hexdigest()[:16]

    @staticmethod
    def _is_correction(text: str) -> bool:
        lower = text.lower()
        return any(marker in lower for marker in _CORRECTION_MARKERS)

    @staticmethod
    def _is_preference(text: str) -> bool:
        lower = text.lower()
        return any(marker in lower for marker in _PREFERENCE_MARKERS)

    @staticmethod
    def _is_decision(text: str) -> bool:
        lower = text.lower()
        return any(marker in lower for marker in _DECISION_MARKERS)

    @staticmethod
    def _is_handoff(text: str) -> bool:
        lower = text.lower()
        return any(marker in lower for marker in _HANDOFF_MARKERS)

    @staticmethod
    def _workflow_summary(text: str) -> str:
        cleaned = re.sub(r"\s+", " ", text.strip())
        return f"Workflow rule: {cleaned[:200]}"

    @staticmethod
    def _decision_summary(text: str) -> str:
        cleaned = re.sub(r"\s+", " ", text.strip())
        return f"Decision: {cleaned[:200]}"

    @staticmethod
    def _handoff_summary(text: str) -> str:
        cleaned = re.sub(r"\s+", " ", text.strip())
        return f"Handoff: {cleaned[:200]}"

    @staticmethod
    def _mistake_summary(text: str, signature: str) -> str:
        cleaned = re.sub(r"\s+", " ", text.strip())
        return f"Do not repeat ({signature}): {cleaned[:180]}"