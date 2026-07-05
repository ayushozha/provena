"""Eval scorecard — code recall, agent lift, mistake recall, stale facts, secrets."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.mock_store import MockStore, redact_secrets

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTELLIGENCE_ROOT = _REPO_ROOT / "intelligence"
_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _ensure_intelligence_path() -> None:
    intel = str(_INTELLIGENCE_ROOT)
    if intel not in sys.path:
        sys.path.insert(0, intel)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _index_repo_files(store: MockStore, scope: dict[str, Any]) -> int:
    """Index a lightweight file map into the mock store for code_recall."""
    skip_dirs = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "agent-tools"}
    count = 0
    for path in _REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix not in {".py", ".md", ".ts", ".go", ".rs", ".json"}:
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        snippet = text[:1200]
        store.create(
            {
                "kind": "artifact",
                "scope": scope,
                "content": f"File {rel}: {snippet}",
                "summary": f"Indexed source file {rel}",
                "tags": [f"file:{rel}"],
                "metadata": {"path": rel},
            }
        )
        count += 1
    return count


def run_code_recall(store: MockStore, scope: dict[str, Any]) -> dict[str, Any]:
    dataset = _load_json(_FIXTURES / "code_recall_dataset.json")
    queries = dataset.get("queries") or []
    if not store.memories:
        _index_repo_files(store, scope)

    hits_at_1 = hits_at_5 = mrr_sum = 0.0
    token_total = 0
    scored = 0
    for item in queries:
        query = str(item.get("query") or "")
        relevant_paths = [str(p) for p in (item.get("relevant_paths") or [])]
        results = store.search(query, max_characters=2000)
        retrieved_paths = []
        for hit in results:
            tags = hit["memory"].get("tags") or []
            for tag in tags:
                if tag.startswith("file:"):
                    retrieved_paths.append(tag.removeprefix("file:"))
            meta_path = hit["memory"].get("metadata", {}).get("path")
            if meta_path:
                retrieved_paths.append(str(meta_path))
        token_total += sum(len(r["memory"].get("content", "")) for r in results)

        rank = 0
        for i, path in enumerate(retrieved_paths[:5]):
            if any(path.endswith(rp) or rp in path for rp in relevant_paths):
                rank = i + 1
                break
        if rank:
            hits_at_1 += 1.0 if rank == 1 else 0.0
            hits_at_5 += 1.0
            mrr_sum += 1.0 / rank
        scored += 1

    n = max(scored, 1)
    return {
        "hit_rate@1": round(hits_at_1 / n, 4),
        "hit_rate@5": round(hits_at_5 / n, 4),
        "mrr": round(mrr_sum / n, 4),
        "avg_tokens": round(token_total / n),
        "queries_scored": scored,
        "files_indexed": len(store.memories),
    }


def _heuristic_task_score(context: str, expected_keywords: list[str]) -> float:
    if not context.strip():
        return 0.0
    lower = context.lower()
    hits = sum(1 for kw in expected_keywords if kw.lower() in lower)
    return round(hits / max(len(expected_keywords), 1), 4)


async def _llm_judge_score(context: str, task: str, expected_keywords: list[str]) -> float | None:
    """Optional LLM judge when PROVENA_EVAL_JUDGE_MODEL is configured."""
    model = os.environ.get("PROVENA_EVAL_JUDGE_MODEL", "").strip()
    if not model:
        return None
    _ensure_intelligence_path()
    try:
        from app.llm import LLMClient
        from app.model_router import ModelRouter
        from app.models import ModelTier

        router = ModelRouter.from_settings(
            type("S", (), {"llm_model": model, "llm_providers": "", "llm_base_url": os.environ.get("PROVENA_EVAL_JUDGE_BASE_URL", "http://localhost:11434/v1"), "llm_api_key": os.environ.get("PROVENA_EVAL_JUDGE_API_KEY", "")})()
        )
        client = LLMClient(router)
        if not client.enabled:
            return None
        payload = await client.chat_json(
            task="eval_judge",
            tier=ModelTier.BALANCED,
            system="Score task success from 0.0 to 1.0. Reply JSON only.",
            user=(
                f"Task: {task}\nContext:\n{context}\n"
                f"Expected themes: {', '.join(expected_keywords)}\n"
                'Return {"score": 0.0}'
            ),
        )
        if isinstance(payload, dict) and isinstance(payload.get("score"), (int, float)):
            return max(0.0, min(1.0, float(payload["score"])))
    except Exception:
        return None
    return None


def run_agent_loop(store: MockStore, scope: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    dataset = _load_json(_FIXTURES / "agent_tasks.json")
    tasks = dataset.get("tasks") or []

    store.create(
        {
            "kind": "workflow",
            "scope": scope,
            "content": "Use pnpm not npm for installs in this monorepo",
            "summary": "Workflow rule: use pnpm not npm",
        }
    )
    store.create(
        {
            "kind": "mistake",
            "scope": scope,
            "content": "TypeError: cannot read property foo of undefined",
            "summary": "Do not repeat (err-ts-foo): TypeError on undefined foo",
        }
    )

    without_scores: list[float] = []
    with_scores: list[float] = []
    for task in tasks:
        prompt = str(task.get("prompt") or "")
        expected = [str(k) for k in (task.get("expected_keywords") or [])]
        brain_kw = [str(k) for k in (task.get("brain_keywords") or expected)]

        without = _heuristic_task_score("", expected)
        ctx = store.agent_context(prompt).get("context", "")
        with_brain = _heuristic_task_score(ctx, brain_kw)
        judged = asyncio.run(_llm_judge_score(ctx, prompt, brain_kw))
        if judged is not None:
            with_brain = judged

        without_scores.append(without)
        with_scores.append(with_brain)

    without_avg = round(sum(without_scores) / max(len(without_scores), 1), 4)
    with_avg = round(sum(with_scores) / max(len(with_scores), 1), 4)
    return {
        "without_brain_avg": without_avg,
        "with_brain_avg": with_avg,
        "lift_delta": round(with_avg - without_avg, 4),
        "tasks_scored": len(tasks),
        "judge": "llm" if os.environ.get("PROVENA_EVAL_JUDGE_MODEL") else "heuristic",
    }


def run_mistake_recall(store: MockStore, scope: dict[str, Any]) -> dict[str, Any]:
    mistake = store.create(
        {
            "kind": "mistake",
            "scope": scope,
            "content": "Do not use npm install; use pnpm install instead",
            "summary": "Do not repeat: used npm and broke lockfile",
        }
    )["memory"]
    results = store.search("install dependencies for this repo", kinds=["mistake", "workflow"])
    top_kind = results[0]["memory"]["kind"] if results else ""
    top_id = results[0]["memory"]["memory_id"] if results else ""
    recalled_first = top_id == mistake["memory_id"]
    return {
        "mistake_recalled_first": recalled_first,
        "top_kind": top_kind,
        "top_memory_id": top_id,
        "pass": recalled_first and top_kind == "mistake",
    }


def run_stale_fact_suppression(store: MockStore, scope: dict[str, Any]) -> dict[str, Any]:
    old = store.create(
        {
            "kind": "fact",
            "scope": scope,
            "content": "The primary database is MySQL",
            "summary": "Database is MySQL",
        }
    )["memory"]
    store.create(
        {
            "kind": "fact",
            "scope": scope,
            "content": "The primary database is PostgreSQL",
            "summary": "Database is PostgreSQL",
            "supersedes_memory_id": old["memory_id"],
        }
    )
    results = store.search("primary database", kinds=["fact"])
    active_ids = [r["memory"]["memory_id"] for r in results]
    stale_present = old["memory_id"] in active_ids
    return {
        "stale_excluded": not stale_present,
        "active_results": len(active_ids),
        "pass": not stale_present and len(active_ids) >= 1,
    }


def run_secret_not_stored(store: MockStore, scope: dict[str, Any]) -> dict[str, Any]:
    raw = "Deploy key sk-EVALTEST-TOKEN and token ghp_EVALTESTTOKEN"
    stored = store.create(
        {
            "kind": "fact",
            "scope": scope,
            "content": raw,
            "summary": "CI deploy credentials",
        }
    )["memory"]
    content = stored["content"]
    leaked = "sk-" in content or "ghp_" in content
    return {
        "redacted": not leaked,
        "stored_excerpt": content[:80],
        "pass": not leaked,
    }


def run_cold_start(store: MockStore, scope: dict[str, Any]) -> dict[str, Any]:
    _ensure_intelligence_path()
    try:
        from eval.cold_start_eval import evaluate_cold_start
    except ImportError:
        return {"skipped": True, "reason": "intelligence eval module unavailable"}

    store.create({"kind": "decision", "scope": scope, "content": "Use PostgreSQL", "summary": "Decision: PostgreSQL", "entity_keys": ["PostgreSQL"]})
    store.create({"kind": "fact", "scope": scope, "content": "Team uses Rust for systems", "summary": "Rust systems", "entity_keys": ["Rust"]})
    overview = {
        "key_entities": ["PostgreSQL", "Rust"],
        "recent_decisions": ["Decision: PostgreSQL"],
        "summary": "PostgreSQL and Rust are core stack choices.",
    }
    metrics = evaluate_cold_start(overview, ["PostgreSQL", "Rust"], ["Decision: PostgreSQL"])
    return {"metrics": metrics, "skipped": False}


def run_scorecard(*, output_path: Path | None = None) -> dict[str, Any]:
    start = time.monotonic()
    scope = {
        "tenant_id": os.environ.get("PROVENA_TENANT_ID", "eval"),
        "project_id": os.environ.get("PROVENA_PROJECT_ID", "provena"),
    }

    code_store = MockStore()
    agent_store = MockStore()
    mistake_store = MockStore()
    stale_store = MockStore()
    secret_store = MockStore()

    scorecard: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "benchmarks": {
            "code_recall": run_code_recall(code_store, scope),
            "agent_in_the_loop": run_agent_loop(agent_store, scope),
            "mistake_recall": run_mistake_recall(mistake_store, scope),
            "stale_fact_suppression": run_stale_fact_suppression(stale_store, scope),
            "secret_not_stored": run_secret_not_stored(secret_store, scope),
            "cold_start": run_cold_start(MockStore(), scope),
        },
        "elapsed_seconds": 0.0,
    }

    agent = scorecard["benchmarks"]["agent_in_the_loop"]
    scorecard["headline"] = {
        "brain_lift_delta": agent["lift_delta"],
        "mistake_recall_pass": scorecard["benchmarks"]["mistake_recall"]["pass"],
        "code_recall_hit_rate@5": scorecard["benchmarks"]["code_recall"]["hit_rate@5"],
    }
    scorecard["elapsed_seconds"] = round(time.monotonic() - start, 3)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(scorecard, indent=2), encoding="utf-8")
    return scorecard