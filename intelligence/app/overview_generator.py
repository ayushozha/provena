"""Overview generator — builds project summaries without LLM calls (v1)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

import httpx

from app.models import ProjectOverview


class OverviewGenerator:
    """Generate project overviews from stored memories."""

    def __init__(
        self,
        store_url: str = "http://localhost:8000",
        service_headers: dict[str, str] | None = None,
    ) -> None:
        self.store_url = store_url
        self.service_headers = service_headers or {}

    async def generate(
        self,
        scope: dict[str, Any],
        access_headers: dict[str, str] | None = None,
    ) -> ProjectOverview:
        """Query recent memories and build a summary overview.

        No LLM calls in v1 — pure aggregation.
        """
        memories = await self._fetch_recent_memories(scope, access_headers)

        # Count by kind
        kind_counts: Counter[str] = Counter()
        entity_counter: Counter[str] = Counter()
        recent_decisions: list[str] = []

        for mem in memories:
            kind = mem.get("kind", "unknown")
            kind_counts[kind] += 1

            for ek in mem.get("entity_keys", []):
                entity_counter[ek] += 1

            if kind == "decision":
                title = mem.get("title", mem.get("content", "")[:80])
                recent_decisions.append(title)

        # Top entities by frequency
        top_entities = [e for e, _ in entity_counter.most_common(20)]

        # Build summary
        total = sum(kind_counts.values())
        parts = [f"{total} memories"]
        for kind, count in kind_counts.most_common():
            parts.append(f"{count} {kind}s")
        summary = f"Project has {', '.join(parts)}."

        if top_entities:
            summary += f" Key entities: {', '.join(top_entities[:5])}."

        return ProjectOverview(
            tenant_id=scope.get("tenant_id", ""),
            project_id=scope.get("project_id", ""),
            summary=summary,
            key_entities=top_entities,
            recent_decisions=recent_decisions[:10],
            active_memory_count=total,
            generated_at=datetime.now(timezone.utc),
        )

    async def _fetch_recent_memories(
        self,
        scope: dict[str, Any],
        access_headers: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch recent memories from the store service."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.store_url}/v1/memories/search",
                    json={"query": "", "scope": scope, "limit": 50},
                    headers=access_headers or self.service_headers,
                )
                if resp.status_code < 400:
                    data = resp.json()
                    return [item.get("memory", {}) for item in data.get("results", [])]
        except Exception:
            pass
        return []
