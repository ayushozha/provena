"""Smoke tests for the eval scorecard harness."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

def _run_scorecard():
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from evals.scorecard import run_scorecard

    return run_scorecard()


class TestScorecard(unittest.TestCase):
    def test_run_scorecard_produces_headline_metrics(self) -> None:
        scorecard = _run_scorecard()
        self.assertIn("benchmarks", scorecard)
        self.assertIn("headline", scorecard)
        agent = scorecard["benchmarks"]["agent_in_the_loop"]
        self.assertGreater(agent["with_brain_avg"], agent["without_brain_avg"])
        self.assertTrue(scorecard["benchmarks"]["mistake_recall"]["pass"])
        self.assertTrue(scorecard["benchmarks"]["stale_fact_suppression"]["pass"])
        self.assertTrue(scorecard["benchmarks"]["secret_not_stored"]["pass"])
        self.assertGreaterEqual(scorecard["benchmarks"]["code_recall"]["queries_scored"], 10)

    def test_scorecard_json_serializable(self) -> None:
        scorecard = _run_scorecard()
        json.dumps(scorecard)


if __name__ == "__main__":
    unittest.main()