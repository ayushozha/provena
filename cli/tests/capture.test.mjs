import assert from "node:assert/strict";
import childProcess, { spawn, spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { syncBuiltinESMExports } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import {
  CAPTURE_DIRECTORY, CAPTURE_LIMITS, captureHook, draftCapturedEpisode,
  listCapturedEpisodes, parseCapturePayload,
} from "../dist/capture/index.js";
import { installCaptureHooks, uninstallCaptureHooks } from "../dist/integrations/capture-hooks.js";
import { learnProcedure, approveProcedure, recallProcedures } from "../dist/procedures/index.js";
import { readMemoryLedgerSnapshot } from "../dist/brain/events.js";

const cli = fileURLToPath(new URL("../dist/cli.js", import.meta.url));
const handler = new URL("../dist/commands/capture.js", import.meta.url).href;
const sandbox = await mkdtemp(join(tmpdir(), "provena capture adapters-"));
const privateMarker = "PRIVATE_CAPTURE_CONTENT_93f35e";
const secret = "ghp_" + "Q".repeat(36);
async function fixture(name) {
  const root = join(sandbox, name);
  await mkdir(join(root, "src"), { recursive: true });
  const git = spawnSync("git", ["init", "-q"], { cwd: root, encoding: "utf8" });
  assert.equal(git.status, 0, git.stderr);
  await writeFile(join(root, ".gitignore"), ".provena/cache/\n.claude/settings.local.json\n");
  await writeFile(join(root, "src/token.js"), "export const tokenValid = false;\n");
  const runtime = join(root, ".provena/runtime/node_modules/@provena/cli/dist/cli.js");
  await mkdir(dirname(runtime), { recursive: true });
  await writeFile(runtime, `import { runCaptureCommand } from ${JSON.stringify(handler)};\nprocess.exitCode = await runCaptureCommand(process.argv.slice(3));\n`);
  return { root, runtime };
}
function payload(root, call, changes = {}) {
  return {
    session_id: "native-session-1", cwd: root, hook_event_name: "PostToolUse",
    transcript_path: `${sandbox}/DO_NOT_READ_PRIVATE_SESSION.jsonl`,
    model: privateMarker, permission_mode: "bypassPermissions", prompt: privateMarker,
    tool_name: "Bash", tool_use_id: call, tool_input: { command: "npm test" },
    tool_response: { stdout: privateMarker, stderr: secret, interrupted: false, isImage: false },
    ...changes,
  };
}
function cliRun(root, args, input) {
  return spawnSync(process.execPath, [cli, "capture", ...args], { cwd: root, encoding: "utf8", input, timeout: 20_000, maxBuffer: 1_048_576 });
}
async function hookProcess(root, args, input) {
  const child = spawn(process.execPath, [cli, "capture", "hook", ...args], { cwd: root, stdio: ["pipe", "pipe", "pipe"] });
  let stdout = "", stderr = "";
  child.stdout.setEncoding("utf8"); child.stderr.setEncoding("utf8");
  child.stdout.on("data", (text) => stdout += text);
  child.stderr.on("data", (text) => stderr += text);
  const closed = new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (code) => resolve({ code, stdout, stderr }));
  });
  child.stdin.on("error", () => {});
  child.stdin.end(input);
  const timer = setTimeout(() => child.kill(), 20_000);
  try { return await closed; } finally { clearTimeout(timer); }
}
async function readEpisode(root, id) {
  return JSON.parse(await readFile(join(root, CAPTURE_DIRECTORY, "observations", `${id}.json`), "utf8"));
}
async function executeInstalled(root, provider, input) {
  const config = JSON.parse(await readFile(join(root, provider === "codex" ? ".codex/hooks.json" : ".claude/settings.local.json"), "utf8"));
  const event = input.hook_event_name;
  const hook = config.hooks[event].at(-1).hooks[0];
  // Execute the exact installed hook definition. This proves adapter/config delivery,
  // not native engine emission or Codex's human trust gate.
  return hook.args
    ? spawnSync(hook.command, hook.args, { cwd: root, input: JSON.stringify(input), encoding: "utf8", timeout: 20_000 })
    : spawnSync(hook.command, { cwd: root, input: JSON.stringify(input), encoding: "utf8", shell: true, timeout: 20_000 });
}
try {
  const { root } = await fixture("main path");
  await mkdir(join(root, ".claude"));
  const userHook = { matcher: "Read", hooks: [{ type: "command", command: "echo user-owned" }] };
  const userSettings = { hooks: { PostToolUse: [userHook], Stop: [{ hooks: [{ type: "command", command: "echo stop" }] }] }, permissions: { deny: ["Bash(rm *)"] }, disableAllHooks: false };
  await writeFile(join(root, ".claude/settings.local.json"), JSON.stringify(userSettings));
  const installedClaude = await installCaptureHooks(root, "claude");
  assert.equal(installedClaude.action, "updated", installedClaude.reason);
  assert.equal((await installCaptureHooks(root, "claude")).action, "unchanged", "installation is idempotent");
  assert.equal((await installCaptureHooks(root, "codex")).action, "created");
  const config = JSON.parse(await readFile(join(root, ".claude/settings.local.json"), "utf8"));
  assert.deepEqual(config.hooks.PostToolUse[0], userHook);
  assert.deepEqual(config.permissions, userSettings.permissions);
  assert.equal(config.disableAllHooks, false);
  assert.equal(config.hooks.PostToolUseFailure.length, 1);
  assert.deepEqual(Object.keys(config).sort(), Object.keys(userSettings).sort(), "capture does not add permission or model controls");

  // A real failed check, code edit, and passing retry supply the observation receipts.
  await writeFile(join(root, "package.json"), JSON.stringify({ scripts: { test: "node check.mjs" } }));
  await writeFile(join(root, "check.mjs"), `import { readFileSync } from 'node:fs';\nconsole.log(${JSON.stringify(privateMarker)});\nprocess.exit(readFileSync('src/token.js','utf8').includes('true') ? 0 : 7);\n`);
  const runTest = () => process.platform === "win32"
    ? spawnSync("cmd.exe", ["/d", "/s", "/c", "npm test"], { cwd: root, encoding: "utf8", timeout: 20_000 })
    : spawnSync("npm", ["test"], { cwd: root, encoding: "utf8", timeout: 20_000 });
  const failed = runTest();
  assert.equal(failed.status, 7, failed.stderr);
  const first = await executeInstalled(root, "claude", payload(root, "failed-check", {
    hook_event_name: "PostToolUseFailure", error: `Exit code ${failed.status}\n${failed.stdout}\n${secret}`, duration_ms: 50,
    tool_input: { command: "npm test", env: { PRIVATE_VALUE: secret }, description: privateMarker },
  }));
  assert.equal(first.status, 0, first.stderr);
  assert.equal(first.stdout, "{}\n");
  assert.equal(first.stderr, "");
  await writeFile(join(root, "src/token.js"), "export const tokenValid = true;\n");
  const edit = await executeInstalled(root, "claude", payload(root, "repair-file", {
    tool_name: "Write", tool_input: { file_path: join(root, "src/token.js"), content: privateMarker + secret },
    tool_response: { filePath: join(root, "src/token.js"), type: "create" },
  }));
  assert.equal(edit.status, 0, edit.stderr);
  const success = runTest();
  assert.equal(success.status, 0, success.stderr);
  const retry = await executeInstalled(root, "claude", payload(root, "passing-retry", { tool_response: { stdout: success.stdout, stderr: "", interrupted: false, isImage: false } }));
  assert.equal(retry.status, 0, retry.stderr);
  const listed = await listCapturedEpisodes(root);
  assert.equal(listed.length, 1);
  assert.deepEqual(listed[0].statuses, { success: 2, failure: 1, unknown: 0 });
  assert.equal(listed[0].complete, false);
  assert.equal(listed[0].calls.length, 3, "call IDs permit explicit bounded draft selection");
  const episode = await readEpisode(root, listed[0].id);
  assert.equal(episode.taskOutcome, "unknown");
  assert.equal(episode.ordering, "completion-only");
  assert.equal(episode.observations[0].workingDirectory, ".");
  assert.equal(episode.observations[0].exitCode, 7);
  assert.deepEqual(episode.observations[0].args, { command: "npm test" });
  assert.deepEqual(episode.observations[1].args, { file_path: "src/token.js" });
  assert.equal(episode.observations[1].sources[0].fingerprintTiming, "post-tool-capture");
  assert.match(episode.observations[1].sources[0].blob, /^sha256-lf:[a-f0-9]{64}$/);
  assert(episode.observations[1].issues.includes("arguments-redacted"));
  const rawEpisode = JSON.stringify(episode);
  for (const forbidden of [privateMarker, secret, "DO_NOT_READ_PRIVATE_SESSION", "transcript_path", "permission_mode", "PRIVATE_VALUE", "tool_response", "content"]) assert(!rawEpisode.includes(forbidden), `must not persist ${forbidden}`);
  const beforeDraft = await readMemoryLedgerSnapshot(root);
  const result = await draftCapturedEpisode(root, { episodeId: episode.id, goal: "Repair token validity", sources: ["src/token.js"] });
  assert.equal(result.taskOutcome, "unknown");
  assert.equal(result.complete, false);
  assert.equal(result.reviewRequired, true);
  const draft = JSON.parse(await readFile(join(root, result.draftPath), "utf8"));
  const candidate = JSON.parse(await readFile(join(root, result.candidatePath), "utf8"));
  assert.deepEqual(candidate.verification, []);
  assert.match(candidate.recovery, /INCOMPLETE CAPTURE/);
  assert.match(candidate.steps[1].expected, /Arguments are incomplete/);
  assert.equal(draft.sourceEvidence[0].changedSinceCapture, false);
  assert.equal(draft.sourceEvidence[0].fingerprintTiming, "draft-time");
  assert.equal((await readMemoryLedgerSnapshot(root)).rawLedger, beforeDraft.rawLedger, "drafting never writes durable memory");
  assert.equal((await draftCapturedEpisode(root, { episodeId: episode.id, goal: "Repair token validity", sources: ["src/token.js"] })).draftPath, result.draftPath);
  const learned = await learnProcedure(root, candidate);
  assert.equal(learned.event.authority, "agent");
  assert.equal((await recallProcedures(root, "Repair token validity")).ready.length, 0);
  const anotherResult = await draftCapturedEpisode(root, { episodeId: episode.id, goal: "Validate corrected token checks", sources: ["src/token.js"], observationIds: [episode.observations[2].id] });
  const anotherCandidate = JSON.parse(await readFile(join(root, anotherResult.candidatePath), "utf8"));
  assert.notEqual(anotherCandidate.episodeId, candidate.episodeId, "distinct drafts in one native session require distinct learning identities");
  assert.equal(anotherCandidate.sessionId, candidate.sessionId);
  const anotherLearned = await learnProcedure(root, anotherCandidate);
  assert.notEqual(anotherLearned.event.id, learned.event.id);
  assert.equal((await learnProcedure(root, candidate)).duplicate, true, "same reviewed draft remains idempotent");
  const approved = await approveProcedure(root, { procedureId: learned.event.id, actor: "reviewer" });
  assert.equal((await recallProcedures(root, "Repair token validity")).ready.length, 0, "captured passing tools cannot create a successful approved-version outcome");
  assert.equal(approved.event.authority, "human");
  await writeFile(join(root, "src/token.js"), "export const tokenValid = 'changed after capture';\n");
  const changed = JSON.parse(await readFile(join(root, (await draftCapturedEpisode(root, { episodeId: episode.id, goal: "Review later change", sources: ["src/token.js"] })).draftPath), "utf8"));
  assert.equal(changed.sourceEvidence[0].changedSinceCapture, true);
  assert(changed.warnings.some((warning) => warning.includes("changed since")));

  const codex0 = await captureHook(root, "codex", payload(root, "code-zero", { turn_id: "turn-1", tool_response: { exit_code: 0, output: privateMarker } }));
  const codexFail = await captureHook(root, "codex", payload(root, "code-failed", { turn_id: "turn-1", tool_response: { exit_code: 2, output: privateMarker } }));
  await captureHook(root, "codex", payload(root, "opaque", { turn_id: "turn-1", tool_response: "This sounds successful but is only model-facing output." }));
  const codexEpisode = await readEpisode(root, codex0.episodeId);
  assert.deepEqual(codexEpisode.observations.map((call) => call.status), ["success", "failure", "unknown"]);
  assert.equal(codexEpisode.taskOutcome, "unknown");
  for (const [call, flags] of [["explicit-error", { tool_response: { exit_code: 0, isError: true } }], ["interrupted-response", { tool_response: { exit_code: 0, interrupted: true } }], ["interrupted-payload", { is_interrupt: true, tool_response: { exit_code: 0 } }]]) {
    const captured = await captureHook(root, "codex", payload(root, call, { session_id: "status-precedence", ...flags }));
    assert.equal((await readEpisode(root, captured.episodeId)).observations.at(-1).status, "failure", "explicit error/interruption dominates exit zero");
  }
  assert.equal((await captureHook(root, "codex", payload(root, "code-zero", { turn_id: "turn-1", tool_response: { exit_code: 0, output: "different discarded output" } }))).duplicate, true);
  await assert.rejects(captureHook(root, "codex", payload(root, "code-zero", { turn_id: "turn-1", tool_input: { command: "npm run build" }, tool_response: { exit_code: 0 } })), /reused/);
  assert.equal((await captureHook(root, "codex", payload(root, "own-cli", { tool_input: { command: "node .provena/runtime/node_modules/@provena/cli/dist/cli.js capture list" } }))).skipped, true);
  assert.equal((await captureHook(root, "codex", payload(root, "own-mcp", { tool_name: "mcp__repo_a__provena_procedure_learn" }))).skipped, true);
  const redacted = await captureHook(root, "codex", payload(root, "opaque-shell", { tool_input: { command: `echo ${secret} > src/token.js`, env: { SECRET: secret } } }));
  const redactedEpisode = await readEpisode(root, redacted.episodeId);
  assert.deepEqual(redactedEpisode.observations.at(-1).args, {});
  assert(!JSON.stringify(redactedEpisode).includes(secret));
  const patch = await captureHook(root, "codex", payload(root, "patch", { tool_name: "apply_patch", tool_input: { command: `*** Begin Patch\n${privateMarker}\n*** End Patch` } }));
  assert.deepEqual((await readEpisode(root, patch.episodeId)).observations.at(-1).args, {});

  const parallel = await Promise.all(Array.from({ length: 12 }, (_, i) => captureHook(root, "claude", payload(root, `concurrent-${i}`, { session_id: "parallel-session", tool_response: {}, ...(i % 3 === 0 ? { hook_event_name: "PostToolUseFailure", error: "Exit code 1\nfailed" } : {}) }))));
  assert.equal((await readEpisode(root, parallel[0].episodeId)).observations.length, 12);
  const duplicateResults = await Promise.all(Array.from({ length: 6 }, () => captureHook(root, "claude", payload(root, "same-parallel", { session_id: "parallel-session" }))));
  assert.equal(duplicateResults.filter((entry) => !entry.duplicate).length, 1);
  assert.equal((await readEpisode(root, parallel[0].episodeId)).observations.length, 13);

  await assert.rejects(captureHook(root, "claude", payload(root, "escape", { cwd: sandbox })), /outside|checkout|repository/);
  for (const path of ["../outside.txt", ".env", ".ssh/id_rsa", ".claude/settings.local.json"]) {
    await assert.rejects(captureHook(root, "claude", payload(root, `unsafe-${path.replace(/[^a-z]/g, "")}`, { tool_name: "Read", tool_input: { file_path: path } })), /private|unsafe|inside/);
  }
  const outside = join(sandbox, "outside");
  await mkdir(outside);
  await writeFile(join(outside, "private.txt"), privateMarker);
  await symlink(outside, join(root, "linked"), process.platform === "win32" ? "junction" : "dir");
  await assert.rejects(captureHook(root, "claude", payload(root, "junction", { tool_name: "Read", tool_input: { file_path: join(root, "linked/private.txt") } })), /symlink|junction|reparse/);
  const nested = join(root, "nested");
  await mkdir(nested);
  assert.equal(spawnSync("git", ["init", "-q"], { cwd: nested }).status, 0);
  await assert.rejects(captureHook(root, "claude", payload(root, "nested", { cwd: nested })), /another checkout/);
  assert.throws(() => parseCapturePayload(Buffer.from("{PRIVATE_CAPTURE_CONTENT")), /invalid capture JSON/);
  assert.throws(() => parseCapturePayload(Buffer.alloc(CAPTURE_LIMITS.payloadBytes + 1, 32)), /byte limit/);
  let deep = { ok: true }; for (let i = 0; i < 10; i++) deep = { next: deep };
  assert.throws(() => parseCapturePayload(Buffer.from(JSON.stringify(deep))), /nesting/);
  assert.throws(() => parseCapturePayload(Buffer.from(JSON.stringify(Array.from({ length: 129 }, () => 1)))), /entry limit/);
  await assert.rejects(draftCapturedEpisode(root, { episodeId: episode.id, goal: "", sources: ["src/token.js"] }), /explicit goal/);
  await assert.rejects(draftCapturedEpisode(root, { episodeId: episode.id, goal: "Repair", sources: [join(root, "src/token.js")] }), /repository-relative/);
  await assert.rejects(draftCapturedEpisode(root, { episodeId: episode.id, goal: secret, sources: ["src/token.js"] }), /credential/);

  const safeHook = cliRun(root, ["hook", "--provider", "claude", "--root", root], JSON.stringify(payload(root, "cli-hook")));
  assert.equal(safeHook.status, 0, safeHook.stderr);
  assert.equal(safeHook.stdout, "{}\n");
  const invalid = await hookProcess(root, ["--provider", "claude", "--root", root], `{${privateMarker}${secret}`);
  assert.equal(invalid.code, 0);
  assert.equal(invalid.stdout, "{}\n");
  assert.match(invalid.stderr, /^provena capture: observation not recorded;/);
  assert(!invalid.stderr.includes(privateMarker) && !invalid.stderr.includes(secret));
  const oversized = await hookProcess(root, ["--provider", "claude", "--root", root], " ".repeat(CAPTURE_LIMITS.payloadBytes + 16_384));
  assert.equal(oversized.code, 0);
  assert.equal(oversized.stdout, "{}\n");
  assert.equal(cliRun(root, ["hook", "--help"], "").stdout, "{}\n");
  assert.equal(cliRun(root, ["hook", "--provider", "claude"], JSON.stringify(payload(root, "unpinned"))).stdout, "{}\n");

  const edited = JSON.parse(await readFile(join(root, ".codex/hooks.json"), "utf8"));
  edited.hooks.PostToolUse[0].hooks[0].command = "echo changed-user-hook";
  await writeFile(join(root, ".codex/hooks.json"), JSON.stringify(edited));
  assert.equal((await installCaptureHooks(root, "codex")).action, "skipped");
  await assert.rejects(captureHook(root, "codex", payload(root, "changed-config")), /managed hooks have changed/);
  assert.equal((await uninstallCaptureHooks(root, "codex")).action, "updated");
  assert.deepEqual(JSON.parse(await readFile(join(root, ".codex/hooks.json"), "utf8")).hooks.PostToolUse, edited.hooks.PostToolUse, "uninstall preserves edited user-owned commands");
  assert.equal((await uninstallCaptureHooks(root, "claude")).action, "updated");
  assert.deepEqual(JSON.parse(await readFile(join(root, ".claude/settings.local.json"), "utf8")), userSettings, "uninstall restores unrelated settings and hooks");

  const shortName = await fixture("RUNNER~1");
  for (const provider of ["claude", "codex"]) {
    const installed = await installCaptureHooks(shortName.root, provider);
    assert.equal(installed.action, "created", installed.reason);
    const delivered = await executeInstalled(shortName.root, provider, payload(shortName.root, `short-name-${provider}`));
    assert.equal(delivered.status, 0, delivered.stderr);
    assert.equal(delivered.stdout, "{}\n");
  }
  assert.equal((await listCapturedEpisodes(shortName.root)).reduce((count, episode) => count + episode.observations, 0), 2);

  const unsafe = await fixture("unsafe & path");
  assert.equal((await installCaptureHooks(unsafe.root, "codex")).action, "skipped");
  assert.match((await installCaptureHooks(unsafe.root, "codex")).reason, /represented safely/);
  const disabled = await fixture("disabled hooks");
  await mkdir(join(disabled.root, ".claude"));
  await writeFile(join(disabled.root, ".claude/settings.local.json"), JSON.stringify({ disableAllHooks: true, hooks: {} }));
  assert.equal((await installCaptureHooks(disabled.root, "claude")).action, "skipped");
  assert.equal(JSON.parse(await readFile(join(disabled.root, ".claude/settings.local.json"), "utf8")).disableAllHooks, true);
  const forged = await fixture("forged cached content");
  await installCaptureHooks(forged.root, "claude");
  const forgedRecord = await captureHook(forged.root, "claude", payload(forged.root, "cache-entry"));
  const forgedEpisode = await readEpisode(forged.root, forgedRecord.episodeId);
  forgedEpisode.observations[0].args.content = privateMarker;
  await writeFile(join(forged.root, CAPTURE_DIRECTORY, "observations", `${forgedRecord.episodeId}.json`), JSON.stringify(forgedEpisode));
  await assert.rejects(listCapturedEpisodes(forged.root), /invalid capture episode/);
  const ignored = await fixture("not ignored");
  await writeFile(join(ignored.root, ".gitignore"), "");
  assert.equal((await installCaptureHooks(ignored.root, "claude")).action, "skipped");
  const selective = await fixture("selective ignore");
  await installCaptureHooks(selective.root, "claude");
  await writeFile(join(selective.root, ".gitignore"), ".provena/cache/capture-hooks.json\n.provena/cache/episodes/observation.json\n");
  await assert.rejects(captureHook(selective.root, "claude", payload(selective.root, "selective-private-record")), /every capture cache file/);
  await assert.rejects(readdir(join(selective.root, CAPTURE_DIRECTORY, "observations")), { code: "ENOENT" }, "no observation is persisted under a sentinel-only ignore");
  const temporary = await fixture("negated temp ignore");
  await installCaptureHooks(temporary.root, "claude");
  await writeFile(join(temporary.root, ".gitignore"), ".provena/cache/*\n!.provena/cache/episodes/\n.provena/cache/episodes/*\n!.provena/cache/episodes/observations/\n.provena/cache/episodes/observations/*\n!.provena/cache/episodes/observations/*.tmp\n");
  await assert.rejects(captureHook(temporary.root, "claude", payload(temporary.root, "unignored-temp")), /every capture cache file/);
  await assert.rejects(readdir(join(temporary.root, CAPTURE_DIRECTORY, "observations")), { code: "ENOENT" }, "temp privacy rejection occurs before any observation write");
  const negated = await fixture("negated candidate ignore");
  await installCaptureHooks(negated.root, "claude");
  const negatedRecord = await captureHook(negated.root, "claude", payload(negated.root, "one"));
  await writeFile(join(negated.root, ".gitignore"), ".provena/cache/*\n!.provena/cache/episodes/\n.provena/cache/episodes/*\n!.provena/cache/episodes/drafts/\n.provena/cache/episodes/drafts/*\n!.provena/cache/episodes/drafts/*.candidate.json\n");
  await assert.rejects(draftCapturedEpisode(negated.root, { episodeId: negatedRecord.episodeId, goal: "Review tests", sources: ["src/token.js"] }), /every capture cache file/);
  assert(!(await readdir(join(negated.root, CAPTURE_DIRECTORY, "drafts"))).some((name) => name.endsWith(".candidate.json")), "unignored candidate payload is never written");
  const memoized = await fixture("memoized cwd");
  await installCaptureHooks(memoized.root, "claude");
  const workdir = join(memoized.root, "src");
  const memoizedCall = await captureHook(memoized.root, "claude", payload(memoized.root, "workdir-template", { tool_input: { command: "npm test", workdir } }));
  const memoizedEpisode = await readEpisode(memoized.root, memoizedCall.episodeId);
  // Expand normalized synthetic data within the real schema limits. The test
  // measures validation work, not historical tool activity or task success.
  memoizedEpisode.observations = Array.from({ length: 64 }, (_, index) => ({ ...memoizedEpisode.observations[0], id: index.toString(16).padStart(64, "0") }));
  await writeFile(join(memoized.root, CAPTURE_DIRECTORY, "observations", `${memoizedCall.episodeId}.json`), JSON.stringify(memoizedEpisode));
  const originalExecSync = childProcess.execSync;
  let workdirGitChecks = 0;
  childProcess.execSync = (command, options) => {
    if (command === "git rev-parse --show-toplevel" && resolve(options.cwd) === resolve(workdir)) workdirGitChecks++;
    return originalExecSync(command, options);
  };
  syncBuiltinESMExports();
  try {
    assert.equal((await listCapturedEpisodes(memoized.root))[0].observations, 64);
    assert.equal(workdirGitChecks, 1, "one read must validate a repeated absolute workdir once, preserving per-reference path checks");
    await listCapturedEpisodes(memoized.root);
    assert.equal(workdirGitChecks, 2, "each independent read must revalidate its checkout boundaries");
    assert.equal(spawnSync("git", ["init", "-q"], { cwd: workdir }).status, 0);
    await assert.rejects(listCapturedEpisodes(memoized.root), /another checkout/);
    assert.equal(workdirGitChecks, 3, "a nested checkout created after a prior read must still be rejected");
  } finally {
    childProcess.execSync = originalExecSync;
    syncBuiltinESMExports();
  }

  const quota = await fixture("quotas");
  await installCaptureHooks(quota.root, "claude");
  for (let i = 0; i < CAPTURE_LIMITS.observationsPerEpisode; i++) await captureHook(quota.root, "claude", payload(quota.root, `quota-${i}`));
  await assert.rejects(captureHook(quota.root, "claude", payload(quota.root, "quota-overflow")), /accumulation limit/);
  assert.equal((await listCapturedEpisodes(quota.root))[0].quotaReached, true);
  assert.equal((await listCapturedEpisodes(quota.root))[0].observations, CAPTURE_LIMITS.observationsPerEpisode);
  await assert.rejects(draftCapturedEpisode(quota.root, { episodeId: (await listCapturedEpisodes(quota.root))[0].id, goal: "Check token", sources: ["src/token.js"] }), /selection/);
  const chosen = (await listCapturedEpisodes(quota.root))[0];
  const limitedDraft = await draftCapturedEpisode(quota.root, { episodeId: chosen.id, goal: "Check token", sources: ["src/token.js"], observationIds: chosen.calls.slice(0, 2).map((item) => item.id) });
  assert(limitedDraft.warnings.some((warning) => warning.includes("quota")));
  for (let i = 1; i < CAPTURE_LIMITS.episodes; i++) await captureHook(quota.root, "claude", payload(quota.root, "one", { session_id: `quota-session-${i}` }));
  await assert.rejects(captureHook(quota.root, "claude", payload(quota.root, "one", { session_id: "quota-session-overflow" })), /accumulation limit/);
  assert.equal((await listCapturedEpisodes(quota.root)).length, CAPTURE_LIMITS.episodes);
  const files = await readdir(join(quota.root, CAPTURE_DIRECTORY, "observations"));
  await writeFile(join(quota.root, CAPTURE_DIRECTORY, "observations", files[0]), " ".repeat(CAPTURE_LIMITS.episodeBytes + 1));
  await assert.rejects(listCapturedEpisodes(quota.root), /byte limit/);
  console.log("capture integration tests passed (installed adapter delivery; native trust gate remains explicit)");
} finally {
  await rm(sandbox, { recursive: true, force: true });
}
