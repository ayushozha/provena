# Provena Codex Loop

This directory holds a Provena-specific autonomous loop inspired by the Ralph
pattern, but implemented around `codex exec` and a strict multi-role gate.

## Files

- `prd.json`: the Provena backlog and current story state
- `PM_PRD_TEMPLATE.md`: product-manager template for new Provena backlog items
- `PM_CREATION_PROMPT.md`: PM authoring prompt for feature PRDs
- `progress.txt`: append-only learnings and iteration notes
- `CODEX_PROMPT.md`: prompt template for the engineer phase
- `QA_PROMPT.md`: prompt template for tester planning
- `PM_REVIEW_PROMPT.md`: prompt template for PM acceptance review
- `TESTER_EXECUTION_PROMPT.md`: prompt template for tester execution
- `qa_test_plan.json`: QA-owned test case inventory for epics, features, and stories
- `STOP`: optional stop signal file
- `logs/`: per-iteration Codex output logs

## How it works

Each phase starts a fresh Codex process with:

- the current Provena story from `prd.json`
- the recent tail of `progress.txt`
- the repo root plus `services/provena/AGENTS.md`

The loop only works one story at a time, and it enforces this order:

1. PM writes or finalizes the feature PRD.
2. Tester writes exhaustive test cases for the story, feature, and epic.
3. Engineer implements the feature.
4. PM reviews the implementation against the PRD.
5. Tester executes the planned test cases.
6. If PM or tester rejects the feature, the loop routes back to engineering and
   repeats review plus test until green.

A story is not complete until tester execution is green and `prd.json` reflects:

```json
{
  "pmPrdStatus": "complete",
  "testerPlanStatus": "complete",
  "engineerStatus": "complete",
  "pmReviewStatus": "approved",
  "testerExecutionStatus": "green",
  "status": "passed",
  "passes": true
}
```

If the current backlog is fully passed, the loop idles and waits for more
stories to be added. That makes it suitable for long-running product work until
you explicitly stop it.

## Role model

- PM owns user stories, feature description, expected behaviors, and acceptance
  criteria.
- Tester planning owns exhaustive test coverage before engineering starts.
- Engineer owns implementation and fix cycles only.
- PM review decides whether the feature matches the PRD well enough to test.
- Tester execution is the final gate. The next feature cannot begin until tester
  gives green.
- Sentry checks should be included in tester planning and tester execution when
  Sentry credentials are available in the environment.

## Run once

```powershell
cd services/provena
python scripts/provena_loop.py --once
```

## Run forever until stopped

```powershell
cd services/provena
python scripts/provena_loop.py
```

The loop will keep polling forever. To stop it, create `services/provena/loop/STOP`.

```powershell
Set-Content services/provena/loop/STOP "Stop after current iteration"
```

## Safe validation modes

Validate loop state:

```powershell
cd services/provena
python scripts/provena_loop.py --validate-only
```

Render the next prompt without invoking Codex:

```powershell
cd services/provena
python scripts/provena_loop.py --once --dry-run
```

## Notes

- The loop is Provena-specific. Stories should generally stay inside
  `services/provena`.
- Story sizing matters. Split oversized work before implementation.
- `services/provena/AGENTS.md` contains the default verification policy used by
  loop iterations.
- The current seeded backlog is already PM-authored, so the first runnable phase
  for most stories will usually be tester planning.
- If Sentry credentials are missing, the loop records Sentry verification as
  pending instead of inventing runtime results.
