import { isAbsolute, join, resolve } from "node:path";
import { realpathSync } from "node:fs";
import { z } from "zod";
import { getGitRoot } from "../config.js";
import { withRepoMemoryLock } from "../brain/lock.js";
import { canonicalJson, normalizeRepoPath, sha256 } from "../brain/utils.js";
import { isDeniedSecretPath } from "../indexer/discover.js";
import { assertNoSecretMaterial } from "../security/memory.js";
import { assertSafeRepoPath } from "../security/paths.js";
import { learnProcedureInputSchema, type LearnProcedureInput } from "../procedures/index.js";
import {
  assertCaptureEnabled, assertCaptureIgnored, captureRepoRoot, readCaptureDirectory, readCaptureFile, writeCaptureDraftPair, writeCaptureFile,
  type CaptureProvider,
} from "../integrations/capture-hooks.js";

export const CAPTURE_DIRECTORY = ".provena/cache/episodes";
export const CAPTURE_LIMITS = Object.freeze({
  payloadBytes: 262_144, depth: 8, entries: 128, episodeBytes: 262_144,
  observationsPerEpisode: 128, episodes: 32, totalEpisodeBytes: 2_097_152,
  drafts: 32, draftBytes: 80_000, sourceBytes: 1_048_576,
});
const id = z.string().regex(/^[a-f0-9]{64}$/);
const pathSchema = z.string().min(1).max(2_048);
const issueSchema = z.enum([
  "completion-order-is-not-causal-order", "hook-coverage-is-partial", "arguments-redacted",
  "unknown-tool-status", "missing-tool-input", "source-unavailable", "interrupted-tool",
]);
const sourceSchema = z.object({
  path: pathSchema, blob: z.string().regex(/^sha256-lf:[a-f0-9]{64}$/).optional(),
  fingerprintTiming: z.literal("post-tool-capture"),
}).strict();
const ARGUMENT_KEYS = new Set(["command", "workdir", "file_path", "path", "offset", "limit", "start_line", "end_line"]);
const observationSchema = z.object({
  id, recordedAt: z.iso.datetime(), event: z.enum(["PostToolUse", "PostToolUseFailure"]),
  tool: z.string().regex(/^[A-Za-z0-9_.:-]{1,128}$/),
  workingDirectory: pathSchema,
  args: z.record(z.string(), z.json()).refine((args) => Object.keys(args).every((key) => ARGUMENT_KEYS.has(key))), status: z.enum(["success", "failure", "unknown"]),
  exitCode: z.number().int().min(-2_147_483_648).max(2_147_483_647).optional(),
  durationMs: z.number().int().min(0).max(86_400_000).optional(),
  turnId: id.optional(), issues: z.array(issueSchema).max(8), sources: z.array(sourceSchema).max(8),
}).strict();
const episodeSchema = z.object({
  schemaVersion: z.literal(1), id, provider: z.enum(["codex", "claude"]),
  createdAt: z.iso.datetime(), updatedAt: z.iso.datetime(),
  complete: z.literal(false), ordering: z.literal("completion-only"), taskOutcome: z.literal("unknown"),
  quotaReached: z.boolean(), observations: z.array(observationSchema).max(CAPTURE_LIMITS.observationsPerEpisode),
}).strict();
export type CapturedObservation = z.infer<typeof observationSchema>;
export type CapturedEpisode = z.infer<typeof episodeSchema>;
export interface CaptureWriteResult { episodeId?: string; observationId?: string; duplicate: boolean; skipped: boolean }
export interface CapturedEpisodeSummary {
  id: string; provider: CaptureProvider; observations: number;
  statuses: { success: number; failure: number; unknown: number };
  updatedAt: string; complete: false; quotaReached: boolean; redacted: number;
  calls: { id: string; tool: string; status: CapturedObservation["status"]; issues: CapturedObservation["issues"] }[];
}
export interface DraftCaptureInput {
  episodeId: string; goal: string; sources: string[]; title?: string; observationIds?: string[];
}
export interface CaptureDraftResult {
  episodeId: string; draftPath: string; candidatePath: string; observations: number;
  taskOutcome: "unknown"; reviewRequired: true; complete: false; warnings: string[];
}

