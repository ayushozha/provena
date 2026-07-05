"""Tests for workflow/mistake capture engine."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from app.capture import CaptureEngine, CaptureSignal


class MockStore:
    def __init__(self) -> None:
        self.memories: list[dict[str, Any]] = []
        self.writes: list[dict[str, Any]] = []

    async def search(
        self,
        query: str,
        scope: dict[str, Any],
        *,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        hits = []
        for memory in self.memories:
            if kind and memory.get("kind") != kind:
                continue
            hay = f"{memory.get('summary', '')} {memory.get('content', '')}".lower()
            if query.lower() in hay or any(word in hay for word in query.lower().split()):
                hits.append(memory)
        return hits

    async def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.writes.append(payload)
        memory = {**payload, "memory_id": payload.get("memory_id", "m-new")}
        self.memories.append(memory)
        return {"created": True, "memory": memory}


class TestCaptureEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MockStore()
        self.engine = CaptureEngine(self.store)
        self.scope = {"tenant_id": "test", "project_id": "provena"}

    def _run(self, coro):
        return asyncio.run(coro)

    def test_correction_yields_workflow_memory(self) -> None:
        signal = CaptureSignal(
            text="Use pnpm not npm for this repo",
            scope=self.scope,
            source_references=[{"source_type": "transcript", "source_id": "t1"}],
        )
        results = self._run(self.engine.process(signal))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].created)
        self.assertEqual(results[0].kind, "workflow")
        self.assertEqual(self.store.writes[0]["kind"], "workflow")
        self.assertIn("pnpm", self.store.writes[0]["summary"].lower())

    def test_preference_correction_yields_preference_memory(self) -> None:
        signal = CaptureSignal(text="I prefer dark mode over light mode", scope=self.scope)
        results = self._run(self.engine.process(signal))
        self.assertEqual(results[0].kind, "preference")

    def test_twice_seen_failure_yields_one_mistake(self) -> None:
        signal = CaptureSignal(
            text="TypeError: cannot read property 'foo' of undefined",
            signal_type="tool_failure",
            scope=self.scope,
            error_signature="err-ts-foo",
        )
        first = self._run(self.engine.capture_failure(signal))
        self.assertIsNotNone(first)
        assert first is not None
        self.assertFalse(first.created)
        self.assertEqual(first.reason, "awaiting_second_occurrence")

        second = self._run(self.engine.capture_failure(signal))
        self.assertIsNotNone(second)
        assert second is not None
        self.assertTrue(second.created)
        self.assertEqual(second.kind, "mistake")

        third = self._run(self.engine.capture_failure(signal))
        self.assertIsNotNone(third)
        assert third is not None
        self.assertFalse(third.created)

        mistake_writes = [w for w in self.store.writes if w["kind"] == "mistake"]
        self.assertEqual(len(mistake_writes), 1)

    def test_decision_capture(self) -> None:
        signal = CaptureSignal(text="We decided to use PostgreSQL for prod", scope=self.scope)
        result = self._run(self.engine.capture_decision(signal))
        self.assertTrue(result.created)
        self.assertEqual(result.kind, "decision")

    def test_handoff_capture(self) -> None:
        signal = CaptureSignal(
            text="Handoff: auth refactor is ready for review, next owner is platform",
            scope=self.scope,
        )
        result = self._run(self.engine.capture_handoff(signal))
        self.assertTrue(result.created)
        self.assertEqual(result.kind, "handoff")

    def test_supersede_on_similar_workflow(self) -> None:
        self.store.memories.append(
            {
                "memory_id": "old-rule",
                "kind": "workflow",
                "summary": "Workflow rule: use npm",
                "content": "use npm",
            }
        )
        signal = CaptureSignal(text="Use pnpm not npm for installs", scope=self.scope)
        result = self._run(self.engine.capture_correction(signal))
        self.assertTrue(result.created)
        self.assertEqual(result.superseded_id, "old-rule")
        self.assertEqual(self.store.writes[-1]["supersedes_memory_id"], "old-rule")


if __name__ == "__main__":
    unittest.main()