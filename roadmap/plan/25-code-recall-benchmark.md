# PLAN-25: Code Recall Benchmark

## Goal

Extend the eval harness with a **code recall** benchmark: given a repo index and
a set of question→expected-file/symbol pairs, measure hit-rate@k and report
latency — proving indexing quality quantitatively.

## Why this is its own plan

Quality gate for the grand vision — separate from building the indexer so eval
can iterate on metrics without blocking feature work.

## Prerequisites

- PLAN-09 complete (index command)
- PLAN-10 complete (search)
- Existing `intelligence/eval/benchmarks.py` harness (merged PR #18)

## Success criteria

- [ ] `intelligence/eval/fixtures/code_recall_dataset.json` with ≥10 Q→relevant_ids pairs
- [ ] Dataset schema supports `relevant_paths` and `relevant_symbols`
- [ ] `python -m eval.run_benchmark --benchmark code_recall --dataset ...` runs against live index
- [ ] Metrics: `hit_rate@1`, `hit_rate@5`, `mrr`, `search_latency_p50_ms`
- [ ] CLI wrapper: `provena benchmark` invokes harness with repo scope from config
- [ ] CI optional job: index fixture repo → run benchmark → threshold ≥0.6 hit_rate@5

## Scope

### In scope

- `intelligence/eval/code_recall.py` handler
- `intelligence/eval/fixtures/code_recall_dataset.json`
- `cli/src/commands/benchmark.ts` thin wrapper
- Dogfood dataset from Provena repo (e.g. "Where is sqlite-vec used?" → `app/store.py`)

### Out of scope

- LLM-judged answer correctness
- Cross-repo benchmarks

## Implementation

### Steps

1. Register `code_recall` handler in `BenchmarkRunner`.
2. Map `relevant_paths` to memory IDs via index-state or search by tag `file:path`.
3. Run search per query; score if retrieved memory matches expected path/symbol.
4. Add `provena benchmark` post-index verification to PLAN-26 docs.
5. Tests in `intelligence/tests/test_code_recall.py` with mock pipeline client.

## Files to create or modify

| Path | Action |
|------|--------|
| `intelligence/eval/code_recall.py` | create |
| `intelligence/eval/benchmarks.py` | register handler |
| `intelligence/eval/fixtures/code_recall_dataset.json` | create |
| `intelligence/tests/test_code_recall.py` | create |
| `cli/src/commands/benchmark.ts` | create |

## Verification

```powershell
provena serve --detach
provena index
provena benchmark
cd intelligence
..\.venv\Scripts\python.exe -m pytest tests/test_code_recall.py -q
..\.venv\Scripts\python.exe -m eval.run_benchmark --benchmark code_recall --dataset eval/fixtures/code_recall_dataset.json
```

## Handoff to next plan

Benchmark becomes regression gate before npm publish (PLAN-26).