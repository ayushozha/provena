import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createDefaultConfig, readMemoryEvents, writeConfig } from "../dist/index.js";

const cli = fileURLToPath(new URL("../dist/cli.js", import.meta.url));
const root = mkdtempSync(join(tmpdir(), "provena-procedure-cli-"));
const marker = join(root, "procedure-executed.marker");
function run(args, status = 0) {
  const result = spawnSync(process.execPath, [cli, "procedure", ...args], {
    cwd: root, encoding: "utf8", timeout: 20_000,
  });
  assert.equal(result.error, undefined);
  assert.equal(result.status, status, result.stderr || result.stdout);
  assert.equal(existsSync(marker), false, "procedure commands must never execute recorded steps");
  return result;
}
function json(args) { return JSON.parse(run(args).stdout); }
function save(name, data) {
  const path = join(root, name);
  writeFileSync(path, JSON.stringify(data));
  return path;
}
try {
  assert.equal(spawnSync("git", ["init", "--quiet"], { cwd: root }).status, 0);
  mkdirSync(join(root, "src"));
  writeFileSync(join(root, "package.json"), JSON.stringify({ name: "procedure-cli-fixture" }));
  writeFileSync(join(root, "src", "session.ts"), "export const sessionValidation = true;\n");
  writeConfig(root, createDefaultConfig({ cwd: root, gitRoot: root }));
  assert.match(run(["--help"]).stdout, /never execute stored tool calls/);
  const trace = {
    title: "Validate session handling", episodeId: "cli-learn-episode", sessionId: "cli-session",
    goal: "Validate session handling before a handoff", triggers: ["session validation"], prerequisites: [],
    sources: [{ path: "src/session.ts", startLine: 1 }], verification: [],
    steps: [{ tool: "shell", args: { command: "node -e \"require('node:fs').writeFileSync('procedure-executed.marker','executed')\"" } }],
  };
  const traceFile = save("trace.json", trace);
  const learned = json(["learn", "--file", traceFile, "--actor", "cli-test"]);
  assert.equal(learned.duplicate, false);
  assert.equal(learned.event.structured_data.procedure.state, "candidate");
  assert.equal(learned.event.authority, "agent");
  assert.match(learned.event.sources[0].blob, /^sha256-lf:[a-f0-9]{64}$/);
  assert.equal(json(["learn", "--file", traceFile, "--actor", "cli-test"]).duplicate, true);
  const query = ["recall", "session validation", "--tool", "shell", "--include-review", "--max-tokens", "4096", "--json"];
  let recall = json(query);
  assert.deepEqual(recall.ready, []);
  assert.equal(recall.review[0].state, "candidate");
  assert.equal(json(["inspect", learned.event.id, "--max-tokens", "4096", "--json"]).review[0].procedureId, learned.event.id);

  assert.match(run(["approve", learned.event.id], 1).stderr, /--authority human/);
  assert.equal((await readMemoryEvents(root)).filter((event) => event.structuredData.procedure?.state === "approved").length, 0);
  const approved = json(["approve", learned.event.id, "--authority", "human", "--actor", "fixture-maintainer"]);
  assert.equal(approved.event.authority, "human");
  assert.equal(approved.event.structured_data.procedure.state, "approved");
  assert.deepEqual(approved.event.supersedes, [learned.event.id]);
  recall = json(query);
  assert.deepEqual(recall.ready, []);
  assert.equal(recall.review[0].state, "unverified", "approval alone must not invent a successful task outcome");

  const receipt = {
    procedureId: approved.event.id, receiptId: "cli-goal-receipt", episodeId: "cli-goal-episode",
    sessionId: "cli-goal-session", goal: trace.goal, outcome: "success",
    verification: [{ check: "Fixture caller goal attestation", passed: true, evidence: "Synthetic lifecycle receipt; no task command was executed." }],
  };
  const rejected = json(["inspect", approved.event.id, "--max-tokens", "4096", "--json"]);
  assert.equal(rejected.review[0].state, "unverified");
  const invalidReceipt = save("invalid-receipt.json", { ...receipt, verification: [] });
  assert.match(run(["outcome", "--file", invalidReceipt], 1).stderr, /require caller-reported passing goal verification/);
  const receiptFile = save("receipt.json", receipt);
  const outcome = json(["outcome", "--file", receiptFile, "--actor", "cli-test"]);
  assert.equal(outcome.event.structured_data.procedureOutcome.attestation, "caller-reported");
  assert.equal(json(["outcome", "--file", receiptFile, "--actor", "cli-test"]).duplicate, true);
  recall = json(query);
  assert.equal(recall.ready.length, 1);
  assert.deepEqual(recall.review, []);
  assert.equal(recall.ready[0].procedureId, approved.event.id);
  assert.match(recall.ready[0].pointer, new RegExp(approved.event.id));
  assert.deepEqual(recall.ready[0].citations.outcomeIds, [outcome.event.id]);
  assert.deepEqual(recall.ready[0].procedure.steps, trace.steps);
  const compact = run(["recall", "session validation", "--tool", "shell", "--max-tokens", "350", "--json"]).stdout.trimEnd();
  const compactResult = JSON.parse(compact);
  assert.equal(compactResult.ready.length, 1, "a complete procedure must fit within this measured JSON budget");
  assert.equal(compact.length, compactResult.characters);
  assert(Buffer.byteLength(compact, "utf8") <= 350 * 4, "visible CLI JSON must respect the advertised ASCII fixture budget");
  assert.equal(json(["recall", "astronomy nebulae", "--include-review", "--json"]).ready.length, 0);
  const bounded = json(["recall", "session validation", "--max-tokens", "128", "--json"]);
  assert(bounded.estimatedTokens <= 128);
  assert.equal(bounded.ready.length, 0, "an oversized procedure is omitted as a whole");
  assert.equal(bounded.omitted, 1);
  writeFileSync(join(root, "src", "session.ts"), "export const sessionValidationChanged = true;\n");
  const stale = json(["inspect", approved.event.id, "--max-tokens", "4096", "--json"]);
  assert.equal(stale.ready.length, 0);
  assert.equal(stale.review[0].state, "stale", "inspection checks current source bytes without a refresh");
  assert.equal((await readMemoryEvents(root)).filter((event) => event.structuredData.procedureOutcome).length, 1);
} finally {
  rmSync(root, { recursive: true, force: true });
}
console.log("procedure-cli.test: ok");
