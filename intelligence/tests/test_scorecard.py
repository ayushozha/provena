"""Integration tests for live eval scorecard."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class TestScorecard(unittest.TestCase):
    def test_live_scorecard_passes_core_benchmarks(self) -> None:
        from evals.scorecard import run_scorecard

        scorecard = run_scorecard()
        self.assertEqual(scorecard["benchmarks"]["code_recall"]["mode"], "live_pipeline")
        self.assertTrue(scorecard["benchmarks"]["secret_not_stored"]["pass"])
        self.assertTrue(scorecard["benchmarks"]["mistake_recall"]["pass"])
        self.assertGreaterEqual(scorecard["benchmarks"]["agent_in_the_loop"]["lift_delta"], 0.0)
        json.dumps(scorecard)


if __name__ == "__main__":
    unittest.main()