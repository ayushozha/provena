import { relative, resolve, sep } from "node:path";
import chokidar, { type FSWatcher } from "chokidar";
import { loadConfig, type ProvenaConfig } from "../config.js";
import { ProvenaClient } from "../client.js";
import {
  buildRepoIgnoreMatcher,
  isDeniedSecretPath,
  resolveRepoRoot,
} from "../indexer/discover.js";
import { runIndex } from "../indexer/run.js";

const DEFAULT_DEBOUNCE_MS = 500;

export interface WatchOptions {
  debounceMs: number;
  /** Polling interval; when set, native file watching is disabled. */
  intervalMs?: number;
}

function toPosixPath(filePath: string): string {
  return filePath.split(sep).join("/");
}

function hasFlag(args: string[], ...flags: string[]): boolean {
  return flags.some((flag) => args.includes(flag));
}

function optionValue(args: string[], flag: string): string | undefined {
  const index = args.indexOf(flag);
  if (index === -1) {
    return undefined;
  }
  const value = args[index + 1];
  if (!value || value.startsWith("-")) {
    return undefined;
  }
  return value;
}

/** Parse `--interval 30s` or `--interval 30` into milliseconds. */
export function parseIntervalMs(raw: string): number | null {
  const trimmed = raw.trim().toLowerCase();
  const match = /^(\d+(?:\.\d+)?)(ms|s|m)?$/.exec(trimmed);
  if (!match) {
    return null;
  }
  const amount = Number.parseFloat(match[1]!);
  if (!Number.isFinite(amount) || amount <= 0) {
    return null;
  }
  const unit = match[2] ?? "s";
  if (unit === "ms") {
    return Math.round(amount);
  }
  if (unit === "m") {
    return Math.round(amount * 60_000);
  }
  return Math.round(amount * 1000);
}

export function parseWatchArgs(args: string[]): WatchOptions | "help" | "error" {
  if (hasFlag(args, "--help", "-h")) {
    return "help";
  }

  const intervalRaw = optionValue(args, "--interval");
  let intervalMs: number | undefined;
  if (intervalRaw) {
    const parsed = parseIntervalMs(intervalRaw);
    if (parsed === null) {
      return "error";
    }
    intervalMs = parsed;
  }

  return {
    debounceMs: DEFAULT_DEBOUNCE_MS,
    intervalMs,
  };
}

export function formatWatchLog(message: string, at = new Date()): string {
  return `${at.toISOString()} watch: ${message}`;
}

export class DebouncedIndexFlush {
  private readonly pending = new Set<string>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private flushing = false;

  constructor(
    private readonly debounceMs: number,
    private readonly onFlush: (paths: string[]) => Promise<void>,
  ) {}

  noteChange(relativePath: string): void {
    this.pending.add(relativePath);
    if (this.timer) {
      clearTimeout(this.timer);
    }
    this.timer = setTimeout(() => {
      void this.flush();
    }, this.debounceMs);
  }

  async flush(): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.flushing || this.pending.size === 0) {
      return;
    }
    this.flushing = true;
    const paths = [...this.pending];
    this.pending.clear();
    try {
      await this.onFlush(paths);
    } finally {
      this.flushing = false;
      if (this.pending.size > 0) {
        this.timer = setTimeout(() => {
          void this.flush();
        }, this.debounceMs);
      }
    }
  }

  dispose(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.pending.clear();
  }
}

function isWatchIgnored(
  repoRoot: string,
  absolutePath: string,
  config: ProvenaConfig,
  gitIgnored: (relativePath: string) => boolean,
): boolean {
  const rel = toPosixPath(relative(repoRoot, absolutePath));
  if (!rel || rel === ".." || rel.startsWith("../")) {
    return true;
  }
  if (rel === ".provena" || rel.startsWith(".provena/")) {
    return true;
  }
  if (isDeniedSecretPath(rel)) {
    return true;
  }
  for (const pattern of config.index.exclude) {
    if (pattern.endsWith("/**")) {
      const prefix = pattern.slice(0, -3);
      if (rel === prefix || rel.startsWith(`${prefix}/`)) {
        return true;
      }
    }
  }
  return gitIgnored(rel);
}

async function runIncrementalFlush(
  config: ProvenaConfig,
  projectRoot: string,
  cwd: string,
  paths: string[],
): Promise<void> {
  const pathHint =
    paths.length === 1 ? paths[0]! : `${paths.length} paths`;
  console.log(formatWatchLog(`indexing after change (${pathHint})`));

  const result = await runIndex(config, projectRoot, { cwd });
  const { summary } = result;
  const delta =
    summary.filesAdded +
    summary.filesChanged +
    summary.filesRemoved;
  if (delta === 0 && summary.filesUnchanged > 0) {
    console.log(formatWatchLog("no file changes detected"));
  } else {
    console.log(
      formatWatchLog(
        `indexed ${summary.filesIndexed} file(s) (+${summary.filesAdded} ~${summary.filesChanged} -${summary.filesRemoved})`,
      ),
    );
  }
  if (result.exitCode !== 0) {
    console.error(formatWatchLog("incremental index finished with errors"));
  }
}

