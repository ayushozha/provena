from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROVENA_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROVENA_ROOT.parents[1]
LOOP_ROOT = PROVENA_ROOT / "loop"
STOP_PATH = LOOP_ROOT / "STOP"
STATE_PATH = LOOP_ROOT / "prd.json"
PROGRESS_PATH = LOOP_ROOT / "progress.txt"
QA_PLAN_PATH = LOOP_ROOT / "qa_test_plan.json"
LOG_DIR = LOOP_ROOT / "logs"
WATCHDOG_LOG = LOG_DIR / "watchdog.log"
DAEMON_STDOUT = LOG_DIR / "daemon.stdout.log"
DAEMON_STDERR = LOG_DIR / "daemon.stderr.log"
LOOP_SCRIPT = PROVENA_ROOT / "scripts" / "provena_loop.py"
LOOP_ARGS = [
    str(LOOP_SCRIPT),
    "--max-attempts",
    "5",
    "--daemon-stdout",
    "loop/logs/daemon.stdout.log",
    "--daemon-stderr",
    "loop/logs/daemon.stderr.log",
]
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
PHASE_STARTED_AT_KEYS = {
    "pmPrdStatus": ("PM-PRD", "lastPmPrdStartedAt"),
    "testerPlanStatus": ("TESTER-PLAN", "lastTesterPlanStartedAt"),
    "engineerStatus": ("ENGINEER", "lastEngineerStartedAt"),
    "pmReviewStatus": ("PM-REVIEW", "lastPmReviewStartedAt"),
    "testerExecutionStatus": ("TESTER-EXEC", "lastTesterExecutionStartedAt"),
}
PHASE_RECENT_START_GRACE_MINUTES = 10
PHASE_NO_CHILD_STUCK_MINUTES = 15
PHASE_SOFT_STUCK_MINUTES = 45
PHASE_HARD_STUCK_MINUTES = 120
CPU_IDLE_SAMPLE_SECONDS = 5
LOW_ACTIVITY_THRESHOLD_SECONDS = 0.02


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor and restart the Provena loop when needed.")
    parser.add_argument("--status-only", action="store_true", help="Inspect the loop and print health without restarting it.")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")


def log(message: str) -> None:
    ensure_file(WATCHDOG_LOG)
    with WATCHDOG_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{utc_now()}] {message}\n")


def powershell(command: str) -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"powershell failed: {command}")
    return result.stdout


def parse_json_output(output: str) -> list[dict[str, Any]]:
    payload = output.strip()
    if not payload:
        return []
    parsed = json.loads(payload)
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return []


def list_processes() -> list[dict[str, Any]]:
    command = (
        "$procs = Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name, CommandLine, CreationDate; "
        "$procs | ConvertTo-Json -Compress"
    )
    return parse_json_output(powershell(command))


def is_loop_process(process: dict[str, Any]) -> bool:
    name = str(process.get("Name", "")).lower()
    command_line = str(process.get("CommandLine", ""))
    return name in {"python.exe", "pythonw.exe"} and "scripts/provena_loop.py" in command_line


def list_loop_pids(processes: list[dict[str, Any]]) -> list[int]:
    return sorted(
        int(process.get("ProcessId"))
        for process in processes
        if is_loop_process(process) and str(process.get("ProcessId", "")).isdigit()
    )


def build_parent_map(processes: list[dict[str, Any]]) -> dict[int, list[int]]:
    parent_map: dict[int, list[int]] = {}
    for process in processes:
        pid = process.get("ProcessId")
        parent_pid = process.get("ParentProcessId")
        if not isinstance(pid, int) or not isinstance(parent_pid, int):
            continue
        parent_map.setdefault(parent_pid, []).append(pid)
    return parent_map


def descendant_pids(processes: list[dict[str, Any]], root_pids: list[int]) -> list[int]:
    parent_map = build_parent_map(processes)
    descendants: list[int] = []
    queue = list(root_pids)
    seen = set(root_pids)
    while queue:
        current = queue.pop(0)
        for child_pid in parent_map.get(current, []):
            if child_pid in seen:
                continue
            seen.add(child_pid)
            descendants.append(child_pid)
            queue.append(child_pid)
    return descendants


