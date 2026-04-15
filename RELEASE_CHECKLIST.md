# Provena Release Checklist

Use this checklist when cutting a Provena release for self-hosted operators or
embedded SDK consumers. It is intentionally limited to the release surfaces the
repo ships today: Docker images, SDK packaging, `openapi.json`, verification
commands, and immediate runtime checks.

## 1. Confirm the release scope

- [ ] Record the release tag or version you are cutting.
- [ ] Decide whether the release changes container images, SDK packages,
  `openapi.json`, or a combination of those artifacts.
- [ ] If the release changes gateway auth, schema, or connected-mode behavior,
  plan to run the full regression gate in addition to any package-specific
  checks.

## 2. Validate the shipped container image inventory

Run from `services/provena` before publishing any images:

```powershell
docker compose config -q
```

Publish the image surfaces that match the current repo topology:

| Surface | Build context | Dockerfile | Release note |
|---|---|---|---|
| `store` | `.` | `Dockerfile.store` | Python memory store image. |
| `intelligence` | `./intelligence` | `intelligence/Dockerfile` | Python intelligence image. |
| `orchestration` | `./orchestration` | `orchestration/Dockerfile` | Rust orchestration image. |
| `gateway` | `./control-plane` | `control-plane/Dockerfile` | Go control-plane image built with `--build-arg CMD=gateway`. |
| `mcp` | `./control-plane` | `control-plane/Dockerfile` | Same Dockerfile, built with `--build-arg CMD=mcp`. |
| `queue` | `./control-plane` | `control-plane/Dockerfile` | Same Dockerfile, built with `--build-arg CMD=queue`. |
| `lifecycle` | `./control-plane` | `control-plane/Dockerfile` | Same Dockerfile, built with `--build-arg CMD=lifecycle`. |

- [ ] Do not invent per-service Go Dockerfiles. Provena ships one
  `control-plane/Dockerfile` with `CMD` build variants.
- [ ] Keep standalone and polyglot boundaries aligned with the shipped docs.
  Provena does not ship a separate standalone Dockerfile today; standalone is
  documented as direct `uvicorn app.main:app`, while the closest shipped
  container artifact is the `store` image from `Dockerfile.store`. Polyglot
  exposes `gateway` and keeps `store`, `intelligence`, `orchestration`,
  `queue`, `lifecycle`, and `mcp` behind it.
- [ ] If you publish images, tag them consistently for the same release cut.

Example build commands:

```powershell
docker build -f Dockerfile.store -t <registry>/provena-store:<tag> .
docker build -f intelligence/Dockerfile -t <registry>/provena-intelligence:<tag> intelligence
docker build -f orchestration/Dockerfile -t <registry>/provena-orchestration:<tag> orchestration
docker build -f control-plane/Dockerfile --build-arg CMD=gateway -t <registry>/provena-gateway:<tag> control-plane
docker build -f control-plane/Dockerfile --build-arg CMD=mcp -t <registry>/provena-mcp:<tag> control-plane
docker build -f control-plane/Dockerfile --build-arg CMD=queue -t <registry>/provena-queue:<tag> control-plane
docker build -f control-plane/Dockerfile --build-arg CMD=lifecycle -t <registry>/provena-lifecycle:<tag> control-plane
```

## 3. Release the SDK surfaces honestly

### Publishable packages

| SDK | Current release surface | Required command(s) | Notes |
|---|---|---|---|
| TypeScript | `@altrixy/provena-sdk` from `sdk/typescript/package.json` | `npx -y -p typescript tsc -p .` then `npm pack` | Publishable package backed by `dist/`. |
| Python | `altrixy-provena-sdk` from `sdk/python/pyproject.toml` | `python -m build` | Publishable package with import path `provena_sdk`; this Windows host may need the direct-interpreter `setup.py sdist bdist_wheel` fallback because `python -m build` has hit temp ACL issues locally. |

- [ ] Run the TypeScript packaging commands from `services/provena/sdk/typescript`.
- [ ] Run the Python packaging command from `services/provena/sdk/python`.
- [ ] If the release includes TypeScript SDK changes, rebuild `dist/` and
  verify the packed tarball with `npm pack`.
