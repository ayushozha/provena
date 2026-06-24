# Provena Execution Guide

This directory is the product boundary for Provena. Treat work here as
Provena-specific unless a story explicitly requires a repo-level change.

## Autonomous loop files

- Loop state: `services/provena/loop/prd.json`
- Append-only learnings: `services/provena/loop/progress.txt`
- Codex prompt template: `services/provena/loop/CODEX_PROMPT.md`
- PM creation template: `services/provena/loop/PM_CREATION_PROMPT.md`
- Tester planning template: `services/provena/loop/QA_PROMPT.md`
- PM review template: `services/provena/loop/PM_REVIEW_PROMPT.md`
- Tester execution template: `services/provena/loop/TESTER_EXECUTION_PROMPT.md`
- QA plan: `services/provena/loop/qa_test_plan.json`
- PM template: `services/provena/loop/PM_PRD_TEMPLATE.md`
- Stop signal: `services/provena/loop/STOP`

Each autonomous iteration handles exactly one small Provena feature slice and
must follow this order:

1. PM authors or finalizes the PRD for the feature.
2. Tester writes exhaustive test cases for the story, feature, and epic scope.
3. Engineer implements the feature.
4. PM reviews the built feature against the PRD.
5. Tester executes the planned test cases.
6. If PM or tester rejects the feature, engineering fixes it and the review/test
   cycle repeats.
7. Only after tester green does the loop move to the next feature.

## PM story contract

Stories in `services/provena/loop/prd.json` are PM-authored. They should
include:

- epic and feature identifiers
- persona
- user story
- problem statement
- feature description
- expected behaviors
- acceptance criteria
- verification commands
- UI test focus
- Sentry checks

## Required workflow for every Provena story

1. Read the selected story from `services/provena/loop/prd.json`.
2. Respect the current phase gate instead of skipping ahead.
3. Make only the smallest change needed for the current feature.
4. Run every `verificationCommands` entry required by the current phase.
5. Do not mark `passes: true` unless tester execution is green.
6. Update `services/provena/loop/prd.json` with the phase outcome.
7. Append a short learning entry to `services/provena/loop/progress.txt`.

## Phase gates

- PM creation owns the product contract in `prd.json`.
- Tester planning owns exhaustive planned coverage in `qa_test_plan.json`.
- Engineer owns implementation and fix cycles only.
- PM review can approve or request changes, but does not execute tests.
- Tester execution can give green or red, and green is the only gate that marks
  a story complete.
- A story is fully complete only when:
  - `pmPrdStatus: "complete"`
  - `testerPlanStatus: "complete"`
  - `engineerStatus: "complete"`
  - `pmReviewStatus: "approved"`
  - `testerExecutionStatus: "green"`
  - `passes: true`

## Default verification expectations

- If a story touches `app/` or `storage/`, run:
  - `python -m pytest tests/test_main.py -q`
- If a story touches `intelligence/`, run:
  - `python -m pytest tests -q` from `services/provena/intelligence`
- If a story touches `control-plane/`, run:
  - `go build ./...` from `services/provena/control-plane`
- If a story touches `orchestration/`, run:
  - `cargo test` from `services/provena/orchestration`
- If a story changes cross-service behavior, SDKs, deployment, gateway routing,
  MCP, queue behavior, or connected-mode flows, run:
  - `python scripts/run_e2e.py`

## Sentry

- If `SENTRY_AUTH_TOKEN` or other Sentry credentials are available in the
  environment, use Sentry as part of QA planning and post-change validation.
- Do not invent Sentry issue data if credentials are not available.
- When Sentry cannot be checked, record the pending follow-up explicitly in
  `services/provena/loop/qa_test_plan.json` or `progress.txt`.

## Story sizing

Stories must be small enough to finish in one Codex iteration. Split large work
before implementation. If a story is too large or blocked, do not leave it in
`in_progress`; mark it blocked or split it into smaller stories in
`services/provena/loop/prd.json`.

## Scope discipline

- Prefer edits inside `services/provena`.
- Avoid repo-wide changes unless the current story explicitly requires them.
- Do not change unrelated stories while working on one story.

## `@provena/cli` versioning

When touching `cli/package.json` version or preparing an npm publish:

1. **First public package release is `0.1.0`** — start fresh on npm; do not carry
   over internal monorepo version history.
2. **Routine releases stay on `0.1.x`** — bump **patch** only (`0.1.0` → `0.1.1`).
3. **Do not bump to `0.2.0` unless it is an approved major release** — minor
   version increases are not for feature slices, docs, or housekeeping PRs.
4. Full semver (`MAJOR.MINOR.PATCH`) always; never two-segment versions (`0.1`).
5. See `cli/README.md` (Versioning) and `cli/tests/smoke.mjs` for enforcement.