def list_orphan_codex_pids(processes: list[dict[str, Any]], loop_pids: list[int]) -> list[int]:
    loop_tree = set(loop_pids) | set(descendant_pids(processes, loop_pids))
    orphan_pids: list[int] = []
    for process in processes:
        pid = process.get("ProcessId")
        command_line = str(process.get("CommandLine", ""))
        if not isinstance(pid, int):
            continue
        if "codex.cmd exec -C C:\\Users\\ayush\\Desktop\\YC\\altrixy" not in command_line:
            continue
        if pid not in loop_tree:
            orphan_pids.append(pid)
    return sorted(orphan_pids)


def process_name(processes: list[dict[str, Any]], pid: int) -> str:
    for process in processes:
        if process.get("ProcessId") == pid:
            return str(process.get("Name", "unknown"))
    return "unknown"


def load_state() -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def parse_timestamp(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def find_active_phase(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    stories = state.get("userStories")
    if not isinstance(stories, list):
        return None
    active: list[dict[str, Any]] = []
    for story in stories:
        if not isinstance(story, dict):
            continue
        for status_key, (phase_label, started_key) in PHASE_STARTED_AT_KEYS.items():
            if story.get(status_key) != "in_progress":
                continue
            started_at = parse_timestamp(story.get(started_key))
            active.append(
                {
                    "story_id": story.get("id", "unknown"),
                    "story_title": story.get("title", "unknown"),
                    "phase_label": phase_label,
                    "status_key": status_key,
                    "started_at": started_at,
                }
            )
    if not active:
        return None
    active.sort(
        key=lambda item: (
            item["started_at"] or datetime.min.replace(tzinfo=UTC),
            str(item["story_id"]),
        )
    )
    return active[-1]


def minutes_since(moment: datetime | None) -> float | None:
    if moment is None:
        return None
    return (datetime.now(UTC) - moment).total_seconds() / 60.0


def get_cpu_seconds_map(pids: list[int]) -> dict[int, float]:
    if not pids:
        return {}
    pid_list = ",".join(str(pid) for pid in sorted(set(pids)))
    command = (
        f"$procs = Get-Process -Id {pid_list} -ErrorAction SilentlyContinue | Select-Object Id, CPU; "
        "$procs | ConvertTo-Json -Compress"
    )
    cpu_map: dict[int, float] = {}
    for entry in parse_json_output(powershell(command)):
        pid = entry.get("Id")
        cpu = entry.get("CPU")
        if isinstance(pid, int):
            cpu_map[pid] = float(cpu or 0.0)
    return cpu_map


def cpu_delta(pid_tree: list[int], sample_seconds: int = CPU_IDLE_SAMPLE_SECONDS) -> float:
    if not pid_tree:
        return 0.0
    before = get_cpu_seconds_map(pid_tree)
    time.sleep(sample_seconds)
    after = get_cpu_seconds_map(pid_tree)
    total = 0.0
    for pid in set(before) | set(after):
        total += max(0.0, after.get(pid, before.get(pid, 0.0)) - before.get(pid, 0.0))
    return total


def latest_activity_timestamp() -> datetime | None:
    candidates: list[datetime] = []
    for path in (STATE_PATH, PROGRESS_PATH, QA_PLAN_PATH, DAEMON_STDOUT, DAEMON_STDERR):
        if path.exists():
            candidates.append(datetime.fromtimestamp(path.stat().st_mtime, UTC))
    if LOG_DIR.exists():
        for path in LOG_DIR.glob("iteration-*-last-message.txt"):
            candidates.append(datetime.fromtimestamp(path.stat().st_mtime, UTC))
        for path in LOG_DIR.glob("iteration-*.jsonl"):
            candidates.append(datetime.fromtimestamp(path.stat().st_mtime, UTC))
    if not candidates:
        return None
    return max(candidates)


def summarize_pids(processes: list[dict[str, Any]], pids: list[int]) -> str:
    if not pids:
        return "none"
    return ", ".join(f"{pid}:{process_name(processes, pid)}" for pid in sorted(set(pids)))


def assess_loop_health(processes: list[dict[str, Any]]) -> tuple[str, str, list[int], list[int]]:
    loop_pids = list_loop_pids(processes)
    loop_descendants = descendant_pids(processes, loop_pids)
    state = load_state()
    active_phase = find_active_phase(state)
    activity_age_minutes = minutes_since(latest_activity_timestamp())

    if not loop_pids:
        return "dead", "No Provena loop process is running.", loop_pids, []

    if active_phase is None:
        return "healthy", "Loop is alive and no story phase is currently in progress.", loop_pids, loop_descendants

    active_phase_age = minutes_since(active_phase.get("started_at"))
    if not loop_descendants:
        if active_phase_age is None or active_phase_age >= PHASE_NO_CHILD_STUCK_MINUTES:
            return (
                "stuck",
                f"{active_phase['story_id']} is in {active_phase['phase_label']} but no Codex child process exists.",
                loop_pids,
                loop_descendants,
            )
        return (
            "healthy",
            f"{active_phase['story_id']} entered {active_phase['phase_label']} recently; waiting for the child process to appear.",
            loop_pids,
            loop_descendants,
        )

    if active_phase_age is not None and active_phase_age >= PHASE_HARD_STUCK_MINUTES:
        return (
            "stuck",
            f"{active_phase['story_id']} has been in {active_phase['phase_label']} for {active_phase_age:.1f} minutes (hard timeout).",
            loop_pids,
            loop_descendants,
        )

    if active_phase_age is not None and active_phase_age < PHASE_RECENT_START_GRACE_MINUTES:
        return (
            "healthy",
            f"{active_phase['story_id']} started {active_phase['phase_label']} {active_phase_age:.1f} minutes ago; within grace window.",
            loop_pids,
            loop_descendants,
        )

    if active_phase_age is not None and active_phase_age >= PHASE_SOFT_STUCK_MINUTES:
        total_cpu = cpu_delta(loop_descendants)
        if total_cpu <= LOW_ACTIVITY_THRESHOLD_SECONDS:
            extra = ""
            if activity_age_minutes is not None:
                extra = f" Latest loop activity was {activity_age_minutes:.1f} minutes ago."
            return (
                "stuck",
                f"{active_phase['story_id']} has been in {active_phase['phase_label']} for {active_phase_age:.1f} minutes and the loop child tree is CPU-idle.{extra}",
                loop_pids,
                loop_descendants,
            )
        return (
            "healthy",
            f"{active_phase['story_id']} has been in {active_phase['phase_label']} for {active_phase_age:.1f} minutes, but the child tree is still consuming CPU.",
            loop_pids,
            loop_descendants,
        )

    return (
        "healthy",
        f"{active_phase['story_id']} is actively running {active_phase['phase_label']} with child tree {summarize_pids(processes, loop_descendants)}.",
        loop_pids,
        loop_descendants,
    )


def kill_process(pid: int) -> None:
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def kill_process_tree(processes: list[dict[str, Any]], root_pids: list[int]) -> list[int]:
    descendants = descendant_pids(processes, root_pids)
    killed: list[int] = []
    for pid in sorted(descendants, reverse=True):
        kill_process(pid)
        killed.append(pid)
    for pid in sorted(set(root_pids), reverse=True):
        kill_process(pid)
        killed.append(pid)
    return killed


def start_loop() -> int:
    ensure_file(DAEMON_STDOUT)
    ensure_file(DAEMON_STDERR)
    pythonw = PYTHONW if PYTHONW.exists() else Path(sys.executable)
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    process = subprocess.Popen(
        [str(pythonw), *LOOP_ARGS],
        cwd=PROVENA_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    return process.pid


def main() -> int:
    args = parse_args()
    if STOP_PATH.exists():
        log(f"STOP file present at {STOP_PATH}; watchdog will not restart the loop.")
        return 0

    processes = list_processes()
    health, reason, loop_pids, loop_descendants = assess_loop_health(processes)

    if args.status_only:
        print(f"[{health}] {reason}")
        if loop_pids:
            print(f"loop_pids={','.join(str(pid) for pid in loop_pids)}")
        if loop_descendants:
            print(f"loop_descendants={','.join(str(pid) for pid in loop_descendants)}")
        return 0

    if health == "healthy":
        log(reason)
        return 0

    if health == "stuck" and loop_pids:
        killed = kill_process_tree(processes, loop_pids)
        log(
            f"Detected stuck Provena loop. {reason} Killed process tree: {', '.join(str(pid) for pid in killed)}."
        )
        time.sleep(2)
        processes = list_processes()
        loop_pids = list_loop_pids(processes)
        if loop_pids:
            log(f"Loop process tree still present after stuck recovery attempt: {', '.join(str(pid) for pid in loop_pids)}.")
            return 1

    orphan_codex = list_orphan_codex_pids(processes, loop_pids)
    if orphan_codex:
        for pid in orphan_codex:
            kill_process(pid)
        log(f"Killed orphaned Codex child process(es): {', '.join(str(pid) for pid in orphan_codex)}.")

    pid = start_loop()
    log(f"Restarted Provena loop with PID {pid}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
