# Publish `@provena/cli` to npm

Indexer MVP ships at **`0.1.0`** (fresh start). Routine releases bump **patch** only
(`0.1.1`, …). See `AGENTS.md` and `cli/README.md` for versioning rules.

## Checklist

| Step | Status | Action |
|------|--------|--------|
| Code + tests on `main` | PR #32 | Merge when CI green |
| `pyproject.toml` editable install | Fixed | `pip install -e .` installs `app` only |
| GitHub secret `NPM_TOKEN` | **You** | npm access token with publish scope |
| npm org `@provena` | **You** | Create org or use personal scope access |
| Tag | **You** | `git tag cli-v0.1.0 && git push origin cli-v0.1.0` |
| Workflow | Ready | `.github/workflows/publish-cli.yml` runs test + publish |

## 1. npm account + scope

1. Log in: `npm login`
2. Ensure you can publish `@provena/cli`:
   - Create npm org **provena**, or
   - Publish under your user if the scope is available
3. Create an **Automation** or **Publish** token at https://www.npmjs.com/settings/~tokens

## 2. GitHub secret

```powershell
gh secret set NPM_TOKEN -R ayushozha/provena
# paste token when prompted
```

## 3. Publish first release

After PR #32 is on `main`:

```powershell
git checkout main
git pull
git tag cli-v0.1.0
git push origin cli-v0.1.0
```

Watch: https://github.com/ayushozha/provena/actions/workflows/publish-cli.yml

## 4. Verify consumer install

```powershell
mkdir $env:TEMP\provena-consumer-test
cd $env:TEMP\provena-consumer-test
git init
npm install @provena/cli
npx provena --version   # expect 0.1.0
```

Store setup still requires [QUICKSTART.md](./QUICKSTART.md) clone + `PROVENA_STORE_ROOT`.

## Local dry-run (no registry)

```powershell
cd cli
npm pack
npm install .\provena-cli-0.1.0.tgz
node node_modules\@provena\cli\dist\cli.js --version
```

Or: `node tests/pack-install.test.mjs` (automated).