export function assertCaptureJsonBounds(value: unknown, depth = 0): void {
  if (depth > CAPTURE_LIMITS.depth) throw new Error("capture payload exceeds its nesting limit");
  if (!value || typeof value !== "object") return;
  const values = Array.isArray(value) ? value : Object.values(value);
  if (values.length > CAPTURE_LIMITS.entries) throw new Error("capture payload exceeds its entry limit");
  for (const item of values) assertCaptureJsonBounds(item, depth + 1);
}
export function parseCapturePayload(bytes: Uint8Array): unknown {
  if (bytes.byteLength > CAPTURE_LIMITS.payloadBytes) throw new Error("capture payload exceeds its byte limit");
  let value: unknown;
  try { value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)); }
  catch { throw new Error("invalid capture JSON payload"); }
  assertCaptureJsonBounds(value);
  return value;
}
const object = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === "object" && !Array.isArray(value);
function nativeIdentifier(value: unknown): string {
  if (typeof value !== "string" || !/^[A-Za-z0-9_.:-]{1,256}$/.test(value)) throw new Error("invalid native capture identifier");
  return value;
}
function safePath(root: string, value: unknown, cwd = root): string {
  if (typeof value !== "string" || !value.length || value.length > 2_048 || /[\x00-\x1f\x7f]/.test(value)) throw new Error("invalid capture file reference");
  // Provider-native absolute paths are accepted only inside this pinned checkout.
  const path = normalizeRepoPath(root, resolve(cwd, value));
  if (path === "." || /(?:^|\/)(?:\.git|\.provena|\.codex|\.claude|\.ssh|\.gnupg)(?:\/|$)/.test(path) || isDeniedSecretPath(path)) throw new Error("capture file reference is private or unsafe");
  assertSafeRepoPath(root, join(root, path));
  assertNoSecretMaterial(path);
  return path;
}
function validateCwd(root: string, value: unknown): string {
  if (typeof value !== "string" || !isAbsolute(value)) throw new Error("capture cwd must identify this repository");
  const cwd = resolve(value);
  assertSafeRepoPath(root, cwd);
  if (realpathSync.native(getGitRoot(cwd)) !== realpathSync.native(root)) throw new Error("capture cwd belongs to another checkout");
  return cwd;
}
async function sourceFingerprint(root: string, path: string): Promise<string> {
  const bytes = await readCaptureFile(root, join(root, path), CAPTURE_LIMITS.sourceBytes);
  let text: string;
  try { text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes).replace(/\r\n?/g, "\n"); }
  catch { throw new Error("capture source must be valid UTF-8 text"); }
  return `sha256-lf:${sha256(Buffer.from(text, "utf8"))}`;
}

