# Provena

Provena is a provenance-first memory layer for LLM applications. It stores
typed memories, source references, temporal validity, scoped recall, and
explainable retrieval behind a small HTTP API. It now supports four runtime
surfaces:

- `MCP mode` for agent clients that want memory through a tool bridge.
- `Standalone mode` for teams running the single-process Python API directly.
- `Polyglot mode` for teams exposing the full gateway, queue, lifecycle,
  orchestration, intelligence, store, and MCP topology.
- `Connected mode` for embedding Provena into existing products and enterprise
  systems with connector inventory, principal mapping, permission sync, and
  sync coverage reporting.

Architecture is documented in [ARCHITECTURE.md](./ARCHITECTURE.md).
Product positioning is documented in [PRODUCT.md](./PRODUCT.md).
Deployment instructions are documented in [DEPLOYMENT.md](./DEPLOYMENT.md).
Release steps are documented in [RELEASE_CHECKLIST.md](./RELEASE_CHECKLIST.md).
External Go SDK usage is documented in [sdk/go/README.md](./sdk/go/README.md).
The Provena-only autonomous loop is documented in [loop/README.md](./loop/README.md).

## Core ideas

- Typed memory: facts, episodes, decisions, artifacts, preferences, and more.
- Provenance first: every memory can cite source objects and source spans.
- Temporal by default: memories support validity windows and supersession.
- Scoped recall: tenant, workspace, project, user, agent, and session scopes.
- Permission-preserving context: source ACLs can be synchronized into principal
  mappings and source permission grants before recall.
- Connected-mode coverage: connector health, source freshness, and sync job
  status can be queried per tenant.
- Local-first: SQLite is the default backend for fast local development.

## Local run

```powershell
cd services/provena
$env:PROVENA_DB_PATH = ".\\data\\provena.db"
python -m uvicorn app.main:app --reload --port 8092
```

## Docker Compose auth defaults

The packaged `docker-compose.yml` is configured for local development and smoke
checks. It sets `PROVENA_AUTH_ENABLED=false` on the gateway so a local stack can
boot without pre-provisioned credentials. That setting is not a safe production
default for standalone or polyglot deployments because the gateway injects an
anonymous super-admin context when auth is disabled.

For authenticated deployments, replace the gateway environment through a
compose override, Helm values, or another deployment manifest so the gateway
runs with `PROVENA_AUTH_ENABLED=true` and receives `PROVENA_API_KEYS` from your
secret manager or deployment environment. `PROVENA_API_KEYS` must be a JSON
array of API key records, and each `hashed_key` field must contain the SHA-256
hex digest of the raw bearer token instead of the token itself.

```json
[
  {
    "key_id": "platform-admin",
    "tenant_id": "tenant-prod",
    "role": "admin",
    "hashed_key": "<sha256-hex-of-raw-token>",
    "description": "Primary platform admin key"
  }
]
```

Setting `PROVENA_AUTH_ENABLED=true` without provisioning `PROVENA_API_KEYS`
will make authenticated requests fail, so treat the auth flag and key material
as a single production rollout step.

The packaged compose stack still defaults to host ports `50051`, `8000`,
`8080`, `8081`, `8090`, `8091`, and `8092`, but each published port is now
overrideable for shared hosts or CI runners where one of those ports is already
occupied. Set the matching `*_HOST_PORT` environment variables before
`docker compose up`, for example:

```powershell
cd services/provena
$env:PROVENA_STORE_HOST_PORT = "18000"
$env:PROVENA_GATEWAY_HOST_PORT = "18080"
docker compose up --build -d
```

That override only changes host bindings. Container-to-container URLs inside
the compose network still use the default internal service ports.

## Export OpenAPI

```powershell
cd services/provena
python .\scripts\export_openapi.py
```

## End-to-end verification

```powershell
cd services/provena
python .\scripts\run_e2e.py
```

## Provena autonomous loop

```powershell
cd services/provena
python .\scripts\provena_loop.py --validate-only
python .\scripts\provena_loop.py --once --dry-run
python .\scripts\provena_loop.py
```

This loop is specific to Provena. It reads `loop/prd.json`, works one story at
a time through `codex exec`, enforces a strict PM -> tester plan -> engineer ->
PM review -> tester execution cycle, updates `loop/qa_test_plan.json` and
`loop/progress.txt`, and keeps idling for new stories until `loop/STOP` is
created.

## Connected-mode ingestion

Connected mode today is **push-based**. There are no first-party scheduled
sync workers yet. Data lands in Provena through the integration-plane batch
APIs and the direct sync-job route, all driven by an external caller that has
already fetched data from the upstream system:

- `POST /v1/integrations/connectors/{connector_id}/sources/batch`
- `POST /v1/integrations/connectors/{connector_id}/principal-mappings/batch`
- `POST /v1/integrations/connectors/{connector_id}/permissions/batch`
- `POST /v1/integrations/connectors/{connector_id}/sync-jobs`

The `ConnectorProvider` enum (Slack, Google Drive, Notion, etc.) only
type-validates connector records on `POST /v1/integrations/connectors`. Listing
a provider there does **not** mean Provena ships a sync worker that talks to
that system.

### Optional scheduler tick

`scripts/run_scheduler_tick.py` exists as an internal operator entrypoint for
fixed-cadence ticks, but it only runs providers that have a *registered real
worker*. The tick scans stored connectors, evaluates each connector's
`metadata.scheduler` config, and dispatches to a registered worker via the
shared `ConnectorExecutionService` contract. The bundled CLI registers no
workers, so every eligible connector is reported as skipped with reason
`provider_not_implemented` and nothing is written to the ledger. Until a real
worker is registered for a provider, scheduled ticks are a no-op for it.

Use this metadata shape on a connector to mark it eligible for scheduled ticks
(it still requires a registered worker to actually run):

```json
{
  "scheduler": {
    "enabled": true,
    "cadence_seconds": 900,
    "job_type": "full"
  }
}
```

Run one scheduler tick locally with:

```powershell
cd services/provena
python .\scripts\run_scheduler_tick.py --tenant-id tenant-acme
```

## Integration-plane admin API

Provena exposes admin endpoints for connected-mode foundations:

- `POST /v1/integrations/connectors`
- `POST /v1/integrations/connectors/{connector_id}/sources/batch`
- `POST /v1/integrations/connectors/{connector_id}/principal-mappings/batch`
- `POST /v1/integrations/connectors/{connector_id}/permissions/batch`
- `POST /v1/integrations/connectors/{connector_id}/sync-jobs`
- `GET /v1/integrations/coverage`

These endpoints are intended to sit behind the control plane and let existing
enterprise systems contribute governed context instead of forcing teams to
replace their tools of record. The direct sync-job POST remains available for
manual or upstream-driven writes, while the internal scheduler tick creates the
same ledger records on a fixed cadence for opted-in connectors.
