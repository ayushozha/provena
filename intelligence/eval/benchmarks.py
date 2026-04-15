"""Benchmark runner — dispatches to benchmark handlers and collects results."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx


class BenchmarkRunner:
    """Run named benchmarks against the store and pipeline."""

    def __init__(
        self,
        store_url: str = "http://localhost:8000",
        pipeline_url: str = "http://localhost:8081",
    ) -> None:
        self.store_url = store_url
        self.pipeline_url = pipeline_url

    async def run(
        self,
        benchmark_name: str,
        dataset: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Dispatch to the appropriate benchmark handler."""
        start = time.monotonic()
        handler = {
            "locomo": self._run_locomo,
            "longmemeval": self._run_longmemeval,
            "custom": self._run_custom,
        }.get(benchmark_name, self._run_custom)

        metrics = await handler(dataset)
        elapsed = time.monotonic() - start

        return {
            "name": benchmark_name,
            "metrics": metrics,
            "elapsed_seconds": round(elapsed, 3),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Benchmark handlers (placeholders)
    # ------------------------------------------------------------------

    async def _run_locomo(
        self, dataset: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Placeholder for the LoCoMo benchmark.

        LoCoMo evaluates long-context memory over multi-turn conversations.
        Full implementation will load the LoCoMo dataset, run write+read
        pipelines, and compute recall / faithfulness metrics.
        """
        return {
            "benchmark": "locomo",
            "status": "placeholder",
            "recall": 0.0,
            "faithfulness": 0.0,
            "note": "Full implementation pending dataset integration",
        }

    async def _run_longmemeval(
        self, dataset: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Placeholder for the LongMemEval benchmark.

        LongMemEval tests long-term memory retention and retrieval
        accuracy across extended interaction histories.
        """
        return {
            "benchmark": "longmemeval",
            "status": "placeholder",
            "accuracy": 0.0,
            "latency_p50_ms": 0.0,
            "note": "Full implementation pending dataset integration",
        }

    async def _run_custom(
        self, dataset: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Run a custom benchmark using the provided dataset.

        Each dataset entry should contain:
            - query: str
            - expected_ids: list[str]  (relevant memory IDs)
        """
        if not dataset:
            return {
                "benchmark": "custom",
                "status": "no_dataset",
                "queries_evaluated": 0,
            }

        correct = 0
        total = len(dataset)

        for entry in dataset:
            query = entry.get("query", "")
            expected = set(entry.get("expected_ids", []))
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        f"{self.pipeline_url}/v1/pipeline/search",
                        json={"query": query, "limit": 10},
                    )
                    if resp.status_code < 400:
                        data = resp.json()
                        retrieved = {
                            r.get("memory_id", "")
                            for r in data.get("results", [])
                        }
                        if expected & retrieved:
                            correct += 1
            except Exception:
                pass

        return {
            "benchmark": "custom",
            "status": "completed",
            "queries_evaluated": total,
            "hit_rate": correct / total if total > 0 else 0.0,
        }