- [ ] If the release includes Python SDK changes, build the sdist/wheel with
  `python -m build`.
- [ ] Do not claim a TypeScript or Python package release without running the
  matching packaging command for that SDK.

### Smoke-only SDKs today

| SDK | Current state | Verification path | Notes |
|---|---|---|---|
| Go | Repo subdirectory module, no standalone semver tag flow yet | `go run ./examples/smoke` from `sdk/go`, or `python scripts/run_e2e.py` | Keep the README caveat about pseudo-versions and local `replace` workflows. |
| Rust | Repo crate with example smoke, no documented crates.io publication flow | `cargo run --example smoke` from `sdk/rust`, or `python scripts/run_e2e.py` | Treat as smoke coverage, not a registry publication promise. |

- [ ] Do not describe Go or Rust as publishable registry packages unless their
  packaging story changes in the repo.
- [ ] If the release touches Go or Rust SDK code, run the repo smoke path or
  the centralized E2E gate before shipping.

## 4. Export and include the OpenAPI contract

Run from `services/provena`:

```powershell
python .\scripts\export_openapi.py
```

- [ ] Confirm the command rewrites `services/provena/openapi.json`.
- [ ] Include the refreshed `openapi.json` artifact in the release whenever the
  HTTP contract changes.
- [ ] Treat OpenAPI export as a required release step, not an optional local
  convenience command.

## 5. Run the standard release verification gates

Run from `services/provena`:

```powershell
docker compose config -q
python .\scripts\run_e2e.py
python .\scripts\export_openapi.py
```

- [ ] `docker compose config -q` passes before image publication.
- [ ] `python .\scripts\run_e2e.py` passes before releases that change SDKs,
  gateway/store/intelligence behavior, connected mode, or other cross-surface
  flows. This gate exercises the standalone and polyglot suites plus the
  Python, TypeScript, Go, and Rust SDK smokes.
- [ ] `python .\scripts\export_openapi.py` passes before updating release
  artifacts or docs that reference the HTTP contract.
- [ ] If you ran per-SDK packaging commands above, keep those artifacts aligned
  with the same commit as the E2E and OpenAPI checks.

Windows host note for this repo: if the generic `python` launcher is blocked by
local policy, use
`C:\Users\ayush\AppData\Local\Programs\Python\Python312\python.exe` for the
Python commands above. If `python -m build` still fails on this host because of
temp-directory ACLs, fall back to
`C:\Users\ayush\AppData\Local\Programs\Python\Python312\python.exe setup.py sdist bdist_wheel`
inside `services/provena/sdk/python`.

## 6. Perform immediate post-release runtime checks

- [ ] Confirm the expected services boot after rollout:
  `store`, `gateway`, `intelligence`, `queue`, `lifecycle`, `mcp`, and
  `orchestration` for polyglot, or the standalone Python API for standalone.
- [ ] Check the basic runtime health surfaces immediately after deployment:

```powershell
docker compose ps
curl http://127.0.0.1:<gateway-or-standalone-port>/healthz
curl http://127.0.0.1:<gateway-port>/v1/cold-start
```

- [ ] If you have a release validation token, run one authenticated memory
  write or search against `/v1/memories` and one authenticated coverage read
  against `/v1/integrations/coverage?tenant_id=<tenant>` on the fresh
  deployment.
- [ ] Watch for new startup failures, 5xx spikes, timeout spikes, or auth
  failures on `/healthz`, `/v1/cold-start`, `/v1/memories`, and
  `/v1/integrations/coverage`.
- [ ] If Sentry or another runtime dashboard exists in the deployment
  environment, check it for startup failures and new error or latency spikes
  across `gateway`, `store`, `intelligence`, `queue`, `lifecycle`, `mcp`, and
  `orchestration` right after rollout.
- [ ] Watch for downstream SDK-consumer breakage after the release, especially
  auth, validation, or schema-mismatch spikes from TypeScript or Python SDK
  traffic and CI or startup failures triggered by Go or Rust consumer upgrades.
