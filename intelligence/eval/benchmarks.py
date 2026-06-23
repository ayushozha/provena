"""Benchmark runner — ingest a dataset through the pipeline and score retrieval.

This produces REAL numbers: it ingests a dataset's memories via
``/v1/pipeline/write``, issues each query via ``/v1/pipeline/search`` against the
live pipeline, maps the dataset's relevant ids onto the store ids assigned at
ingest, and scores retrieval with ``eval.retrieval_quality`` plus search latency.

Datasets use a normalized schema::

    {
      "memories": [{"id": "m1", "content": "...", "kind": "fact",
                    "scope": {"tenant_id": "bench"}}],
      "queries":  [{"query": "...", "relevant_ids": ["m1"], "limit": 10}]
    }

LoCoMo / LongMemEval read their normalized dataset from
``PROVENA_BENCH_LOCOMO_PATH`` / ``PROVENA_BENCH_LONGMEMEVAL_PATH``. They are NOT
shipped — point the env var at a converted dataset file, or pass ``dataset=``
directly. If no dataset is available the run raises ``BenchmarkDatasetError``
rather than returning fabricated zeros.

Scope: this reports retrieval quality + latency, which the pipeline directly
determines. End-to-end answer accuracy (e.g. LongMemEval's LLM-judged answers)
needs a generation + judge step layered on top and is intentionally out of scope
here.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from eval.retrieval_quality import evaluate_retrieval

_DEFAULT_SCOPE: dict[str, Any] = {"tenant_id": "bench"}
_NAMED_DATASET_ENV = {
    "locomo": "PROVENA_BENCH_LOCOMO_PATH",
    "longmemeval": "PROVENA_BENCH_LONGMEMEVAL_PATH",
}


class BenchmarkDatasetError(RuntimeError):
    """Raised when a named benchmark has no dataset configured/available."""


class BenchmarkRunner:
    """Run a retrieval benchmark against a live store + pipeline."""

    def __init__(
        self,
        store_url: str = "http://localhost:8000",
        pipeline_url: str = "http://localhost:8081",
        timeout: float = 30.0,
    ) -> None:
        self.store_url = store_url
        self.pipeline_url = pipeline_url
        self.timeout = timeout

    async def run(
        self,
        benchmark_name: str,
        dataset: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run *benchmark_name*. Uses *dataset* if given, else loads the named
        dataset from its configured path. Raises BenchmarkDatasetError when a
        named benchmark has no dataset rather than reporting fake metrics."""
        start = time.monotonic()
        if dataset is None:
            dataset = self._load_named_dataset(benchmark_name)
        metrics = await self._run_dataset(dataset)
        return {
            "name": benchmark_name,
            "metrics": metrics,
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _load_named_dataset(benchmark_name: str) -> dict[str, Any]:
        env = _NAMED_DATASET_ENV.get(benchmark_name)
        if env is None:
            raise BenchmarkDatasetError(
                f"benchmark {benchmark_name!r} requires an explicit dataset= argument"
            )
        path = os.environ.get(env, "").strip()
        if not path:
            raise BenchmarkDatasetError(
                f"{benchmark_name} dataset not configured; set {env} to a normalized dataset file"
            )
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    # ------------------------------------------------------------------
    # The real ingest -> query -> score loop
    # ------------------------------------------------------------------

    async def _run_dataset(self, dataset: dict[str, Any]) -> dict[str, Any]:
        memories = dataset.get("memories") or []
        queries = dataset.get("queries") or []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            id_map = await self._ingest(client, memories)
            per_query, latencies_ms, skipped = await self._search_all(client, queries, id_map)

        metrics = evaluate_retrieval(per_query)
        metrics.update(
            {
                "memories_ingested": sum(1 for ids in id_map.values() if ids),
                "queries_scored": len(per_query),
                "queries_skipped_no_relevant": skipped,
                "search_latency_p50_ms": _percentile(latencies_ms, 50),
                "search_latency_p95_ms": _percentile(latencies_ms, 95),
            }
        )
        return metrics

    async def _ingest(
        self, client: httpx.AsyncClient, memories: list[dict[str, Any]]
    ) -> dict[str, list[str]]:
        """Write each dataset memory; map its dataset id -> store ids assigned at
        ingest (the primary memory_id plus any ADD-only extracted ids)."""
        id_map: dict[str, list[str]] = {}
        for mem in memories:
            bench_id = str(mem.get("id") or "")
            payload = {
                "kind": mem.get("kind") or "fact",
                "scope": mem.get("scope") or _DEFAULT_SCOPE,
                "content": mem.get("content") or "",
            }
            store_ids: list[str] = []
            try:
                resp = await client.post(f"{self.pipeline_url}/v1/pipeline/write", json=payload)
                if resp.status_code < 400:
                    body = resp.json().get("memory") or {}
                    if body.get("memory_id"):
                        store_ids.append(str(body["memory_id"]))
                    store_ids.extend(str(x) for x in (body.get("extracted_memory_ids") or []))
            except Exception:
                pass
            if bench_id:
                id_map[bench_id] = store_ids
        return id_map

    async def _search_all(
        self,
        client: httpx.AsyncClient,
        queries: list[dict[str, Any]],
        id_map: dict[str, list[str]],
    ) -> tuple[list[dict[str, Any]], list[float], int]:
        """Run each query; translate its relevant dataset ids to store ids and
        record (query, relevant, retrieved) for scoring. Queries whose relevant
        memories failed to ingest are skipped (not scored as free wins)."""
        per_query: list[dict[str, Any]] = []
        latencies_ms: list[float] = []
        skipped = 0
        for q in queries:
            relevant: set[str] = set()
            for bid in q.get("relevant_ids") or []:
                relevant.update(id_map.get(str(bid), []))
            if not relevant:
                skipped += 1
                continue
            payload = {
                "query": q.get("query") or "",
                "scope": q.get("scope") or _DEFAULT_SCOPE,
                "limit": int(q.get("limit") or 10),
            }
            retrieved: list[str] = []
            t0 = time.monotonic()
            try:
                resp = await client.post(f"{self.pipeline_url}/v1/pipeline/search", json=payload)
                latencies_ms.append((time.monotonic() - t0) * 1000.0)
                if resp.status_code < 400:
                    retrieved = [str(r.get("memory_id", "")) for r in (resp.json().get("results") or [])]
            except Exception:
                latencies_ms.append((time.monotonic() - t0) * 1000.0)
            per_query.append(
                {"query": payload["query"], "relevant_ids": list(relevant), "retrieved_ids": retrieved}
            )
        return per_query, latencies_ms, skipped


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile in milliseconds, rounded to 2dp."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(math.ceil(pct / 100.0 * len(ordered))) - 1))
    return round(ordered[rank], 2)
