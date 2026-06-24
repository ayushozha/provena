import {
  isInsecureBindAllowed,
  loadConfig,
  parseStoreUrl,
  pidFilePath,
  resolveDbPath,
} from "../config.js";
import {
  ensureParentDir,
  findStoreRoot,
  isPortInUse,
  isProcessRunning,
  killProcess,
  readPidFile,
  removePidFile,
  resolvePythonRunner,
  spawnUvicornDetachedUnix,
  spawnUvicornDetachedWindows,
  spawnUvicornForeground,
  waitForListenerPid,
  writePidFile,
} from "../process.js";

async function waitForHealth(healthUrl: string): Promise<boolean> {
  for (let i = 0; i < 30; i += 1) {
    try {
      const response = await fetch(healthUrl, { signal: AbortSignal.timeout(2000) });
      if (response.ok) {
        const body = (await response.json()) as { status?: string };
        if (body.status === "ok") {
          return true;
        }
      }
    } catch {
      // retry
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

export async function runServe(argv: string[]): Promise<void> {
  const detach = argv.includes("--detach");
  const stop = argv.includes("--stop");

  const loaded = loadConfig();
  const { config, projectRoot } = loaded;
  const pidPath = pidFilePath(projectRoot);
  const endpoint = parseStoreUrl(config.store_url);

  if (endpoint.host === "0.0.0.0" && !isInsecureBindAllowed()) {
    console.error(
      "provena serve: refusing bind to 0.0.0.0 (set PROVENA_INSECURE_BIND=1 to override)",
    );
    process.exit(1);
  }

  if (stop) {
    const pid = readPidFile(pidPath);
    if (!pid) {
      console.error("provena serve --stop: no PID file (.provena/store.pid)");
      process.exit(1);
    }
    if (!isProcessRunning(pid)) {
      removePidFile(pidPath);
      console.log("provena serve: process not running (removed stale PID file)");
      return;
    }
    killProcess(pid);
    removePidFile(pidPath);
    console.log(`provena serve: stopped (pid ${pid})`);
    return;
  }

  const existingPid = readPidFile(pidPath);
  if (existingPid && isProcessRunning(existingPid)) {
    console.error(
      `provena serve: already running (pid ${existingPid}); use \`provena serve --stop\` first`,
    );
    process.exit(1);
  }
  if (existingPid) {
    removePidFile(pidPath);
  }

  const storeRoot = findStoreRoot(projectRoot);
  if (!storeRoot) {
    console.error(
      "provena serve: cannot find Python store (app/main.py). Set PROVENA_STORE_ROOT.",
    );
    process.exit(1);
  }

  const dbPath = resolveDbPath(config, projectRoot);
  ensureParentDir(dbPath);

  if (await isPortInUse(endpoint.host, endpoint.port)) {
    console.error(`provena serve: port ${endpoint.port} on ${endpoint.host} is in use`);
    process.exit(1);
  }

  const runner = resolvePythonRunner(storeRoot);

  if (detach) {
    let pid: number;
    if (process.platform === "win32") {
      spawnUvicornDetachedWindows(runner, endpoint.host, endpoint.port, dbPath);
      if (!(await waitForHealth(endpoint.healthUrl))) {
        console.error("provena serve: /healthz did not become ready");
        process.exit(1);
      }
      const listenerPid = await waitForListenerPid(endpoint.port);
      if (!listenerPid) {
        console.error("provena serve: store did not bind to port");
        process.exit(1);
      }
      pid = listenerPid;
    } else {
      pid = spawnUvicornDetachedUnix(runner, endpoint.host, endpoint.port, dbPath);
      if (!(await waitForHealth(endpoint.healthUrl))) {
        killProcess(pid);
        console.error(`provena serve: pid ${pid} started but /healthz not ready`);
        process.exit(1);
      }
    }

    writePidFile(pidPath, pid);
    console.log(
      `provena serve: detached on ${config.store_url} (pid ${pid}, db ${dbPath})`,
    );
    return;
  }

  const child = spawnUvicornForeground(runner, endpoint.host, endpoint.port, dbPath);
  if (!child.pid) {
    console.error("provena serve: failed to start uvicorn");
    process.exit(1);
  }

  console.log(
    `provena serve: ${config.store_url} (pid ${child.pid}, db ${dbPath}) — Ctrl+C to stop`,
  );

  await new Promise<void>((resolvePromise, rejectPromise) => {
    child.on("error", rejectPromise);
    child.on("exit", (code, signal) => {
      if (code === 0 || signal === "SIGINT" || signal === "SIGTERM") {
        resolvePromise();
        return;
      }
      rejectPromise(new Error(`uvicorn exited (code=${code}, signal=${signal})`));
    });
  });
}