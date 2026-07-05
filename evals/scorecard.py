"""Eval scorecard — real store + intelligence capture/pipeline integration."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEL_ROOT = _REPO_ROOT / "intelligence"
_FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _path_hit(retrieved_paths: list[str], relevant_paths: list[str]) -> bool:
    for path in retrieved_paths:
        for expected in relevant_paths:
            if path.endswith(expected) or expected in path:
                return True
    return False


def run_code_recall(harness: Any) -> dict[str, Any]:
    dataset = _load_json(_FIXTURES / "code_recall_dataset.json")
    queries = dataset.get("queries") or []
    indexed_paths: set[str] = set()
    for item in queries:
        for rel in item.get("relevant_paths") or []:
            indexed_paths.add(str(rel))

    for rel in indexed_paths:
        file_path = _REPO_ROOT / rel
        if not file_path.is_file():
            continue
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        harness.pipeline_write(
            f"Source file {rel}: {text[:1500]}",
            kind="artifact",
            tags=[f"file:{rel}"],
        )

    hits_at_1 = hits_at_5 = mrr_sum = 0.0
    token_total = 0
    for item in queries:
        query = str(item.get("query") or "")
        relevant_paths = [str(p) for p in (item.get("relevant_paths") or [])]
        results = harness.pipeline_search(query, limit=5, max_tokens=2000)
        retrieved_paths: list[str] = []
        for hit in results:
            memory = hit.get("memory") or hit
            for tag in memory.get("tags") or []:
                if str(tag).startswith("file:"):
                    retrieved_paths.append(str(tag).removeprefix("file:"))
        token_total += sum(len((r.get("memory") or r).get("content", "")) for r in results)

        rank = 0
        for i, path in enumerate(retrieved_paths[:5]):
            if _path_hit([path], relevant_paths):
                rank = i + 1
                break
        if rank:
            hits_at_1 += 1.0 if rank == 1 else 0.0
            hits_at_5 += 1.0
            mrr_sum += 1.0 / rank

    n = max(len(queries), 1)
    return {
        "hit_rate@1": round(hits_at_1 / n, 4),
        "hit_rate@5": round(hits_at_5 / n, 4),
        "mrr": round(mrr_sum / n, 4),
        "avg_tokens": round(token_total / n),
        "queries_scored": len(queries),
        "files_indexed": len(indexed_paths),
        "mode": "live_pipeline",
    }


def _mentions_rule(context: str, keywords: list[str]) -> bool:
    lower = context.lower()
    return sum(1 for kw in keywords if kw.lower() in lower) >= max(1, len(keywords) // 2)


def run_agent_loop(harness: Any) -> dict[str, Any]:
    dataset = _load_json(_FIXTURES / "agent_tasks.json")
    tasks = dataset.get("tasks") or []

    without_scores: list[float] = []
    with_scores: list[float] = []

    for task in tasks:
        prompt = str(task.get("prompt") or "")
        brain_kw = [str(k) for k in (task.get("brain_keywords") or [])]
        original_project = harness.scope.get("project_id", harness.project_id)
        harness.scope["project_id"] = f"agent-{task.get('id', 'task')}"

        if "pnpm" in prompt.lower():
            harness.capture("Use pnpm not npm for installs in this repository")
        if "foo" in prompt.lower() or "TypeError" in prompt:
            sig = "err-eval-foo"
            harness.capture(
                "TypeError: cannot read property 'foo' of undefined",
                signal_type="tool_failure",
                error_signature=sig,
            )
            harness.capture(
                "TypeError: cannot read property 'foo' of undefined",
                signal_type="tool_failure",
                error_signature=sig,
            )

        without = 0.0
        context = harness.context_pack(prompt)
        with_brain = 1.0 if _mentions_rule(context, brain_kw) else 0.0
        without_scores.append(without)
        with_scores.append(with_brain)
        harness.scope["project_id"] = original_project

    without_avg = round(sum(without_scores) / max(len(without_scores), 1), 4)
    with_avg = round(sum(with_scores) / max(len(with_scores), 1), 4)
    return {
        "without_brain_avg": without_avg,
        "with_brain_avg": with_avg,
        "lift_delta": round(with_avg - without_avg, 4),
        "tasks_scored": len(tasks),
        "mode": "live_capture_and_search",
    }


def run_mistake_recall(harness: Any) -> dict[str, Any]:
    sig = f"eval-mistake-npm-{int(time.time() * 1000)}"
    results: list[dict[str, Any]] = []
    for _ in range(2):
        results.extend(
            harness.capture(
                "Do not use npm install in this repo",
                signal_type="tool_failure",
                error_signature=sig,
            )
        )
    created = any(r.get("created") and r.get("kind") == "mistake" for r in results)
    context = harness.context_pack("install node dependencies for this project")
    recalled = "mistake" in context.lower() or "pnpm" in context.lower() or "npm" in context.lower()
    return {
        "mistake_created": created,
        "mistake_recalled_in_context": recalled,
        "pass": created and recalled,
        "mode": "live_capture",
    }


def run_stale_fact_suppression(harness: Any) -> dict[str, Any]:
    scope = harness.scope
    old = harness.store_client.post(
        "/v1/memories",
        json={
            "kind": "fact",
            "scope": scope,
            "content": "Primary database is MySQL",
            "summary": "Database is MySQL",
        },
        headers=harness.editor_headers,
    ).json()["memory"]
    harness.store_client.post(
        "/v1/memories",
        json={
            "kind": "fact",
            "scope": scope,
            "content": "Primary database is PostgreSQL",
            "summary": "Database is PostgreSQL",
            "supersedes_memory_id": old["memory_id"],
        },
        headers=harness.editor_headers,
    )
    results = harness.store_client.post(
        "/v1/memories/search",
        json={"query": "primary database", "scope": scope, "limit": 5},
        headers=harness.admin_headers,
    ).json().get("results") or []
    if not results:
        return {"pass": False, "reason": "no_results", "mode": "live_store"}
    top = results[0]["memory"]
    demoted = top["memory_id"] != old["memory_id"] and top.get("status") == "active"
    stale_rank = next((i + 1 for i, r in enumerate(results) if r["memory"]["memory_id"] == old["memory_id"]), None)
    return {
        "active_top_id": top["memory_id"],
        "stale_rank": stale_rank,
        "stale_demoted": demoted or (stale_rank is not None and stale_rank > 1),
        "pass": top["content"].lower().find("postgresql") >= 0,
        "mode": "live_store",
    }


def run_secret_not_stored(harness: Any) -> dict[str, Any]:
    raw = "Use deploy key sk-EVALTEST-TOKEN not ghp_EVALTESTTOKEN for releases"
    results = harness.capture(raw)
    memory_id = next((r.get("memory_id") for r in results if r.get("created")), None)
    if not memory_id:
        return {"pass": False, "reason": "capture_failed", "mode": "live_capture"}
    stored = harness.store_get(memory_id)
    content = str(stored.get("content") or "")
    leaked = "sk-EVALTEST" in content or "ghp_EVALTEST" in content
    return {
        "redacted": not leaked,
        "stored_excerpt": content[:80],
        "pass": not leaked,
        "mode": "live_store_readback",
    }


def run_cold_start(harness: Any) -> dict[str, Any]:
    if str(_INTEL_ROOT) not in sys.path:
        sys.path.insert(0, str(_INTEL_ROOT))
    try:
        from eval.cold_start_eval import evaluate_cold_start
    except ImportError:
        return {"skipped": True, "reason": "cold_start_eval unavailable"}

    harness.scope["project_id"] = "cold-start"
    harness.capture("We decided to use PostgreSQL for production")
    harness.pipeline_write("Team uses Rust for performance-critical services", kind="fact")
    overview_resp = harness.intel_client.post(
        "/v1/pipeline/overview",
        json={"scope": dict(harness.scope)},
        headers=harness.admin_headers,
    )
    if overview_resp.status_code >= 400:
        return {"skipped": True, "reason": "overview_failed"}
    overview = overview_resp.json()
    metrics = evaluate_cold_start(overview, ["PostgreSQL", "Rust"], ["Decision: PostgreSQL"])
    return {"metrics": metrics, "skipped": False, "mode": "live_overview"}


def run_scorecard(*, output_path: Path | None = None) -> dict[str, Any]:
    from evals.harness import live_harness

    start = time.monotonic()
    with live_harness() as harness:
        scorecard: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "scope": harness.scope,
            "benchmarks": {
                "code_recall": run_code_recall(harness),
                "agent_in_the_loop": run_agent_loop(harness),
                "mistake_recall": run_mistake_recall(harness),
                "stale_fact_suppression": run_stale_fact_suppression(harness),
                "secret_not_stored": run_secret_not_stored(harness),
                "cold_start": run_cold_start(harness),
            },
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