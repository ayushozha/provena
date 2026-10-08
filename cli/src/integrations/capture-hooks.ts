import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { lstatSync, realpathSync } from "node:fs";
import { open, opendir, mkdir, rename, rm, lstat } from "node:fs/promises";
import type { Dirent } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { z } from "zod";
import { getGitRoot } from "../config.js";
import { withRepoMemoryLock } from "../brain/lock.js";
import { canonicalJson, normalizeRepoPath } from "../brain/utils.js";
import { assertSafeRepoPath } from "../security/paths.js";

export type CaptureProvider = "codex" | "claude";
export const CAPTURE_INSTALLATION_PATH = ".provena/cache/capture-hooks.json";
const RUNTIME = ".provena/runtime/node_modules/@provena/cli/dist/cli.js";
const CONFIG_BYTES = 262_144;
const hookSchema = z.object({
  type: z.literal("command"), command: z.string().max(8_192),
  args: z.array(z.string().max(4_096)).max(16).optional(), timeout: z.literal(45),
}).strict();
const groupSchema = z.object({ matcher: z.literal(".*"), hooks: z.array(hookSchema).length(1) }).strict();
const installationSchema = z.object({
  schemaVersion: z.literal(1), providers: z.object({
    codex: z.array(z.object({ event: z.literal("PostToolUse"), group: groupSchema }).strict()).max(1).optional(),
    claude: z.array(z.object({ event: z.enum(["PostToolUse", "PostToolUseFailure"]), group: groupSchema }).strict()).max(2).optional(),
  }).strict(),
}).strict();
type InstallationState = z.infer<typeof installationSchema>;
type ManagedEntry = NonNullable<InstallationState["providers"]["claude"]>[number];
export interface CaptureHookConfigResult {
  provider: CaptureProvider;
  path: string;
  action: "created" | "updated" | "unchanged" | "skipped";
  reason?: string;
  instruction: string;
}

/** Unlike getGitRoot's convenience fallback, capture requires a real, exact checkout root. */
export function captureRepoRoot(value: string): string {
  if (!isAbsolute(value)) throw new Error("capture requires an absolute repository root");
  const root = resolve(value);
  try {
    if (execFileSync("git", ["rev-parse", "--is-inside-work-tree"], { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim() !== "true" ||
        realpathSync.native(getGitRoot(root)) !== realpathSync.native(root) || lstatSync(root).isSymbolicLink()) throw new Error();
    assertSafeRepoPath(root, root);
  } catch { throw new Error("capture requires the exact root of a Git working tree"); }
  return root;
}

/** Bound allocation as well as the parsed file size; never follow a managed symlink. */
export async function readCaptureFile(root: string, path: string, maxBytes: number): Promise<Buffer> {
  assertSafeRepoPath(root, path);
  assertPrivateCaptureTarget(root, path);
  const handle = await open(path, "r");
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size > maxBytes) throw new Error("capture file exceeds its byte limit or is not a file");
    const bytes = Buffer.alloc(maxBytes + 1);
    let size = 0;
    while (size < bytes.length) {
      const result = await handle.read(bytes, size, bytes.length - size, null);
      if (!result.bytesRead) break;
      size += result.bytesRead;
    }
    if (size > maxBytes) throw new Error("capture file exceeds its byte limit");
    assertSafeRepoPath(root, path);
    return bytes.subarray(0, size);
  } finally { await handle.close(); }
}

