from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
IS_WINDOWS = os.name == "nt"

STANDALONE_PORT = 8092
STANDALONE_BASE_URL = f"http://127.0.0.1:{STANDALONE_PORT}"
AUTH_GATEWAY_PORT = 18083
AUTH_STORE_PORT = 18084
AUTH_INTELLIGENCE_PORT = 18085
AUTH_QUEUE_PORT = 18086
AUTH_LIFECYCLE_PORT = 18087

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
                    subprocess.run(
                        ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                        check=False,
                        text=True,
                        capture_output=True,
                    )
                else:
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
    if IS_WINDOWS and name in {"queue", "mcp", "lifecycle"}:
        # Windows App Control on this host blocks launching some freshly built
        # Go executables from the workspace temp directory, while `go run`
        # remains allowed for the same commands.
        return ["go", "run", f"./cmd/{name}"]
    return [str(binary_path)]


def verify_polyglot_http(
    base_urls: dict[str, str],
    lifecycle_available: bool,
    queue_ingress_token: str,
) -> None:
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
        request_headers=admin_headers,
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
        request_headers=admin_headers,
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
        request_headers=admin_headers,
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
        request_headers=admin_headers,
    )
    memory_id = held_write["memory"]["memory_id"]
    held_before = httpx.get(
        f"{gateway}/v1/memories/{memory_id}",
        headers=admin_headers,
        timeout=10.0,
    )
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
    held_after = httpx.get(
        f"{gateway}/v1/memories/{memory_id}",
        headers=admin_headers,
        timeout=10.0,
    )
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
        request_headers={"Authorization": f"Bearer {queue_ingress_token}"},
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

    # Queue workers do not yet carry end-user credentials. Keep this suite on
    # its existing local-bypass write contract and verify persistence; snapshot
    # authorization for background writes is a separate delegated-auth story.
    queued_search = post_json(
        f"{gateway}/v1/memories/search",
        {
            "query": "queue processing persist update snapshots",
            "scope": {
                "tenant_id": "tenant-poly",
                "workspace_id": "ws-queue",
                "project_id": "proj-queue",
                "user_id": "pm-queue",
            },
            "limit": 5,
        },
        request_headers=admin_headers,
    )
    if not queued_search["results"]:
        raise RuntimeError("queued write was not persisted")

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


