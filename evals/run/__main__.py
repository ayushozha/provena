"""CLI entry: python -m evals.run (run from repo root)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals.scorecard import run_scorecard


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Provena eval scorecard.")
    parser.add_argument(
        "--output",
        "-o",
        default="evals/scorecard.json",
        help="Path to write scorecard JSON (default: evals/scorecard.json)",
    )
    args = parser.parse_args()
    output = _REPO_ROOT / args.output if not Path(args.output).is_absolute() else Path(args.output)
    scorecard = run_scorecard(output_path=output)
    print(json.dumps(scorecard, indent=2))
    lift = scorecard.get("headline", {}).get("brain_lift_delta", 0.0)
    print(f"\nBrain lift delta (with - without): {lift:+.4f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())