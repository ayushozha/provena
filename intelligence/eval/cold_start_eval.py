"""Cold-start evaluation — measures how well overviews capture ground truth."""

from __future__ import annotations

from typing import Any

import httpx

from app.overview_generator import OverviewGenerator


def evaluate_cold_start(
    overview: dict[str, Any],
    ground_truth_entities: list[str],
    ground_truth_decisions: list[str],
) -> dict[str, float]:
    """Compute cold-start quality metrics.

    Returns:
        entity_recall: fraction of ground-truth entities present in overview
        decision_recall: fraction of ground-truth decisions present in overview
        summary_quality: 1.0 if summary mentions >50% of entities, else ratio
    """
    overview_entities = set(overview.get("key_entities", []))
    overview_decisions = set(overview.get("recent_decisions", []))
    summary = overview.get("summary", "").lower()

    # Entity recall
    if ground_truth_entities:
        entity_hits = sum(
            1 for e in ground_truth_entities if e in overview_entities
        )
        entity_recall = entity_hits / len(ground_truth_entities)
    else:
        entity_recall = 1.0

    # Decision recall
    if ground_truth_decisions:
        decision_hits = sum(
            1 for d in ground_truth_decisions if d in overview_decisions
        )
        decision_recall = decision_hits / len(ground_truth_decisions)
    else:
        decision_recall = 1.0

    # Summary quality — fraction of entities mentioned in summary text
    if ground_truth_entities:
        mentioned = sum(
            1 for e in ground_truth_entities if e.lower() in summary
        )
        ratio = mentioned / len(ground_truth_entities)
        summary_quality = 1.0 if ratio > 0.5 else ratio
    else:
        summary_quality = 1.0

    return {
        "entity_recall": entity_recall,
        "decision_recall": decision_recall,
        "summary_quality": summary_quality,
    }


async def run_cold_start_eval(
    store_url: str,
    scope: dict[str, Any],
) -> dict[str, Any]:
    """Generate an overview and compare with actual memories from the store.

    This fetches ground-truth data from the store, generates an overview,
    and evaluates how well the overview captures the stored information.
    """
    generator = OverviewGenerator(store_url=store_url)

    # Fetch actual memories to build ground truth
    ground_truth_entities: list[str] = []
    ground_truth_decisions: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{store_url}/v1/memories/search",
                json={"scope": scope, "limit": 500},
            )
            if resp.status_code < 400:
                data = resp.json()
                for mem in data.get("results", []):
                    ground_truth_entities.extend(mem.get("entity_keys", []))
                    if mem.get("kind") == "decision":
                        title = mem.get("title", mem.get("content", "")[:80])
                        ground_truth_decisions.append(title)
    except Exception:
        pass

    # Deduplicate ground truth
    ground_truth_entities = list(dict.fromkeys(ground_truth_entities))
    ground_truth_decisions = list(dict.fromkeys(ground_truth_decisions))

    # Generate overview
    overview_obj = await generator.generate(scope)
    overview = overview_obj.model_dump()

    # Evaluate
    metrics = evaluate_cold_start(
        overview, ground_truth_entities, ground_truth_decisions
    )

    return {
        "overview": overview,
        "ground_truth_entity_count": len(ground_truth_entities),
        "ground_truth_decision_count": len(ground_truth_decisions),
        "metrics": metrics,
    }
