"""Shared helpers for Provena capture hooks — fail-open, no cross-track imports."""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

LOG = logging.getLogger("provena.hooks")


def store_url() -> str:
    """Resolve store URL from env or .provena/config.json."""
    env = os.environ.get("PROVENA_STORE_URL", "").strip()
    if env:
        return env.rstrip("/")
    for candidate in (Path.cwd() / ".provena" / "config.json", Path.home() / ".provena" / "config.json"):
        if candidate.is_file():
            try:
                config = json.loads(candidate.read_text(encoding="utf-8"))
                url = str(config.get("store_url") or "").strip()
                if url:
                    return url.rstrip("/")
            except (OSError, json.JSONDecodeError):
                pass
    return "http://127.0.0.1:18092"


def default_scope() -> dict[str, Any]:
    tenant = os.environ.get("PROVENA_TENANT_ID", "local").strip() or "local"
    project = os.environ.get("PROVENA_PROJECT_ID", Path.cwd().name).strip() or "local"
    return {"tenant_id": tenant, "project_id": project}


def post_memory(payload: dict[str, Any], *, store: str | None = None) -> bool:
    """POST a memory to the store. Returns True on success; never raises."""
    url = f"{(store or store_url()).rstrip('/')}/v1/memories"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        LOG.warning("store write failed (fail-open): %s", exc)
        return False


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw.strip()}