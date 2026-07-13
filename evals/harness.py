"""Live eval harness — real store + intelligence capture/pipeline (in-process)."""

from __future__ import annotations

import os
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generator

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEL_ROOT = _REPO_ROOT / "intelligence"


def _ensure_paths() -> None:
    for path in (str(_REPO_ROOT), str(_INTEL_ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)


class _StoreHttpxClient:
    """Route intelligence httpx store calls to the in-process store TestClient."""

    def __init__(self, store_client: Any, headers: dict[str, str], **_kwargs: Any) -> None:
        self._store = store_client
        self._headers = headers

    async def __aenter__(self) -> _StoreHttpxClient:
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False

    async def post(self, url: str, json: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        hdrs = {**self._headers, **(headers or {})}
        if url.endswith("/v1/memories/search"):
            response = self._store.post("/v1/memories/search", json=json or {}, headers=hdrs)
        elif url.endswith("/v1/memories"):
            response = self._store.post("/v1/memories", json=json or {}, headers=hdrs)
        else:
            class _Resp:
                status_code = 404
                text = "not found"

                @staticmethod
                def json() -> dict:
                    return {}

            return _Resp()

        class _Resp:
            def __init__(self, raw: Any) -> None:
                self.status_code = raw.status_code
                self.text = raw.text
                self._raw = raw

            def json(self) -> Any:
                return self._raw.json()

        return _Resp(response)

    async def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
        memory_id = url.rstrip("/").split("/")[-1]
        response = self._store.get(f"/v1/memories/{memory_id}", headers={**self._headers, **(headers or {})})

        class _Resp:
            def __init__(self, raw: Any) -> None:
                self.status_code = raw.status_code
                self.text = raw.text
                self._raw = raw

            def json(self) -> Any:
                return self._raw.json()

        return _Resp(response)


@dataclass
class EvalHarness:
    tenant_id: str = "eval"
    project_id: str = "provena"
    scope: dict[str, str] = field(default_factory=dict)
    temp_dir: Path = field(default_factory=Path)
    store_client: Any = field(default=None, repr=False)
    intel_client: Any = field(default=None, repr=False)
    _store_cm: Any = field(default=None, repr=False)
    _intel_cm: Any = field(default=None, repr=False)
    _httpx_patch: Any = field(default=None, repr=False)
    _store_app: Any = field(default=None, repr=False)
    _intel_app: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.scope:
            self.scope = {"tenant_id": self.tenant_id, "project_id": self.project_id}

    @property
    def admin_headers(self) -> dict[str, str]:
        return {
            "X-Provena-Tenant-Id": self.tenant_id,
            "X-Provena-Role": "admin",
            "X-Provena-Principal-Id": "eval-runner",
        }

    @property
    def editor_headers(self) -> dict[str, str]:
        return {**self.admin_headers, "X-Provena-Role": "editor"}

    def start(self) -> EvalHarness:
        _ensure_paths()
        from unittest.mock import patch

        from fastapi.testclient import TestClient

        temp_root = _REPO_ROOT / ".tmp-e2e" / "eval-harness"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_root / f"run-{int(time.time() * 1000)}-{os.getpid()}"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        os.environ["PROVENA_DB_PATH"] = str(self.temp_dir / "eval.db")
        os.environ["PROVENA_CAPTURE_FAILURE_STATE"] = str(self.temp_dir / "capture-failures.json")
        os.environ["PROVENA_INTEL_EMBEDDING_PROVIDER"] = "local"
        os.environ["PROVENA_INTEL_EMBEDDING_MODEL"] = "local-minilm"
        os.environ["PROVENA_INTEL_EMBEDDING_DIMENSIONS"] = "384"

        from evals.store_loader import load_intel_modules, load_store_modules

        self._store_app = load_store_modules(pin=True)
        self._store_app.config.get_settings.cache_clear()
        self._store_cm = TestClient(self._store_app.main.create_app())
        self.store_client = self._store_cm.__enter__()

        self._intel_app = load_intel_modules(pin=True)
        self._intel_app.config.settings.pipeline_url = "http://store.local"
        self._intel_app.config.settings.embedding_provider = "local"
        self._intel_app.config.settings.embedding_model = "local-minilm"
        self._intel_app.config.settings.embedding_dimensions = 384

        httpx_client = _StoreHttpxClient(self.store_client, self.admin_headers)
        self._httpx_patch = patch("httpx.AsyncClient", return_value=httpx_client)
        self._httpx_patch.start()

        self._intel_cm = TestClient(self._intel_app.main.create_app())
        self.intel_client = self._intel_cm.__enter__()
        return self

    def stop(self) -> None:
        if self._httpx_patch is not None:
            self._httpx_patch.stop()
        if self._intel_cm is not None:
            self._intel_cm.__exit__(None, None, None)
        if self._store_cm is not None:
            self._store_cm.__exit__(None, None, None)
        if self._intel_app is not None:
            self._intel_app.restore()
            self._intel_app = None
        if self._store_app is not None:
            self._store_app.restore()
            self._store_app = None
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.environ.pop("PROVENA_DB_PATH", None)
        os.environ.pop("PROVENA_CAPTURE_FAILURE_STATE", None)

    def capture(self, text: str, *, signal_type: str = "message", error_signature: str = "") -> list[dict[str, Any]]:
        body: dict[str, Any] = {"text": text, "signal_type": signal_type, "scope": self.scope}
        if error_signature:
            body["error_signature"] = error_signature
        response = self.intel_client.post("/v1/capture/process", json=body, headers=self.editor_headers)
        if response.status_code >= 400:
            return []
        return response.json().get("results") or []

    def pipeline_write(self, content: str, *, kind: str = "artifact", tags: list[str] | None = None) -> dict[str, Any]:
        response = self.intel_client.post(
            "/v1/pipeline/write",
            json={"kind": kind, "scope": self.scope, "content": content, "tags": tags or []},
            headers=self.editor_headers,
        )
        if response.status_code >= 400:
            return {}
        return response.json().get("memory") or {}

    def pipeline_search(self, query: str, *, limit: int = 10, max_tokens: int = 4096) -> list[dict[str, Any]]:
        response = self.intel_client.post(
            "/v1/pipeline/search",
            json={"query": query, "scope": self.scope, "limit": limit, "max_tokens": max_tokens},
            headers=self.admin_headers,
        )
        if response.status_code >= 400:
            return []
        return response.json().get("results") or []

    def store_get(self, memory_id: str) -> dict[str, Any]:
        response = self.store_client.get(f"/v1/memories/{memory_id}", headers=self.admin_headers)
        if response.status_code >= 400:
            return {}
        return response.json()

    def context_pack(self, task: str, *, max_tokens: int = 2000) -> str:
        results = self.pipeline_search(task, limit=8, max_tokens=max_tokens)
        lines = []
        for item in results:
            memory = item.get("memory") or item
            kind = (memory.get("metadata") or {}).get("capture_kind") or memory.get("kind")
            summary = memory.get("summary") or memory.get("content") or ""
            lines.append(f"[{kind}] {summary}")
        return "\n".join(lines)


@contextmanager
def live_harness() -> Generator[EvalHarness, None, None]:
    harness = EvalHarness().start()
    try:
        yield harness
    finally:
        harness.stop()