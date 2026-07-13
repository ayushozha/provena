"""Integration tests for /v1/capture/process against a live in-process store."""

from __future__ import annotations

import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class _StoreHttpxClient:
    def __init__(self, store_client, headers, **_kwargs):
        self._store = store_client
        self._headers = headers

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, json=None, headers=None):
        hdrs = {**self._headers, **(headers or {})}
        if url.endswith("/v1/memories/search"):
            raw = self._store.post("/v1/memories/search", json=json or {}, headers=hdrs)
        else:
            raw = self._store.post("/v1/memories", json=json or {}, headers=hdrs)

        class _Resp:
            def __init__(self, response):
                self.status_code = response.status_code
                self.text = response.text
                self._response = response

            def json(self):
                return self._response.json()

        return _Resp(raw)


class TestCaptureApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from evals.store_loader import load_intel_modules, load_store_modules

        temp_root = _REPO_ROOT / ".tmp-e2e" / "capture-api"
        temp_root.mkdir(parents=True, exist_ok=True)
        cls.temp_dir = temp_root / f"test-{int(time.time() * 1000)}"
        cls.temp_dir.mkdir()
        os.environ["PROVENA_DB_PATH"] = str(cls.temp_dir / "test.db")

        cls._store_app = load_store_modules(pin=True)
        cls._store_app.config.get_settings.cache_clear()
        cls.store_cm = TestClient(cls._store_app.main.create_app())
        cls.store = cls.store_cm.__enter__()
        cls.headers = {
            "X-Provena-Tenant-Id": "capture-test",
            "X-Provena-Role": "editor",
            "X-Provena-Principal-Id": "tester",
        }

        cls._intel_app = load_intel_modules(pin=True)
        cls._intel_app.config.settings.pipeline_url = "http://store.local"
        cls.httpx_patch = patch("httpx.AsyncClient", return_value=_StoreHttpxClient(cls.store, cls.headers))
        cls.httpx_patch.start()
        cls.intel_cm = TestClient(cls._intel_app.main.create_app())
        cls.intel = cls.intel_cm.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.intel_cm.__exit__(None, None, None)
        cls.httpx_patch.stop()
        cls.store_cm.__exit__(None, None, None)
        cls._intel_app.restore()
        cls._store_app.restore()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)
        os.environ.pop("PROVENA_DB_PATH", None)

    def test_capture_correction_persists_workflow(self) -> None:
        response = self.intel.post(
            "/v1/capture/process",
            json={
                "text": "Use pnpm not npm in this repo",
                "scope": {"tenant_id": "capture-test", "project_id": "demo"},
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertTrue(results[0]["created"])
        self.assertEqual(results[0]["kind"], "workflow")

    def test_capture_redacts_secrets_on_write(self) -> None:
        response = self.intel.post(
            "/v1/capture/process",
            json={
                "text": "Use key sk-EVALTEST-TOKEN not ghp_EVALTESTTOKEN for deploys",
                "scope": {"tenant_id": "capture-test", "project_id": "secrets"},
            },
            headers=self.headers,
        )
        memory_id = response.json()["results"][0]["memory_id"]
        stored = self.store.get(f"/v1/memories/{memory_id}", headers=self.headers)
        content = stored.json()["content"]
        self.assertNotIn("sk-EVALTEST", content)
        self.assertIn("[REDACTED]", content)


if __name__ == "__main__":
    unittest.main()