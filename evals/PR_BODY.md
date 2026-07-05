## Summary

Ships the grok build-plan track: workflow/mistake capture engine, fail-open capture hooks, eval scorecard proving brain lift, and ledger/docs reconciliation.

## Changes

- `intelligence/app/capture.py` — correction→workflow/preference, repeated failure→mistake, decision/handoff capture with supersede
- `scripts/hooks/` — Claude Code correction hook + git post-commit decision hook (fail-open)
- `evals/` — `python -m evals.run` scorecard: code_recall, agent-in-the-loop lift, mistake-recall, stale-fact suppression, secret-not-stored, cold_start wired
- `loop/prd.json` — PROVENA-001 passes=false; PROVENA-008 reconciled to blocked/red
- `docs/POSITIONING.md` — wedge vs enterprise + local-mode freeze note
- `roadmap/README.md` — plans 11/15/18/23/25 status updates

## Testing

```powershell
cd intelligence
uv run pytest tests -q
# 57 passed

$env:PYTHONPATH = (Get-Location).Parent.FullName
uv run python -m evals.run -o evals/scorecard.json
# brain_lift_delta=+1.0, mistake_recall_pass=true, code_recall hit_rate@5=0.4

uv run ruff check app/capture.py tests/test_capture.py tests/test_hooks.py tests/test_scorecard.py
# All checks passed
```

Scorecard artifact: `evals/scorecard.json`