"""Smoke tests for capture hooks against a mock HTTP store."""

from __future__ import annotations

import json
import sys
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOKS = _REPO_ROOT / "scripts" / "hooks"
if str(_HOOKS) not in sys.path:
    sys.path.insert(0, str(_HOOKS))


class _StoreHandler(BaseHTTPRequestHandler):
    posts: list[dict] = []

    def log_message(self, format, *args):  # noqa: A003
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        _StoreHandler.posts.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"created": True, "memory": body}).encode())


class TestHooks(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), _StoreHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.store_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def setUp(self) -> None:
        _StoreHandler.posts.clear()

    def test_claude_hook_writes_workflow_on_correction(self) -> None:
        import claude_capture

        payload = {"user_message": "Use pnpm not npm in this repo", "session_id": "s1"}
        with patch.object(claude_capture, "read_stdin_json", return_value=payload):
            with patch.object(claude_capture, "store_url", return_value=self.store_url):
                with patch.object(claude_capture, "_already_seen", return_value=False):
                    rc = claude_capture.main()
        self.assertEqual(rc, 0)
        kinds = [p.get("kind") for p in _StoreHandler.posts]
        self.assertIn("workflow", kinds)

    def test_post_commit_writes_decision(self) -> None:
        import post_commit

        fake_commit = {
            "sha": "abc123def456",
            "subject": "feat: add capture engine",
            "body": "Implements workflow/mistake capture",
            "diff_stat": "1 file changed",
        }
        with patch.object(post_commit, "_latest_commit", return_value=fake_commit):
            with patch.object(post_commit, "store_url", return_value=self.store_url):
                with patch.object(post_commit, "_load_seen", return_value={}):
                    rc = post_commit.main()
        self.assertEqual(rc, 0)
        self.assertTrue(_StoreHandler.posts)
        self.assertEqual(_StoreHandler.posts[0]["kind"], "decision")


if __name__ == "__main__":
    unittest.main()