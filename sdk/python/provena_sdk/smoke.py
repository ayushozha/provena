from __future__ import annotations

import os

from .client import ProvenaClient


def run_smoke(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> None:
    resolved_base_url = base_url or os.environ.get("PROVENA_BASE_URL")
    if not resolved_base_url:
        raise RuntimeError("PROVENA_BASE_URL is required")

    resolved_api_key = api_key or os.environ.get("PROVENA_API_KEY")
    client = ProvenaClient(resolved_base_url, api_key=resolved_api_key)

    health = client.health()
    if health["status"] != "ok":
        raise RuntimeError("health check failed")

    created = client.create_memory(
        {
            "kind": "artifact",
            "scope": {
                "tenant_id": "tenant-e2e",
                "workspace_id": "ws-e2e",
                "user_id": "python-sdk",
            },
            "title": "Python SDK smoke memory",
            "content": "Python SDK can write and search Provena memories.",
            "tags": ["sdk", "python"],
            "entity_keys": ["smoke"],
        }
    )

    results = client.search_memories(
        {
            "query": "Python SDK smoke",
            "scope": {
                "tenant_id": "tenant-e2e",
                "workspace_id": "ws-e2e",
                "user_id": "python-sdk",
            },
            "limit": 3,
        }
    )

    found = any(
        item["memory"]["memory_id"] == created["memory"]["memory_id"]
        for item in results["results"]
    )
    if not found:
        raise RuntimeError("search did not return created memory")

    print("python-sdk-smoke: ok")


def main() -> None:
    run_smoke()


if __name__ == "__main__":
    main()