/** Retain only a deliberately small command language; do not parse or store arbitrary shell code. */
function safeCommand(root: string, cwd: string, value: unknown): string | undefined {
  if (typeof value !== "string" || value.length > 2_048) return undefined;
  const command = value.trim().replace(/ +/g, " ");
  try { assertNoSecretMaterial(command); } catch { return undefined; }
  if (/^(?:npm|pnpm|yarn|bun) (?:test|(?:run|run-script) [A-Za-z0-9_:-]+)$/.test(command) ||
      /^(?:cargo (?:test|check|build)|git (?:diff --check|status --short))$/.test(command)) return command;
  if (/^(?:python3?|py) -m pytest(?: [A-Za-z0-9_./-]+)?$/.test(command)) {
    const path = command.split(" ")[3];
    if (path) safePath(root, path, cwd);
    return command;
  }
  if (/^go test \.[A-Za-z0-9_./-]*$/.test(command)) {
    const path = command.slice("go test ".length).replace(/\/\.\.\.$/, "");
    if (path !== "." && path !== "...") safePath(root, path, cwd);
    return command;
  }
  return undefined;
}
function ownCall(tool: string, input: unknown): boolean {
  if (/^(?:mcp__.*__)?provena_/i.test(tool)) return true;
  if (!object(input)) return false;
  const command = input.command ?? input.cmd;
  return typeof command === "string" && /(?:\bprovena\b|@provena[\/\\]cli|[\/\\]@provena[\/\\]|\.provena[\/\\]runtime)/i.test(command);
}
function toolStatus(provider: CaptureProvider, payload: Record<string, unknown>): Pick<CapturedObservation, "status" | "exitCode"> {
  if (payload.hook_event_name === "PostToolUseFailure") {
    const match = typeof payload.error === "string" ? /^Exit code (-?\d+)\r?(?:\n|$)/.exec(payload.error) : null;
    const exitCode = match ? Number(match[1]) : undefined;
    return { status: "failure", ...(Number.isInteger(exitCode) && Math.abs(exitCode!) <= 2_147_483_647 ? { exitCode } : {}) };
  }
  const response = payload.tool_response;
  if (object(response)) {
    const exitCode = response.exit_code ?? response.exitCode;
    const code = typeof exitCode === "number" && Number.isInteger(exitCode) && Math.abs(exitCode) <= 2_147_483_647 ? { exitCode } : {};
    if (payload.is_interrupt === true || response.interrupted === true || response.isError === true) return { status: "failure", ...code };
    if (code.exitCode !== undefined) return { status: code.exitCode === 0 ? "success" : "failure", ...code };
    if (response.isError === false) return { status: "success" };
  }
  if (payload.is_interrupt === true) return { status: "failure" };
  return { status: provider === "claude" ? "success" : "unknown" };
}
async function normalize(root: string, provider: CaptureProvider, value: unknown): Promise<{ episodeId: string; observation?: CapturedObservation }> {
  assertCaptureJsonBounds(value);
  const bytes = Buffer.byteLength(JSON.stringify(value) ?? "");
  if (bytes > CAPTURE_LIMITS.payloadBytes || !object(value)) throw new Error("invalid or oversized capture payload");
  const cwd = validateCwd(root, value.cwd);
  const session = nativeIdentifier(value.session_id);
  const call = nativeIdentifier(value.tool_use_id);
  const event = value.hook_event_name;
  if (event !== "PostToolUse" && !(provider === "claude" && event === "PostToolUseFailure")) throw new Error("unsupported capture event");
  const tool = nativeIdentifier(value.tool_name);
  if (tool.length > 128) throw new Error("capture tool name exceeds its limit");
  assertNoSecretMaterial(tool);
  const episodeId = sha256(`${provider}\0${session}`);
  if (ownCall(tool, value.tool_input)) return { episodeId };
  const issues: CapturedObservation["issues"] = ["completion-order-is-not-causal-order", "hook-coverage-is-partial"];
  const args: CapturedObservation["args"] = {};
  const sources: CapturedObservation["sources"] = [];
  const input = value.tool_input;
  if (!object(input)) issues.push("missing-tool-input");
  else {
    const allowed = new Set<string>();
    if (["Bash", "PowerShell", "exec_command", "shell"].includes(tool)) {
      const commandCwd = input.workdir === undefined ? cwd : validateCwd(root, input.workdir);
      const command = safeCommand(root, commandCwd, input.command ?? input.cmd);
      if (command) { args.command = command; allowed.add("command"); allowed.add("cmd"); }
      else issues.push("arguments-redacted");
      if (input.workdir !== undefined) { args.workdir = normalizeRepoPath(root, commandCwd); allowed.add("workdir"); }
    }
    for (const key of ["file_path", "path"]) if (input[key] !== undefined) {
      const path = safePath(root, input[key], cwd);
      args[key] = path; allowed.add(key);
      if (!sources.some((source) => source.path === path)) {
        const source: CapturedObservation["sources"][number] = { path, fingerprintTiming: "post-tool-capture" };
        try { source.blob = await sourceFingerprint(root, path); } catch { issues.push("source-unavailable"); }
        sources.push(source);
      }
    }
    for (const key of ["offset", "limit", "start_line", "end_line"]) if (input[key] !== undefined && typeof input[key] === "number" && Number.isSafeInteger(input[key]) && (input[key] as number) >= 0 && (input[key] as number) <= 1_000_000) {
      args[key] = input[key] as number; allowed.add(key);
    }
    // Content, patches, descriptions, prompts, environment values, and arbitrary
    // MCP fields are intentionally neither retained nor reflected in diagnostics.
    if (Object.keys(input).some((key) => !allowed.has(key))) issues.push("arguments-redacted");
    if (!Object.keys(args).length && !issues.includes("arguments-redacted")) issues.push("missing-tool-input");
  }
  const status = toolStatus(provider, value);
  if (status.status === "unknown") issues.push("unknown-tool-status");
  if (value.is_interrupt === true || (object(value.tool_response) && value.tool_response.interrupted === true)) issues.push("interrupted-tool");
  const turn = provider === "codex" ? value.turn_id : value.prompt_id;
  const observation: CapturedObservation = {
    id: sha256(`${provider}\0${session}\0${call}`), recordedAt: new Date().toISOString(), event,
    tool, workingDirectory: normalizeRepoPath(root, cwd), args, ...status, issues: [...new Set(issues)], sources,
    ...(turn !== undefined ? { turnId: sha256(nativeIdentifier(turn)) } : {}),
    ...(typeof value.duration_ms === "number" && Number.isSafeInteger(value.duration_ms) && value.duration_ms >= 0 && value.duration_ms <= 86_400_000 ? { durationMs: value.duration_ms } : {}),
  };
  assertNoSecretMaterial(canonicalJson(observation));
  return { episodeId, observation: observationSchema.parse(observation) };
}

