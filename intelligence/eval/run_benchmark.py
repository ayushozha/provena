"""CLI: run a Provena retrieval benchmark against a live pipeline.

    cd intelligence
    PROVENA_INTEL_PIPELINE_URL=http://localhost:8081 \
        python -m eval.run_benchmark --benchmark custom --dataset eval/fixtures/sample_dataset.json

For LoCoMo / LongMemEval, point the dataset env var at a normalized dataset and
omit --dataset:

    PROVENA_BENCH_LOCOMO_PATH=/data/locomo.normalized.json \
        python -m eval.run_benchmark --benchmark locomo

Prints the metrics as JSON. Requires a running store + intelligence pipeline
(and, for the LLM-backed stages to contribute, a configured provider — see
app/config.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from eval.benchmarks import BenchmarkDatasetError, BenchmarkRunner


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Provena retrieval benchmark.")
    parser.add_argument("--benchmark", default="custom", help="locomo | longmemeval | custom")
    parser.add_argument("--dataset", help="path to a normalized dataset JSON (required for custom)")
    parser.add_argument(
        "--pipeline-url",
        default=os.environ.get("PROVENA_INTEL_PIPELINE_URL", "http://localhost:8081"),
    )
    args = parser.parse_args()

    dataset = None
    if args.dataset:
        with open(args.dataset, encoding="utf-8") as fh:
            dataset = json.load(fh)
    elif args.benchmark == "custom":
        parser.error("custom benchmark needs --dataset")

    runner = BenchmarkRunner(pipeline_url=args.pipeline_url)
    try:
        result = asyncio.run(runner.run(args.benchmark, dataset))
    except BenchmarkDatasetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
