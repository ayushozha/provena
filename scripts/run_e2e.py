from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import rfc8785


ROOT = Path(__file__).resolve().parents[1]
IS_WINDOWS = os.name == "nt"

STANDALONE_PORT = 8092
STANDALONE_BASE_URL = f"http://127.0.0.1:{STANDALONE_PORT}"
AUTH_GATEWAY_PORT = 18083

POLYGLOT_PORTS = {
    "store": 18000,
    "orchestration": 15051,
    "intelligence": 18081,
    "gateway": 18080,
    "queue": 18091,
    "mcp": 18090,
    "lifecycle": 18092,
}


@contextmanager
def managed_temp_dir(prefix: str) -> Iterator[Path]:
    # Avoid Python tempfile ACL issues observed on this Windows host by
    # creating ordinary workspace directories for ephemeral verification assets.
    base_dir = Path(os.environ.get("PROVENA_E2E_TMP_DIR", ROOT / ".tmp-e2e" / "runs"))
    base_dir.mkdir(parents=True, exist_ok=True)

    temp_path = base_dir / f"{prefix}-{int(time.time() * 1000)}-{os.getpid()}"
    suffix = 0
    while temp_path.exists():
        suffix += 1
        temp_path = base_dir / f"{prefix}-{int(time.time() * 1000)}-{os.getpid()}-{suffix}"
    temp_path.mkdir()

    try:
        yield temp_path
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)


def executable_name(name: str) -> str:
    return f"{name}.exe" if IS_WINDOWS else name


def normalise_command(command: list[str]) -> list[str]:
    if IS_WINDOWS and command[0] in {"npm", "npx", "pnpm"}:
        return [f"{command[0]}.cmd", *command[1:]]
    return command


