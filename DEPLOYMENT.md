# Provena Deployment Guide

This guide is for teams hosting Provena for another product or internal
platform. It covers the two deployment shapes that Provena ships today:

- `Standalone mode`: run the single-process Python API directly.
- `Polyglot mode`: run the full multi-service topology and expose the Go
  gateway as the external entrypoint.

For release-manager steps covering image publication, SDK packaging,
`openapi.json`, and verification gates, use
[RELEASE_CHECKLIST.md](./RELEASE_CHECKLIST.md).

Connected-mode admin APIs and MCP are not separate deployment modes today.
Connected mode rides on the same HTTP surfaces below, and MCP is an optional
tool bridge on top of the polyglot gateway instead of the primary integration
surface for product-to-product traffic.

## Choose a mode

| Mode | Shipped entrypoint today | External base URL | Use when |
|---|---|---|---|
| Standalone | `python -m uvicorn app.main:app` | `http://<host>:8092` | You want the smallest single-process deployment and can provide your own network boundary or auth in front of Provena. |
| Polyglot | `docker compose` with `gateway` exposed | `http://<host>:8080` | You want the full routed topology, production auth, connected-mode admin APIs, queueing, and MCP. |

## Standalone mode

### What it runs

Standalone mode runs the FastAPI app in [app/main.py](./app/main.py). This is
the current single-process Provena entrypoint that `scripts/run_e2e.py`
verifies today.

### Minimum configuration

| Variable | Required | Purpose |
|---|---|---|
| `PROVENA_DB_PATH` | Yes | Path to the SQLite database file used by the standalone store. |
| `PROVENA_ENVIRONMENT` | No | Labels the deployment environment in `/healthz`. Defaults to `development`. |

### Start

```powershell
cd services/provena
$env:PROVENA_DB_PATH = ".\data\provena.db"
$env:PROVENA_ENVIRONMENT = "production"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8092
```

### Verify

```powershell
curl http://127.0.0.1:8092/healthz
python .\scripts\export_openapi.py
```

Expected health response:

```json
{"service":"provena","environment":"production","status":"ok"}
```

### Stop

Stop the `uvicorn` process with `Ctrl+C`, or stop the service/unit that owns
the process if you wrapped it in `systemd`, Docker, or another supervisor.

### Connected-mode scheduled sync ticks

Standalone deployments can run scheduled connected-mode sync creation with the
same store database by invoking:

```powershell
cd services/provena
$env:PROVENA_DB_PATH = ".\data\provena.db"
python .\scripts\run_scheduler_tick.py --tenant-id tenant-prod
```

That script evaluates connector records with
`metadata.scheduler = {"enabled": true, "cadence_seconds": <seconds>}` and
dispatches through the existing connector execution seam. It only runs
providers that have a registered real sync worker; the bundled CLI registers
none, so every eligible connector is reported as skipped with reason
`provider_not_implemented` and nothing is written to the ledger. It does not
introduce a second public sync-write route, and it does not fabricate sync
jobs for providers that have no integration.

### Auth expectations

Standalone mode does not run the Go gateway auth middleware. It accepts the
`X-Provena-*` access-context headers used by the store, but it does not enforce
bearer API keys by itself. Use standalone mode for local development, CI, or
trusted internal deployments where another layer already provides
authentication, authorization, and rate limiting.

If you need Provena-managed bearer auth with `PROVENA_AUTH_ENABLED` and
`PROVENA_API_KEYS`, deploy polyglot mode instead.

## Polyglot mode

### What it runs

Polyglot mode uses [docker-compose.yml](./docker-compose.yml) to start the
shipped service graph:

- `gateway` on `:8080` for the external HTTP API
- `store` on `:8000` for memory CRUD and integration-plane persistence
- `intelligence` on `:8081` for write/read pipelines
- `orchestration` on `:50051` for trigger lookup and context budgeting
- `queue` on `:8091` for buffered writes
- `lifecycle` on `:8092` for retention, RTBF, and legal hold
- `mcp` on `:8090` for Model Context Protocol clients

