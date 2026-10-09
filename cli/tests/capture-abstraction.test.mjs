import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { createServer } from "node:http";
import fileSystem from "node:fs/promises";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { syncBuiltinESMExports } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CAPTURE_DIRECTORY, captureHook, draftCapturedEpisode } from "../dist/capture/index.js";
import { CAPTURE_ABSTRACTION_LIMITS, captureAbstractionInputSchema, captureAbstractionResponseSchema, captureAbstractionSha256, validateCaptureAbstraction } from "../dist/capture/abstraction.js";
import { ProvenaClient } from "../dist/client.js";
import { createDefaultConfig, writeConfig } from "../dist/config.js";
import { runCaptureCommand } from "../dist/commands/capture.js";
import { installCaptureHooks, writeCaptureDraftPair } from "../dist/integrations/capture-hooks.js";
import { readMemoryLedgerSnapshot } from "../dist/brain/events.js";

// These are native-schema fixtures and mock service reports, never model calls,
// actual task-completion evidence, or fabricated procedure outcome receipts.
const sandbox = await mkdtemp(join(tmpdir(), "provena capture abstraction-"));
const credential = `runtime-credential-${randomUUID()}`;
const oldCredential = process.env.PROVENA_API_KEY;
process.env.PROVENA_API_KEY = credential;
const oldFetch = globalThis.fetch;
const source = "src/session.js";
const goal = "Verify session handling and review the retry";
let fixtureIndex = 0;
async function fixture() {
  const root = join(sandbox, `${fixtureIndex++}`);
  await mkdir(join(root, "src"), { recursive: true });
  assert.equal(spawnSync("git", ["init", "-q"], { cwd: root }).status, 0);
  await writeFile(join(root, ".gitignore"), ".provena/cache/\n");
  await writeFile(join(root, source), "export const valid = false;\n");
  const runtime = join(root, ".provena/runtime/node_modules/@provena/cli/dist");
  await mkdir(runtime, { recursive: true });
  await writeFile(join(runtime, "cli.js"), "// Fixture runtime presence, not executed.\n");
  await installCaptureHooks(root, "codex");
  const config = createDefaultConfig({ cwd: root, gitRoot: root });
  config.intelligence_url = "http://127.0.0.1:18093";
  writeConfig(root, config);
  const calls = [];
  for (const [name, code, input] of [
    ["failed", 1, { command: "npm test" }],
    ["retry", 0, { command: "npm test" }],
    ["inspect", 0, { file_path: join(root, source), offset: 0, limit: 10 }],
  ]) calls.push(await captureHook(root, "codex", {
    session_id: "fixture-session", cwd: root, hook_event_name: "PostToolUse", tool_use_id: name,
    tool_name: name === "inspect" ? "Read" : "Bash", tool_input: input,
    tool_response: { exit_code: code, stdout: "PRIVATE_OUTPUT_NOT_FOR_ABSTRACTION", stderr: credential },
    transcript_path: join(root, "PRIVATE_TRANSCRIPT_NOT_FOR_ABSTRACTION"),
  }));
  return { root, calls, input: { episodeId: calls[0].episodeId, goal, sources: [source], abstract: true } };
}
function report(request, changes = {}) {
  const output = {
    schemaVersion: 1, title: "Review session retry", triggers: ["When reviewing session changes"],
    // Deliberately reversed: candidate order must remain the original capture order.
    keptObservationIds: request.observations.slice(1).map((item) => item.id).reverse(),
    omitted: [{ observationId: request.observations[0].id, reason: "Earlier failed attempt; retain it in local review history." }],
    recoverySummary: "The fixture model claims task success; this is not goal evidence.",
    promptRevision: "fixture-v1", inputSha256: captureAbstractionSha256(request),
    model: { provider: "fixture", model: process.env.PROVENA_TEST_MODEL ?? randomUUID(), tier: "balanced" },
    ...changes,
  };
  return { ...output, outputSha256: captureAbstractionSha256(output) };
}
const jsonResponse = (value, options = {}) => new Response(JSON.stringify(value), { headers: { "content-type": "application/json" }, ...options });
async function drafts(root) { try { return (await readdir(join(root, CAPTURE_DIRECTORY, "drafts"))).sort(); } catch (error) { if (error.code === "ENOENT") return []; throw error; } }
async function rejectDraft(state, fetchImpl, pattern = /capture abstraction/, timeoutMs = 10_000) {
  const before = await drafts(state.root);
  await assert.rejects(draftCapturedEpisode(state.root, state.input, { fetchImpl, timeoutMs }), (error) => {
    assert.match(error.message, pattern);
    assert(!String(error.stack).includes(process.env.PROVENA_API_KEY));
    assert(!("cause" in error));
    return true;
  });
  assert.deepEqual(await drafts(state.root), before, "a rejected service report creates no review or candidate file");
  assert.equal((await readMemoryLedgerSnapshot(state.root)).events.length, 0, "abstraction never writes the ledger");
}
try {
  const state = await fixture();
  let networkCalls = 0;
  const noNetwork = async () => { networkCalls++; throw new Error("unexpected network"); };
  globalThis.fetch = noNetwork;
  const offline = await draftCapturedEpisode(state.root, { ...state.input, abstract: false }, { fetchImpl: noNetwork });
  assert.equal(networkCalls, 0, "default draft stays offline even with intelligence configured");
  const offlineWrapper = JSON.parse(await readFile(join(state.root, offline.draftPath), "utf8"));
  assert(!("abstraction" in offlineWrapper));
  const oldStdout = process.stdout.write, oldStderr = process.stderr.write;
  let hookStdout = "";
  process.stdout.write = (value) => { hookStdout += String(value); return true; };
  process.stderr.write = () => true;
  try { assert.equal(await runCaptureCommand(["hook", "--provider", "codex", "--root", state.root, "--abstract"], state.root), 0); }
  finally { process.stdout.write = oldStdout; process.stderr.write = oldStderr; }
  assert.equal(hookStdout, "{}\n");
  await assert.rejects(runCaptureCommand(["draft", state.input.episodeId, "--abstract", "--abstract"], state.root), /duplicate/);
  assert.equal(networkCalls, 0);

  let sent;
  const groundedFetch = async (url, options) => {
    sent = JSON.parse(options.body);
    assert.equal(url, "http://127.0.0.1:18093/v1/procedures/abstract");
    assert.equal(options.redirect, "error");
    assert.equal(options.headers.Authorization, `Bearer ${credential}`);
    assert.equal(options.headers["X-Provena-Role"], "editor");
    assert(options.signal instanceof AbortSignal);
    assert(Buffer.byteLength(options.body) <= CAPTURE_ABSTRACTION_LIMITS.requestBytes);
    assert.deepEqual(Object.keys(sent).sort(), ["goal", "observations", "schemaVersion"]);
    assert.equal(sent.goal, goal);
    for (const item of sent.observations) assert.deepEqual(Object.keys(item).sort(), ["args", "id", "issues", "status", "tool", "workingDirectory"]);
    for (const privateValue of [credential, state.root, "PRIVATE_OUTPUT", "PRIVATE_TRANSCRIPT", "recordedAt", "fingerprintTiming", "exitCode"]) assert(!options.body.includes(privateValue));
    return jsonResponse(report(sent));
  };
  const result = await draftCapturedEpisode(state.root, { ...state.input, title: "Human review title" }, { fetchImpl: groundedFetch });
  const wrapperText = await readFile(join(state.root, result.draftPath), "utf8");
  const wrapper = JSON.parse(wrapperText);
  const candidate = JSON.parse(await readFile(join(state.root, result.candidatePath), "utf8"));
  assert.equal(result.observations, 3); assert.equal(result.retainedObservations, 2);
  assert.equal(candidate.title, "Human review title", "explicit caller title wins");
  assert.equal(candidate.goal, goal);
  assert.deepEqual(candidate.steps.map(({ tool, args }) => ({ tool, args })), wrapper.observations.slice(1).map(({ tool, args }) => ({ tool, args })));
  assert.equal(wrapper.observations[0].status, "failure", "omitted failure remains inspectable");
  assert.equal(wrapper.abstraction.omitted[0].observationId, wrapper.observations[0].id);
  assert.match(candidate.recovery, /1 failures/);
  assert(!candidate.recovery.includes("fixture model claims"), "model recovery prose cannot become executable candidate recovery");
  assert.equal(wrapper.taskOutcome, "unknown"); assert.equal(wrapper.complete, false); assert.deepEqual(candidate.verification, []);
  assert(!("abstraction" in candidate)); assert(!wrapperText.includes(credential));
  assert.equal((await readMemoryLedgerSnapshot(state.root)).events.length, 0);

  // The actual CLI flag reaches the same explicit path; hooks cannot select it.
  globalThis.fetch = groundedFetch;
  const oldLog = console.log;
  console.log = () => {};
  try { assert.equal(await runCaptureCommand(["draft", state.input.episodeId, "--goal", goal, "--source", source, "--abstract", "--json"], state.root), 0); }
  finally { console.log = oldLog; }

  for (const mutate of [
    (request) => ({ keptObservationIds: ["f".repeat(64)], omitted: [] }),
    (request) => ({ keptObservationIds: [request.observations[1].id, request.observations[1].id] }),
    () => ({ omitted: [] }),
    (request) => ({ omitted: [{ observationId: request.observations[1].id, reason: "overlap" }] }),
    () => ({ keptObservationIds: [] }),
    () => ({ steps: [{ tool: "Bash", args: { command: "new command" } }] }),
    () => ({ paths: ["outside/new-path"] }),
    () => ({ receipt: { success: true } }),
    () => ({ title: "x".repeat(513) }),
    () => ({ title: "ghp_" + "Q".repeat(36) }),
    () => ({ inputSha256: "0".repeat(64) }),
    () => ({ model: { provider: "fixture", model: credential, tier: "balanced" } }),
  ]) await rejectDraft(state, async (_, options) => { const request = JSON.parse(options.body); return jsonResponse(report(request, mutate(request))); });
  await rejectDraft(state, async (_, options) => jsonResponse({ ...report(JSON.parse(options.body)), outputSha256: "0".repeat(64) }));
  await rejectDraft(state, async () => { throw new Error(`capture abstraction malicious transport ${credential}`); }, /unavailable or rejected/);
  await rejectDraft(state, async () => new Response(credential, { status: 401 }), /HTTP 401/);
  await rejectDraft(state, async () => new Response(null, { status: 302, headers: { location: `https://invalid.example/${credential}` } }), /redirects/);
  await rejectDraft(state, async () => new Response("x", { headers: { "content-type": "text/plain" } }), /JSON response/);
  await rejectDraft(state, async () => jsonResponse({}, { headers: { "content-type": "application/json", "content-length": String(CAPTURE_ABSTRACTION_LIMITS.responseBytes + 1) } }), /byte limit/);
  await rejectDraft(state, async () => new Response(new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(CAPTURE_ABSTRACTION_LIMITS.responseBytes + 1)); controller.close(); } }), { headers: { "content-type": "application/json" } }), /byte limit/);
  await rejectDraft(state, async () => jsonResponse({}, { headers: { "content-type": "application/json", "content-encoding": "gzip" } }), /compressed/);
  await rejectDraft(state, async () => new Promise(() => {}), /deadline/, 50);
  await rejectDraft(state, async () => new Response(new ReadableStream({ pull() { return new Promise(() => {}); } }), { headers: { "content-type": "application/json" } }), /deadline/, 50);

  const escapedCredential = `opaque"fragment\\${randomUUID()}`;
  process.env.PROVENA_API_KEY = escapedCredential;
  try {
    await rejectDraft({ ...state, input: { ...state.input, goal: `Review ${escapedCredential}` } }, noNetwork, /runtime credential/);
    await rejectDraft({ ...state, input: { ...state.input, title: `Review ${escapedCredential}` } }, noNetwork, /runtime credential/);
    await rejectDraft(state, async (_, options) => jsonResponse(report(JSON.parse(options.body), { title: escapedCredential })));
    await rejectDraft(state, async (_, options) => {
      const payload = report(JSON.parse(options.body), { title: escapedCredential });
      const escaped = [...escapedCredential].map((character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`).join("");
      const body = JSON.stringify(payload).replace(JSON.stringify(escapedCredential).slice(1, -1), escaped);
      assert(!body.includes(escapedCredential));
      return new Response(body, { headers: { "content-type": "application/json" } });
    });
  } finally { process.env.PROVENA_API_KEY = credential; }
  assert.equal(networkCalls, 0, "credential-bearing caller data fails before egress");

  const changedSource = await fixture();
  await rejectDraft(changedSource, async (_, options) => {
    await writeFile(join(changedSource.root, source), "export const valid = true;\n");
    return jsonResponse(report(JSON.parse(options.body)));
  }, /source changed/);
  const changedEpisode = await fixture();
  await rejectDraft(changedEpisode, async (_, options) => {
    // A real capture operation can acquire the repository lock during HTTP.
    await captureHook(changedEpisode.root, "codex", { session_id: "fixture-session", cwd: changedEpisode.root, hook_event_name: "PostToolUse", tool_use_id: "during-http", tool_name: "Bash", tool_input: { command: "npm test" }, tool_response: { exit_code: 1 } });
    return jsonResponse(report(JSON.parse(options.body)));
  }, /episode or source changed/);
  const changedBoundary = await fixture();
  await rejectDraft(changedBoundary, async (_, options) => {
    assert.equal(spawnSync("git", ["init", "-q"], { cwd: join(changedBoundary.root, "src") }).status, 0);
    return jsonResponse(report(JSON.parse(options.body)));
  }, /another checkout/);
  const changedWorkingDirectory = await fixture();
  await writeFile(join(changedWorkingDirectory.root, "root.js"), "export const rootSource = true;\n");
  changedWorkingDirectory.input.sources = ["root.js"];
  await captureHook(changedWorkingDirectory.root, "codex", { session_id: "fixture-session", cwd: join(changedWorkingDirectory.root, "src"), hook_event_name: "PostToolUse", tool_use_id: "subdirectory-without-workdir-argument", tool_name: "Bash", tool_input: { command: "npm test" }, tool_response: { exit_code: 0 } });
  await rejectDraft(changedWorkingDirectory, async (_, options) => {
    assert.equal(spawnSync("git", ["init", "-q"], { cwd: join(changedWorkingDirectory.root, "src") }).status, 0);
    return jsonResponse(report(JSON.parse(options.body)));
  }, /another checkout/);
  const changedArgumentBoundary = await fixture();
  await writeFile(join(changedArgumentBoundary.root, "root.js"), "export const rootSource = true;\n");
  changedArgumentBoundary.input.sources = ["root.js"];
  await rejectDraft(changedArgumentBoundary, async (_, options) => {
    assert.equal(spawnSync("git", ["init", "-q"], { cwd: join(changedArgumentBoundary.root, "src") }).status, 0);
    return jsonResponse(report(JSON.parse(options.body)));
  }, /another checkout/);
  const removedEpisode = await fixture();
  await rejectDraft(removedEpisode, async (_, options) => {
    await rm(join(removedEpisode.root, CAPTURE_DIRECTORY, "observations", `${removedEpisode.input.episodeId}.json`));
    return jsonResponse(report(JSON.parse(options.body)));
  }, /episode was not found/);
  for (const candidateRule of ["*.candidate.json", "*.candidate.json.capture-*.tmp"]) {
    const privatePair = await fixture();
    await rejectDraft(privatePair, async (_, options) => {
      await writeFile(join(privatePair.root, ".gitignore"), `.provena/cache/*\n!.provena/cache/episodes/\n.provena/cache/episodes/*\n!.provena/cache/episodes/drafts/\n.provena/cache/episodes/drafts/*\n!.provena/cache/episodes/drafts/${candidateRule}\n`);
      return jsonResponse(report(JSON.parse(options.body)));
    }, /ignored and untracked/);
    assert.deepEqual(await drafts(privatePair.root), [], "candidate or candidate-temp privacy failure leaves no wrapper, candidate, or payload temp");
  }
  const missing = await fixture();
  const config = createDefaultConfig({ cwd: missing.root, gitRoot: missing.root });
  writeConfig(missing.root, config);
  await rejectDraft(missing, noNetwork, /requires config.intelligence_url/);
  assert.equal(networkCalls, 0);

  const hugeInput = { schemaVersion: 1, goal, observations: Array.from({ length: 32 }, (_, index) => ({ id: index.toString(16).padStart(64, "0"), tool: "Read", workingDirectory: "d".repeat(2048), args: { path: "p".repeat(2048) }, status: "unknown", issues: [] })) };
  const client = new ProvenaClient({ storeUrl: "http://127.0.0.1:18092", intelligenceUrl: "http://127.0.0.1:18093", fetchImpl: noNetwork });
  await assert.rejects(client.abstractCapturedProcedure(hugeInput, { tenant_id: "fixture" }), /request exceeds/);
  assert.equal(networkCalls, 0, "oversized input fails before egress");
  const contract = JSON.parse(await readFile(new URL("./fixtures/capture-abstraction-contract.json", import.meta.url), "utf8"));
  const normalized = captureAbstractionInputSchema.parse(contract.request);
  assert.equal(normalized.goal, contract.normalizedGoal);
  assert.equal(captureAbstractionSha256(normalized), contract.inputSha256);
  const shared = validateCaptureAbstraction(normalized, contract.response);
  assert.equal(shared.title, contract.response.title, "response whitespace remains part of hashed semantics");
  assert.equal(shared.recoverySummary, "");
  assert(!captureAbstractionInputSchema.safeParse({ ...normalized, schemaVersion: true }).success);
  assert(!captureAbstractionInputSchema.safeParse({ ...normalized, observations: [{ ...normalized.observations[0], args: { offset: true } }] }).success);
  assert(!captureAbstractionInputSchema.safeParse({ ...normalized, observations: [{ ...normalized.observations[0], args: { offset: 1.5 } }] }).success);
  assert(!captureAbstractionInputSchema.safeParse({ ...normalized, goal: "🧭".repeat(1025) }).success);
  assert(captureAbstractionInputSchema.safeParse({ ...normalized, goal: "🧭".repeat(1024) }).success);
  assert(!captureAbstractionInputSchema.safeParse({ ...normalized, goal: "\uD800" }).success);
  assert(!captureAbstractionResponseSchema.safeParse({ ...contract.response, schemaVersion: true }).success);
  assert(!captureAbstractionResponseSchema.safeParse({ ...contract.response, title: "🧭".repeat(257) }).success);
  assert(!captureAbstractionResponseSchema.safeParse({ ...contract.response, recoverySummary: "🧭".repeat(1025) }).success);
  for (const intelligenceUrl of ["http://example.invalid", `https://user:${credential}@example.invalid`, "http://127.0.0.1:18093/path", `http://127.0.0.1:18093?key=${credential}`]) {
    const badOriginClient = new ProvenaClient({ storeUrl: "http://127.0.0.1:18092", intelligenceUrl, apiKey: credential, fetchImpl: noNetwork });
    await assert.rejects(badOriginClient.abstractCapturedProcedure(normalized, { tenant_id: "fixture" }), (error) => {
      assert.match(error.message, /authorized HTTP\(S\) origin/); assert(!String(error.stack).includes(credential)); return true;
    });
  }
  assert.equal(networkCalls, 0);

  // Real loopback HTTP: rejecting headers must stop an unread, indefinitely
  // dripping error body. No provider is contacted and no body becomes memory.
  const sockets = new Set();
  let responseClosed;
  const responseClosedPromise = new Promise((resolve) => { responseClosed = resolve; });
  const server = createServer((request, response) => {
    request.resume();
    response.writeHead(503, { "content-type": "application/json" });
    response.write("unavailable");
    const interval = setInterval(() => response.write(credential), 20);
    response.once("close", () => { clearInterval(interval); responseClosed(); });
  });
  server.on("connection", (socket) => { sockets.add(socket); socket.once("close", () => sockets.delete(socket)); });
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  let closeTimer;
  try {
    const origin = `http://127.0.0.1:${server.address().port}`;
    const httpClient = new ProvenaClient({ storeUrl: origin, intelligenceUrl: origin, apiKey: credential, fetchImpl: oldFetch, timeoutMs: 1000 });
    await assert.rejects(httpClient.abstractCapturedProcedure(normalized, { tenant_id: "fixture" }), /HTTP 503/);
    await Promise.race([responseClosedPromise, new Promise((_, reject) => { closeTimer = setTimeout(() => reject(new Error("rejected HTTP body remained live")), 2000); })]);
  } finally {
    clearTimeout(closeTimer);
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve) => server.close(resolve));
  }

  // Real filesystem fault injection: an unsuccessful second publish restores
  // every preexisting byte, including non-UTF8 bytes, and removes only owned temps.
  const pairState = await fixture();
  const directory = join(pairState.root, CAPTURE_DIRECTORY, "drafts");
  await mkdir(directory, { recursive: true });
  const pairId = captureAbstractionSha256(randomUUID());
  const wrapperPath = join(directory, `${pairId}.json`), candidatePath = join(directory, `${pairId}.candidate.json`);
  const oldWrapper = Buffer.from([0xff, 0, 1, 2]), oldCandidate = Buffer.from([0xfe, 3, 4, 5]);
  await writeFile(wrapperPath, oldWrapper); await writeFile(candidatePath, oldCandidate);
  const originalRename = fileSystem.rename;
  const failingTargets = new Set([candidatePath]);
  fileSystem.rename = async (from, to) => {
    if (failingTargets.has(to)) throw Object.assign(new Error("controlled second rename failure"), { code: "EACCES" });
    return originalRename(from, to);
  };
  syncBuiltinESMExports();
  try {
    await assert.rejects(writeCaptureDraftPair(pairState.root, wrapperPath, "new wrapper", candidatePath, "new candidate"), /previous files were preserved/);
    assert((await readFile(wrapperPath)).equals(oldWrapper)); assert((await readFile(candidatePath)).equals(oldCandidate));
    const newId = captureAbstractionSha256(randomUUID());
    const newWrapper = join(directory, `${newId}.json`), newCandidate = join(directory, `${newId}.candidate.json`);
    failingTargets.add(newCandidate);
    await assert.rejects(writeCaptureDraftPair(pairState.root, newWrapper, "new wrapper", newCandidate, "new candidate"), /previous files were preserved/);
    assert.deepEqual((await readdir(directory)).sort(), [`${pairId}.candidate.json`, `${pairId}.json`].sort());
  } finally { fileSystem.rename = originalRename; syncBuiltinESMExports(); }

  // Changing ignore rules after staging must not strand an unignored private
  // nonce. Cleanup owns only wx-created files; prior-byte rollback is covered above.
  const ignoreRace = await fixture();
  const raceDirectory = join(ignoreRace.root, CAPTURE_DIRECTORY, "drafts");
  await mkdir(raceDirectory, { recursive: true });
  const raceId = captureAbstractionSha256(randomUUID());
  const raceWrapper = join(raceDirectory, `${raceId}.json`), raceCandidate = join(raceDirectory, `${raceId}.candidate.json`);
  fileSystem.rename = async (from, to) => {
    if (to === raceWrapper) await writeFile(join(ignoreRace.root, ".gitignore"), ".provena/cache/**\n!.provena/cache/episodes/\n!.provena/cache/episodes/drafts/\n!.provena/cache/episodes/drafts/*.tmp\n");
    return originalRename(from, to);
  };
  syncBuiltinESMExports();
  try {
    await assert.rejects(writeCaptureDraftPair(ignoreRace.root, raceWrapper, "private new wrapper", raceCandidate, "private new candidate"), /previous files were preserved/);
    assert.deepEqual(await readdir(raceDirectory), [], "no draft or private temporary payload survives a concurrent ignore change");
  } finally { fileSystem.rename = originalRename; syncBuiltinESMExports(); }
  console.log("capture abstraction tests passed (mock transport; no model or goal-success claims)");
} finally {
  globalThis.fetch = oldFetch;
  if (oldCredential === undefined) delete process.env.PROVENA_API_KEY; else process.env.PROVENA_API_KEY = oldCredential;
  const resolved = join(tmpdir(), "");
  assert(sandbox.startsWith(resolved), "fixture cleanup remains inside the system temporary directory");
  await rm(sandbox, { recursive: true, force: true });
}
