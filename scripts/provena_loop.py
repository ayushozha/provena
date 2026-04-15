from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROVENA_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROVENA_ROOT.parents[1]
LOOP_ROOT = PROVENA_ROOT / "loop"
DEFAULT_STATE_PATH = LOOP_ROOT / "prd.json"
DEFAULT_PROGRESS_PATH = LOOP_ROOT / "progress.txt"
DEFAULT_TEMPLATE_PATH = LOOP_ROOT / "CODEX_PROMPT.md"
DEFAULT_QA_TEMPLATE_PATH = LOOP_ROOT / "QA_PROMPT.md"
DEFAULT_PM_CREATION_TEMPLATE_PATH = LOOP_ROOT / "PM_CREATION_PROMPT.md"
DEFAULT_PM_REVIEW_TEMPLATE_PATH = LOOP_ROOT / "PM_REVIEW_PROMPT.md"
DEFAULT_TESTER_EXECUTION_TEMPLATE_PATH = LOOP_ROOT / "TESTER_EXECUTION_PROMPT.md"
DEFAULT_QA_PLAN_PATH = LOOP_ROOT / "qa_test_plan.json"
DEFAULT_STOP_PATH = LOOP_ROOT / "STOP"
DEFAULT_LOG_DIR = LOOP_ROOT / "logs"
DEFAULT_DAEMON_STDOUT_LOG = DEFAULT_LOG_DIR / "daemon.stdout.log"
DEFAULT_DAEMON_STDERR_LOG = DEFAULT_LOG_DIR / "daemon.stderr.log"

VALID_STORY_STATUS = {"pending", "in_progress", "passed", "blocked"}
VALID_PM_PRD_STATUS = {"pending", "in_progress", "complete", "blocked"}
VALID_TESTER_PLAN_STATUS = {"pending", "in_progress", "complete", "blocked"}
VALID_ENGINEER_STATUS = {"pending", "in_progress", "complete", "fix_pending", "blocked"}
VALID_PM_REVIEW_STATUS = {
    "pending",
    "in_progress",
    "approved",
    "changes_requested",
    "revalidate_pending",
    "blocked",
}
VALID_TESTER_EXECUTION_STATUS = {
    "pending",
    "in_progress",
    "green",
    "red",
    "retest_pending",
    "blocked",
}

