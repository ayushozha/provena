#!/usr/bin/env python3
"""Git post-commit hook — extract commit decision and POST to capture API."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from _common import default_scope, post_capture, setup_logging

_STATE = Path.home() / ".provena" / "hook-state" / "commits.json"


def _load_seen() -> dict[str, bool]:
    if not _STATE.is_file():
        return {}
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _mark_seen(sha: str) -> None:
    seen = _load_seen()
    seen[sha] = True
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps(seen, indent=2), encoding="utf-8")


def _git(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return (result.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _latest_commit() -> dict[str, str]:
    return {
        "sha": _git(["rev-parse", "HEAD"]),
        "subject": _git(["log", "-1", "--pretty=%s"]),
        "body": _git(["log", "-1", "--pretty=%b"]),
        "diff_stat": _git(["show", "--stat", "--oneline", "-1"]),
    }


def _decision_summary(commit: dict[str, str]) -> str:
    parts = [commit.get("subject", "")]
    body = commit.get("body", "").strip()
    if body:
        parts.append(body.splitlines()[0])
    diff = commit.get("diff_stat", "").strip()
    if diff:
        parts.append(diff.splitlines()[-1])
    return " | ".join(p for p in parts if p)[:400]


def main() -> int:
    setup_logging()
    commit = _latest_commit()
    sha = commit.get("sha", "")
    if not sha or _load_seen().get(sha):
        return 0

    summary = _decision_summary(commit)
    response = post_capture(
        {
            "text": summary,
            "signal_type": "message",
            "scope": default_scope(),
            "source_references": [
                {
                    "source_type": "git",
                    "source_id": sha,
                    "uri": f"commit:{sha}",
                    "metadata": {"hook": "post_commit"},
                }
            ],
            "metadata": {"commit_sha": sha, "capture_source": "post_commit_hook"},
        }
    )
    if response is not None:
        _mark_seen(sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())