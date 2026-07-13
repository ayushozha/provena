#!/usr/bin/env python3
"""Claude Code hook — forward corrections and tool failures to capture API."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from _common import default_scope, post_capture, read_stdin_json, setup_logging

_STATE_DIR = Path.home() / ".provena" / "hook-state"
_SEEN_STATE = _STATE_DIR / "seen.json"

_CORRECTION_HINTS = (
    "use ",
    "not ",
    "always ",
    "never ",
    "correction",
    "instead of",
    "don't use",
    "do not use",
    "prefer ",
)


def _load_seen() -> dict[str, bool]:
    if not _SEEN_STATE.is_file():
        return {}
    try:
        return json.loads(_SEEN_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _mark_seen(key: str) -> None:
    seen = _load_seen()
    seen[key] = True
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _SEEN_STATE.write_text(json.dumps(seen, indent=2), encoding="utf-8")


def _was_seen(key: str) -> bool:
    return bool(_load_seen().get(key))


def _fingerprint(kind: str, text: str) -> str:
    return hashlib.sha256(f"{kind}|{text.strip().lower()}".encode()).hexdigest()


def _is_correction(text: str) -> bool:
    lower = text.lower()
    return any(hint in lower for hint in _CORRECTION_HINTS)


def _extract_text(payload: dict[str, Any]) -> str:
    for key in ("user_message", "message", "text", "content", "prompt"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    transcript = payload.get("transcript")
    if isinstance(transcript, list):
        parts = [str(item.get("content", "")) for item in transcript if isinstance(item, dict)]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _extract_failure(payload: dict[str, Any]) -> tuple[str, str]:
    tool_result = payload.get("tool_result") or payload.get("error") or {}
    if isinstance(tool_result, str):
        text = tool_result.strip()
        return text, hashlib.sha256(text.encode()).hexdigest()[:16] if text else ("", "")
    if isinstance(tool_result, dict):
        text = str(tool_result.get("error") or tool_result.get("message") or tool_result.get("output") or "")
        text = text.strip()
        if text:
            return text, hashlib.sha256(text.encode()).hexdigest()[:16]
    hook_event = str(payload.get("hook_event_name") or payload.get("event") or "")
    if "tool" in hook_event.lower() and payload.get("is_error"):
        text = _extract_text(payload)
        if text:
            return text, hashlib.sha256(text.encode()).hexdigest()[:16]
    return "", ""


def _capture(payload: dict[str, Any], *, signal_type: str, text: str, error_signature: str = "") -> dict[str, Any] | None:
    body = {
        "text": text,
        "signal_type": signal_type,
        "scope": default_scope(),
        "source_references": [
            {
                "source_type": "hook",
                "source_id": str(payload.get("session_id") or "hook-session"),
                "metadata": {"hook": "claude_capture"},
            }
        ],
        "metadata": {"capture_source": "claude_hook"},
    }
    if error_signature:
        body["error_signature"] = error_signature
    return post_capture(body)


def main() -> int:
    setup_logging()
    payload = read_stdin_json()
    text = _extract_text(payload)
    if text and _is_correction(text):
        key = _fingerprint("workflow", text)
        if not _was_seen(key):
            response = _capture(payload, signal_type="message", text=text)
            if response is not None:
                _mark_seen(key)

    failure_text, signature = _extract_failure(payload)
    if failure_text and signature:
        key = _fingerprint("mistake", signature)
        if not _was_seen(key):
            response = _capture(payload, signal_type="tool_failure", text=failure_text, error_signature=signature)
            if response and any(
                item.get("created") and item.get("kind") == "mistake" for item in response.get("results") or []
            ):
                _mark_seen(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())