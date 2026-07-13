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
| `PROVENA_DB_PATH` | Yes* | Path to the SQLite database file used by the standalone store. |
| `PROVENA_DATABASE_URL` | Yes* | PostgreSQL connection URL (e.g. `postgresql://user:pass@host:5432/provena?sslmode=require`). When set, selects the Postgres backend and **takes precedence** over `PROVENA_DB_PATH`. |
| `PROVENA_ENVIRONMENT` | No | Labels the deployment environment in `/healthz`. Defaults to `development`. |
| `PROVENA_SERVICE_TOKEN` | One of token or registry is required outside explicit local development | Bearer secret for the simple tenant-dedicated identity. |
| `PROVENA_SERVICE_IDENTITIES` | Alternative to `PROVENA_SERVICE_TOKEN` | JSON array of tenant-bound identities containing only SHA-256 token digests, never raw tokens. |
| `PROVENA_SERVICE_TENANT_ID` | Yes with a service token | Tenant boundary assigned to the authenticated service identity. Request headers cannot override it. |
| `PROVENA_SERVICE_PRINCIPAL_ID` | No | ACL principal assigned to the service identity. Defaults to `provena-service`. |
| `PROVENA_SERVICE_ROLE` | No | Constrained service role: `viewer`, `editor`, or `admin`. Defaults to `editor`. |
| `PROVENA_GATEWAY_SERVICE_TOKEN` | Yes for authenticated polyglot mode | Private gateway-to-service transport bearer. It must not match any external gateway API key or tenant service credential. |
| `PROVENA_QUEUE_INGRESS_TOKEN` | Yes when queue runs | Separate bearer required on `POST /enqueue`; it must differ from the queue's downstream `PROVENA_SERVICE_TOKEN`. |
| `PROVENA_LIFECYCLE_SERVICE_TOKEN` | Yes for production lifecycle | Tenant-dedicated lifecycle bearer whose digest is registered with role `admin`. |
| `PROVENA_LIFECYCLE_TENANT_ID` | Yes with lifecycle token | The only tenant an instance may enumerate or enforce. |
| `PROVENA_LIFECYCLE_PRINCIPAL_ID` | Yes with lifecycle token | Stable lifecycle process principal. |
| `PROVENA_ALLOW_UNAUTHENTICATED_LOCAL` | No | Preserves legacy header/no-auth behavior only in `development`, `local`, or `test`. Defaults to `true`; it never bypasses production auth. |

\* Configure **one** storage backend: either `PROVENA_DATABASE_URL` (PostgreSQL) or
`PROVENA_DB_PATH` (SQLite). If both are set, `PROVENA_DATABASE_URL` wins.
Schema setup runs automatically on startup. PostgreSQL applies the canonical
schema plus `storage/migrations/002_tenant_integrity_postgres.sql`; SQLite uses
the matching `002_tenant_integrity_sqlite.sql` upgrade when an existing database
still has identifier-only integration foreign keys. pgvector KNN is not enabled
until PLAN-16; FTS uses `tsvector`.

The tenant-integrity upgrade is fail-closed. SQLite audits legacy integration
rows before rebuilding the four affected child tables in one transaction and
aborts startup without changing those tables if any parent or tenant mismatch
exists. PostgreSQL first installs named `NOT VALID` composite foreign keys so
new writes are protected, audits legacy rows, and validates the constraints only
when the audit is clean. A dirty PostgreSQL store keeps `/healthz` live for
diagnostics, returns `503` from `/readyz` and every `/v1/*` route, and requires
the operator to repair the reported rows and restart the service.

### Start

```powershell
cd services/provena
$env:PROVENA_DB_PATH = ".\data\provena.db"
$env:PROVENA_ENVIRONMENT = "production"
$env:PROVENA_SERVICE_TOKEN = "<long-random-service-token>"
$env:PROVENA_SERVICE_TENANT_ID = "tenant-prod"
$env:PROVENA_SERVICE_PRINCIPAL_ID = "neverzero-service"
$env:PROVENA_SERVICE_ROLE = "editor"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8092
```