External clients should call the gateway base URL, not the internal
service-to-service URLs inside the compose network.

### Required production settings

The shipped compose file is intentionally local-development friendly. It hard
codes `PROVENA_AUTH_ENABLED=false` on the gateway so smoke checks can boot
without credentials. Do not use that default for production.

For a real deployment, replace the gateway auth settings through a compose
override, Helm values, or another container runtime injection point. Provena
does not ship a `docker-compose.prod.yml`; if you prefer a second Compose file,
create and manage that override in your deployment repo or operator workspace:

```yaml
# Operator-authored example override file, not shipped in this repo.
services:
  gateway:
    environment:
      PROVENA_AUTH_ENABLED: "true"
      PROVENA_API_KEYS: >-
        [{"key_id":"platform-admin","tenant_id":"tenant-prod","role":"admin","hashed_key":"<sha256-hex-of-raw-token>","description":"Primary platform admin key"}]
```

`PROVENA_API_KEYS` must be a JSON array. Each `hashed_key` is the SHA-256 hex
digest of the raw bearer token, not the raw token itself.

### Operator-supplied and overrideable variables

| Variable | Required | Purpose |
|---|---|---|
| `PROVENA_AUTH_ENABLED` | Yes for production | Set this to `true` in your compose override or deployment manifest to enable gateway auth. |
| `PROVENA_API_KEYS` | Yes when auth is enabled | JSON array of gateway API key records. |
| `PROVENA_STORE_HOST_PORT` | No | Host port bound to the `store` container. Defaults to `8000`. |
| `PROVENA_GATEWAY_HOST_PORT` | No | Host port bound to the external gateway. Defaults to `8080`. |
| `PROVENA_INTELLIGENCE_HOST_PORT` | No | Host port bound to the intelligence service. Defaults to `8081`. |
| `PROVENA_LIFECYCLE_HOST_PORT` | No | Host port bound to lifecycle. Defaults to `8092`. |
| `PROVENA_QUEUE_HOST_PORT` | No | Host port bound to queue. Defaults to `8091`. |
| `PROVENA_MCP_HOST_PORT` | No | Host port bound to MCP. Defaults to `8090`. |
| `PROVENA_ORCHESTRATION_HOST_PORT` | No | Host port bound to orchestration. Defaults to `50051`. |

Inside compose, the shipped internal URLs remain:

- `http://gateway:8080`
- `http://store:8000`
- `http://intelligence:8081`
- `http://orchestration:50051`
- `http://queue:8091`
- `http://lifecycle:8092`
- `http://mcp:8090`

Those are internal-only addresses for the compose network. External products
should not call them directly.

### Start

```powershell
cd services/provena
docker compose -f docker-compose.yml -f .\docker-compose.auth.override.yml up --build -d
```

The second Compose file in that example is operator-authored. It should carry
the gateway auth override shown above. If you inject the same values through
Helm, Kubernetes manifests, or another runtime, use that mechanism instead of a
Compose override file.

If the default host ports are already occupied, set host-port overrides before
the same `docker compose` command and update the external gateway URL you use
for verification and client traffic:

```powershell
cd services/provena
$env:PROVENA_GATEWAY_HOST_PORT = "18080"
$env:PROVENA_STORE_HOST_PORT = "18000"
$gatewayBaseUrl = "http://127.0.0.1:18080"
docker compose -f docker-compose.yml -f .\docker-compose.auth.override.yml up --build -d
```

### Verify

```powershell
$gatewayBaseUrl = "http://127.0.0.1:8080"
docker compose ps
curl "$gatewayBaseUrl/healthz"
curl "$gatewayBaseUrl/v1/cold-start"
```

`docker compose ps` should show `store`, `orchestration`, `intelligence`,
`gateway`, `queue`, `lifecycle`, and `mcp` running. `/healthz` should return
`{"status":"ok"}`, and `/v1/cold-start` should report upstream service health.
If you set `PROVENA_GATEWAY_HOST_PORT`, point `$gatewayBaseUrl` at that host
port instead of `:8080`.

