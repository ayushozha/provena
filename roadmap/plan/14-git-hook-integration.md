# PLAN-14: Git Hook Integration

## Goal

`provena hooks install` adds an optional pre-commit hook that runs incremental
index on staged files — memory stays aligned with commits without manual steps.

## Why this is its own plan

Git hook installation is opt-in, platform-sensitive, and must not break repos —
isolated from watch and index commands.

## Prerequisites

- PLAN-12 complete (incremental index)

## Success criteria

- [ ] `provena hooks install` writes `.git/hooks/pre-commit` (or husky-compatible snippet)
- [ ] Hook runs `provena index --staged-only` in <10s for typical commits
- [ ] `provena hooks uninstall` removes hook cleanly
- [ ] `--staged-only` flag indexes only git staged paths
- [ ] Hook skips if `.provena/` missing (prints hint to run `provena init`)
- [ ] Document bypass: `git commit --no-verify`

## Scope

### In scope

- `cli/src/commands/hooks.ts`
- `cli/src/indexer/staged.ts` — `git diff --cached --name-only`
- Shell script template for pre-commit

### Out of scope

- post-merge hooks
- CI indexing (separate future plan)

## Implementation

### Steps

1. `git diff --cached --name-only --diff-filter=ACMR` → filter indexable extensions.
2. `runIncrementalIndex(paths)` for staged set only.
3. Install hook script:
   ```sh
   #!/bin/sh
   provena index --staged-only || exit 1
   ```
4. Windows: also support `pre-commit` via Git Bash path documented.

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/commands/hooks.ts` | create |
| `cli/src/indexer/staged.ts` | create |
| `cli/templates/pre-commit.sh` | create |
| `cli/src/commands/index.ts` | add `--staged-only` |

## Verification

```powershell
provena hooks install
git add some-file.ts
git commit -m "test" --no-verify  # baseline
provena hooks install
git add some-file.ts
git commit -m "test index hook"
# verify index state updated
```

## Handoff to next plan

Users can choose watch (PLAN-13) OR hooks (PLAN-14) OR manual index.