### Verify

```powershell
curl http://127.0.0.1:8092/healthz
curl http://127.0.0.1:8092/readyz
python .\scripts\export_openapi.py
```

Expected health response:

```json
{"service":"provena","environment":"production","status":"ok"}
```

`/readyz` returns `200` only after the backend's tenant-integrity constraints
are installed and all legacy integration rows pass the audit. Use `/readyz`, not
`/healthz`, for readiness and traffic admission.

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

Standalone production mode validates `Authorization: Bearer <token>` on every
`/v1/*` request. A valid token resolves through either the single configured
identity or the hashed service-identity registry; caller-supplied
`X-Provena-*` headers cannot elevate or move that identity. The sole exception
is `PROVENA_GATEWAY_SERVICE_TOKEN`: it authenticates the gateway transport, and
the store then accepts identity headers that the gateway rebuilt from its
validated API-key record. External API-key bearers are never forwarded.
`/healthz` and `/readyz` remain unauthenticated for container and load-balancer
probes; readiness exposes integrity counts, never row contents.

Legacy no-auth and `X-Provena-*` behavior remains available only when
`PROVENA_ENVIRONMENT` is `development`, `local`, or `test` and
`PROVENA_ALLOW_UNAUTHENTICATED_LOCAL=true`. Non-local settings without a token
or a non-empty service-identity registry fail during startup instead of
silently running unrestricted.

For a shared private store, hash each high-entropy token outside the config and
register its tenant-bound identity:

```powershell
$env:PROVENA_TOKEN_TO_HASH = '<raw-token-from-secret-manager>'
python -c "import hashlib,os; print(hashlib.sha256(os.environ['PROVENA_TOKEN_TO_HASH'].encode()).hexdigest())"
$env:PROVENA_TOKEN_TO_HASH = $null

$env:PROVENA_SERVICE_IDENTITIES = '[{"token_sha256":"<64-hex-digest>","tenant_id":"tenant-a","principal_id":"neverzero-a","role":"editor"}]'
```

The raw token belongs only in the calling worker's secret manager. Provena
hashes every presented bearer token and compares it in constant time to every
configured digest.

## Polyglot mode

### What it runs

Polyglot mode uses [docker-compose.yml](./docker-compose.yml) to start the
shipped service graph:

- `gateway` on `:8080` for the external HTTP API
- `store` on internal Compose port `8000` for memory CRUD and integration-plane persistence
- `intelligence` on `:8081` for write/read pipelines
- `orchestration` on `:50051` for trigger lookup and context budgeting
- `queue` on `:8091` for buffered writes
- `lifecycle` on `:8092` for retention, RTBF, and legal hold
- `mcp` on `:8090` for Model Context Protocol clients

External clients should call the gateway base URL, not the internal
service-to-service URLs inside the compose network.
The shipped compose file does not publish the store port to the host.

### Required production settings

The Compose interpolation fallback is intentionally local-development friendly:
it uses `PROVENA_AUTH_ENABLED=false` when no environment is supplied. The
shipped [.env.example](./.env.example) instead uses fail-closed production
defaults. Copy it to `.env`, replace every blank credential, and keep that file
out of version control before starting a production-shaped stack.

Compose reads those values directly from `.env`. A Compose override, Helm
values, or another container runtime injection point remains appropriate when
your secret manager owns environment injection. Provena does not ship a
`docker-compose.prod.yml`; if you prefer a second Compose file, create and
manage it in your deployment repo or operator workspace:

```yaml
# Operator-authored example override file, not shipped in this repo.
services:
  gateway:
    environment:
      PROVENA_AUTH_ENABLED: "true"
      PROVENA_GATEWAY_SERVICE_TOKEN: "<internal-gateway-transport-token>"
      PROVENA_API_KEYS: >-
        [{"key_id":"platform-admin","tenant_id":"tenant-prod","role":"admin","principal_id":"platform-admin","groups":["platform-admins"],"hashed_key":"<sha256-hex-of-external-raw-token>","description":"Primary platform admin key"}]
  store:
    environment:
      PROVENA_GATEWAY_SERVICE_TOKEN: "<same-internal-gateway-transport-token>"
```

`PROVENA_API_KEYS` must be a JSON array. Each `hashed_key` is the SHA-256 hex
digest of the raw external bearer token, not the raw token itself. `principal_id`
and `groups` are authoritative identity metadata; `principal_id` defaults to
`key_id`. Caller-supplied `X-Provena-*` identity headers are ignored when auth
is enabled. The internal gateway token must be independently generated and
must never equal an external raw bearer.

### Operator-supplied and overrideable variables

| Variable | Required | Purpose |
|---|---|---|
| `PROVENA_AUTH_ENABLED` | Yes for production | Set this to `true` in your compose override or deployment manifest to enable gateway auth. |
| `PROVENA_API_KEYS` | Yes when auth is enabled | JSON array of gateway API key records. |
| `PROVENA_GATEWAY_SERVICE_TOKEN` | Yes when auth is enabled | Internal bearer shared by gateway, intelligence, store, and lifecycle ingress; never give it to external clients. |
| `PROVENA_INTEL_ENVIRONMENT` | Yes for a directly deployed intelligence service | Trust mode for intelligence ingress. Compose derives it from `PROVENA_ENVIRONMENT`. |
| `PROVENA_INTEL_ALLOW_UNAUTHENTICATED_LOCAL` | No | Enables no-auth intelligence ingress only for `development`, `local`, or `test`. Compose derives it from `PROVENA_ALLOW_UNAUTHENTICATED_LOCAL`. |
| `PROVENA_INTEL_GATEWAY_SERVICE_TOKEN` | Yes for direct authenticated intelligence deployment | Gateway transport bearer. Compose injects `PROVENA_GATEWAY_SERVICE_TOKEN`. |
| `PROVENA_INTEL_SERVICE_TOKEN` | Required when the queue calls intelligence outside local bypass | Queue's downstream tenant service bearer. Compose injects `PROVENA_SERVICE_TOKEN`. |
| `PROVENA_INTEL_SERVICE_TENANT_ID`, `PROVENA_INTEL_SERVICE_PRINCIPAL_ID`, `PROVENA_INTEL_SERVICE_ROLE` | Required with `PROVENA_INTEL_SERVICE_TOKEN` | Exact queue identity binding; Compose derives these from the common service identity variables. |
| `PROVENA_INTEL_EMBEDDING_PROVIDER` | No | `local` keeps the deterministic offline/test embedder; `openai` calls the configured OpenAI-compatible endpoint. |
| `PROVENA_INTEL_EMBEDDING_MODEL` | Yes when the intelligence embedding provider is remote | Exact model id served by the configured embedding endpoint. Provena does not select a production model in code. |
| `PROVENA_INTEL_EMBEDDING_BASE_URL` | Yes when the intelligence embedding provider is remote | OpenAI-compatible embedding API base URL. |
| `PROVENA_INTEL_EMBEDDING_API_KEY` | Provider-specific | Bearer credential for the embedding endpoint; may be empty for a trusted local endpoint. |
| `PROVENA_INTEL_EMBEDDING_DIMENSIONS` | No | Stored embedding vector width. Defaults to `768`; set it to the configured model's output width. |
| `PROVENA_INTEL_LLM_MODEL` | No | Single-provider model id for every LLM-backed stage. Empty keeps deterministic heuristics active. |
| `PROVENA_INTEL_LLM_BASE_URL` | Required with the single-provider LLM path | OpenAI-compatible chat-completions API base URL. |
| `PROVENA_INTEL_LLM_API_KEY` | Provider-specific | Bearer credential for the single-provider LLM path. |
| `PROVENA_INTEL_LLM_PROVIDERS` | No | JSON multi-provider registry; takes precedence over the single-provider variables. |
| `PROVENA_INTEL_MODEL_ROUTER_DEFAULT_TIER` | No | Initial routing tier. Defaults to `balanced`. |
| `PROVENA_GATEWAY_HOST_PORT` | No | Host port bound to the external gateway. Defaults to `8080`. |
| `PROVENA_INTELLIGENCE_HOST_PORT` | No | Host port bound to the intelligence service. Defaults to `8081`. |
| `PROVENA_LIFECYCLE_HOST_PORT` | No | Host port bound to lifecycle. Defaults to `8092`. |
| `PROVENA_QUEUE_HOST_PORT` | No | Host port bound to queue. Defaults to `8091`. |
| `PROVENA_MCP_HOST_PORT` | No | Host port bound to MCP. Defaults to `8090`. |
| `PROVENA_ORCHESTRATION_HOST_PORT` | No | Host port bound to orchestration. Defaults to `50051`. |
| `PROVENA_<SERVICE>_HOST_BIND` | No | Host address for each published service (`GATEWAY`, `MCP`, `ORCHESTRATION`, `INTELLIGENCE`, `QUEUE`, or `LIFECYCLE`). Every service defaults to `127.0.0.1`; expose one only through an explicit operator override. |