### Stop

```powershell
cd services/provena
docker compose down
```

### Connected-mode scheduled sync ticks

Polyglot deployments use the same scheduler script as an internal operator
task, cron job, or Kubernetes `CronJob`. Run it anywhere that shares the
Provena store database and code checkout, for example inside the `store`
container or another internal task container built from the same image:

```powershell
cd services/provena
docker compose exec store python .\scripts\run_scheduler_tick.py --tenant-id tenant-prod
```

This keeps cadence evaluation inside the existing store and integration-plane
boundary. The resulting sync jobs appear through the normal connector sync-job
and coverage APIs exposed by the gateway, while direct
`POST /v1/integrations/connectors/{connector_id}/sync-jobs` remains available
for manual or upstream-driven writes.

## Calling Provena from another project

### Base URLs

- `Standalone`: `http://<host>:8092` by default, or the port you pass to
  `uvicorn`
- `Polyglot`: `http://<host>:8080` by default, or the host port bound through
  `PROVENA_GATEWAY_HOST_PORT`
- `MCP`: `http://<host>:8090` only for MCP-capable clients using `/rpc` or
  `/sse`, or the host port bound through `PROVENA_MCP_HOST_PORT`

For ordinary product integrations, use the HTTP API or an SDK against the
standalone or polyglot base URL. Do not treat MCP as the general application
integration surface.

### Auth model

- `Standalone`: no built-in bearer API key enforcement. Put Provena behind your
  own ingress, mesh, VPN, or service auth layer if the endpoint is not private.
- `Polyglot local smoke/default compose`: gateway auth is disabled only for
  local development.
- `Polyglot production`: set `PROVENA_AUTH_ENABLED=true` and provide
  `PROVENA_API_KEYS`, then call the gateway with `Authorization: Bearer
  <raw-token>`.

### HTTP example

Polyglot write request with production auth enabled:

```bash
curl -X POST http://127.0.0.1:<gateway-host-port>/v1/memories \
  -H "Authorization: Bearer <raw-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "fact",
    "scope": {"tenant_id": "tenant-prod"},
    "title": "Launch decision",
    "content": "Ship the beta behind a feature flag."
  }'
```

Connected-mode coverage request through the same gateway surface:

```bash
curl "http://127.0.0.1:<gateway-host-port>/v1/integrations/coverage?tenant_id=tenant-prod" \
  -H "Authorization: Bearer <raw-token>"
```

Connected mode currently lands through the gateway and store APIs rather than a
separate connector worker binary. Another product should integrate with those
HTTP routes, not wait for a separate connector daemon.

### OpenAPI and SDK discovery

Generate the current OpenAPI artifact from the shipped FastAPI app:

```powershell
cd services/provena
python .\scripts\export_openapi.py
```

That command rewrites [openapi.json](./openapi.json). The exported schema is
the easiest contract artifact for another team to import into an API client,
generate SDK bindings from, or diff during upgrades. In polyglot mode, call
the same documented memory and integration routes through the gateway base URL.

The repository also includes SDK smoke coverage under:

- [sdk/python](./sdk/python)
- [sdk/typescript](./sdk/typescript)
- [sdk/go](./sdk/go)
- [sdk/rust](./sdk/rust)

## Immediate runtime checks after a fresh deployment

Watch these signals immediately after booting either mode:

- Startup failures for the documented services and entrypoints:
  `store`, `gateway`, `intelligence`, `queue`, `lifecycle`, `mcp`, and
  `orchestration`
- Unauthorized or invalid API key spikes on `/v1/memories` and
  `/v1/integrations/*` after turning on polyglot production auth
- 404, 422, or 5xx bursts from new external clients on the documented
  standalone or polyglot base URLs
- Stale or unhealthy upstreams in `docker compose ps`, `/healthz`, or
  `/v1/cold-start`
