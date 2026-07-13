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


def capture_url() -> str:
    return os.environ.get("PROVENA_CAPTURE_URL", "http://127.0.0.1:8081/v1/capture/process").strip()


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


def post_capture(payload: dict[str, Any], *, url: str | None = None) -> dict[str, Any] | None:
    """POST to intelligence capture API. Returns parsed JSON on success, else None."""
    target = (url or capture_url()).strip()
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        target,
        data=body,
        headers={"Content-Type": "application/json", "X-Provena-Role": "editor"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            if response.status >= 400:
                return None
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        LOG.warning("capture API failed (fail-open): %s", exc)
        return None


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