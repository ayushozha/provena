# Publish `@provena/cli` to npm

`@provena/cli` is currently unpublished. The first public release is `0.1.0`;
routine releases remain on `0.1.x` and bump patch only. A `0.2.0` release
requires explicit major-product approval under the repository versioning rules.

Until this checklist succeeds, installation documentation must lead with the
source-built tarball flow rather than a registry command.

## Release prerequisites

- The `@provena` npm scope exists and the release owner can publish `@provena/cli`.
- GitHub Actions has either a scoped `NPM_TOKEN` repository secret or npm
  Trusted Publishing configured for this repository and workflow.
- The release commit is on `main`, its working tree is clean, and the package
  version is not already present on npm.
- `cli/package.json` and the `cli-vMAJOR.MINOR.PATCH` tag agree exactly.
- CI, packed-consumer installation, dependency audit, and the repository's
  push/PR review gates are green.
- Maintainers accept that the package is marked `UNLICENSED` and the repository
  has no license file. Publishing does not grant an open-source license.

The package bundles its production dependency tree; the pack test verifies each
direct runtime dependency is present and rejects unrelated top-level files.
The publish workflow grants `id-token: write` and uses
`npm publish --provenance`, producing npm provenance for the GitHub Actions
build. `NPM_TOKEN` remains the configured authentication path in the checked-in
workflow; remove that secret only after npm Trusted Publishing is configured
and verified for the exact repository and workflow filename (plus the GitHub
environment name if one is later added).

## Local release rehearsal

From a clean source checkout:

```powershell
Push-Location .\cli
npm ci
npm test
npm audit --omit=dev
npm pack --dry-run --json
npm pack --json
Pop-Location
```

Install the resulting tarball into an unrelated temporary Git repository and
run the portable harness:

```powershell
$Tarball = (Resolve-Path .\cli\provena-cli-0.1.0.tgz)
$Consumer = Join-Path $env:TEMP "provena-consumer-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $Consumer | Out-Null
Push-Location $Consumer
git init
npm init -y
npm install --save-dev $Tarball
npx provena init --no-daemon
npx provena harness verify
Pop-Location
```

The automated equivalent is `node cli/tests/pack-install.test.mjs` from the
repository root after dependencies are installed.

## Configure npm publication

1. Confirm scope ownership with `npm owner ls @provena/cli` after the package is
   created, or verify the organization/team grant before the first publish.
2. Create a granular npm publish token restricted to this package and store it
   as the `NPM_TOKEN` repository secret:

   ```powershell
   gh secret set NPM_TOKEN -R ayushozha/provena
   ```

3. Prefer npm Trusted Publishing once configured. Keep the workflow's OIDC
   permission and provenance flag in either authentication mode.

Never paste a token into `.npmrc`, a tracked environment file, a workflow, or a
Provena memory event.

## Publish `0.1.0`

After the release PR is merged and the exact commit is verified:

```powershell
git switch main
git pull --ff-only origin main
git status --short
git tag -a cli-v0.1.0 -m "@provena/cli 0.1.0"
git push origin cli-v0.1.0
```

The tag starts `.github/workflows/publish-cli.yml`. The workflow tests the
declared minimum Node 18.14.1 plus Node 22 and Node 24, then publishes once from
Node 24 with provenance. Do not retag or reuse a released version; fix forward
with the next `0.1.x` patch.

## Verify the public release

Only after the workflow and npm package page both succeed:

```powershell
$Consumer = Join-Path $env:TEMP "provena-registry-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $Consumer | Out-Null
Push-Location $Consumer
git init
npm init -y
npm install --save-dev @provena/cli@0.1.0
npx provena --version
npx provena init --no-daemon
npx provena harness verify
Pop-Location
```

Then update documentation that labels registry installation as post-release.
The optional governed Python store still requires a Provena source/deployment
checkout and `PROVENA_STORE_ROOT`; it is not bundled into the Node-only package.
