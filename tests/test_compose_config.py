from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PUBLISHED_SERVICES = {
    "orchestration": "PROVENA_ORCHESTRATION_HOST_BIND",
    "gateway": "PROVENA_GATEWAY_HOST_BIND",
    "mcp": "PROVENA_MCP_HOST_BIND",
    "queue": "PROVENA_QUEUE_HOST_BIND",
    "lifecycle": "PROVENA_LIFECYCLE_HOST_BIND",
    "intelligence": "PROVENA_INTELLIGENCE_HOST_BIND",
}


def render_compose(overrides: dict[str, str] | None = None) -> dict[str, object]:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is required for Compose configuration checks")

    env = os.environ.copy()
    for variable in PUBLISHED_SERVICES.values():
        env.pop(variable, None)
    env.update(overrides or {})
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def published_host_ip(config: dict[str, object], service: str) -> str:
    services = config["services"]
    assert isinstance(services, dict)
    service_config = services[service]
    assert isinstance(service_config, dict)
    ports = service_config["ports"]
    assert isinstance(ports, list) and len(ports) == 1
    port = ports[0]
    assert isinstance(port, dict)
    host_ip = port["host_ip"]
    assert isinstance(host_ip, str)
    return host_ip


def test_published_services_bind_loopback_by_default() -> None:
    config = render_compose()

    for service in PUBLISHED_SERVICES:
        assert published_host_ip(config, service) == "127.0.0.1"

    services = config["services"]
    assert isinstance(services, dict)
    store = services["store"]
    assert isinstance(store, dict)
    assert "ports" not in store


def test_published_service_bind_addresses_are_explicitly_overrideable() -> None:
    overrides = {variable: "0.0.0.0" for variable in PUBLISHED_SERVICES.values()}
    config = render_compose(overrides)

    for service in PUBLISHED_SERVICES:
        assert published_host_ip(config, service) == "0.0.0.0"