function watchPathFromEvent(
  repoRoot: string,
  absolutePath: string,
): string | null {
  const rel = toPosixPath(relative(repoRoot, absolutePath));
  if (!rel || rel === ".." || rel.startsWith("../")) {
    return null;
  }
  return rel;
}

async function startChokidarWatch(
  config: ProvenaConfig,
  projectRoot: string,
  repoRoot: string,
  options: WatchOptions,
): Promise<() => Promise<void>> {
  const gitIgnored = buildRepoIgnoreMatcher(repoRoot);
  const debouncer = new DebouncedIndexFlush(options.debounceMs, (paths) =>
    runIncrementalFlush(config, projectRoot, projectRoot, paths),
  );

  let watcher: FSWatcher | null = null;
  let retriedEmfile = false;

  const startWatcher = (): FSWatcher => {
    const instance = chokidar.watch(repoRoot, {
      ignored: (watchPath) =>
        isWatchIgnored(repoRoot, watchPath, config, gitIgnored),
      ignoreInitial: true,
      awaitWriteFinish: {
        stabilityThreshold: 100,
        pollInterval: 50,
      },
      persistent: true,
    });

    instance.on("add", (watchPath) => {
      const rel = watchPathFromEvent(repoRoot, watchPath);
      if (rel) {
        debouncer.noteChange(rel);
      }
    });
    instance.on("change", (watchPath) => {
      const rel = watchPathFromEvent(repoRoot, watchPath);
      if (rel) {
        debouncer.noteChange(rel);
      }
    });
    instance.on("unlink", (watchPath) => {
      const rel = watchPathFromEvent(repoRoot, watchPath);
      if (rel) {
        debouncer.noteChange(rel);
      }
    });
    instance.on("error", (error: unknown) => {
      const code =
        error && typeof error === "object" && "code" in error
          ? String((error as NodeJS.ErrnoException).code)
          : "";
      if (code === "EMFILE" && !retriedEmfile) {
        retriedEmfile = true;
        console.error(formatWatchLog("EMFILE: retrying watcher once in 1s"));
        instance.close().catch(() => undefined);
        setTimeout(() => {
          watcher = startWatcher();
        }, 1000);
        return;
      }
      console.error(
        formatWatchLog(
          `watcher error: ${error instanceof Error ? error.message : String(error)}`,
        ),
      );
    });

    return instance;
  };

  watcher = startWatcher();
  console.log(
    formatWatchLog(`watching ${repoRoot} (debounce ${options.debounceMs}ms)`),
  );

  return async () => {
    debouncer.dispose();
    if (watcher) {
      await watcher.close();
      watcher = null;
    }
  };
}

async function startPollingWatch(
  config: ProvenaConfig,
  projectRoot: string,
  intervalMs: number,
): Promise<() => Promise<void>> {
  console.log(formatWatchLog(`polling incremental index every ${intervalMs}ms`));

  const timer = setInterval(() => {
    void runIncrementalFlush(config, projectRoot, projectRoot, ["poll"]);
  }, intervalMs);

  return async () => {
    clearInterval(timer);
  };
}

export function printWatchHelp(): void {
  console.log("Usage: provena watch [options]");
  console.log("");
  console.log("Options:");
  console.log("  --interval <dur>  Polling fallback (e.g. 30s, 5000ms) instead of native watch");
  console.log("  --help, -h        Show this help");
  console.log("");
  console.log("Requires a running local store (`provena serve --detach`) and prior `provena index`.");
}

export async function runWatchCommand(args: string[]): Promise<number> {
  const parsed = parseWatchArgs(args);
  if (parsed === "help") {
    printWatchHelp();
    return 0;
  }
  if (parsed === "error") {
    console.error("provena watch: invalid --interval value (use e.g. 30s or 5000ms)");
    return 1;
  }

  const { config, projectRoot } = loadConfig();
  const repoRoot = resolve(resolveRepoRoot(projectRoot));

  const client = new ProvenaClient({ storeUrl: config.store_url });
  if (!(await client.healthz())) {
    console.error("provena watch: store unreachable; run `provena serve --detach` first");
    return 1;
  }

  const stop =
    parsed.intervalMs !== undefined
      ? await startPollingWatch(config, projectRoot, parsed.intervalMs)
      : await startChokidarWatch(config, projectRoot, repoRoot, parsed);

  let shuttingDown = false;
  const shutdown = async (signal: string) => {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    console.log(formatWatchLog(`shutting down (${signal})`));
    await stop();
    process.exit(0);
  };

  process.on("SIGINT", () => {
    void shutdown("SIGINT");
  });
  process.on("SIGTERM", () => {
    void shutdown("SIGTERM");
  });

  await new Promise<void>(() => {
    // Long-running until signal.
  });

  return 0;
}