/** Private local observations and consent must never enter the repository ledger or Git. */
export function assertCaptureIgnored(root: string): void {
  try {
    const tracked = execFileSync("git", ["ls-files", "--", ".provena/cache/episodes", CAPTURE_INSTALLATION_PATH], { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
    if (tracked.trim()) throw new Error();
    for (const path of [".provena/cache/episodes/observation.json", CAPTURE_INSTALLATION_PATH]) {
      execFileSync("git", ["check-ignore", "--no-index", "--quiet", "--", path], { cwd: root, stdio: "ignore" });
    }
  } catch { throw new Error("capture cache must be ignored and untracked; initialize Provena or add local cache ignore rules first"); }
}

function assertPrivateCaptureTarget(root: string, path: string): void {
  const relative = normalizeRepoPath(root, path);
  if (!relative.startsWith(".provena/cache/")) return;
  try {
    const tracked = execFileSync("git", ["ls-files", "--", relative], { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
    if (tracked.trim()) throw new Error();
    execFileSync("git", ["check-ignore", "--no-index", "--quiet", "--", relative], { cwd: root, stdio: "ignore" });
  } catch { throw new Error("every capture cache file and temporary file must be ignored and untracked"); }
}

async function privateWrite(root: string, path: string, content: string | Buffer): Promise<void> {
  assertSafeRepoPath(root, dirname(path));
  assertSafeRepoPath(root, path);
  const temporary = `${path}.capture-${process.pid}-${randomUUID()}.tmp`;
  assertSafeRepoPath(root, temporary);
  assertPrivateCaptureTarget(root, path);
  assertPrivateCaptureTarget(root, temporary);
  await mkdir(dirname(path), { recursive: true });
  try {
    const handle = await open(temporary, "wx", 0o600);
    try { await handle.writeFile(content); await handle.sync(); } finally { await handle.close(); }
    assertSafeRepoPath(root, path);
    assertPrivateCaptureTarget(root, path);
    assertPrivateCaptureTarget(root, temporary);
    await rename(temporary, path);
  } finally {
    assertSafeRepoPath(root, temporary);
    await rm(temporary, { force: true });
  }
}
export { privateWrite as writeCaptureFile };

/** Publish the two local draft views together, restoring original bytes on a failed second rename. */
export async function writeCaptureDraftPair(root: string, wrapperPath: string, wrapper: string, candidatePath: string, candidate: string): Promise<void> {
  const relative = normalizeRepoPath(root, wrapperPath);
  if (!/^\.provena\/cache\/episodes\/drafts\/[a-f0-9]{64}\.json$/.test(relative) ||
      normalizeRepoPath(root, candidatePath) !== relative.replace(/\.json$/, ".candidate.json")) throw new Error("invalid capture draft pair paths");
  const maximum = 80_000;
  const plans = [
    { path: wrapperPath, bytes: Buffer.from(wrapper) }, { path: candidatePath, bytes: Buffer.from(candidate) },
  ].map((item) => ({ ...item, temporary: `${item.path}.capture-${process.pid}-${randomUUID()}.tmp`, previous: undefined as Buffer | undefined, identity: undefined as { dev: bigint; ino: bigint } | undefined, staged: false, published: false }));
  const guard = (path: string) => { assertSafeRepoPath(root, path); assertPrivateCaptureTarget(root, path); };
  const readPrevious = async (path: string): Promise<Buffer | undefined> => {
    try { return await readCaptureFile(root, path, maximum); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined; throw error; }
  };
  // Preflight every actual destination and nonce before persisting either payload.
  for (const plan of plans) {
    if (plan.bytes.length > maximum) throw new Error("capture draft pair exceeds its byte limit");
    guard(plan.path); guard(plan.temporary);
    plan.previous = await readPrevious(plan.path);
  }
  let failure: unknown;
  let recoveryFailed = false;
  try {
    for (const plan of plans) {
      await mkdir(dirname(plan.path), { recursive: true });
      guard(plan.path); guard(plan.temporary);
      const handle = await open(plan.temporary, "wx", 0o600);
      plan.staged = true;
      try {
        plan.identity = await handle.stat({ bigint: true });
        await handle.writeFile(plan.bytes); await handle.sync();
      } finally { await handle.close(); }
    }
    // Check both destinations before the first publish, then immediately before each rename.
    for (const plan of plans) { guard(plan.path); guard(plan.temporary); }
    for (const plan of plans) {
      guard(plan.path); guard(plan.temporary);
      const current = await readPrevious(plan.path);
      if (Boolean(current) !== Boolean(plan.previous) || current && !current.equals(plan.previous!)) throw new Error("capture draft destination changed during write");
      await rename(plan.temporary, plan.path);
      plan.staged = false;
      plan.published = true;
    }
  } catch (error) {
    failure = error;
    for (const plan of [...plans].reverse().filter((item) => item.published)) {
      try {
        guard(plan.path);
        const current = await readPrevious(plan.path);
        if (!current?.equals(plan.bytes)) throw new Error("capture draft destination changed before rollback");
        if (plan.previous) await privateWrite(root, plan.path, plan.previous);
        else { guard(plan.path); await rm(plan.path); }
      } catch { recoveryFailed = true; }
    }
  } finally {
    for (const plan of plans.filter((item) => item.staged)) {
      try {
        // Ignore changes must not strand our private payload; never delete a
        // replacement file or follow a path that became unsafe after staging.
        assertSafeRepoPath(root, plan.temporary);
        const current = await lstat(plan.temporary, { bigint: true });
        if (!current.isFile() || current.dev !== plan.identity?.dev || current.ino !== plan.identity.ino) throw new Error("capture temporary ownership changed");
        assertSafeRepoPath(root, plan.temporary);
        await rm(plan.temporary);
      }
      catch { recoveryFailed = true; }
    }
  }
  if (recoveryFailed) throw new Error("capture draft pair failed; local draft files require review");
  if (failure) throw new Error("capture draft pair was not written; previous files were preserved");
}

export async function readCaptureDirectory(root: string, directory: string, maxEntries: number): Promise<Dirent[]> {
  assertSafeRepoPath(root, directory);
  const entries: Dirent[] = [];
  try {
    const handle = await opendir(directory);
    for await (const entry of handle) {
      if (entries.length >= maxEntries) throw new Error("capture directory exceeds its entry limit");
      entries.push(entry);
    }
  } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  return entries;
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function configPath(provider: CaptureProvider): string {
  return provider === "codex" ? ".codex/hooks.json" : ".claude/settings.local.json";
}
async function readConfig(root: string, provider: CaptureProvider): Promise<{ data: Record<string, unknown>; raw: string }> {
  let raw = "";
  try { raw = new TextDecoder("utf-8", { fatal: true }).decode(await readCaptureFile(root, join(root, configPath(provider)), CONFIG_BYTES)); }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  let data: unknown;
  try { data = raw.trim() ? JSON.parse(raw) : {}; } catch { throw new Error("cannot merge invalid agent hook JSON"); }
  if (!object(data) || (data.hooks !== undefined && !object(data.hooks))) throw new Error("agent hook config must contain a hooks object");
  const hooks = data.hooks ?? {};
  for (const value of Object.values(hooks)) if (!Array.isArray(value)) throw new Error("agent hook event entries must be arrays");
  return { data, raw };
}
export async function readCaptureInstallations(root: string): Promise<InstallationState> {
  try {
    const raw = await readCaptureFile(root, join(root, CAPTURE_INSTALLATION_PATH), CONFIG_BYTES);
    const result = installationSchema.safeParse(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw)));
    if (!result.success) throw new Error("invalid local capture installation state");
    return result.data;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return { schemaVersion: 1, providers: {} };
    throw error;
  }
}
export async function assertCaptureEnabled(root: string, provider: CaptureProvider): Promise<void> {
  assertCaptureIgnored(root);
  const owned = (await readCaptureInstallations(root)).providers[provider];
  const { data } = await readConfig(root, provider);
  const hooks = (data.hooks ?? {}) as Record<string, unknown[]>;
  if (!owned?.length || owned.some(({ event, group }) => !hooks[event]?.some((item) => canonicalJson(item) === canonicalJson(group)))) {
    throw new Error("capture is not installed for this provider or its managed hooks have changed");
  }
  if (provider === "claude" && data.disableAllHooks === true) throw new Error("agent hooks are disabled in local settings");
}

function managedEntries(root: string, provider: CaptureProvider): ManagedEntry[] {
  const runtime = join(root, ...RUNTIME.split("/"));
  assertSafeRepoPath(root, runtime);
  // Codex uses shell commands. A deliberately restricted alphabet makes double quoting
  // safe under POSIX sh, PowerShell and cmd without interpolating hook input or env vars.
  // Windows runner temp paths can use 8.3 names such as RUNNER~1. A tilde
  // inside these double-quoted paths has no expansion in the supported shells.
  if (![root, runtime].every((path) => /^[A-Za-z0-9 _./:@\\~-]+$/.test(path))) {
    throw new Error("this repository path cannot be represented safely by the supported hook command; capture installation was skipped");
  }
  const hook = provider === "claude"
    ? { type: "command" as const, command: "node", args: [runtime, "capture", "hook", "--provider", provider, "--root", root], timeout: 45 as const }
    : { type: "command" as const, command: `node "${runtime}" capture hook --provider codex --root "${root}"`, timeout: 45 as const };
  return (provider === "claude" ? ["PostToolUse", "PostToolUseFailure"] as const : ["PostToolUse"] as const)
    .map((event) => ({ event, group: { matcher: ".*" as const, hooks: [hook] } }));
}

async function configure(value: string, provider: CaptureProvider, install: boolean): Promise<CaptureHookConfigResult> {
  const result: CaptureHookConfigResult = {
    provider, path: configPath(provider), action: "skipped",
    instruction: provider === "codex" ? "Review and trust the exact hook definition in Codex /hooks; installation does not bypass trust or enable disabled hooks." : "Review local Claude hooks with /hooks. Managed policy or disabled hooks can prevent delivery.",
  };
  try {
    if (provider !== "codex" && provider !== "claude") throw new Error("unsupported capture provider");
    const root = captureRepoRoot(value);
    assertCaptureIgnored(root);
    return await withRepoMemoryLock(root, async () => {
      const state = await readCaptureInstallations(root);
      const old = state.providers[provider] ?? [];
      const { data, raw } = await readConfig(root, provider);
      const hooks = { ...(data.hooks ?? {}) as Record<string, unknown[]> };
      if (install) {
        await readCaptureFile(root, join(root, ...RUNTIME.split("/")), 2_097_152);
        if (provider === "claude" && data.disableAllHooks === true) throw new Error("local settings disable hooks; preserved without enabling capture");
        if (old.some(({ event, group }) => !hooks[event]?.some((item) => canonicalJson(item) === canonicalJson(group)))) {
          throw new Error("a managed capture hook was removed or edited; preserved for manual review");
        }
      }
      for (const { event, group } of old) {
        const existing = [...(hooks[event] ?? [])];
        const index = existing.findIndex((item) => canonicalJson(item) === canonicalJson(group));
        if (index >= 0) existing.splice(index, 1);
        hooks[event] = existing;
      }
      const next = install ? managedEntries(root, provider) : [];
      for (const { event, group } of next) {
        if (hooks[event]?.some((item) => canonicalJson(item) === canonicalJson(group))) throw new Error("an identical user-owned hook already exists; preserved without claiming it");
        hooks[event] = [...(hooks[event] ?? []), group];
      }
      if (!install && !old.length) return { ...result, action: "unchanged" };
      for (const event of Object.keys(hooks)) if (!hooks[event]!.length) delete hooks[event];
      const updated = { ...data, hooks };
      const text = `${JSON.stringify(updated, null, 2)}\n`;
      if (Buffer.byteLength(text) > CONFIG_BYTES) throw new Error("merged hook config exceeds its byte limit");
      if (install) {
        if (provider === "codex") state.providers.codex = next as NonNullable<InstallationState["providers"]["codex"]>;
        else state.providers.claude = next;
      } else delete state.providers[provider];
      // Record exact ownership first; interruption before the config write leaves capture
      // disabled rather than allowing an unowned executable definition to ingest events.
      await privateWrite(root, join(root, CAPTURE_INSTALLATION_PATH), canonicalJson(state));
      if (text !== raw) await privateWrite(root, join(root, configPath(provider)), text);
      return { ...result, action: text === raw ? "unchanged" : raw ? "updated" : "created" };
    });
  } catch (error) {
    const safeReasons = [
      "unsupported capture provider", "capture requires an absolute repository root", "capture requires the exact root of a Git working tree",
      "capture cache must be ignored and untracked; initialize Provena or add local cache ignore rules first", "every capture cache file and temporary file must be ignored and untracked",
      "this repository path cannot be represented safely by the supported hook command; capture installation was skipped",
      "cannot merge invalid agent hook JSON", "agent hook config must contain a hooks object", "agent hook event entries must be arrays",
      "local settings disable hooks; preserved without enabling capture", "a managed capture hook was removed or edited; preserved for manual review",
      "an identical user-owned hook already exists; preserved without claiming it", "merged hook config exceeds its byte limit",
    ];
    return { ...result, reason: error instanceof Error && safeReasons.includes(error.message) ? error.message : "Capture setup was skipped: check the runtime, ignored cache, safe repository path, valid hook JSON, and existing hook ownership. Existing trust and hook settings were preserved." };
  }
}
export const installCaptureHooks = (root: string, provider: CaptureProvider): Promise<CaptureHookConfigResult> => configure(root, provider, true);
export const uninstallCaptureHooks = (root: string, provider: CaptureProvider): Promise<CaptureHookConfigResult> => configure(root, provider, false);
