"""Tests for the retrieval benchmark harness.

Uses a mock pipeline client so the ingest -> query -> score loop and the metric
computation are verified without a live server or dataset.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from eval.benchmarks import BenchmarkDatasetError, BenchmarkRunner


class _Resp:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


class _MockPipeline:
    """One client instance spans an ingest+search run. Writes assign store ids;
    search returns store memories whose content contains a query word."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self._n = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, json=None):  # noqa: A002 - mirror httpx kwarg
        body = json or {}
        if url.endswith("/v1/pipeline/write"):
            self._n += 1
            sid = f"s{self._n}"
            self.store[sid] = body.get("content", "")
            return _Resp(200, {"created": True, "memory": {"memory_id": sid}})
        if url.endswith("/v1/pipeline/search"):
            words = {w.strip(".,?").lower() for w in body.get("query", "").split()}
            hits = [
                {"memory_id": sid}
                for sid, content in self.store.items()
                if words & {w.strip(".,?").lower() for w in content.split()}
            ]
            return _Resp(200, {"results": hits})
        return _Resp(404, {})


class TestBenchmarkRunner(unittest.TestCase):
    def test_ingest_query_score_loop(self) -> None:
        dataset = {
            "memories": [
                {"id": "m1", "content": "Karan prefers Rust"},
                {"id": "m2", "content": "team uses Postgres"},
            ],
            "queries": [
                {"query": "Rust", "relevant_ids": ["m1"]},
                {"query": "Postgres", "relevant_ids": ["m2"]},
            ],
        }
        with patch("eval.benchmarks.httpx.AsyncClient", return_value=_MockPipeline()):
            result = asyncio.run(BenchmarkRunner().run("custom", dataset))

        m = result["metrics"]
        self.assertEqual(result["name"], "custom")
        self.assertEqual(m["memories_ingested"], 2)
        self.assertEqual(m["queries_scored"], 2)
        self.assertEqual(m["queries_skipped_no_relevant"], 0)
        # Each relevant memory is retrieved at rank 1 -> perfect recall + MRR.
        self.assertEqual(m["recall@5"], 1.0)
        self.assertEqual(m["mrr"], 1.0)

    def test_query_with_uningested_relevant_is_skipped(self) -> None:
        dataset = {
            "memories": [{"id": "m1", "content": "Karan prefers Rust"}],
            "queries": [{"query": "Rust", "relevant_ids": ["ghost"]}],
        }
        with patch("eval.benchmarks.httpx.AsyncClient", return_value=_MockPipeline()):
            result = asyncio.run(BenchmarkRunner().run("custom", dataset))
        self.assertEqual(result["metrics"]["queries_scored"], 0)
        self.assertEqual(result["metrics"]["queries_skipped_no_relevant"], 1)

    def test_named_benchmark_without_dataset_raises(self) -> None:
        # No PROVENA_BENCH_LOCOMO_PATH configured -> loud error, not fake zeros.
        with self.assertRaises(BenchmarkDatasetError):
            asyncio.run(BenchmarkRunner().run("locomo"))


if __name__ == "__main__":
    unittest.main()