async function readEpisodes(root: string): Promise<{ episodes: CapturedEpisode[]; bytes: number }> {
  const directory = join(root, CAPTURE_DIRECTORY, "observations");
  assertSafeRepoPath(root, directory);
  const entries = await readCaptureDirectory(root, directory, CAPTURE_LIMITS.episodes);
  const episodes: CapturedEpisode[] = [];
  // Keep checkout validation local to this read. Many observations share one
  // directory, but a later operation must recheck changed Git boundaries.
  const validatedCwds = new Map<string, string>();
  const storedCwd = (value: unknown): string => {
    if (typeof value !== "string") throw new Error("invalid stored capture working directory");
    const cwd = resolve(root, value);
    assertSafeRepoPath(root, cwd);
    if (normalizeRepoPath(root, cwd) !== value) throw new Error("invalid stored capture working directory");
    const validated = validatedCwds.get(cwd) ?? validateCwd(root, cwd);
    validatedCwds.set(cwd, validated);
    return validated;
  };
  let bytes = 0;
  for (const entry of entries) {
    if (!entry.isFile() || !/^[a-f0-9]{64}\.json$/.test(entry.name)) throw new Error("unsafe or unexpected capture episode entry");
    const data = await readCaptureFile(root, join(directory, entry.name), CAPTURE_LIMITS.episodeBytes);
    bytes += data.length;
    if (bytes > CAPTURE_LIMITS.totalEpisodeBytes) throw new Error("capture episodes exceed the global byte limit");
    let parsed: unknown;
    try { parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(data)); }
    catch { throw new Error("invalid capture episode JSON"); }
    // Stored episodes can contain 128 observations; their enclosing array is bounded separately.
    assertCaptureJsonBounds(parsed);
    const result = episodeSchema.safeParse(parsed);
    if (!result.success || result.data.id !== entry.name.slice(0, -5)) throw new Error("invalid capture episode file");
    for (const observation of result.data.observations) {
      assertNoSecretMaterial(canonicalJson(observation));
      for (const source of observation.sources) safePath(root, source.path);
      const args = observation.args;
      const observedCwd = resolve(root, observation.workingDirectory);
      assertSafeRepoPath(root, observedCwd);
      if (normalizeRepoPath(root, observedCwd) !== observation.workingDirectory) throw new Error("invalid stored capture working directory");
      const cwd = args.workdir === undefined ? observedCwd : storedCwd(args.workdir);
      if (args.command !== undefined && safeCommand(root, cwd, args.command) !== args.command) throw new Error("invalid stored capture command");
      for (const key of ["file_path", "path"]) if (args[key] !== undefined && safePath(root, args[key]) !== args[key]) throw new Error("invalid stored capture file reference");
      for (const key of ["offset", "limit", "start_line", "end_line"]) if (args[key] !== undefined && (typeof args[key] !== "number" || !Number.isSafeInteger(args[key]) || (args[key] as number) < 0 || (args[key] as number) > 1_000_000)) throw new Error("invalid stored capture line range");
    }
    episodes.push(result.data);
  }
  return { episodes, bytes };
}