def verify_authenticated_chain(
    base_urls: dict[str, str],
    gateway_binary: Path,
    queue_command: list[str],
    lifecycle_binary: Path,
    env: dict[str, str],
    log_dir: Path,
) -> None:
    auth_gateway_url = f"http://127.0.0.1:{AUTH_GATEWAY_PORT}"
    auth_store_url = f"http://127.0.0.1:{AUTH_STORE_PORT}"
    auth_intelligence_url = f"http://127.0.0.1:{AUTH_INTELLIGENCE_PORT}"
    auth_queue_url = f"http://127.0.0.1:{AUTH_QUEUE_PORT}"
    auth_lifecycle_url = f"http://127.0.0.1:{AUTH_LIFECYCLE_PORT}"
    superadmin_token = secrets.token_urlsafe(32)
    viewer_pm_1_token = secrets.token_urlsafe(32)
    viewer_pm_2_token = secrets.token_urlsafe(32)
    tenant_b_token = secrets.token_urlsafe(32)
    queue_service_token = secrets.token_urlsafe(32)
    queue_caller_token = secrets.token_urlsafe(32)
    queue_ingress_token = secrets.token_urlsafe(32)
    gateway_service_token = secrets.token_urlsafe(32)
    lifecycle_service_token = secrets.token_urlsafe(32)
    raw_tokens = [
        superadmin_token,
        viewer_pm_1_token,
        viewer_pm_2_token,
        tenant_b_token,
        queue_service_token,
        queue_caller_token,
        queue_ingress_token,
        gateway_service_token,
        lifecycle_service_token,
    ]
    processes: list[ManagedProcess] = []

    def launch_healthy(
        name: str,
        command: list[str],
        cwd: Path,
        process_env: dict[str, str],
        log_path: Path,
        health_url: str,
        health_headers: dict[str, str] | None = None,
    ) -> ManagedProcess:
        process = launch(name, command, cwd, process_env, log_path)
        processes.append(process)
        try:
            wait_for_health(health_url, headers=health_headers)
        except Exception:
            for started in reversed(processes):
                started.stop()
            raise
        return process

    auth_store_env = env.copy()
    for inherited_name in (
        "PROVENA_DATABASE_URL",
        "PROVENA_SERVICE_TOKEN",
        "PROVENA_SERVICE_TENANT_ID",
        "PROVENA_SERVICE_PRINCIPAL_ID",
        "PROVENA_SERVICE_ROLE",
    ):
        auth_store_env.pop(inherited_name, None)
    auth_store_env["PROVENA_DB_PATH"] = str(log_dir.parent / "provena-authenticated.db")
    auth_store_env["PROVENA_ENVIRONMENT"] = "production"
    auth_store_env["PROVENA_ALLOW_UNAUTHENTICATED_LOCAL"] = "false"
    auth_store_env["PROVENA_SERVICE_IDENTITIES"] = json.dumps(
        [
            {
                "token_sha256": sha256_hex(queue_service_token),
                "tenant_id": "tenant-poly",
                "principal_id": "queue-service-registry",
                "role": "editor",
            },
            {
                "token_sha256": sha256_hex(lifecycle_service_token),
                "tenant_id": "tenant-poly",
                "principal_id": "lifecycle-service-registry",
                "role": "admin",
            },
        ],
        separators=(",", ":"),
    )
    auth_store_env["PROVENA_GATEWAY_SERVICE_TOKEN"] = gateway_service_token
    launch_healthy(
        "store-auth",
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(AUTH_STORE_PORT),
        ],
        ROOT,
        auth_store_env,
        log_dir / "store-auth.log",
        f"{auth_store_url}/healthz",
    )

    auth_intelligence_env = env.copy()
    auth_intelligence_env["PROVENA_INTEL_PIPELINE_URL"] = auth_store_url
    auth_intelligence_env["PROVENA_INTEL_ORCHESTRATION_URL"] = base_urls["orchestration"]
    auth_intelligence_env["PROVENA_INTEL_LISTEN_PORT"] = str(AUTH_INTELLIGENCE_PORT)
    launch_healthy(
        "intelligence-auth",
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(AUTH_INTELLIGENCE_PORT),
        ],
        ROOT / "intelligence",
        auth_intelligence_env,
        log_dir / "intelligence-auth.log",
        f"{auth_intelligence_url}/healthz",
    )

    auth_lifecycle_env = env.copy()
    auth_lifecycle_env["PROVENA_LIFECYCLE_LISTEN_ADDR"] = f"127.0.0.1:{AUTH_LIFECYCLE_PORT}"
    auth_lifecycle_env["PROVENA_STORE_URL"] = auth_store_url
    auth_lifecycle_env["PROVENA_ENVIRONMENT"] = "production"
    auth_lifecycle_env["PROVENA_ALLOW_UNAUTHENTICATED_LOCAL"] = "false"
    auth_lifecycle_env["PROVENA_GATEWAY_SERVICE_TOKEN"] = gateway_service_token
    auth_lifecycle_env["PROVENA_LIFECYCLE_SERVICE_TOKEN"] = lifecycle_service_token
    auth_lifecycle_env["PROVENA_LIFECYCLE_TENANT_ID"] = "tenant-poly"
    auth_lifecycle_env["PROVENA_LIFECYCLE_PRINCIPAL_ID"] = "lifecycle-service-configured-sentinel"
    launch_healthy(
        "lifecycle-auth",
        control_plane_launch_command("lifecycle", lifecycle_binary),
        ROOT / "control-plane",
        auth_lifecycle_env,
        log_dir / "lifecycle-auth.log",
        f"{auth_lifecycle_url}/healthz",
    )

    auth_gateway_env = env.copy()
    auth_gateway_env["PROVENA_LISTEN_ADDR"] = f"127.0.0.1:{AUTH_GATEWAY_PORT}"
    auth_gateway_env["PROVENA_ORCHESTRATION_URL"] = base_urls["orchestration"]
    auth_gateway_env["PROVENA_INTELLIGENCE_URL"] = auth_intelligence_url
    auth_gateway_env["PROVENA_STORE_URL"] = auth_store_url
    auth_gateway_env["PROVENA_LIFECYCLE_URL"] = auth_lifecycle_url
    auth_gateway_env["PROVENA_AUTH_ENABLED"] = "true"
    auth_gateway_env["PROVENA_GATEWAY_SERVICE_TOKEN"] = gateway_service_token
    auth_gateway_env["PROVENA_API_KEYS"] = json.dumps(
        [
            {
                "key_id": "superadmin-e2e",
                "tenant_id": "",
                "role": "superadmin",
                "hashed_key": sha256_hex(superadmin_token),
                "principal_id": "principal-admin-registry",
                "groups": ["admins", "security"],
                "description": "polyglot permission smoke superadmin",
            },
            {
                "key_id": "viewer-pm-1-e2e",
                "tenant_id": "tenant-poly",
                "role": "viewer",
                "hashed_key": sha256_hex(viewer_pm_1_token),
                "principal_id": "pm-1",
                "description": "polyglot permission smoke viewer pm-1",
            },
            {
                "key_id": "viewer-pm-2-e2e",
                "tenant_id": "tenant-poly",
                "role": "viewer",
                "hashed_key": sha256_hex(viewer_pm_2_token),
                "principal_id": "pm-2",
                "description": "polyglot permission smoke viewer pm-2",
            },
            {
                "key_id": "tenant-b-e2e",
                "tenant_id": "tenant-other",
                "role": "admin",
                "hashed_key": sha256_hex(tenant_b_token),
                "principal_id": "principal-b-registry",
                "groups": ["tenant-b-agents"],
                "description": "polyglot cross-tenant write isolation",
            },
        ],
        separators=(",", ":"),
    )
    launch_healthy(
        "gateway-auth",
        [str(gateway_binary)],
        ROOT / "control-plane",
        auth_gateway_env,
        log_dir / "gateway-auth.log",
        f"{auth_gateway_url}/healthz",
        health_headers=auth_headers(superadmin_token),
    )

    auth_queue_env = env.copy()
    auth_queue_env["PROVENA_QUEUE_LISTEN_ADDR"] = f"127.0.0.1:{AUTH_QUEUE_PORT}"
    auth_queue_env["PROVENA_PIPELINE_URL"] = auth_intelligence_url
    auth_queue_env["PROVENA_QUEUE_WORKERS"] = "1"
    auth_queue_env["PROVENA_QUEUE_MAX_DEPTH"] = "10"
    auth_queue_env["PROVENA_ENVIRONMENT"] = "production"
    auth_queue_env["PROVENA_ALLOW_UNAUTHENTICATED_LOCAL"] = "false"
    auth_queue_env["PROVENA_SERVICE_TOKEN"] = queue_service_token
    auth_queue_env["PROVENA_SERVICE_TENANT_ID"] = "tenant-poly"
    auth_queue_env["PROVENA_SERVICE_PRINCIPAL_ID"] = "queue-service-configured-sentinel"
    auth_queue_env["PROVENA_SERVICE_ROLE"] = "editor"
    auth_queue_env["PROVENA_QUEUE_INGRESS_TOKEN"] = queue_ingress_token
    launch_healthy(
        "queue-auth",
        queue_command,
        ROOT / "control-plane",
        auth_queue_env,
        log_dir / "queue-auth.log",
        f"{auth_queue_url}/healthz",
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
        admin_headers = {
            **auth_headers(
                superadmin_token,
                principal_id="principal-admin-1",
                groups=["admins", "security"],
            ),
            "X-Provena-Tenant-Id": "tenant-other",
            "X-Provena-Role": "viewer",
        }
        viewer_pm_1 = auth_headers(viewer_pm_1_token, principal_id="spoofed-pm-1")
        viewer_pm_2 = auth_headers(viewer_pm_2_token, principal_id="spoofed-pm-2")
        tenant_b_headers = auth_headers(
            tenant_b_token,
            principal_id="principal-admin-registry",
            groups=["admins"],
        )

        direct_external_bearer = httpx.post(
            f"{auth_store_url}/v1/memories",
            json={
                "memory_id": "external-bearer-must-not-reach-store",
                "kind": "fact",
                "scope": {"tenant_id": "tenant-poly"},
                "content": "External gateway credentials are not store credentials.",
            },
            headers=admin_headers,
            timeout=10.0,
        )
        if direct_external_bearer.status_code != 401:
            raise RuntimeError("store accepted an external gateway bearer directly")

        retention_policy = post_json(
            f"{auth_gateway_url}/v1/admin/retention-policies",
            {
                "policy_id": "tenant-poly-default",
                "tenant_id": "tenant-poly",
                "max_age_days": 365,
                "action": "delete_soft",
            },
            admin_headers,
        )
        if retention_policy.get("tenant_id") != "tenant-poly":
            raise RuntimeError("authenticated lifecycle did not persist its tenant policy")
        retention_list = httpx.get(
            f"{auth_gateway_url}/v1/admin/retention-policies",
            params={"tenant_id": "tenant-poly"},
            headers=admin_headers,
            timeout=10.0,
        )
        retention_list.raise_for_status()
        if not any(item.get("policy_id") == "tenant-poly-default" for item in retention_list.json()):
            raise RuntimeError("authenticated lifecycle could not read its tenant policy")

        foreign_lifecycle_admin = httpx.post(
            f"{auth_gateway_url}/v1/admin/retention-policies",
            json={
                "policy_id": "cross-tenant-policy-must-not-persist",
                "tenant_id": "tenant-other",
                "max_age_days": 1,
                "action": "delete_soft",
            },
            headers=tenant_b_headers,
            timeout=10.0,
        )
        if foreign_lifecycle_admin.status_code != 403:
            raise RuntimeError("lifecycle accepted a foreign-tenant admin through the gateway")

        queue_payload = json.dumps(
            {
                "kind": "fact",
                "scope": {
                    "tenant_id": "tenant-poly",
                    "workspace_id": "ws-queue-auth",
                    "project_id": "proj-queue-auth",
                    "user_id": "queue-worker",
                },
                "title": "Authenticated queued service memory",
                "content": "Tenant dedicated queue service authentication persists under registry ownership.",
                "entity_keys": ["queue-service-auth"],
                "tags": ["queue", "auth"],
            },
            separators=(",", ":"),
        )
        queue_spoof_headers = {
            "Authorization": f"Bearer {queue_caller_token}",
            "Cookie": "session=queue-caller",
            "Traceparent": "00-queue-caller-trace-00",
            "X-Forwarded-For": "203.0.113.14",
            "X-Provena-Tenant-Id": "tenant-other",
            "X-Provena-Role": "superadmin",
            "X-Provena-Key-Id": "queue-caller-key",
            "X-Provena-Principal-Id": "queue-caller-principal",
            "X-Provena-Groups": "queue-caller-group",
        }
        unauthorized_queue = httpx.post(
            f"{auth_queue_url}/enqueue",
            json={
                "id": "queue-auth-item",
                "tenant_id": "tenant-poly",
                "payload": base64.b64encode(queue_payload.encode("utf-8")).decode("ascii"),
            },
            headers=queue_spoof_headers,
            timeout=10.0,
        )
        if unauthorized_queue.status_code != 401:
            raise RuntimeError("queue accepted an unauthenticated ingress caller")
        unauthorized_stats = httpx.get(f"{auth_queue_url}/stats", timeout=5.0)
        unauthorized_stats.raise_for_status()
        if any(unauthorized_stats.json().get(name) != 0 for name in ("depth", "processed", "errors")):
            raise RuntimeError("unauthenticated queue request reached queue processing")

        queue_ingress_headers = {
            **queue_spoof_headers,
            "Authorization": f"Bearer {queue_ingress_token}",
        }
        queue_response = httpx.post(
            f"{auth_queue_url}/enqueue",
            json={
                "id": "queue-auth-item",
                "tenant_id": "tenant-poly",
                "payload": base64.b64encode(queue_payload.encode("utf-8")).decode("ascii"),
            },
            headers=queue_ingress_headers,
            timeout=10.0,
        )
        if queue_response.status_code != 202:
            raise RuntimeError("authenticated queue did not accept its configured tenant")

        queue_deadline = time.time() + 20
        queue_stats: dict[str, object] = {}
        while time.time() < queue_deadline:
            stats_response = httpx.get(f"{auth_queue_url}/stats", timeout=5.0)
            stats_response.raise_for_status()
            queue_stats = stats_response.json()
            if queue_stats.get("processed") == 1:
                break
            time.sleep(0.1)
        if queue_stats.get("processed") != 1 or queue_stats.get("errors") != 0:
            raise RuntimeError("authenticated queue did not process its service write")

        queue_search = post_json(
            f"{auth_gateway_url}/v1/memories/search",
            {
                "query": "tenant dedicated queue service authentication registry ownership",
                "scope": {
                    "tenant_id": "tenant-poly",
                    "workspace_id": "ws-queue-auth",
                    "project_id": "proj-queue-auth",
                    "user_id": "queue-worker",
                },
                "limit": 10,
            },
            admin_headers,
        )
        queue_hits = [
            item["memory"]
            for item in queue_search["results"]
            if item["memory"].get("title") == "Authenticated queued service memory"
        ]
        if len(queue_hits) != 1:
            raise RuntimeError("authenticated queued memory was not persisted exactly once")
        queue_owner_ids = {entry["principal_id"] for entry in queue_hits[0]["acl"]}
        if (
            "queue-service-registry" not in queue_owner_ids
            or "queue-caller-principal" in queue_owner_ids
            or "queue-service-configured-sentinel" in queue_owner_ids
        ):
            raise RuntimeError("authenticated queue did not preserve registry principal authority")

        foreign_envelope = httpx.post(
            f"{auth_queue_url}/enqueue",
            json={
                "id": "queue-foreign-envelope",
                "tenant_id": "tenant-other",
                "payload": base64.b64encode(queue_payload.encode("utf-8")).decode("ascii"),
            },
            headers=queue_ingress_headers,
            timeout=10.0,
        )
        if foreign_envelope.status_code != 403:
            raise RuntimeError("tenant-dedicated queue accepted a foreign tenant envelope")

        cross_tenant_queue_payload = json.dumps(
            {
                "kind": "fact",
                "scope": {
                    "tenant_id": "tenant-other",
                    "workspace_id": "ws-queue-cross-tenant",
                    "project_id": "proj-queue-cross-tenant",
                    "user_id": "queue-worker",
                },
                "title": "Rejected cross tenant queued memory",
                "content": "A tenant-poly queue bearer must not persist this tenant-other background write.",
            },
            separators=(",", ":"),
        )
        cross_tenant_queue = httpx.post(
            f"{auth_queue_url}/enqueue",
            json={
                "id": "queue-cross-tenant-payload",
                "tenant_id": "tenant-poly",
                "payload": base64.b64encode(cross_tenant_queue_payload.encode("utf-8")).decode("ascii"),
            },
            headers=queue_ingress_headers,
            timeout=10.0,
        )
        if cross_tenant_queue.status_code != 202:
            raise RuntimeError("queue did not accept worker-tenant envelope for store authority check")

        queue_deadline = time.time() + 20
        while time.time() < queue_deadline:
            stats_response = httpx.get(f"{auth_queue_url}/stats", timeout=5.0)
            stats_response.raise_for_status()
            queue_stats = stats_response.json()
            if queue_stats.get("errors") == 1:
                break
            time.sleep(0.1)
        if queue_stats.get("processed") != 1 or queue_stats.get("errors") != 1:
            raise RuntimeError("cross-tenant queued payload was not denied by the authenticated store chain")

        denied_queue_search = post_json(
            f"{auth_gateway_url}/v1/memories/search",
            {
                "query": "rejected cross tenant queued memory tenant other background write",
                "scope": {
                    "tenant_id": "tenant-other",
                    "workspace_id": "ws-queue-cross-tenant",
                    "project_id": "proj-queue-cross-tenant",
                    "user_id": "queue-worker",
                },
                "limit": 10,
            },
            tenant_b_headers,
        )
        if denied_queue_search["results"]:
            raise RuntimeError("cross-tenant queued payload persisted in the foreign tenant")

        coerced_queue_search = post_json(
            f"{auth_gateway_url}/v1/memories/search",
            {
                "query": "rejected cross tenant queued memory tenant other background write",
                "scope": {
                    "tenant_id": "tenant-poly",
                    "workspace_id": "ws-queue-cross-tenant",
                    "project_id": "proj-queue-cross-tenant",
                    "user_id": "queue-worker",
                },
                "limit": 10,
            },
            admin_headers,
        )
        if coerced_queue_search["results"]:
            raise RuntimeError("cross-tenant queued payload was coerced into the worker tenant")

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

        registry_memory = post_json(
            f"{auth_gateway_url}/v1/memories",
            {
                "kind": "fact",
                "scope": {
                    "tenant_id": "tenant-poly",
                    "workspace_id": "ws-authz",
                    "project_id": "proj-authz",
                    "user_id": "pm-1",
                },
                "title": "Registry identity authority",
                "content": "The authenticated store must replace spoofed caller identity.",
            },
            admin_headers,
        )
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
                    {"principal_id": "pm-1", "principal_type": "user", "permissions": ["read"]},
                    {"principal_id": "pm-2", "principal_type": "user", "permissions": ["read"]},
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
                    {"principal_id": "pm-1", "principal_type": "user", "permissions": ["read"]},
                    {"principal_id": "pm-2", "principal_type": "user", "permissions": ["read"]},
                ],
                "source_references": [
                    {"source_type": "channel", "source_id": "src-authz-blocked"}
                ],
            },
            admin_headers,
        )

        owner_ids = {entry["principal_id"] for entry in registry_memory["memory"]["acl"]}
        if "principal-admin-registry" not in owner_ids or "principal-admin-1" in owner_ids:
            raise RuntimeError("authenticated store did not keep registry principal authoritative")

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

        cross_tenant_write = httpx.post(
            f"{auth_gateway_url}/v1/memories",
            json={
                "kind": "fact",
                "scope": {
                    "tenant_id": "tenant-poly",
                    "workspace_id": "ws-authz",
                    "project_id": "proj-authz",
                    "user_id": "pm-1",
                },
                "title": "Cross-tenant write must fail",
                "content": "A second tenant must not write into tenant-poly.",
            },
            headers=tenant_b_headers,
            timeout=20.0,
        )
        if cross_tenant_write.status_code != 403:
            raise RuntimeError("authenticated intelligence chain accepted a cross-tenant write")

        cross_tenant_search = httpx.post(
            f"{auth_gateway_url}/v1/memories/search",
            json={
                "query": "connected permission smoke roadmap memory",
                "scope": {
                    "tenant_id": "tenant-poly",
                    "workspace_id": "ws-authz",
                    "project_id": "proj-authz",
                    "user_id": "pm-1",
                },
                "limit": 10,
            },
            headers=tenant_b_headers,
            timeout=20.0,
        )
        cross_tenant_search.raise_for_status()
        if cross_tenant_search.json()["results"]:
            raise RuntimeError("authenticated intelligence chain leaked cross-tenant search results")

        cross_tenant_get = httpx.get(
            f"{auth_gateway_url}/v1/memories/{allowed_memory_id}",
            headers=tenant_b_headers,
            timeout=10.0,
        )
        if cross_tenant_get.status_code != 404:
            raise RuntimeError("authenticated gateway leaked a cross-tenant direct memory read")
    except Exception:  # noqa: BLE001
        print("[error] permission-aware gateway verification failed. Redacted auth log tails follow:")
        for process in processes:
            log_tail = tail(process.log_path)
            for token in raw_tokens:
                log_tail = log_tail.replace(token, "[REDACTED]")
            print(f"[log] {process.name}")
            print(log_tail)
        raise
    finally:
        for process in reversed(processes):
            process.stop()
        leaked_logs = [
            process.name
            for process in processes
            if process.log_path.exists()
            and any(token in process.log_path.read_text(encoding="utf-8", errors="ignore") for token in raw_tokens)
        ]
        if leaked_logs:
            raise RuntimeError(f"raw bearer appeared in authenticated service logs: {', '.join(leaked_logs)}")


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
        queue_ingress_token = secrets.token_urlsafe(32)

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
                control_plane_launch_command("lifecycle", control_plane["lifecycle"]),
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
            queue_env["PROVENA_QUEUE_INGRESS_TOKEN"] = queue_ingress_token
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

            verify_polyglot_http(base_urls, lifecycle_available, queue_ingress_token)
            verify_authenticated_chain(
                base_urls,
                control_plane["gateway"],
                control_plane_launch_command("queue", control_plane["queue"]),
                control_plane["lifecycle"],
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
