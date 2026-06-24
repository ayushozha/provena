import { spawn, spawnSync, type ChildProcess, type SpawnOptions } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import net from "node:net";

export interface PythonRunner {
  command: string;
  prefixArgs: string[];
  storeRoot: string;
}

const STORE_MARKER = join("app", "main.py");

export function findStoreRoot(startDir: string): string | null {
  const envRoot = process.env.PROVENA_STORE_ROOT;
  if (envRoot) {
    const resolved = resolve(envRoot);
    if (existsSync(join(resolved, STORE_MARKER))) {
      return resolved;
    }
  }

  let current = resolve(startDir);
  while (true) {
    if (existsSync(join(current, STORE_MARKER))) {
      return current;
    }
    const parent = dirname(current);
    if (parent === current) {
      return null;
    }
    current = parent;
  }
}

function venvPython(storeRoot: string): string | null {
  const win = join(storeRoot, ".venv", "Scripts", "python.exe");
  if (existsSync(win)) {
    return win;
  }
  const unix = join(storeRoot, ".venv", "bin", "python");
  if (existsSync(unix)) {
    return unix;
  }
  return null;
}

function hasUv(): boolean {
  const probe = spawnSync("uv", ["--version"], {
    encoding: "utf8",
    shell: process.platform === "win32",
  });
  return probe.status === 0;
}

export function resolvePythonRunner(storeRoot: string): PythonRunner {
  const venv = venvPython(storeRoot);
  if (venv) {
    return { command: venv, prefixArgs: [], storeRoot };
  }
  if (existsSync(join(storeRoot, "pyproject.toml")) && hasUv()) {
    return { command: "uv", prefixArgs: ["run", "python"], storeRoot };
  }
  return {
    command: process.platform === "win32" ? "python" : "python3",
    prefixArgs: [],
    storeRoot,
  };
}

export function ensureParentDir(filePath: string): void {
  mkdirSync(dirname(filePath), { recursive: true });
}

export function readPidFile(pidPath: string): number | null {
  if (!existsSync(pidPath)) {
    return null;
  }
  const pid = Number.parseInt(readFileSync(pidPath, "utf8").trim(), 10);
  return Number.isFinite(pid) && pid > 0 ? pid : null;
}

export function writePidFile(pidPath: string, pid: number): void {
  ensureParentDir(pidPath);
  writeFileSync(pidPath, `${pid}\n`, "utf8");
}

export function removePidFile(pidPath: string): void {
  if (existsSync(pidPath)) {
    unlinkSync(pidPath);
  }
}

export function isProcessRunning(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

export function killProcess(pid: number): void {
  if (!isProcessRunning(pid)) {
    return;
  }
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore" });
    return;
  }
  try {
    process.kill(pid, "SIGTERM");
  } catch {
    process.kill(pid, "SIGKILL");
  }
}

export function isPortInUse(host: string, port: number): Promise<boolean> {
  return new Promise((resolvePromise) => {
    const server = net.createServer();
    server.once("error", (err: NodeJS.ErrnoException) => {
      resolvePromise(err.code === "EADDRINUSE");
    });
    server.once("listening", () => {
      server.close(() => resolvePromise(false));
    });
    server.listen(port, host);
  });
}

function buildUvicornArgs(host: string, port: number): string[] {
  return ["-m", "uvicorn", "app.main:app", "--host", host, "--port", String(port)];
}

export function findListenerPid(port: number): number | null {
  const result = spawnSync("netstat", ["-ano"], { encoding: "utf8", shell: false });
  if (result.status !== 0) {
    return null;
  }
  const suffix = `:${port}`;
  for (const line of result.stdout.split(/\r?\n/)) {
    if (!line.includes("LISTENING") || !line.includes(suffix)) {
      continue;
    }
    const parts = line.trim().split(/\s+/);
    const pid = Number.parseInt(parts.at(-1) ?? "", 10);
    if (Number.isFinite(pid) && pid > 0) {
      return pid;
    }
  }
  return null;
}

export async function waitForListenerPid(
  port: number,
  attempts = 30,
  delayMs = 500,
): Promise<number | null> {
  for (let i = 0; i < attempts; i += 1) {
    const pid = findListenerPid(port);
    if (pid) {
      return pid;
    }
    await new Promise((r) => setTimeout(r, delayMs));
  }
  return null;
}

function vbsEscape(value: string): string {
  return value.replace(/"/g, '""');
}

export function spawnUvicornDetachedWindows(
  runner: PythonRunner,
  host: string,
  port: number,
  dbPath: string,
): void {
  const args = [...runner.prefixArgs, ...buildUvicornArgs(host, port)];
  const vbsPath = join(runner.storeRoot, ".provena", "serve.vbs");
  ensureParentDir(vbsPath);

  const command = [runner.command, ...args]
    .map((part) => (/\s/u.test(part) ? `"${part.replace(/"/g, '""')}"` : part))
    .join(" ");

  const vbs = [
    'Set shell = CreateObject("WScript.Shell")',
    `shell.Environment("Process")("PROVENA_DB_PATH") = "${vbsEscape(dbPath)}"`,
    `shell.CurrentDirectory = "${vbsEscape(runner.storeRoot)}"`,
    `shell.Run "${vbsEscape(command)}", 0, False`,
    "",
  ].join("\r\n");
  writeFileSync(vbsPath, vbs, "utf8");

  const result = spawnSync("wscript.exe", ["//B", vbsPath], {
    stdio: "ignore",
    windowsHide: true,
    shell: false,
  });

  if (result.status !== 0) {
    throw new Error("failed to start detached store via wscript");
  }
}

export function spawnUvicornForeground(
  runner: PythonRunner,
  host: string,
  port: number,
  dbPath: string,
): ChildProcess {
  const args = [...runner.prefixArgs, ...buildUvicornArgs(host, port)];
  const shell =
    runner.command === "uv" ||
    runner.command === "python" ||
    runner.command === "python3";
  const spawnOptions: SpawnOptions = {
    cwd: runner.storeRoot,
    env: { ...process.env, PROVENA_DB_PATH: dbPath },
    stdio: "inherit",
    shell,
  };
  return spawn(runner.command, args, spawnOptions);
}

export function spawnUvicornDetachedUnix(
  runner: PythonRunner,
  host: string,
  port: number,
  dbPath: string,
): number {
  const args = [...runner.prefixArgs, ...buildUvicornArgs(host, port)];
  const shell =
    runner.command === "uv" ||
    runner.command === "python" ||
    runner.command === "python3";
  const child = spawn(runner.command, args, {
    cwd: runner.storeRoot,
    env: { ...process.env, PROVENA_DB_PATH: dbPath },
    detached: true,
    stdio: "ignore",
    shell,
  });
  if (!child.pid) {
    throw new Error("failed to start uvicorn");
  }
  child.unref();
  return child.pid;
}