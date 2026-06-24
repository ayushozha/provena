# PLAN-26: npm Publish and Quickstart

## Goal

Publish `@provena/cli` to npm, add a root README quickstart for the grand vision
flow, and verify a **fresh machine** can run `npx provena init && provena index &&
provena search` end-to-end.

## Why this is its own plan

Distribution and documentation are the final mile — separate from feature code
so publish checklist, versioning, and onboarding copy get full attention.

## Prerequisites

- PLAN-01 through PLAN-10 minimum (MVP path)
- PLAN-24 recommended (status command for support)
- PLAN-25 recommended (benchmark before marketing "production ready")

## Success criteria

- [ ] `@provena/cli` published to npm (public)
- [ ] `npx @provena/cli init` works in empty git repo without cloning Provena monorepo
- [ ] Package bundles or clearly documents Python store dependency (`provena` pip package future OR `npx provena serve` downloads runtime — document chosen approach)
- [ ] `docs/QUICKSTART.md` with 5-minute flow
- [ ] README badge + link to `roadmap/`
- [ ] Version tagging policy: cli version independent of server version
- [ ] `npm publish` dry-run + smoke on verdaccio or `npm pack` install test

## Scope

### In scope

- `docs/QUICKSTART.md`
- `cli/package.json` publishConfig finalization
- `cli/README.md` standalone consumer doc
- Optional: `postinstall` hint (not heavy install — avoid slow npm installs)
- GitHub Action `publish-cli.yml` on tag `cli-v*`

### Out of scope

- Renaming `@altrixy/provena-sdk` (can deprecate with notice)
- pip package `provena` (future plan if needed — note in quickstart)

## Implementation

### Steps

1. **Dependency strategy** (pick one, document):
   - **A)** Monorepo dev: CLI expects `provena serve` from pip install `provena` package.
   - **B)** Embedded: CLI ships standalone store as optional peer — heavier.
   - Recommend **A** for v1: `pip install provena` + `npm i -D @provena/cli`.
2. Write QUICKSTART:
   ```bash
   pip install provena
   npm install -D @provena/cli
   npx provena init
   npx provena serve --detach
   npx provena index
   npx provena search "authentication"
   ```
3. Add GitHub Action for CLI test + publish on tag.
4. Move PLAN-01 through PLAN-10 to `completed/` as they finish before publish.
5. Update root README "Developer install" section linking quickstart.

## Files to create or modify

| Path | Action |
|------|--------|
| `docs/QUICKSTART.md` | create |
| `cli/README.md` | create |
| `cli/package.json` | version bump, repository field |
| `.github/workflows/publish-cli.yml` | create |
| `README.md` | quickstart link |

## Verification

```powershell
# simulate fresh consumer in temp dir
mkdir C:\temp\provena-consumer-test
cd C:\temp\provena-consumer-test
git init
npm install @provena/cli
npx provena init
npx provena serve --detach
npx provena index
npx provena search "test"
npx provena status
```

All steps succeed without referencing monorepo paths.

## Handoff

Grand vision MVP complete. Subsequent roadmap phases (not yet planned as files):

- pip package `provena` wrapping store + intelligence for one-command Python install
- CI index job for GitHub Actions
- VS Code extension
- First-party connector workers (post push-based indexer)