PHASE_LABELS = {
    "pm_prd": "PM-PRD",
    "tester_plan": "TESTER-PLAN",
    "engineer": "ENGINEER",
    "pm_review": "PM-REVIEW",
    "tester_execution": "TESTER-EXEC",
}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class StorySelection:
    index: int
    story: dict[str, Any]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def ensure_file(path: Path, default_content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(default_content, encoding="utf-8")


def append_log(path: Path | None, message: str) -> None:
    if path is None:
        return
    ensure_file(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")


def emit(message: str, stdout_path: Path | None = None, stderr_path: Path | None = None, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    try:
        print(message, file=stream, flush=True)
    except Exception:
        pass
    append_log(stderr_path if error else stdout_path, message)


def log_trace(path: Path | None) -> None:
    append_log(path, traceback.format_exc().rstrip())


def sentry_enabled() -> bool:
    return bool(
        any(
            key in os.environ
            for key in ("SENTRY_AUTH_TOKEN", "SENTRY_DSN", "SENTRY_ORG", "SENTRY_PROJECT", "SENTRY_BASE_URL")
        )
    )


def phase_reset_status(phase: str, story: dict[str, Any]) -> str:
    if phase == "engineer" and (
        story.get("pmReviewStatus") == "changes_requested" or story.get("testerExecutionStatus") == "red"
    ):
        return "fix_pending"
    return "pending"


def story_required_fields() -> tuple[str, ...]:
    return (
        "id",
        "title",
        "priority",
        "status",
        "passes",
        "epicId",
        "epicTitle",
        "featureId",
        "featureTitle",
        "persona",
        "userStory",
        "problemStatement",
        "featureDescription",
        "pmPrdStatus",
        "testerPlanStatus",
        "engineerStatus",
        "pmReviewStatus",
        "testerExecutionStatus",
    )


def validate_state(state: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(state, dict):
        return ["State root must be an object."]
    if not isinstance(state.get("userStories"), list):
        errors.append("State must contain a 'userStories' array.")
        return errors

    seen_ids: set[str] = set()
    for index, story in enumerate(state["userStories"]):
        prefix = f"userStories[{index}]"
        if not isinstance(story, dict):
            errors.append(f"{prefix} must be an object.")
            continue
        for field in story_required_fields():
            if field not in story:
                errors.append(f"{prefix} missing required field '{field}'.")
        story_id = story.get("id")
        if isinstance(story_id, str):
            if story_id in seen_ids:
                errors.append(f"Duplicate story id '{story_id}'.")
            seen_ids.add(story_id)
        if story.get("status") not in VALID_STORY_STATUS:
            errors.append(f"{prefix} has invalid status '{story.get('status')}'.")
        if story.get("pmPrdStatus") not in VALID_PM_PRD_STATUS:
            errors.append(f"{prefix} has invalid pmPrdStatus '{story.get('pmPrdStatus')}'.")
        if story.get("testerPlanStatus") not in VALID_TESTER_PLAN_STATUS:
            errors.append(f"{prefix} has invalid testerPlanStatus '{story.get('testerPlanStatus')}'.")
        if story.get("engineerStatus") not in VALID_ENGINEER_STATUS:
            errors.append(f"{prefix} has invalid engineerStatus '{story.get('engineerStatus')}'.")
        if story.get("pmReviewStatus") not in VALID_PM_REVIEW_STATUS:
            errors.append(f"{prefix} has invalid pmReviewStatus '{story.get('pmReviewStatus')}'.")
        if story.get("testerExecutionStatus") not in VALID_TESTER_EXECUTION_STATUS:
            errors.append(f"{prefix} has invalid testerExecutionStatus '{story.get('testerExecutionStatus')}'.")
        if not isinstance(story.get("passes"), bool):
            errors.append(f"{prefix} field 'passes' must be boolean.")
        if "verificationCommands" in story and not isinstance(story["verificationCommands"], list):
            errors.append(f"{prefix} field 'verificationCommands' must be a list.")
        if "acceptanceCriteria" in story and not isinstance(story["acceptanceCriteria"], list):
            errors.append(f"{prefix} field 'acceptanceCriteria' must be a list.")
        if "expectedBehaviors" in story and not isinstance(story["expectedBehaviors"], list):
            errors.append(f"{prefix} field 'expectedBehaviors' must be a list.")
        if "uiTestFocus" in story and not isinstance(story["uiTestFocus"], list):
            errors.append(f"{prefix} field 'uiTestFocus' must be a list.")
        if "sentryChecks" in story and not isinstance(story["sentryChecks"], list):
            errors.append(f"{prefix} field 'sentryChecks' must be a list.")
        if "qaArtifacts" in story and not isinstance(story["qaArtifacts"], list):
            errors.append(f"{prefix} field 'qaArtifacts' must be a list.")
        if "fixRecommendations" in story and not isinstance(story["fixRecommendations"], list):
            errors.append(f"{prefix} field 'fixRecommendations' must be a list.")
    return errors


def normalize_story_state(state: dict[str, Any]) -> dict[str, Any]:
    changed = False
    for story in state.get("userStories", []):
        story.setdefault("attempts", 0)
        story.setdefault("pmPrdAttempts", 0)
        story.setdefault("testerPlanAttempts", 0)
        story.setdefault("engineerAttempts", 0)
        story.setdefault("pmReviewAttempts", 0)
        story.setdefault("testerExecutionAttempts", 0)
        story.setdefault("notes", "")
        story.setdefault("pmPrdNotes", "")
        story.setdefault("testerPlanNotes", "")
        story.setdefault("pmReviewNotes", "")
        story.setdefault("testerExecutionNotes", "")
        story.setdefault("verificationCommands", [])
        story.setdefault("acceptanceCriteria", [])
        story.setdefault("expectedBehaviors", [])
        story.setdefault("uiTestFocus", [])
        story.setdefault("sentryChecks", [])
        story.setdefault("pmPrdStatus", "complete")
        story.setdefault("testerPlanStatus", "pending")
        story.setdefault("engineerStatus", "pending")
        story.setdefault("pmReviewStatus", "pending")
        story.setdefault("testerExecutionStatus", "pending")
        story.setdefault("qaArtifacts", [])
        story.setdefault("qaCoverage", {"api": True, "integration": True, "ui": False})
        story.setdefault("fixRecommendations", [])

        if story.get("pmReviewStatus") == "changes_requested" and story.get("engineerStatus") == "complete":
            story["engineerStatus"] = "fix_pending"
            changed = True
        if story.get("testerExecutionStatus") == "red" and story.get("engineerStatus") == "complete":
            story["engineerStatus"] = "fix_pending"
            changed = True

        if story.get("pmReviewStatus") == "approved" and story.get("testerExecutionStatus") == "green":
            if not story.get("passes"):
                story["passes"] = True
                changed = True
            if story.get("status") != "passed":
                story["status"] = "passed"
                changed = True

        if story.get("passes") and story.get("status") != "passed":
            story["status"] = "passed"
            changed = True

        if any(
            story.get(key) == "blocked"
            for key in (
                "pmPrdStatus",
                "testerPlanStatus",
                "engineerStatus",
                "pmReviewStatus",
                "testerExecutionStatus",
            )
        ):
            if story.get("status") != "blocked":
                story["status"] = "blocked"
                changed = True
            continue

        if not story.get("passes"):
            if any(
                story.get(key) == "in_progress"
                for key in (
                    "pmPrdStatus",
                    "testerPlanStatus",
                    "engineerStatus",
                    "pmReviewStatus",
                    "testerExecutionStatus",
                )
            ):
                if story.get("status") != "in_progress":
                    story["status"] = "in_progress"
                    changed = True
            elif story.get("status") != "pending":
                story["status"] = "pending"
                changed = True

    if changed:
        state["updatedAt"] = utc_now()
    return state


def sort_key(story: dict[str, Any]) -> tuple[int, str]:
    return (int(story.get("priority", 9999)), str(story.get("id", "")))


def select_current_story(state: dict[str, Any]) -> StorySelection | None:
    pending: list[tuple[int, dict[str, Any]]] = []
    for index, story in enumerate(state["userStories"]):
        if story.get("passes"):
            continue
        if story.get("status") == "blocked":
            continue
        pending.append((index, story))
    if not pending:
        return None
    index, story = sorted(pending, key=lambda item: sort_key(item[1]))[0]
    return StorySelection(index=index, story=story)


def determine_phase(story: dict[str, Any]) -> str | None:
    if story.get("passes") or story.get("status") == "passed":
        return None
    if story.get("status") == "blocked":
        return None
    if story.get("pmPrdStatus") != "complete":
        return "pm_prd"
    if story.get("testerPlanStatus") != "complete":
        return "tester_plan"
    if (
        story.get("pmReviewStatus") == "changes_requested"
        or story.get("testerExecutionStatus") == "red"
        or story.get("engineerStatus") in {"pending", "in_progress", "fix_pending"}
    ):
        return "engineer"
    if story.get("pmReviewStatus") in {"pending", "in_progress", "revalidate_pending"}:
        return "pm_review"
    if story.get("pmReviewStatus") == "approved" and story.get("testerExecutionStatus") in {
        "pending",
        "in_progress",
        "retest_pending",
    }:
        return "tester_execution"
    if story.get("pmReviewStatus") == "approved" and story.get("testerExecutionStatus") == "green":
        return None
    return None


def summarize_stories(state: dict[str, Any], limit: int = 8) -> str:
    lines: list[str] = []
    for story in sorted(state.get("userStories", []), key=sort_key)[:limit]:
        lines.append(
            f"- {story['id']} | p{story['priority']} | pm={story['pmPrdStatus']} | tester-plan={story['testerPlanStatus']} | engineer={story['engineerStatus']} | pm-review={story['pmReviewStatus']} | tester-run={story['testerExecutionStatus']} | passes={story['passes']} | {story['title']}"
        )
    return "\n".join(lines)


def tail_progress(path: Path, lines: int = 20) -> str:
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(content[-lines:])


def render_prompt(
    template_path: Path,
    selection: StorySelection,
    state: dict[str, Any],
    progress_path: Path,
    state_path: Path,
    stop_path: Path,
    qa_plan_path: Path,
    iteration: int,
) -> str:
    template = template_path.read_text(encoding="utf-8")
    story_json = json.dumps(selection.story, indent=2)
    replacements = {
        "{{ITERATION}}": str(iteration),
        "{{REPO_ROOT}}": str(REPO_ROOT),
        "{{PROVENA_ROOT}}": str(PROVENA_ROOT),
        "{{STATE_PATH}}": str(state_path),
        "{{PROGRESS_PATH}}": str(progress_path),
        "{{STOP_PATH}}": str(stop_path),
        "{{QA_PLAN_PATH}}": str(qa_plan_path),
        "{{CURRENT_STORY_JSON}}": story_json,
        "{{PENDING_SUMMARY}}": summarize_stories(state),
        "{{PROGRESS_TAIL}}": tail_progress(progress_path),
        "{{OBJECTIVE}}": str(state.get("objective", "Complete the Provena product backlog.")),
        "{{SENTRY_ENABLED}}": "true" if sentry_enabled() else "false",
    }
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace(key, value)
    return rendered


def update_story_for_phase_start(state: dict[str, Any], selection: StorySelection, phase: str) -> None:
    story = state["userStories"][selection.index]
    story["status"] = "in_progress"
    now = utc_now()
    if phase == "pm_prd":
        story["pmPrdStatus"] = "in_progress"
        story["pmPrdAttempts"] = int(story.get("pmPrdAttempts", 0)) + 1
        story["lastPmPrdStartedAt"] = now
    elif phase == "tester_plan":
        story["testerPlanStatus"] = "in_progress"
        story["testerPlanAttempts"] = int(story.get("testerPlanAttempts", 0)) + 1
        story["lastTesterPlanStartedAt"] = now
    elif phase == "engineer":
        story["engineerStatus"] = "in_progress"
        story["engineerAttempts"] = int(story.get("engineerAttempts", 0)) + 1
        story["attempts"] = int(story.get("attempts", 0)) + 1
        story["lastEngineerStartedAt"] = now
    elif phase == "pm_review":
        story["pmReviewStatus"] = "in_progress"
        story["pmReviewAttempts"] = int(story.get("pmReviewAttempts", 0)) + 1
        story["lastPmReviewStartedAt"] = now
    elif phase == "tester_execution":
        story["testerExecutionStatus"] = "in_progress"
        story["testerExecutionAttempts"] = int(story.get("testerExecutionAttempts", 0)) + 1
        story["lastTesterExecutionStartedAt"] = now
    state["updatedAt"] = now


def recover_in_progress(state: dict[str, Any]) -> None:
    changed = False
    for story in state.get("userStories", []):
        if story.get("pmPrdStatus") == "in_progress":
            story["pmPrdStatus"] = "pending"
            story["pmPrdNotes"] = (
                (story.get("pmPrdNotes", "").strip() + "\nRecovered from interrupted PM PRD iteration.")
                .strip()
            )
            changed = True
        if story.get("testerPlanStatus") == "in_progress":
            story["testerPlanStatus"] = "pending"
            story["testerPlanNotes"] = (
                (story.get("testerPlanNotes", "").strip() + "\nRecovered from interrupted tester planning iteration.")
                .strip()
            )
            changed = True
        if story.get("engineerStatus") == "in_progress":
            story["engineerStatus"] = phase_reset_status("engineer", story)
            story["notes"] = (
                (story.get("notes", "").strip() + "\nRecovered from interrupted engineer iteration.").strip()
            )
            changed = True
        if story.get("pmReviewStatus") == "in_progress":
            story["pmReviewStatus"] = "pending"
            story["pmReviewNotes"] = (
                (story.get("pmReviewNotes", "").strip() + "\nRecovered from interrupted PM review iteration.")
                .strip()
            )
            changed = True
        if story.get("testerExecutionStatus") == "in_progress":
            story["testerExecutionStatus"] = "pending"
            story["testerExecutionNotes"] = (
                (story.get("testerExecutionNotes", "").strip() + "\nRecovered from interrupted tester execution iteration.")
                .strip()
            )
            changed = True
    if changed:
        state["updatedAt"] = utc_now()


def reset_phase_if_stuck(state: dict[str, Any], selection: StorySelection, phase: str) -> None:
    story = state["userStories"][selection.index]
    if phase == "pm_prd" and story.get("pmPrdStatus") == "in_progress":
        story["pmPrdStatus"] = "pending"
        story["pmPrdNotes"] = (
            (story.get("pmPrdNotes", "").strip() + "\nPM PRD iteration ended without a terminal state. Reset to pending.")
            .strip()
        )
    elif phase == "tester_plan" and story.get("testerPlanStatus") == "in_progress":
        story["testerPlanStatus"] = "pending"
        story["testerPlanNotes"] = (
            (story.get("testerPlanNotes", "").strip() + "\nTester planning ended without a terminal state. Reset to pending.")
            .strip()
        )
    elif phase == "engineer" and story.get("engineerStatus") == "in_progress":
        story["engineerStatus"] = phase_reset_status("engineer", story)
        story["notes"] = (
            (story.get("notes", "").strip() + "\nEngineer iteration ended without a terminal state. Reset for another engineering pass.")
            .strip()
        )
    elif phase == "pm_review" and story.get("pmReviewStatus") == "in_progress":
        story["pmReviewStatus"] = "pending"
        story["pmReviewNotes"] = (
            (story.get("pmReviewNotes", "").strip() + "\nPM review ended without approval or change request. Reset to pending.")
            .strip()
        )
    elif phase == "tester_execution" and story.get("testerExecutionStatus") == "in_progress":
        story["testerExecutionStatus"] = "pending"
        story["testerExecutionNotes"] = (
            (story.get("testerExecutionNotes", "").strip() + "\nTester execution ended without green or red. Reset to pending.")
            .strip()
        )
    state["updatedAt"] = utc_now()


def apply_phase_failure(state: dict[str, Any], selection: StorySelection, phase: str, return_code: int) -> None:
    story = state["userStories"][selection.index]
    note = f"{PHASE_LABELS[phase]} failed with exit code {return_code}. Loop reset the phase for retry."
    if phase == "pm_prd":
        story["pmPrdStatus"] = "pending"
        story["pmPrdNotes"] = (story.get("pmPrdNotes", "").strip() + "\n" + note).strip()
    elif phase == "tester_plan":
        story["testerPlanStatus"] = "pending"
        story["testerPlanNotes"] = (story.get("testerPlanNotes", "").strip() + "\n" + note).strip()
    elif phase == "engineer":
        story["engineerStatus"] = phase_reset_status("engineer", story)
        story["notes"] = (story.get("notes", "").strip() + "\n" + note).strip()
    elif phase == "pm_review":
        story["pmReviewStatus"] = "pending"
        story["pmReviewNotes"] = (story.get("pmReviewNotes", "").strip() + "\n" + note).strip()
    elif phase == "tester_execution":
        story["testerExecutionStatus"] = "pending"
        story["testerExecutionNotes"] = (story.get("testerExecutionNotes", "").strip() + "\n" + note).strip()
    story["status"] = "pending"
    state["updatedAt"] = utc_now()


def append_progress(progress_path: Path, message: str) -> None:
    ensure_file(progress_path)
    with progress_path.open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")


def all_passed(state: dict[str, Any]) -> bool:
    return all(bool(story.get("passes")) for story in state.get("userStories", []))


def resolve_codex_command() -> list[str]:
    if os.name != "nt":
        return [shutil.which("codex") or "codex"]
    codex_cmd = shutil.which("codex.cmd")
    if codex_cmd:
        return [codex_cmd]
    codex_ps1 = shutil.which("codex.ps1")
    if codex_ps1:
        return ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", codex_ps1]
    return ["codex"]


def run_codex(
    prompt: str,
    log_dir: Path,
    iteration: int,
    phase: str,
    model: str | None,
    sandbox: str,
    approval: str,
) -> int:
    log_dir.mkdir(parents=True, exist_ok=True)
    output_file = log_dir / f"iteration-{iteration:04d}-{phase}-last-message.txt"
    event_log = log_dir / f"iteration-{iteration:04d}-{phase}.jsonl"
    stdout_log = log_dir / f"iteration-{iteration:04d}-{phase}.stdout.log"
    stderr_log = log_dir / f"iteration-{iteration:04d}-{phase}.stderr.log"

    command = [
        *resolve_codex_command(),
        "exec",
        "-C",
        str(REPO_ROOT),
        "-o",
        str(output_file),
        "--json",
    ]
    if approval == "never" and sandbox == "workspace-write":
        command.append("--full-auto")
    elif approval == "never" and sandbox == "danger-full-access":
        command.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        command.extend(["--sandbox", sandbox])
    if model:
        command.extend(["-m", model])

    process = subprocess.run(
        command,
        input=prompt.encode("utf-8"),
        text=False,
        cwd=REPO_ROOT,
        capture_output=True,
    )
    stdout_text = (process.stdout or b"").decode("utf-8", errors="replace")
    stderr_text = (process.stderr or b"").decode("utf-8", errors="replace")
    stdout_log.write_text(stdout_text, encoding="utf-8")
    stderr_log.write_text(stderr_text, encoding="utf-8")
    event_log.write_text(stdout_text, encoding="utf-8")
    return process.returncode


def maybe_block_exhausted_stories(state: dict[str, Any], max_attempts: int) -> bool:
    changed = False
    for story in state.get("userStories", []):
        for status_key, attempts_key, note_key, label, terminal_states in (
            ("pmPrdStatus", "pmPrdAttempts", "pmPrdNotes", "PM PRD", {"complete"}),
            ("testerPlanStatus", "testerPlanAttempts", "testerPlanNotes", "tester planning", {"complete"}),
            ("engineerStatus", "attempts", "notes", "engineering", {"complete"}),
            ("pmReviewStatus", "pmReviewAttempts", "pmReviewNotes", "PM review", {"approved"}),
            ("testerExecutionStatus", "testerExecutionAttempts", "testerExecutionNotes", "tester execution", {"green"}),
        ):
            if story.get(status_key) == "blocked":
                continue
            if story.get(status_key) in terminal_states:
                continue
            if int(story.get(attempts_key, 0)) >= max_attempts:
                story[status_key] = "blocked"
                note = story.get(note_key, "").strip()
                addition = f"Automatically blocked after {max_attempts} {label} attempts."
                story[note_key] = f"{note}\n{addition}".strip()
                story["status"] = "blocked"
                changed = True
    if changed:
        state["updatedAt"] = utc_now()
    return changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Provena Codex loop.")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--progress", type=Path, default=DEFAULT_PROGRESS_PATH)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE_PATH)
    parser.add_argument("--qa-template", type=Path, default=DEFAULT_QA_TEMPLATE_PATH)
    parser.add_argument("--pm-creation-template", type=Path, default=DEFAULT_PM_CREATION_TEMPLATE_PATH)
    parser.add_argument("--pm-review-template", type=Path, default=DEFAULT_PM_REVIEW_TEMPLATE_PATH)
    parser.add_argument("--tester-execution-template", type=Path, default=DEFAULT_TESTER_EXECUTION_TEMPLATE_PATH)
    parser.add_argument("--qa-plan", type=Path, default=DEFAULT_QA_PLAN_PATH)
    parser.add_argument("--stop-file", type=Path, default=DEFAULT_STOP_PATH)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--daemon-stdout", type=Path, default=DEFAULT_DAEMON_STDOUT_LOG)
    parser.add_argument("--daemon-stderr", type=Path, default=DEFAULT_DAEMON_STDERR_LOG)
    parser.add_argument("--model", default=None)
    parser.add_argument("--sandbox", default="workspace-write")
    parser.add_argument("--approval", default="never")
    parser.add_argument("--idle-seconds", type=int, default=60)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-iterations", type=int, default=0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def template_for_phase(args: argparse.Namespace, phase: str) -> Path:
    template_map = {
        "pm_prd": args.pm_creation_template,
        "tester_plan": args.qa_template,
        "engineer": args.template,
        "pm_review": args.pm_review_template,
        "tester_execution": args.tester_execution_template,
    }
    return template_map[phase]


def load_prepared_state(state_path: Path) -> dict[str, Any]:
    state = normalize_story_state(load_json(state_path))
    recover_in_progress(state)
    return normalize_story_state(state)


def main() -> int:
    args = parse_args()
    ensure_file(args.progress, "# Provena loop progress\n")
    ensure_file(
        args.qa_plan,
        '{\n  "product": "Provena",\n  "updatedAt": "",\n  "epics": [],\n  "features": [],\n  "stories": []\n}\n',
    )
    ensure_file(args.daemon_stdout)
    ensure_file(args.daemon_stderr)

    try:
        state = load_prepared_state(args.state)
        errors = validate_state(state)
        if errors:
            for error in errors:
                emit(f"[state-error] {error}", args.daemon_stdout, args.daemon_stderr, error=True)
            return 1
        write_json(args.state, state)
        if args.validate_only:
            emit(f"[ok] validated {args.state}", args.daemon_stdout, args.daemon_stderr)
            return 0

        iteration = 0
        while True:
            if args.stop_file.exists():
                reason = args.stop_file.read_text(encoding="utf-8", errors="ignore").strip()
                emit(f"[stop] stop file detected at {args.stop_file}", args.daemon_stdout, args.daemon_stderr)
                if reason:
                    emit(reason, args.daemon_stdout, args.daemon_stderr)
                return 0

            state = load_prepared_state(args.state)
            maybe_block_exhausted_stories(state, args.max_attempts)
            errors = validate_state(state)
            if errors:
                for error in errors:
                    emit(f"[state-error] {error}", args.daemon_stdout, args.daemon_stderr, error=True)
                return 1
            write_json(args.state, state)

            if all_passed(state):
                emit("[idle] all current Provena stories have passed; waiting for more work.", args.daemon_stdout, args.daemon_stderr)
                if args.once:
                    return 0
                time.sleep(args.idle_seconds)
                continue

            selection = select_current_story(state)
            if selection is None:
                emit("[idle] no pending Provena story is currently runnable; waiting.", args.daemon_stdout, args.daemon_stderr)
                if args.once:
                    return 0
                time.sleep(args.idle_seconds)
                continue

            phase = determine_phase(selection.story)
            if phase is None:
                emit(
                    f"[idle] highest-priority feature {selection.story['id']} has no runnable phase; waiting.",
                    args.daemon_stdout,
                    args.daemon_stderr,
                )
                if args.once:
                    return 0
                time.sleep(args.idle_seconds)
                continue

            iteration += 1
            prompt = render_prompt(
                template_for_phase(args, phase),
                selection,
                state,
                args.progress,
                args.state,
                args.stop_file,
                args.qa_plan,
                iteration,
            )
            emit(
                f"[iteration {iteration}] {PHASE_LABELS[phase]} | {selection.story['id']} | {selection.story['title']}",
                args.daemon_stdout,
                args.daemon_stderr,
            )

            if args.dry_run:
                emit(prompt, args.daemon_stdout, args.daemon_stderr)
                return 0

            update_story_for_phase_start(state, selection, phase)
            write_json(args.state, state)

            return_code = run_codex(prompt, args.log_dir, iteration, phase, args.model, args.sandbox, args.approval)
            refreshed_state = load_prepared_state(args.state)
            if return_code != 0:
                apply_phase_failure(refreshed_state, selection, phase, return_code)
                write_json(args.state, refreshed_state)
                append_progress(
                    args.progress,
                    f"[{utc_now()}] {PHASE_LABELS[phase]} iteration {iteration} failed for {selection.story['id']} with exit code {return_code}.",
                )
            else:
                reset_phase_if_stuck(refreshed_state, selection, phase)
                write_json(args.state, refreshed_state)

            if args.once:
                return 0 if return_code == 0 else return_code
            if args.max_iterations and iteration >= args.max_iterations:
                emit(f"[stop] reached max iterations ({args.max_iterations})", args.daemon_stdout, args.daemon_stderr)
                return 0

            time.sleep(2)
    except KeyboardInterrupt:
        log_trace(args.daemon_stderr)
        emit("[stop] loop interrupted by keyboard signal.", args.daemon_stdout, args.daemon_stderr, error=True)
        return 130
    except Exception:
        log_trace(args.daemon_stderr)
        emit("[fatal] loop crashed with an unhandled exception.", args.daemon_stdout, args.daemon_stderr, error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
