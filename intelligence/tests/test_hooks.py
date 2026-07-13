"""Smoke tests for capture hooks against capture API."""

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


class _CaptureHandler(BaseHTTPRequestHandler):
    posts: list[dict] = []

    def log_message(self, format, *args):  # noqa: A003
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        _CaptureHandler.posts.append(body)
        payload = {
            "results": [
                {
                    "created": True,
                    "kind": "workflow" if "pnpm" in body.get("text", "") else "mistake",
                    "memory_id": "m1",
                    "reason": "stored",
                }
            ]
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())


class TestHooks(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.capture_url = f"http://127.0.0.1:{cls.port}/v1/capture/process"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def setUp(self) -> None:
        _CaptureHandler.posts.clear()

    def test_claude_hook_posts_correction_to_capture_api(self) -> None:
        import _common
        import claude_capture

        payload = {"user_message": "Use pnpm not npm in this repo", "session_id": "s1"}
        with patch.object(claude_capture, "read_stdin_json", return_value=payload):
            with patch.object(_common, "capture_url", return_value=self.capture_url):
                with patch.object(claude_capture, "_was_seen", return_value=False):
                    rc = claude_capture.main()
        self.assertEqual(rc, 0)
        self.assertEqual(len(_CaptureHandler.posts), 1)
        self.assertIn("pnpm", _CaptureHandler.posts[0]["text"])

    def test_post_commit_posts_decision_to_capture_api(self) -> None:
        import _common
        import post_commit

        fake_commit = {
            "sha": "abc123def456",
            "subject": "feat: add capture engine",
            "body": "Implements workflow/mistake capture",
            "diff_stat": "1 file changed",
        }
        with patch.object(post_commit, "_latest_commit", return_value=fake_commit):
            with patch.object(_common, "capture_url", return_value=self.capture_url):
                with patch.object(post_commit, "_load_seen", return_value={}):
                    rc = post_commit.main()
        self.assertEqual(rc, 0)
        self.assertTrue(_CaptureHandler.posts)
        self.assertIn("feat", _CaptureHandler.posts[0]["text"])


if __name__ == "__main__":
    unittest.main()