def run(
    command: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = normalise_command(command)
    print(f"[run] {' '.join(command)}")
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        capture_output=capture,
    )


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[str]
    log_handle: object
    log_path: Path

    def stop(self) -> None:
        try:
            if self.process.poll() is None:
                if IS_WINDOWS:
                    # `go run` spawns the compiled service as a child process;
                    # terminating only the wrapper leaves the service listening.
                    subprocess.run(
                        ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.process.wait(timeout=5)
                    return
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        finally:
            self.log_handle.close()


def launch(
    name: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> ManagedProcess:
    log_handle = log_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            normalise_command(command),
            cwd=cwd,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception:
        log_handle.close()
        raise
    return ManagedProcess(name=name, process=process, log_handle=log_handle, log_path=log_path)


def wait_for_health(
    url: str,
    timeout_seconds: int = 30,
    headers: dict[str, str] | None = None,
) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(url, headers=headers, timeout=2.0)
            response.raise_for_status()
            if response.json().get("status") == "ok":
                return
        except Exception as error:  # noqa: BLE001
            last_error = error
            time.sleep(0.5)
    raise RuntimeError(f"service did not become healthy in time for {url}: {last_error}")


def tail(path: Path, lines: int = 60) -> str:
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(content[-lines:])


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def auth_headers(
    token: str,
    *,
    principal_id: str | None = None,
    groups: list[str] | None = None,
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if principal_id is not None:
        headers["X-Provena-Principal-Id"] = principal_id
    if groups is not None:
        headers["X-Provena-Groups"] = ",".join(groups)
    return headers


def build_go_binary(package: str, out_path: Path) -> None:
    run(["go", "build", "-o", str(out_path), package], ROOT / "control-plane")


def build_go_example(example_dir: Path, out_path: Path) -> None:
    run(["go", "build", "-o", str(out_path), "./examples/smoke"], example_dir)


def build_rust_orchestration() -> Path:
    binary = ROOT / "orchestration" / "target" / "debug" / executable_name("provena-orchestration")
    if binary.exists():
        return binary
    run(["cargo", "build"], ROOT / "orchestration")
    if not binary.exists():
        raise RuntimeError(f"missing orchestration binary: {binary}")
    return binary


def export_openapi(env: dict[str, str]) -> None:
    run([sys.executable, "scripts/export_openapi.py"], ROOT, env)


def run_python_sdk_smoke(base_url: str, env: dict[str, str]) -> None:
    python_env = env.copy()
    python_env["PROVENA_BASE_URL"] = base_url
    python_env["PYTHONPATH"] = str(ROOT / "sdk" / "python")
    run([sys.executable, "tests/smoke.py"], ROOT / "sdk" / "python", python_env)


def run_typescript_sdk_smoke(base_url: str, env: dict[str, str]) -> None:
    sdk_env = env.copy()
    sdk_env["PROVENA_BASE_URL"] = base_url
    run(["npx", "-y", "-p", "typescript", "tsc", "-p", "."], ROOT / "sdk" / "typescript", sdk_env)
    run(["node", "tests/smoke.mjs"], ROOT / "sdk" / "typescript", sdk_env)


def run_go_sdk_smoke(base_url: str, env: dict[str, str], temp_dir: Path) -> None:
    sdk_env = env.copy()
    sdk_env["PROVENA_BASE_URL"] = base_url
    if IS_WINDOWS:
        run(["go", "run", "./examples/smoke"], ROOT / "sdk" / "go", sdk_env)
        return
    binary = temp_dir / executable_name("go-sdk-smoke")
    build_go_example(ROOT / "sdk" / "go", binary)
    run([str(binary)], ROOT / "sdk" / "go", sdk_env)


def run_rust_sdk_smoke(base_url: str, env: dict[str, str]) -> None:
    sdk_env = env.copy()
    sdk_env["PROVENA_BASE_URL"] = base_url
    if IS_WINDOWS:
        try:
            run(["cargo", "check", "--example", "smoke"], ROOT / "sdk" / "rust", sdk_env, capture=True)
        except (subprocess.CalledProcessError, OSError) as error:
            output = ""
            if isinstance(error, subprocess.CalledProcessError):
                output = (error.stdout or "") + (error.stderr or "")
            else:
                output = str(error)
            if (
                "Access is denied" in output
                or "Application Control" in output
                or "not applicable to the" in output
                or "rustc.exe" in output
            ):
                print("[warn] Rust SDK smoke skipped on Windows due to local Rust toolchain policy/configuration.")
                return
            raise
        print("[ok] Rust SDK example compiled.")
        return

    run(["cargo", "run", "--example", "smoke"], ROOT / "sdk" / "rust", sdk_env)


def run_standalone_suite() -> None:
    print("[suite] standalone")
    with managed_temp_dir("standalone") as temp_path:
        db_path = temp_path / "provena-standalone.db"
        server_log = temp_path / "standalone.log"

        env = os.environ.copy()
        env["PROVENA_DB_PATH"] = str(db_path)
        env["PROVENA_ENVIRONMENT"] = "test"

        server = launch(
            "standalone",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(STANDALONE_PORT),
            ],
            ROOT,
            env,
            server_log,
        )

        try:
            wait_for_health(f"{STANDALONE_BASE_URL}/healthz")
            run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"], ROOT, env)
            run_python_sdk_smoke(STANDALONE_BASE_URL, env)
            run_typescript_sdk_smoke(STANDALONE_BASE_URL, env)
            run_go_sdk_smoke(STANDALONE_BASE_URL, env, temp_path)
            run_rust_sdk_smoke(STANDALONE_BASE_URL, env)
            export_openapi(env)
            print("[ok] standalone verification completed.")
        except Exception:  # noqa: BLE001
            print("[error] standalone verification failed. Server log tail follows:")
            print(tail(server_log))
            raise
        finally:
            server.stop()


def build_control_plane_binaries(bin_dir: Path) -> dict[str, Path]:
    binaries = {
        "gateway": bin_dir / executable_name("gateway"),
        "queue": bin_dir / executable_name("queue"),
        "mcp": bin_dir / executable_name("mcp"),
        "lifecycle": bin_dir / executable_name("lifecycle"),
    }
    for name, path in binaries.items():
        build_go_binary(f"./cmd/{name}", path)
    return binaries


def control_plane_launch_command(name: str, binary_path: Path) -> list[str]:
    if IS_WINDOWS and name in {"queue", "mcp"}:
        # Windows App Control on this host blocks launching some freshly built
        # Go executables from the workspace temp directory, while `go run`
        # remains allowed for the same commands.
        return ["go", "run", f"./cmd/{name}"]
    return [str(binary_path)]


def verify_polyglot_http(base_urls: dict[str, str], lifecycle_available: bool) -> None:
    gateway = base_urls["gateway"]
    store = base_urls["store"]
    queue = base_urls["queue"]
    mcp = base_urls["mcp"]
    governance_base = gateway if lifecycle_available else store

    headers = {"Content-Type": "application/json"}
    admin_headers = {
        **headers,
        "X-Provena-Tenant-Id": "tenant-poly",
        "X-Provena-Role": "admin",
        "X-Provena-Principal-Id": "principal-admin-1",
        "X-Provena-Groups": "admins,security",
    }

    def post_json(
        url: str,
        payload: dict[str, object],
        timeout: float = 20.0,
        retries: int = 1,
        request_headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = httpx.post(url, json=payload, headers=request_headers or headers, timeout=timeout)
                response.raise_for_status()
                return response.json()
            except httpx.ReadTimeout as error:
                last_error = error
                if attempt == retries:
                    raise
                time.sleep(0.5)
        raise RuntimeError(f"request failed for {url}: {last_error}")

    created_a = post_json(
        f"{gateway}/v1/memories",
        {
            "kind": "fact",
            "scope": {
                "tenant_id": "tenant-poly",
                "workspace_id": "ws-a",
                "project_id": "proj-main",
                "user_id": "pm-1",
            },
            "title": "Scoped dedupe proof",
            "content": "The same content in different scopes should not dedupe.",
            "entity_keys": ["scope-dedupe"],
            "tags": ["smoke"],
        },
    )
    created_b = post_json(
        f"{gateway}/v1/memories",
        {
            "kind": "fact",
            "scope": {
                "tenant_id": "tenant-poly",
                "workspace_id": "ws-b",
                "project_id": "proj-main",
                "user_id": "pm-1",
            },
            "title": "Scoped dedupe proof",
            "content": "The same content in different scopes should not dedupe.",
            "entity_keys": ["scope-dedupe"],
            "tags": ["smoke"],
        },
    )
    if not created_a["created"] or not created_b["created"]:
        raise RuntimeError("scope-aware dedupe failed in polyglot smoke")
    if created_a["memory"]["memory_id"] == created_b["memory"]["memory_id"]:
        raise RuntimeError("distinct scoped writes returned the same memory id")

    search = post_json(
        f"{gateway}/v1/memories/search",
        {
            "query": "different scopes dedupe",
            "scope": {
                "tenant_id": "tenant-poly",
                "workspace_id": "ws-a",
                "project_id": "proj-main",
                "user_id": "pm-1",
            },
            "limit": 5,
        },
    )
    result_ids = [item["memory"]["memory_id"] for item in search["results"]]
    if created_a["memory"]["memory_id"] not in result_ids:
        raise RuntimeError("polyglot search did not return the created memory")

    hold = post_json(
        f"{governance_base}/v1/admin/legal-hold",
        {
            "hold_id": "smoke-hold",
            "tenant_id": "tenant-poly",
            "scope": {"workspace_id": "ws-held", "project_id": "proj-held"},
            "reason": "smoke-test hold",
        },
        request_headers=admin_headers,
    )
    held_write = post_json(
        f"{gateway}/v1/memories",
        {
            "kind": "decision",
            "scope": {
                "tenant_id": "tenant-poly",
                "workspace_id": "ws-held",
                "project_id": "proj-held",
                "user_id": "pm-held",
            },
            "title": "Held memory",
            "content": "This memory should be born under legal hold.",
            "entity_keys": ["hold-check"],
            "tags": ["smoke"],
        },
    )
    memory_id = held_write["memory"]["memory_id"]
    held_before = httpx.get(f"{gateway}/v1/memories/{memory_id}", timeout=10.0)
    held_before.raise_for_status()
    held_before_json = held_before.json()
    if not held_before_json["held"] or held_before_json["status"] != "held":
        raise RuntimeError("legal hold was not applied to scope-matched memory")

    release = httpx.delete(
        f"{governance_base}/v1/admin/legal-hold/{hold['hold_id']}",
        params={"tenant_id": "tenant-poly"},
        headers=admin_headers,
        timeout=10.0,
    )
    release.raise_for_status()
    held_after = httpx.get(f"{gateway}/v1/memories/{memory_id}", timeout=10.0)
    held_after.raise_for_status()
    held_after_json = held_after.json()
    if held_after_json["held"] or held_after_json["status"] != "active":
        raise RuntimeError("legal hold release did not restore active state")

    queued_payload = json.dumps(
        {
            "kind": "artifact",
            "scope": {
                "tenant_id": "tenant-poly",
                "workspace_id": "ws-queue",
                "project_id": "proj-queue",
                "user_id": "pm-queue",
            },
            "title": "Queued memory",
            "content": "Queue processing should persist and update snapshots.",
            "entity_keys": ["queue-smoke"],
            "tags": ["smoke", "queue"],
        }
    ).encode("utf-8")
    queue_response = post_json(
        f"{queue}/enqueue",
        {
            "id": "queue-item-smoke",
            "tenant_id": "tenant-poly",
            "payload": base64.b64encode(queued_payload).decode("utf-8"),
        },
    )
    if queue_response["status"] != "accepted":
        raise RuntimeError("queue did not accept smoke payload")

    deadline = time.time() + 10
    stats: dict[str, object] | None = None
    while time.time() < deadline:
        response = httpx.get(f"{queue}/stats", timeout=5.0)
        response.raise_for_status()
        stats = response.json()
        if stats["processed"] >= 1 and stats["errors"] == 0:
            break
        time.sleep(0.5)
    if stats is None or stats["processed"] < 1 or stats["errors"] != 0:
        raise RuntimeError(f"queue processing failed: {stats}")

    snapshot = httpx.get(
        f"{gateway}/v1/project-snapshots/latest",
        params={"tenant_id": "tenant-poly", "project_id": "proj-queue"},
        timeout=10.0,
    )
    snapshot.raise_for_status()
    if snapshot.json()["memory_count"] < 1:
        raise RuntimeError("project snapshot was not updated after queued write")

    connector = post_json(
        f"{gateway}/v1/integrations/connectors",
        {
            "connector_id": "conn-slack-poly",
            "tenant_id": "tenant-poly",
            "provider": "slack",
            "display_name": "Slack workspace",
            "remote_workspace_id": "T-poly",
            "auth_type": "oauth",
            "sync_mode": "hybrid",
            "status": "active",
            "metadata": {"workspace_name": "Polyglot smoke"},
            "last_synced_at": "2026-04-10T10:00:00Z",
        },
        request_headers=admin_headers,
    )
    if connector["connector_id"] != "conn-slack-poly":
        raise RuntimeError("gateway connector registry route failed")

    sources = post_json(
        f"{gateway}/v1/integrations/connectors/conn-slack-poly/sources/batch?tenant_id=tenant-poly",
        {
            "sources": [
                {
                    "source_id": "src-smoke-roadmap",
                    "connector_id": "conn-slack-poly",
                    "tenant_id": "tenant-poly",
                    "remote_source_id": "C100",
                    "source_type": "channel",
                    "display_name": "#roadmap",
                    "path": "/channels/roadmap",
                    "status": "stale",
                },
                {
                    "source_id": "src-smoke-launches",
                    "connector_id": "conn-slack-poly",
                    "tenant_id": "tenant-poly",
                    "remote_source_id": "C101",
                    "source_type": "channel",
                    "display_name": "#launches",
                    "path": "/channels/launches",
                    "status": "indexed",
                },
            ]
        },
        request_headers=admin_headers,
    )
    if len(sources) != 2:
        raise RuntimeError("gateway source inventory route failed")

    mappings = post_json(
        f"{gateway}/v1/integrations/connectors/conn-slack-poly/principal-mappings/batch?tenant_id=tenant-poly",
        {
            "mappings": [
                {
                    "mapping_id": "map-smoke-1",
                    "connector_id": "conn-slack-poly",
                    "tenant_id": "tenant-poly",
                    "principal_type": "user",
                    "local_principal_id": "pm-1",
                    "remote_principal_id": "U100",
                    "remote_name": "PM One",
                    "groups": ["product"],
                    "last_synced_at": "2026-04-10T10:05:00Z",
                }
            ]
        },
        request_headers=admin_headers,
    )
    if mappings[0]["remote_principal_id"] != "U100":
        raise RuntimeError("gateway principal mapping route failed")

    grants = post_json(
        f"{gateway}/v1/integrations/connectors/conn-slack-poly/permissions/batch?tenant_id=tenant-poly",
        {
            "grants": [
                {
                    "grant_id": "grant-smoke-view",
                    "source_id": "src-smoke-roadmap",
                    "connector_id": "conn-slack-poly",
                    "tenant_id": "tenant-poly",
                    "principal_type": "user",
                    "principal_id": "pm-1",
                    "permission_level": "view",
                    "inherited": True,
                }
            ]
        },
        request_headers=admin_headers,
    )
    if grants[0]["permission_level"] != "view":
        raise RuntimeError("gateway source permission route failed")

    sync_job = post_json(
        f"{gateway}/v1/integrations/connectors/conn-slack-poly/sync-jobs?tenant_id=tenant-poly",
        {
            "job_id": "sync-smoke-1",
            "connector_id": "conn-slack-poly",
            "tenant_id": "tenant-poly",
            "job_type": "acl_sync",
            "status": "running",
            "stats": {"sources_seen": 2, "grants_written": 1},
            "started_at": "2026-04-10T10:10:00Z",
        },
        request_headers=admin_headers,
    )
    if sync_job["status"] != "running":
        raise RuntimeError("gateway sync job route failed")

    coverage = httpx.get(
        f"{gateway}/v1/integrations/coverage",
        params={"tenant_id": "tenant-poly"},
        headers=admin_headers,
        timeout=10.0,
    )
    coverage.raise_for_status()
    coverage_json = coverage.json()
    if coverage_json["connectors_total"] < 1 or coverage_json["principals_mapped"] < 1 or coverage_json["grants_total"] < 1:
        raise RuntimeError("gateway coverage summary did not reflect connected-mode sync state")

    rpc = post_json(
        f"{mcp}/rpc",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "memory_get",
                "arguments": {"id": created_a["memory"]["memory_id"]},
            },
        },
        timeout=40.0,
        retries=2,
    )
    text = rpc["result"]["content"][0]["text"]
    if created_a["memory"]["memory_id"] not in text:
        raise RuntimeError("mcp memory_get did not return the stored memory")


def verify_permission_aware_gateway_retrieval(
    base_urls: dict[str, str],
    gateway_binary: Path,
    env: dict[str, str],
    log_dir: Path,
) -> None:
    auth_gateway_url = f"http://127.0.0.1:{AUTH_GATEWAY_PORT}"
    superadmin_token = "prov_e2e_superadmin"
    editor_token = "prov_e2e_editor"
    viewer_token = "prov_e2e_viewer"
    denied_viewer_token = "prov_e2e_viewer_denied"
    auth_gateway_env = env.copy()
    auth_gateway_env["PROVENA_LISTEN_ADDR"] = f"127.0.0.1:{AUTH_GATEWAY_PORT}"
    auth_gateway_env["PROVENA_ORCHESTRATION_URL"] = base_urls["orchestration"]
    auth_gateway_env["PROVENA_INTELLIGENCE_URL"] = base_urls["intelligence"]
    auth_gateway_env["PROVENA_STORE_URL"] = base_urls["store"]
    auth_gateway_env["PROVENA_LIFECYCLE_URL"] = base_urls["lifecycle"]
    auth_gateway_env["PROVENA_AUTH_ENABLED"] = "true"
    auth_gateway_env["PROVENA_API_KEYS"] = json.dumps(
        [
            {
                "key_id": "superadmin-e2e",
                "tenant_id": "",
                "role": "superadmin",
                "principal_id": "principal-admin-1",
                "groups": ["admins", "security"],
                "hashed_key": sha256_hex(superadmin_token),
                "description": "polyglot permission smoke superadmin",
            },
            {
                "key_id": "editor-e2e",
                "tenant_id": "tenant-poly",
                "role": "editor",
                "principal_id": "repo-sync-editor",
                "groups": ["repo-writers"],
                "hashed_key": sha256_hex(editor_token),
                "description": "polyglot repository sync editor",
            },
            {
                "key_id": "viewer-e2e",
                "tenant_id": "tenant-poly",
                "role": "viewer",
                "principal_id": "pm-1",
                "hashed_key": sha256_hex(viewer_token),
                "description": "polyglot permission smoke viewer",
            },
            {
                "key_id": "viewer-denied-e2e",
                "tenant_id": "tenant-poly",
                "role": "viewer",
                "principal_id": "pm-2",
                "hashed_key": sha256_hex(denied_viewer_token),
                "description": "polyglot permission smoke denied viewer",
            },
        ],
        separators=(",", ":"),
    )
    auth_gateway = launch(
        "gateway-auth",
        [str(gateway_binary)],
        ROOT / "control-plane",
        auth_gateway_env,
        log_dir / "gateway-auth.log",
    )

    def post_json(
        url: str,
        payload: dict[str, object],
        request_headers: dict[str, str],
    ) -> dict[str, object]:
        response = httpx.post(url, json=payload, headers=request_headers, timeout=20.0)
        response.raise_for_status()
        return response.json()

    try:
        wait_for_health(
            f"{auth_gateway_url}/healthz",
            headers=auth_headers(superadmin_token),
        )
        admin_headers = auth_headers(
            superadmin_token,
            principal_id="principal-admin-1",
            groups=["admins", "security"],
        )
        # Caller-supplied identity headers are intentionally false. The
        # gateway must authorize only the verified claims on each API key.
        viewer_pm_1 = auth_headers(
            viewer_token,
            principal_id="spoofed-pm-2",
            groups=["spoofed-group"],
        )
        viewer_pm_2 = auth_headers(
            denied_viewer_token,
            principal_id="pm-1",
            groups=["spoofed-granted-group"],
        )
        editor_sync_headers = auth_headers(
            editor_token,
            principal_id="spoofed-superadmin-principal",
            groups=["spoofed-admins"],
        )
        editor_sync_headers.update(
            {
                "X-Provena-Tenant-Id": "spoofed-tenant",
                "X-Provena-Role": "superadmin",
                "X-Provena-Key-Id": "spoofed-key",
            }
        )

        repository_event = {
            "applies_to": [],
            "authority": "tool",
            "body": "Authenticated gateway sync preserves verified repository ownership.",
            "confidence": 1.0,
            "created_at": "2026-07-13T10:00:00.000Z",
            "id": "event-auth-gateway-001",
            "importance": 1.0,
            "kind": "invariant",
            "provenance": {"actor": "e2e", "method": "observed"},
            "schema_version": 1,
            "sensitivity": "internal",
            "sources": [],
            "status": "active",
            "structured_data": {"boundary": "verified-gateway-claims"},
            "subject_type": "repo",
            "supersedes": [],
            "tags": ["gateway", "sync"],
            "title": "Verified repository sync",
            "triggers": [],
            "updated_at": "2026-07-13T10:00:00.000Z",
        }
        repository_ledger = rfc8785.dumps(repository_event).decode("utf-8") + "\n"
        repository_sync_payload = {
            "schema_version": 1,
            "scope": {"tenant_id": "tenant-poly", "project_id": "project-auth-sync"},
            "ledger_path": ".provena/memory/events.jsonl",
            "memory_fingerprint": hashlib.sha256(repository_ledger.encode("utf-8")).hexdigest(),
            "ledger_bytes": len(repository_ledger.encode("utf-8")),
            "ledger": repository_ledger,
        }
        repository_sync_url = (
            f"{auth_gateway_url}/v1/repositories/repo-auth-gateway-e2e/memory-events/sync"
        )
        first_sync = post_json(
            repository_sync_url,
            repository_sync_payload,
            editor_sync_headers,
        )
        if first_sync["created_memories"] != 1 or first_sync["no_op"]:
            raise RuntimeError("auth-enabled gateway repository sync did not create its projection")

        denied_sync = httpx.post(
            repository_sync_url,
            json=repository_sync_payload,
            headers=viewer_pm_1,
            timeout=20.0,
        )
        if denied_sync.status_code != 403:
            raise RuntimeError("auth-enabled gateway allowed a viewer to sync repository memory")

        replay_sync = post_json(
            repository_sync_url,
            repository_sync_payload,
            editor_sync_headers,
        )
        if not replay_sync["no_op"] or replay_sync["unchanged_memories"] != 1:
            raise RuntimeError("auth-enabled gateway identical repository replay was not a no-op")

        connector = post_json(
            f"{auth_gateway_url}/v1/integrations/connectors",
            {
                "connector_id": "conn-slack-authz",
                "tenant_id": "tenant-poly",
                "provider": "slack",
                "display_name": "Permission smoke workspace",
                "remote_workspace_id": "T-authz",
                "auth_type": "oauth",
                "sync_mode": "hybrid",
                "status": "active",
            },
            admin_headers,
        )
        if connector["connector_id"] != "conn-slack-authz":
            raise RuntimeError("auth-enabled gateway connector setup failed")

        sources = post_json(
            f"{auth_gateway_url}/v1/integrations/connectors/conn-slack-authz/sources/batch?tenant_id=tenant-poly",
            {
                "sources": [
                    {
                        "source_id": "src-authz-allowed",
                        "connector_id": "conn-slack-authz",
                        "tenant_id": "tenant-poly",
                        "remote_source_id": "C700",
                        "source_type": "channel",
                        "display_name": "#authz-allowed",
                    },
                    {
                        "source_id": "src-authz-blocked",
                        "connector_id": "conn-slack-authz",
                        "tenant_id": "tenant-poly",
                        "remote_source_id": "C701",
                        "source_type": "channel",
                        "display_name": "#authz-blocked",
                    },
                ]
            },
            admin_headers,
        )
        if len(sources) != 2:
            raise RuntimeError("auth-enabled gateway source setup failed")

        grants = post_json(
            f"{auth_gateway_url}/v1/integrations/connectors/conn-slack-authz/permissions/batch?tenant_id=tenant-poly",
            {
                "grants": [
                    {
                        "grant_id": "grant-authz-allowed",
                        "source_id": "src-authz-allowed",
                        "connector_id": "conn-slack-authz",
                        "tenant_id": "tenant-poly",
                        "principal_type": "user",
                        "principal_id": "pm-1",
                        "permission_level": "view",
                        "inherited": True,
                    }
                ]
            },
            admin_headers,
        )
        if len(grants) != 1:
            raise RuntimeError("auth-enabled gateway grant setup failed")

        allowed_memory = post_json(
            f"{auth_gateway_url}/v1/memories",
            {
                "kind": "artifact",
                "scope": {
                    "tenant_id": "tenant-poly",
                    "workspace_id": "ws-authz",
                    "project_id": "proj-authz",
                    "user_id": "pm-1",
                },
                "title": "Granted connected memory",
                "content": "Connected permission smoke should return the granted roadmap memory only.",
                "acl": [
                    {
                        "principal_id": "pm-1",
                        "principal_type": "user",
                        "permissions": ["read"],
                    }
                ],
                "source_references": [
                    {"source_type": "channel", "source_id": "src-authz-allowed"}
                ],
            },
            admin_headers,
        )
        blocked_memory = post_json(
            f"{auth_gateway_url}/v1/memories",
            {
                "kind": "artifact",
                "scope": {
                    "tenant_id": "tenant-poly",
                    "workspace_id": "ws-authz",
                    "project_id": "proj-authz",
                    "user_id": "pm-1",
                },
                "title": "Blocked connected memory",
                "content": "Connected permission smoke should hide blocked roadmap memory.",
                "acl": [
                    {
                        "principal_id": "pm-1",
                        "principal_type": "user",
                        "permissions": ["read"],
                    }
                ],
                "source_references": [
                    {"source_type": "channel", "source_id": "src-authz-blocked"}
                ],
            },
            admin_headers,
        )

        allowed_memory_id = allowed_memory["memory"]["memory_id"]
        blocked_memory_id = blocked_memory["memory"]["memory_id"]
        search_response = post_json(
            f"{auth_gateway_url}/v1/memories/search",
            {
                "query": "connected permission smoke roadmap memory",
                "scope": {
                    "tenant_id": "tenant-poly",
                    "workspace_id": "ws-authz",
                    "project_id": "proj-authz",
                    "user_id": "pm-1",
                },
                "limit": 10,
            },
            viewer_pm_1,
        )
        result_ids = [item["memory"]["memory_id"] for item in search_response["results"]]
        if allowed_memory_id not in result_ids:
            raise RuntimeError("auth-enabled gateway search did not return the granted memory")
        if blocked_memory_id in result_ids:
            raise RuntimeError("auth-enabled gateway search leaked a blocked connected memory")

        denied_search = post_json(
            f"{auth_gateway_url}/v1/memories/search",
            {
                "query": "connected permission smoke roadmap memory",
                "scope": {
                    "tenant_id": "tenant-poly",
                    "workspace_id": "ws-authz",
                    "project_id": "proj-authz",
                    "user_id": "pm-1",
                },
                "limit": 10,
            },
            viewer_pm_2,
        )
        if denied_search["results"]:
            raise RuntimeError("auth-enabled gateway search returned results without a matching grant")

        granted_get = httpx.get(
            f"{auth_gateway_url}/v1/memories/{allowed_memory_id}",
            headers=viewer_pm_1,
            timeout=10.0,
        )
        granted_get.raise_for_status()
        if granted_get.json()["memory_id"] != allowed_memory_id:
            raise RuntimeError("auth-enabled gateway get did not return the granted memory")

        blocked_get = httpx.get(
            f"{auth_gateway_url}/v1/memories/{blocked_memory_id}",
            headers=viewer_pm_1,
            timeout=10.0,
        )
        if blocked_get.status_code != 404:
            raise RuntimeError("auth-enabled gateway get leaked a blocked connected memory")
    except Exception:  # noqa: BLE001
        print("[error] permission-aware gateway verification failed. Auth gateway log tail follows:")
        print(tail(auth_gateway.log_path))
        raise
    finally:
        auth_gateway.stop()


def lifecycle_blocked(log_path: Path) -> bool:
    log_text = tail(log_path)
    return "Application Control policy has blocked this file" in log_text or "Access is denied" in log_text


def run_polyglot_suite() -> None:
    print("[suite] polyglot")
    with managed_temp_dir("polyglot") as temp_path:
        log_dir = temp_path / "logs"
        bin_dir = temp_path / "bin"
        log_dir.mkdir()
        bin_dir.mkdir()

        db_path = temp_path / "provena-polyglot.db"
        base_urls = {name: f"http://127.0.0.1:{port}" for name, port in POLYGLOT_PORTS.items()}
        env = os.environ.copy()

        orchestration_binary = build_rust_orchestration()
        control_plane = build_control_plane_binaries(bin_dir)

        processes: list[ManagedProcess] = []
        lifecycle_available = False

        try:
            store_env = env.copy()
            store_env["PROVENA_DB_PATH"] = str(db_path)
            processes.append(
                launch(
                    "store",
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(POLYGLOT_PORTS["store"]),
                    ],
                    ROOT,
                    store_env,
                    log_dir / "store.log",
                )
            )
            wait_for_health(f"{base_urls['store']}/healthz")

            orchestration_env = env.copy()
            orchestration_env["PROVENA_ORCHESTRATION_LISTEN_ADDR"] = f"127.0.0.1:{POLYGLOT_PORTS['orchestration']}"
            processes.append(
                launch(
                    "orchestration",
                    [str(orchestration_binary)],
                    ROOT / "orchestration",
                    orchestration_env,
                    log_dir / "orchestration.log",
                )
            )
            wait_for_health(f"{base_urls['orchestration']}/healthz")

            intelligence_env = env.copy()
            intelligence_env["PROVENA_INTEL_PIPELINE_URL"] = base_urls["store"]
            intelligence_env["PROVENA_INTEL_ORCHESTRATION_URL"] = base_urls["orchestration"]
            intelligence_env["PROVENA_INTEL_LISTEN_PORT"] = str(POLYGLOT_PORTS["intelligence"])
            intelligence_env["PROVENA_INTEL_SERVICE_ROLE"] = "superadmin"
            intelligence_env["PROVENA_INTEL_SERVICE_KEY_ID"] = "e2e-intelligence"
            intelligence_env["PROVENA_INTEL_SERVICE_PRINCIPAL_ID"] = "e2e-intelligence"
            processes.append(
                launch(
                    "intelligence",
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(POLYGLOT_PORTS["intelligence"]),
                    ],
                    ROOT / "intelligence",
                    intelligence_env,
                    log_dir / "intelligence.log",
                )
            )
            wait_for_health(f"{base_urls['intelligence']}/healthz")

            lifecycle_env = env.copy()
            lifecycle_env["PROVENA_LIFECYCLE_LISTEN_ADDR"] = f"127.0.0.1:{POLYGLOT_PORTS['lifecycle']}"
            lifecycle_env["PROVENA_STORE_URL"] = base_urls["store"]
            lifecycle_process = launch(
                "lifecycle",
                [str(control_plane["lifecycle"])],
                ROOT / "control-plane",
                lifecycle_env,
                log_dir / "lifecycle.log",
            )
            processes.append(lifecycle_process)
            time.sleep(1.5)
            if lifecycle_process.process.poll() is None:
                wait_for_health(f"{base_urls['lifecycle']}/healthz")
                lifecycle_available = True
            elif lifecycle_blocked(lifecycle_process.log_path):
                print("[warn] lifecycle runtime skipped due to local Windows Application Control policy.")
            else:
                raise RuntimeError(f"lifecycle failed to start:\n{tail(lifecycle_process.log_path)}")

            gateway_env = env.copy()
            gateway_env["PROVENA_LISTEN_ADDR"] = f"127.0.0.1:{POLYGLOT_PORTS['gateway']}"
            gateway_env["PROVENA_ORCHESTRATION_URL"] = base_urls["orchestration"]
            gateway_env["PROVENA_INTELLIGENCE_URL"] = base_urls["intelligence"]
            gateway_env["PROVENA_STORE_URL"] = base_urls["store"]
            gateway_env["PROVENA_LIFECYCLE_URL"] = base_urls["lifecycle"]
            # The base smoke exercises routing without credentials. A separate
            # auth-enabled gateway below proves tenant and grant enforcement.
            gateway_env["PROVENA_AUTH_ENABLED"] = "false"
            processes.append(
                launch(
                    "gateway",
                    [str(control_plane["gateway"])],
                    ROOT / "control-plane",
                    gateway_env,
                    log_dir / "gateway.log",
                )
            )
            wait_for_health(f"{base_urls['gateway']}/healthz")

            queue_env = env.copy()
            queue_env["PROVENA_QUEUE_LISTEN_ADDR"] = f"127.0.0.1:{POLYGLOT_PORTS['queue']}"
            queue_env["PROVENA_PIPELINE_URL"] = base_urls["intelligence"]
            processes.append(
                launch(
                    "queue",
                    control_plane_launch_command("queue", control_plane["queue"]),
                    ROOT / "control-plane",
                    queue_env,
                    log_dir / "queue.log",
                )
            )
            wait_for_health(f"{base_urls['queue']}/healthz")

            mcp_env = env.copy()
            mcp_env["PROVENA_MCP_LISTEN_ADDR"] = f"127.0.0.1:{POLYGLOT_PORTS['mcp']}"
            mcp_env["PROVENA_GATEWAY_URL"] = base_urls["gateway"]
            mcp_env["PROVENA_MCP_REQUIRE_AUTH"] = "false"
            processes.append(
                launch(
                    "mcp",
                    control_plane_launch_command("mcp", control_plane["mcp"]),
                    ROOT / "control-plane",
                    mcp_env,
                    log_dir / "mcp.log",
                )
            )
            wait_for_health(f"{base_urls['mcp']}/healthz")

            verify_polyglot_http(base_urls, lifecycle_available)
            verify_permission_aware_gateway_retrieval(
                base_urls,
                control_plane["gateway"],
                env,
                log_dir,
            )
            run_python_sdk_smoke(base_urls["gateway"], env)
            run_typescript_sdk_smoke(base_urls["gateway"], env)
            run_go_sdk_smoke(base_urls["gateway"], env, temp_path)
            run_rust_sdk_smoke(base_urls["gateway"], env)
            print("[ok] polyglot verification completed.")
        except Exception:  # noqa: BLE001
            print("[error] polyglot verification failed.")
            for process in processes:
                print(f"[log] {process.name}")
                print(tail(process.log_path))
            raise
        finally:
            for process in reversed(processes):
                process.stop()


def main() -> None:
    run_standalone_suite()
    run_polyglot_suite()
    print("[ok] Provena end-to-end verification completed.")


if __name__ == "__main__":
    main()
