# PLAN-01: CLI Package Foundation

## Goal

Ship a publishable npm package `@provena/cli` with a working binary entrypoint,
TypeScript build pipeline, and smoke test — no product features yet, just the
distribution shell future commands plug into.

## Why this is its own plan

Every later CLI command (`init`, `index`, `search`) needs a real npm binary,
build output, and publish metadata. Doing this first avoids bolting commands
onto the thin HTTP SDK package.

## Prerequisites

None.

## Success criteria

- [ ] `cli/package.json` publishes as `@provena/cli` with `bin.provena`
- [ ] `provena --version` prints package version
- [ ] `provena --help` lists placeholder subcommands (`init`, `index`, `search`, `status`)
- [ ] `npm run build` emits `dist/` with types
- [ ] `npm pack` tarball contains only `dist/` + `package.json`
- [ ] Smoke test runs in CI-style one-liner

## Scope

### In scope

- New `cli/` directory at repo root (sibling to `sdk/`)
- Commander or minimal hand-rolled argv parser
- Placeholder subcommands that print "not implemented yet"
- Node 18+ engines, ESM output

### Out of scope

- Actual `init` / `index` logic
- Renaming or removing `@altrixy/provena-sdk` (can coexist)

## Implementation

### Steps

1. Create `cli/package.json`:
   - `name`: `@provena/cli`
   - `bin`: `{ "provena": "./dist/cli.js" }`
   - `type`: `module`
   - `files`: `["dist"]`
   - `publishConfig.access`: `public`
2. Create `cli/tsconfig.json` targeting ES2022 + NodeNext.
3. Create `cli/src/cli.ts`:
   - Parse `--version`, `--help`
   - Register stub commands: `init`, `index`, `search`, `status`, `connect`, `mcp`
   - Exit 0 on help; exit 1 with message on unknown command
4. Create `cli/src/index.ts` re-exporting programmatic API (empty for now).
5. Add `cli/tests/smoke.mjs` asserting `--version` and `--help` work on built dist.
6. Add root-level note in `roadmap/README.md` linking `cli/` (already indexed).

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/package.json` | create |
| `cli/tsconfig.json` | create |
| `cli/src/cli.ts` | create |
| `cli/src/index.ts` | create |
| `cli/tests/smoke.mjs` | create |
| `.gitignore` | add `cli/dist/`, `cli/node_modules/` if needed |

## Verification

```powershell
cd cli
npm install
npm run build
node dist/cli.js --version
node dist/cli.js --help
node tests/smoke.mjs
npm pack
```

All commands exit 0.

## Handoff to next plan

PLAN-02 (`provena init`) imports config helpers from `cli/src/config.ts` once
that plan adds them. Keep `cli/src/` modular: one file per command eventually.