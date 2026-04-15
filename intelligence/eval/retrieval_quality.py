"""Retrieval quality metrics — precision, recall, MRR, nDCG."""

from __future__ import annotations

import math
from typing import Any


def precision_at_k(
    retrieved_ids: list[str], relevant_ids: list[str], k: int
) -> float:
    """Precision@K: fraction of top-k results that are relevant."""
    if k <= 0:
        return 0.0
    top_k = retrieved_ids[:k]
    relevant_set = set(relevant_ids)
    hits = sum(1 for rid in top_k if rid in relevant_set)
    return hits / k


def recall_at_k(
    retrieved_ids: list[str], relevant_ids: list[str], k: int
) -> float:
    """Recall@K: fraction of relevant items found in top-k."""
    if not relevant_ids:
        return 1.0
    top_k = set(retrieved_ids[:k])
    relevant_set = set(relevant_ids)
    hits = len(top_k & relevant_set)
    return hits / len(relevant_set)


def mrr(retrieved_ids: list[str], relevant_ids: list[str]) -> float:
    """Mean Reciprocal Rank: 1/rank of first relevant result."""
    relevant_set = set(relevant_ids)
    for i, rid in enumerate(retrieved_ids):
        if rid in relevant_set:
            return 1.0 / (i + 1)
    return 0.0


def ndcg(
    retrieved_ids: list[str], relevant_ids: list[str], k: int
) -> float:
    """Normalised Discounted Cumulative Gain at K.

    Uses binary relevance: 1 if relevant, 0 otherwise.
    """
    if k <= 0 or not relevant_ids:
        return 0.0

    relevant_set = set(relevant_ids)

    # DCG
    dcg = 0.0
    for i, rid in enumerate(retrieved_ids[:k]):
        rel = 1.0 if rid in relevant_set else 0.0
        dcg += rel / math.log2(i + 2)  # i+2 because log2(1) = 0

    # Ideal DCG — all relevant docs at the top
    ideal_count = min(len(relevant_ids), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_count))

    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def evaluate_retrieval(queries: list[dict[str, Any]]) -> dict[str, Any]:
    """Run all metrics across a list of query evaluations.

    Each query dict should contain:
        - query: str
        - relevant_ids: list[str]
        - retrieved_ids: list[str]

    Returns aggregated metrics (mean across queries).
    """
    if not queries:
        return {
            "precision@5": 0.0,
            "precision@10": 0.0,
            "recall@5": 0.0,
            "recall@10": 0.0,
            "mrr": 0.0,
            "ndcg@10": 0.0,
            "num_queries": 0,
        }

    p5_sum = p10_sum = r5_sum = r10_sum = mrr_sum = ndcg_sum = 0.0
    n = len(queries)

    for q in queries:
        ret = q.get("retrieved_ids", [])
        rel = q.get("relevant_ids", [])
        p5_sum += precision_at_k(ret, rel, 5)
        p10_sum += precision_at_k(ret, rel, 10)
        r5_sum += recall_at_k(ret, rel, 5)
        r10_sum += recall_at_k(ret, rel, 10)
        mrr_sum += mrr(ret, rel)
        ndcg_sum += ndcg(ret, rel, 10)

    return {
        "precision@5": p5_sum / n,
        "precision@10": p10_sum / n,
        "recall@5": r5_sum / n,
        "recall@10": r10_sum / n,
        "mrr": mrr_sum / n,
        "ndcg@10": ndcg_sum / n,
        "num_queries": n,
    }
