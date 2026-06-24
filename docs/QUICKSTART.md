# Provena CLI — 5-minute quickstart

Index TypeScript/JavaScript into governed local memory and search from your terminal.

**Package:** `@provena/cli@0.1.0` (first npm release — patch bumps only on `0.1.x` until a major release).

## Prerequisites

- **Node.js 18+**
- **Python 3.11+** with `pip`
- A **git repository** to index (your app repo)
- The **Provena Python store** — the npm CLI does not bundle the server yet. Clone the
  monorepo once and point the CLI at it (see step 1).

## 1. One-time store setup

```powershell
git clone https://github.com/ayushozha/provena.git $env:USERPROFILE\provena-store
cd $env:USERPROFILE\provena-store
pip install -e .
```

Keep this path. The CLI finds `app/main.py` via `PROVENA_STORE_ROOT` or by walking up from your project.

```powershell
$env:PROVENA_STORE_ROOT = "$env:USERPROFILE\provena-store"
```

On macOS/Linux:

```bash
export PROVENA_STORE_ROOT=~/provena-store
```

> **Future:** a PyPI `provena` package will remove the clone step. Until then, the store
> comes from the GitHub repo above.

## 2. Install the CLI in your project

```powershell
cd C:\path\to\your-app
git init   # if not already a git repo
npm install -D @provena/cli
```

Or without adding to `package.json`:

```powershell
npx @provena/cli --version
```

## 3. Initialize and start the store

```powershell
npx provena init
npx provena serve --detach
npx provena doctor
```

`serve` starts the SQLite store using Python/uvicorn from `PROVENA_STORE_ROOT`.
Data lives under `.provena/provena.db` in your project.

## 4. Index and search

```powershell
npx provena index
npx provena search "authentication"
npx provena search "middleware" --json --limit 10
```

## 5. Stop the store

```powershell
npx provena serve --stop
```

## Monorepo developers

If you cloned [provena](https://github.com/ayushozha/provena) for development, you do not
need npm install — see [cli/README.md](../cli/README.md).

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `cannot find Python store` | Set `PROVENA_STORE_ROOT` to the clone with `app/main.py` |
| `port ... is in use` | Change `store_url` in `.provena/config.json` or stop the other process |
| `not a git repository` | Run `git init` (or `provena init` only inside a git root) |
| `status` / `mcp` not implemented | Expected — see [roadmap/plan/](../roadmap/plan/) |

## Next steps

- [Roadmap](../roadmap/README.md) — incremental index, watch mode, npm-less pip story
- [Architecture](../ARCHITECTURE.md) — full memory plane
- [cli/README.md](../cli/README.md) — commands and versioning rules