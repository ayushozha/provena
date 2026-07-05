#!/usr/bin/env python3
"""Claude Code hook — capture user corrections and repeated tool failures.

Reads JSON from stdin (Claude Code hook payload). Fail-open: always exits 0.
Idempotent within a run via content fingerprint in hook state file.

Install: provena hooks install (copies into .claude/hooks or equivalent).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from _common import default_scope, post_memory, read_stdin_json, setup_logging, store_url

_STATE_DIR = Path.home() / ".provena" / "hook-state"
_FAILURE_STATE = _STATE_DIR / "failures.json"
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


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
        return tool_result.strip(), hashlib.sha256(tool_result.encode()).hexdigest()[:16]
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


def _already_seen(kind: str, text: str) -> bool:
    seen = _load_json(_SEEN_STATE)
    fp = _fingerprint(kind, text)
    if fp in seen:
        return True
    seen[fp] = True
    _save_json(_SEEN_STATE, seen)
    return False


def _record_correction(text: str, payload: dict[str, Any]) -> None:
    if not _is_correction(text) or _already_seen("workflow", text):
        return
    kind = "preference" if "prefer" in text.lower() else "workflow"
    scope = default_scope()
    post_memory(
        {
            "kind": kind,
            "scope": scope,
            "content": text,
            "title": f"Captured {kind} from Claude hook",
            "summary": text[:240],
            "source_references": [
                {
                    "source_type": "hook",
                    "source_id": str(payload.get("session_id") or "hook-session"),
                    "metadata": {"hook": "claude_capture"},
                }
            ],
            "metadata": {"capture_source": "claude_hook"},
        },
        store=store_url(),
    )


def _record_failure(text: str, signature: str, payload: dict[str, Any]) -> None:
    if not text:
        return
    failures = _load_json(_FAILURE_STATE)
    count = int(failures.get(signature, 0)) + 1
    failures[signature] = count
    _save_json(_FAILURE_STATE, failures)
    if count < 2:
        return
    mistake_key = f"mistake:{signature}"
    if _already_seen("mistake", mistake_key):
        return
    scope = default_scope()
    post_memory(
        {
            "kind": "mistake",
            "scope": scope,
            "content": text,
            "title": f"Repeated failure {signature}",
            "summary": f"Do not repeat ({signature}): {text[:180]}",
            "source_references": [
                {
                    "source_type": "hook",
                    "source_id": str(payload.get("session_id") or "hook-session"),
                    "metadata": {"hook": "claude_capture", "failure_signature": signature},
                }
            ],
            "metadata": {"capture_source": "claude_hook", "occurrence_count": count},
        },
        store=store_url(),
    )


def main() -> int:
    setup_logging()
    payload = read_stdin_json()
    text = _extract_text(payload)
    if text:
        _record_correction(text, payload)
    failure_text, signature = _extract_failure(payload)
    if failure_text:
        _record_failure(failure_text, signature, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())