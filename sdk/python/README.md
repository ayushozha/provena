# Provena Python SDK

`altrixy-provena-sdk` is the publishable Python client for the Provena governed
memory plane.

## Install

```bash
pip install altrixy-provena-sdk
```

The install package name is `altrixy-provena-sdk`. The Python import path stays
`provena_sdk`.

## Quickstart

```python
from provena_sdk import ProvenaClient

client = ProvenaClient("http://127.0.0.1:8092")
health = client.health()
print(health["status"])
```

Use the standalone base URL (`http://127.0.0.1:8092`) when running the
single-process API directly, or the polyglot gateway URL
(`http://127.0.0.1:8080`) when calling the full multi-service deployment.

If your deployment enables gateway auth, pass a bearer token when you construct
the client:

```python
from provena_sdk import ProvenaClient

client = ProvenaClient(
    "http://127.0.0.1:8080",
    api_key="prov_live_your_token",
)
```

## Smoke check

The installed SDK ships with a smoke module that hits `/healthz`, writes a
memory, and searches for it again.

```bash
export PROVENA_BASE_URL=http://127.0.0.1:8092
python -m provena_sdk.smoke
```

If you are targeting an authenticated deployment, also export
`PROVENA_API_KEY` before running the smoke:

```bash
export PROVENA_BASE_URL=http://127.0.0.1:8080
export PROVENA_API_KEY=prov_live_your_token
python -m provena_sdk.smoke
```

If you prefer the generated console script that `pip install` places on your
PATH, run:

```bash
export PROVENA_BASE_URL=http://127.0.0.1:8092
provena-sdk-smoke
```

The repository smoke command, `python tests/smoke.py`, delegates to the same
shipped module so repo-local verification and installed-consumer verification
stay aligned. The smoke module exits `0` and prints `python-sdk-smoke: ok` on
success.