export async function captureHook(value: string, provider: CaptureProvider, payload: unknown): Promise<CaptureWriteResult> {
  const root = captureRepoRoot(value);
  if (provider !== "codex" && provider !== "claude") throw new Error("unsupported capture provider");
  await assertCaptureEnabled(root, provider);
  const { episodeId, observation } = await normalize(root, provider, payload);
  if (!observation) return { duplicate: false, skipped: true };
  return withRepoMemoryLock(root, async () => {
    await assertCaptureEnabled(root, provider);
    const stored = await readEpisodes(root);
    const current = stored.episodes.find((episode) => episode.id === episodeId);
    const previous = current?.observations.find((item) => item.id === observation.id);
    if (previous) {
      const semantic = ({ recordedAt: _, sources, ...rest }: CapturedObservation) => ({ ...rest, sources: sources.map(({ blob: _, ...source }) => source) });
      if (canonicalJson(semantic(previous)) !== canonicalJson(semantic(observation))) throw new Error("capture tool-call ID was reused with different filtered input or result");
      return { episodeId, observationId: observation.id, duplicate: true, skipped: false };
    }
    const path = join(root, CAPTURE_DIRECTORY, "observations", `${episodeId}.json`);
    const episode: CapturedEpisode = current ?? {
      schemaVersion: 1, id: episodeId, provider, createdAt: observation.recordedAt, updatedAt: observation.recordedAt,
      complete: false, ordering: "completion-only", taskOutcome: "unknown", quotaReached: false, observations: [],
    };
    const oldBytes = current ? Buffer.byteLength(canonicalJson(current)) : 0;
    const next = { ...episode, updatedAt: observation.recordedAt, observations: [...episode.observations, observation] };
    const text = canonicalJson(next);
    if ((!current && stored.episodes.length >= CAPTURE_LIMITS.episodes) || next.observations.length > CAPTURE_LIMITS.observationsPerEpisode ||
        Buffer.byteLength(text) > CAPTURE_LIMITS.episodeBytes || stored.bytes - oldBytes + Buffer.byteLength(text) > CAPTURE_LIMITS.totalEpisodeBytes) {
      if (current && !current.quotaReached) await writeCaptureFile(root, path, canonicalJson({ ...current, quotaReached: true }));
      throw new Error("capture accumulation limit reached; review or remove local episodes before recording more");
    }
    await writeCaptureFile(root, path, text);
    return { episodeId, observationId: observation.id, duplicate: false, skipped: false };
  });
}

export async function listCapturedEpisodes(value: string): Promise<CapturedEpisodeSummary[]> {
  const root = captureRepoRoot(value);
  assertCaptureIgnored(root);
  return withRepoMemoryLock(root, async () => (await readEpisodes(root)).episodes.map((episode) => ({
    id: episode.id, provider: episode.provider, observations: episode.observations.length,
    statuses: episode.observations.reduce((counts, observation) => ({ ...counts, [observation.status]: counts[observation.status] + 1 }), { success: 0, failure: 0, unknown: 0 }),
    updatedAt: episode.updatedAt, complete: false as const, quotaReached: episode.quotaReached,
    redacted: episode.observations.filter((item) => item.issues.includes("arguments-redacted") || item.issues.includes("missing-tool-input")).length,
    calls: episode.observations.map(({ id, tool, status, issues }) => ({ id, tool, status, issues })),
  })).sort((a, b) => a.id.localeCompare(b.id)));
}