### Internal credential boundaries

Use independent credentials for each trust boundary:

1. External gateway API keys authenticate users and agents only at the gateway.
2. `PROVENA_GATEWAY_SERVICE_TOKEN` authenticates the gateway transport to the
   intelligence, store, and lifecycle ingress; the gateway strips caller credentials and
   rebuilds tenant, role, principal, key, and group headers from validated
   API-key metadata.
3. `PROVENA_QUEUE_INGRESS_TOKEN` authenticates callers to `/enqueue`, while the
   queue's separate tenant service token owns downstream writes.

Lifecycle uses another process-owned credential. Set
`PROVENA_LIFECYCLE_SERVICE_TOKEN`, `PROVENA_LIFECYCLE_TENANT_ID`, and
`PROVENA_LIFECYCLE_PRINCIPAL_ID`, then add the lifecycle token's SHA-256 digest
to `PROVENA_SERVICE_IDENTITIES` with the same tenant/principal and role
`admin`. Lifecycle proxy routes also require `PROVENA_GATEWAY_SERVICE_TOKEN`
before reading their request body. They accept only a superadmin or an admin
whose authoritative gateway tenant matches the process tenant. One lifecycle process serves one tenant.
Store 401/403 responses mark that process unhealthy instead of being treated
as an empty retention run.

Intelligence applies the same boundary before parsing any `/v1/*` body or
calling a model, orchestration, or store service. A gateway bearer requires the
gateway-rebuilt identity headers. A queue service bearer must match the
configured tenant, principal, and role exactly. Only the fixed credential and
identity allowlist is forwarded to the store; model and orchestration calls
never receive those headers. `/healthz` remains unauthenticated.

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

The intelligence container receives every provider, model, endpoint, key,
dimension, and router setting from the environment. No model id is selected in
the Compose file. For an operator-managed model server on the Docker host, the
example env uses `host.docker.internal`; the Compose service installs the
standard host-gateway mapping for Linux engines as well as Docker Desktop.

### Start

```powershell
cd services/provena
Copy-Item .env.example .env
# Fill every blank credential in .env through your secret manager, then:
docker compose up --build -d
```

If you inject the same values through an operator-authored Compose override,
Helm, Kubernetes manifests, or another runtime, use that mechanism instead of a
local `.env` file.

If the default host ports are already occupied, set host-port overrides before
the same `docker compose` command and update the external gateway URL you use
for verification and client traffic:

```powershell
cd services/provena
$env:PROVENA_GATEWAY_HOST_PORT = "18080"
$gatewayBaseUrl = "http://127.0.0.1:18080"
docker compose up --build -d
```

All published services bind `127.0.0.1` by default. To publish a service beyond
the local host, set its matching `*_HOST_BIND` variable explicitly and protect
the endpoint with authentication, a reverse proxy, and network policy. In
particular, do not expose MCP solely by changing `PROVENA_MCP_HOST_BIND` while
the gateway is running without authentication.

### Verify

```powershell
$gatewayBaseUrl = "http://127.0.0.1:8080"
docker compose ps
curl "$gatewayBaseUrl/healthz"
curl "$gatewayBaseUrl/readyz"
curl "$gatewayBaseUrl/v1/cold-start"
```

`docker compose ps` should show `store`, `orchestration`, `intelligence`,
`gateway`, `queue`, `lifecycle`, and `mcp` running. `/healthz` should return
`{"status":"ok"}`. `/readyz` and `/v1/cold-start` return `200` with status
`ready` only when every configured upstream is healthy; either returns `503`
with status `blocked` when the store integrity audit or another dependency is
not ready. Both gateway probe endpoints remain unauthenticated and expose only
aggregate service status. Use `/readyz` for gateway readiness probes. If you set
`PROVENA_GATEWAY_HOST_PORT`, point `$gatewayBaseUrl` at that host port instead
of `:8080`.

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

### Private NeverZero Compose path

The direct NeverZero projection path deliberately does not publish Provena's
store port. Both repositories declare the same external network and Provena
assigns the store the `provena-store` alias on it.

Create that network once before starting either project:

```powershell
docker network create neverzero-provena # skip when it already exists
```

Then configure the Provena checkout's `.env` and start the store before the
NeverZero worker:

```powershell
# Provena checkout
docker compose up --build -d store

# NeverZero checkout
docker compose --profile coordination up --build -d coordination-worker
```

Set `PROVENA_INTEGRATION_NETWORK` to the same value in both repositories. Set
NeverZero `PROVENA_STORE_URL=http://provena-store:8000`, make its
`PROVENA_API_KEY` equal Provena's `PROVENA_SERVICE_TOKEN`, and make its
`PROVENA_TENANT_ID` equal Provena's `PROVENA_SERVICE_TENANT_ID`. Also align
NeverZero `PROVENA_SERVICE_PRINCIPAL` with Provena
`PROVENA_SERVICE_PRINCIPAL_ID` so projected writes have one stable service
identity. With `PROVENA_SERVICE_IDENTITIES`, the worker's `PROVENA_API_KEY` is
the raw token whose digest appears in the registry, and its tenant/principal
must match that registry entry.

The simple `PROVENA_SERVICE_TOKEN` path resolves to one tenant and principal.
Use one private process per tenant for the smallest isolation boundary, or use
`PROVENA_SERVICE_IDENTITIES` when multiple tenant-specific NeverZero workers
must share one private store. Every worker still has a distinct token and
tenant; a credential can never select a tenant through request headers.

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

- `Standalone production`: use `PROVENA_SERVICE_TOKEN` for one configured
  tenant and service principal, or use the hashed
  `PROVENA_SERVICE_IDENTITIES` registry for multiple tenant-bound principals;
  `/v1/*` calls fail closed.
- `Standalone local development`: the legacy no-auth/header path is available
  only through the explicit local environment and bypass setting.
- `Polyglot local smoke/default compose`: gateway auth is disabled only for
  local development.
- `Polyglot production`: set `PROVENA_AUTH_ENABLED=true` and provide
  `PROVENA_API_KEYS`, then call the gateway with `Authorization: Bearer
  <raw-token>`.

Copy [.env.example](./.env.example) as an operator-owned starting point. Replace
every placeholder through a secret/configuration manager; do not commit real
tokens or provider keys.

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
