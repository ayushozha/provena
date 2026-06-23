import { existsSync } from "node:fs";
import {
  loadConfig,
  parseStoreUrl,
  pidFilePath,
  resolveDbPath,
} from "../config.js";
import { findStoreRoot, isProcessRunning, readPidFile } from "../process.js";

interface CheckResult {
  name: string;
  ok: boolean;
  detail: string;
}

export async function runDoctor(_argv: string[]): Promise<void> {
  const checks: CheckResult[] = [];

  let loaded;
  try {
    loaded = loadConfig();
    checks.push({ name: "config", ok: true, detail: loaded.configFile });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    checks.push({ name: "config", ok: false, detail: message });
    printReport(checks);
    process.exit(1);
  }

  const { config, projectRoot } = loaded;
  const dbPath = resolveDbPath(config, projectRoot);
  checks.push({
    name: "database path",
    ok: true,
    detail: process.env.PROVENA_DB_PATH
      ? `${dbPath} (PROVENA_DB_PATH)`
      : dbPath,
  });

  const storeRoot = findStoreRoot(projectRoot);
  checks.push({
    name: "store code",
    ok: Boolean(storeRoot),
    detail: storeRoot ?? "app/main.py not found (set PROVENA_STORE_ROOT)",
  });

  const pid = readPidFile(pidFilePath(projectRoot));
  if (pid) {
    const running = isProcessRunning(pid);
    checks.push({
      name: "detached serve",
      ok: running,
      detail: running ? `pid ${pid}` : `stale pid ${pid}`,
    });
  }

  const endpoint = parseStoreUrl(config.store_url);
  checks.push({ name: "store_url", ok: true, detail: config.store_url });
  checks.push({
    name: "sqlite file",
    ok: true,
    detail: existsSync(dbPath) ? "present" : "not created yet",
  });

  try {
    const response = await fetch(endpoint.healthUrl, {
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) {
      checks.push({
        name: "store /healthz",
        ok: false,
        detail: `HTTP ${response.status}`,
      });
    } else {
      const body = (await response.json()) as { status?: string; service?: string };
      checks.push({
        name: "store /healthz",
        ok: body.status === "ok",
        detail:
          body.status === "ok"
            ? body.service
              ? `${body.service} ok`
              : "ok"
            : `status=${String(body.status)}`,
      });
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    checks.push({ name: "store /healthz", ok: false, detail: message });
  }

  printReport(checks);
  if (checks.some((c) => !c.ok)) {
    process.exit(1);
  }
}

function printReport(checks: CheckResult[]): void {
  console.log("provena doctor");
  for (const check of checks) {
    console.log(`  [${check.ok ? "ok" : "FAIL"}] ${check.name}: ${check.detail}`);
  }
}