export async function draftCapturedEpisode(value: string, input: DraftCaptureInput): Promise<CaptureDraftResult> {
  const root = captureRepoRoot(value);
  assertCaptureIgnored(root);
  if (!id.safeParse(input.episodeId).success || !Array.isArray(input.sources) || input.sources.length < 1 || input.sources.length > 32 ||
      typeof input.goal !== "string" || !input.goal.trim() || input.goal.length > 2_048 || (input.title !== undefined && (!input.title.trim() || input.title.length > 512))) throw new Error("draft requires an episode, explicit goal, and one to 32 sources");
  assertNoSecretMaterial(input.goal);
  if (input.title !== undefined) assertNoSecretMaterial(input.title);
  if (input.observationIds && (!input.observationIds.length || input.observationIds.length > 32 || input.observationIds.some((item) => !id.safeParse(item).success))) throw new Error("invalid draft observation selection");
  return withRepoMemoryLock(root, async () => {
    const episode = (await readEpisodes(root)).episodes.find((item) => item.id === input.episodeId);
    if (!episode) throw new Error("capture episode was not found");
    const selected = input.observationIds ? new Set(input.observationIds) : undefined;
    const observations = episode.observations.filter((item) => !selected || selected.has(item.id));
    if (!observations.length || observations.length > 32 || (selected && observations.length !== selected.size)) throw new Error("draft requires an explicit selection of one to 32 captured calls");
    const sources = [...new Set(input.sources.map((path) => {
      if (isAbsolute(path) || /^[A-Za-z]:/.test(path)) throw new Error("draft sources must be repository-relative");
      return safePath(root, path);
    }))];
    const sourceEvidence = await Promise.all(sources.map(async (path) => {
      const blob = await sourceFingerprint(root, path);
      const captured = observations.flatMap((item) => item.sources).filter((source) => source.path === path && source.blob).at(-1);
      return { path, blob, fingerprintTiming: "draft-time" as const,
        captureBlob: captured?.blob ?? null, changedSinceCapture: captured ? captured.blob !== blob : null };
    }));
    const warnings = [
      "Tool completion order is not causal order. Review the selected sequence and all omitted or redacted arguments.",
      "Native hooks provide partial coverage. This draft is incomplete and does not establish task success or tool permissions.",
      "Source fingerprints below describe draft time; learning fingerprints source again. Verify source and argument applicability before learning.",
    ];
    if (episode.quotaReached) warnings.push("The capture quota was reached; some later calls are missing.");
    if (sourceEvidence.some((source) => source.changedSinceCapture)) warnings.push("Selected source changed since its latest captured observation; historical steps need review against current source.");
    if (observations.some((item) => item.issues.includes("arguments-redacted") || item.issues.includes("missing-tool-input"))) warnings.push("Some captured calls have incomplete arguments. Fill or remove those steps after reviewing the failure and recovery history.");
    const goal = input.goal.trim();
    const title = input.title?.trim() ?? goal.slice(0, 512);
    const draftId = sha256(canonicalJson({ episode: episode.id, observations, goal, title, sourceEvidence }));
    const candidate: LearnProcedureInput = {
      episodeId: `capture:${draftId}`, sessionId: `capture:${episode.id}`, actor: `capture-${episode.provider}`,
      title, goal,
      triggers: [], prerequisites: [], sources: sources.map((path) => ({ path })), sensitivity: "internal",
      steps: observations.map((item) => ({ tool: item.tool, args: item.args, expected: `Observed tool status: ${item.status}; session cwd: ${item.workingDirectory}. ${item.issues.includes("arguments-redacted") || item.issues.includes("missing-tool-input") ? "Arguments are incomplete; reconstruct or remove this step during review." : "Review arguments and applicability."} Verify the task independently.` })),
      verification: [], recovery: `INCOMPLETE CAPTURE: outputs and content were omitted, completion order is not causal order, and hooks may miss calls. Captured tool results: ${observations.filter((item) => item.status === "failure").length} failures, ${observations.filter((item) => item.status === "unknown").length} unknown. Review failures, recovery and missing arguments; task outcome is unknown.`,
    };
    if (!learnProcedureInputSchema.safeParse(candidate).success) throw new Error("draft candidate exceeds the procedure schema limits");
    const draftPath = `${CAPTURE_DIRECTORY}/drafts/${draftId}.json`;
    const candidatePath = `${CAPTURE_DIRECTORY}/drafts/${draftId}.candidate.json`;
    const result: CaptureDraftResult = { episodeId: episode.id, draftPath, candidatePath, observations: observations.length, taskOutcome: "unknown", reviewRequired: true, complete: false, warnings };
    const draft = canonicalJson({ schemaVersion: 1, kind: "capture-review-draft", ...result, ordering: "completion-only", sourceEvidence, observations, candidate });
    const candidateText = canonicalJson(candidate);
    if (Buffer.byteLength(draft) > CAPTURE_LIMITS.draftBytes || Buffer.byteLength(candidateText) > CAPTURE_LIMITS.draftBytes) throw new Error("capture draft exceeds its byte limit; select fewer calls");
    assertNoSecretMaterial(draft);
    const directory = join(root, CAPTURE_DIRECTORY, "drafts");
    assertSafeRepoPath(root, directory);
    const draftEntries = await readCaptureDirectory(root, directory, CAPTURE_LIMITS.drafts * 2);
    const entries = draftEntries.map((entry) => entry.name);
    if (draftEntries.some((entry) => !entry.isFile() || !/^[a-f0-9]{64}(?:\.candidate)?\.json$/.test(entry.name))) throw new Error("invalid capture draft directory");
    for (const entry of draftEntries) await readCaptureFile(root, join(directory, entry.name), CAPTURE_LIMITS.draftBytes);
    if (!entries.includes(`${draftId}.json`) && entries.length >= CAPTURE_LIMITS.drafts * 2) throw new Error("capture draft accumulation limit reached");
    await writeCaptureDraftPair(root, join(root, draftPath), draft, join(root, candidatePath), candidateText);
    return result;
  });
}
