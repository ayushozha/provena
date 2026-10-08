import assert from "node:assert/strict";
import { access, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { appendMemoryEvent, readMemoryLedgerSnapshot } from "../dist/brain/events.js";
import { refreshRepoBrain } from "../dist/brain/index.js";
import { learnProcedure, approveProcedure, recordProcedureOutcome, recallProcedures } from "../dist/procedures/index.js";

const root = await mkdtemp(join(tmpdir(), "provena-procedures-"));
const now = () => new Date("2026-10-08T12:00:00.000Z");
const sourceText = "export function sessionToken() { return 'validated'; }\n";
const learning = (episodeId, changes = {}) => ({
  episodeId, sessionId: `session-${episodeId}`, actor: "agent-a",
  title: "Repair login session token validation", goal: "Repair login session token validation",
  triggers: ["login session", "token validation"], prerequisites: [],
  steps: [
    { tool: "read", args: { path: "src/session.ts" } },
    { tool: "edit", args: { path: "src/session.ts", replacement: "validate before issuing" }, expected: "Invalid inputs are rejected." },
    { tool: "shell", args: { command: "npm test -- session" }, expected: "Session behavior checks pass." },
  ],
  verification: [{ check: "Rejected invalid login token", passed: true, evidence: "Session behavior test receipt from this run." }],
  sources: [{ path: "src/session.ts", startLine: 1, endLine: 1 }], ...changes,
});
const outcome = (procedureId, episodeId, changes = {}) => ({
  procedureId, episodeId, receiptId: `receipt-${episodeId}`, sessionId: `session-${episodeId}`,
  actor: "agent-a", goal: "Repair login session token validation", outcome: "success",
  verification: [{ check: "Invalid token behavior is rejected", passed: true, evidence: "Independent goal assertion passed in the task harness." }],
  ...changes,
});

try {
  await mkdir(join(root, "src"));
  await writeFile(join(root, "src/session.ts"), sourceText);
  const candidate = await learnProcedure(root, learning("learn-login"), { now });
  assert.equal(candidate.duplicate, false);
  assert.equal(candidate.event.kind, "workflow");
  assert.match(candidate.event.sources[0].blob, /^sha256-lf:[a-f0-9]{64}$/);
  const prefix = (await readMemoryLedgerSnapshot(root)).rawLedger;
  assert.equal((await learnProcedure(root, learning("learn-login"), { now })).duplicate, true);
  assert.equal((await readMemoryLedgerSnapshot(root)).rawLedger, prefix, "idempotent retries preserve exact bytes");
  await assert.rejects(learnProcedure(root, learning("learn-login", { goal: "Different goal" }), { now }), /already used/);

  let recalled = await recallProcedures(root, "repair login token");
  assert.equal(recalled.ready.length, 0);
  assert.equal(recalled.review[0].state, "candidate");
  const approved = await approveProcedure(root, { procedureId: candidate.event.id, actor: "maintainer" }, { now });
  assert.equal(approved.event.authority, "human");
  assert.deepEqual(approved.event.supersedes, [candidate.event.id]);
  assert.equal((await approveProcedure(root, { procedureId: candidate.event.id, actor: "maintainer" }, { now })).duplicate, true);
  recalled = await recallProcedures(root, "repair login token");
  assert.equal(recalled.ready.length, 0);
  assert.equal(recalled.review[0].state, "unverified", "approval alone cannot invent successful goal evidence");
  await assert.rejects(recordProcedureOutcome(root, outcome(candidate.event.id, "wrong-version"), { now }), /exact approved procedure version/);
  await assert.rejects(recordProcedureOutcome(root, outcome("procedure:missing", "missing"), { now }), /exact approved procedure version/);
  await assert.rejects(recordProcedureOutcome(root, outcome(approved.event.id, "no-proof", { verification: [] }), { now }), /passing goal verification/);
  await assert.rejects(recordProcedureOutcome(root, outcome(approved.event.id, "backdated"), { now: () => new Date("2026-10-07T12:00:00.000Z") }), /cannot predate/);
  await assert.rejects(recordProcedureOutcome(root, outcome(approved.event.id, "failed-without-evidence", { outcome: "failure", verification: [] }), { now }), /failure evidence/);
  const success = await recordProcedureOutcome(root, outcome(approved.event.id, "login-run-1"), { now });
  assert.equal(success.event.structuredData.procedureOutcome.attestation, "caller-reported");
  assert.equal((await recordProcedureOutcome(root, outcome(approved.event.id, "login-run-1"), { now })).duplicate, true);
  await assert.rejects(recordProcedureOutcome(root, outcome(approved.event.id, "login-run-1", { receiptId: "another-receipt" }), { now }), /episode was already recorded/);
  await assert.rejects(recordProcedureOutcome(root, outcome(approved.event.id, "login-run-1", { outcome: "failure", failure: "Goal failed." }), { now }), /already used/);
  recalled = await recallProcedures(root, "repair login token");
  assert.equal(recalled.ready[0].procedureId, approved.event.id);
  assert.equal(recalled.ready[0].reliability.successes, 1);
  assert.deepEqual(recalled.ready[0].citations.outcomeIds, [success.event.id]);
  assert.match(recalled.instruction, /caller-reported/);
  assert.match(recalled.ready[0].pointer, /memory:procedure-approved:/);
  assert.equal((await recallProcedures(root, "configure billing invoices")).ready.length, 0, "reliability cannot retrieve unrelated work");
  assert.equal((await recallProcedures(root, "repair billing invoices")).review.length, 0, "a generic shared word does not justify procedural reuse");
  assert.equal((await recallProcedures(root, "unrelated", { procedureIds: [approved.event.id] })).ready.length, 1, "explicit version selectors work independently of query overlap");

  const newlineSource = "export const lineOne = true;\r\nexport const lineTwo = true;\r\n";
  await writeFile(join(root, "src/newlines.ts"), newlineSource);
  const newlineCandidate = await learnProcedure(root, learning("newline-learning", {
    title: "Review newline portability", goal: "Review newline portability", triggers: ["newline portability"],
    sources: [{ path: "src/newlines.ts", startLine: 2, endLine: 2 }], steps: [{ tool: "read", args: { path: "src/newlines.ts" } }],
  }), { now });
  const newlineApproved = await approveProcedure(root, { procedureId: newlineCandidate.event.id, actor: "maintainer" }, { now });
  await recordProcedureOutcome(root, outcome(newlineApproved.event.id, "newline-outcome", { goal: "Review newline portability" }), { now });
  const checkNewlineEvidence = async () => {
    const recalledNewline = await recallProcedures(root, "newline portability");
    assert.equal(recalledNewline.ready[0].procedureId, newlineApproved.event.id);
    const generated = await refreshRepoBrain(root, { now });
    assert.equal(newlineApproved.event.sources[0].blob, `sha256-lf:${generated.map.files.find((file) => file.path === "src/newlines.ts").sha256}`);
    assert(!generated.maintenancePlan.issues.some((issue) => issue.kind === "source-changed" && issue.memoryIds.includes(newlineApproved.event.id)), "unchanged canonical text must not produce drift advice");
  };
  await checkNewlineEvidence();
  await writeFile(join(root, "src/newlines.ts"), newlineSource.replace(/\r\n/g, "\n"));
  await checkNewlineEvidence();
  await writeFile(join(root, "src/newlines.ts"), newlineSource.replace(/\r\n/g, "\r"));
  await checkNewlineEvidence();
  await writeFile(join(root, "src/newlines.ts"), newlineSource.replace("lineTwo = true", "lineTwo = false"));
  assert.equal((await recallProcedures(root, "newline portability")).review[0].state, "stale");
  const changedNewline = await refreshRepoBrain(root, { now });
  assert(changedNewline.maintenancePlan.issues.some((issue) => issue.kind === "source-changed" && issue.memoryIds.includes(newlineApproved.event.id)), "a real code change must produce a canonical-source drift proposal");

  const cachedSuccessfulSnapshot = await readMemoryLedgerSnapshot(root);
  await recordProcedureOutcome(root, outcome(approved.event.id, "login-run-2", {
    outcome: "failure", failure: "The task harness still accepted an invalid token.",
    verification: [{ check: "Invalid token rejected", passed: false, evidence: "Task harness observed an accepted invalid token." }],
  }), { now });
  recalled = await recallProcedures(root, "repair login token");
  assert.equal(recalled.ready.length, 0);
  assert.equal(recalled.review[0].state, "failed");
  assert.deepEqual(recalled.review[0].reliability, { successes: 1, failures: 1, unknown: 0 });
  const recalledWithOldSuccess = await recallProcedures(root, "repair login token", { snapshot: cachedSuccessfulSnapshot });
  assert.equal(recalledWithOldSuccess.ready.length, 0, "a cached success cannot bypass a later failure");
  assert.equal(recalledWithOldSuccess.review[0].state, "failed");
  assert.equal(recalledWithOldSuccess.memoryFingerprint, recalled.memoryFingerprint, "readiness cites the current ledger");
  await recordProcedureOutcome(root, outcome(approved.event.id, "login-run-3"), { now });
  recalled = await recallProcedures(root, "repair login token");
  assert.equal(recalled.ready.length, 1, "later verified recovery can restore eligibility");
  assert.equal(recalled.ready[0].reliability.failures, 1, "recovery preserves historical failures");
  assert.equal((await recallProcedures(root, "repair login token", { availableTools: ["read"] })).review[0].state, "incompatible");

  const alternate = await learnProcedure(root, learning("learn-login-alternate"), { now });
  const alternateApproved = await approveProcedure(root, { procedureId: alternate.event.id, actor: "maintainer" }, { now });
  await assert.rejects(recordProcedureOutcome(root, outcome(alternateApproved.event.id, "another-episode", { receiptId: "receipt-login-run-1" }), { now }), /already used/);
  recalled = await recallProcedures(root, "repair login token");
  assert.equal(recalled.ready.length, 1, "success receipts never transfer to another procedure version");
  assert.equal(recalled.review.find((item) => item.procedureId === alternateApproved.event.id).state, "unverified");
  await recordProcedureOutcome(root, outcome(alternateApproved.event.id, "alternate-run-1"), { now });
  recalled = await recallProcedures(root, "repair login token");
  assert.equal(recalled.ready[0].procedureId, alternateApproved.event.id, "at equal relevance, recorded failures reduce the reliability prior");

  const compatible = await learnProcedure(root, learning("learn-certificate", {
    title: "Rotate expired service certificate", goal: "Rotate expired service certificate", triggers: ["rotate certificate"],
    prerequisites: [{ tool: "shell" }, { environmentKey: "PLATFORM", equals: "fixture" }, { path: "src/session.ts" }],
    steps: [{ tool: "shell", args: { command: "verify-fixture-certificate" } }],
  }), { now });
  const compatibleApproved = await approveProcedure(root, { procedureId: compatible.event.id, actor: "maintainer" }, { now });
  await recordProcedureOutcome(root, outcome(compatibleApproved.event.id, "certificate-run", { goal: "Rotate expired service certificate" }), { now });
  assert.equal((await recallProcedures(root, "rotate expired certificate")).review[0].state, "incompatible");
  assert.equal((await recallProcedures(root, "rotate expired certificate", { availableTools: ["shell"], environment: { PLATFORM: "fixture" } })).ready.length, 1);

  await writeFile(join(root, "src/session.ts"), "export const changedSource = true;\n");
  recalled = await recallProcedures(root, "repair login token");
  assert.equal(recalled.ready.length, 0);
  assert(recalled.review.every((item) => item.state === "stale"));
  const staleCandidate = await learnProcedure(root, learning("learn-source-version"), { now });
  await writeFile(join(root, "src/session.ts"), sourceText);
  await assert.rejects(approveProcedure(root, { procedureId: staleCandidate.event.id, actor: "maintainer" }, { now }), /source has changed/);

  const bounded = await recallProcedures(root, "repair login token", { maxCharacters: 512, maxTokens: 128 });
  assert.equal(bounded.ready.length + bounded.review.length, 0);
  assert(bounded.omitted > 0);
  assert(bounded.characters <= 512);
  assert.equal(bounded.characters, JSON.stringify(bounded).length);
  assert.equal(bounded.estimatedTokens, Math.ceil(bounded.characters / 4));
  assert.equal((await recallProcedures(root, "repair login token", { maxItems: 0 })).ready.length, 0);
  const noReview = await recallProcedures(root, "repair login token", { includeReview: false });
  assert.equal(noReview.review.length, 0);
  assert(noReview.ready.length > 0);
  await assert.rejects(recallProcedures(root, "repair login token", { maxTokens: -1 }), /budget/);
  const snapshot = await readMemoryLedgerSnapshot(root);
  await assert.rejects(recallProcedures(root, "repair login token", { snapshot: { ...snapshot, bytes: snapshot.bytes + 1 } }), /attestation/);

  for (const transition of ["procedure-retraction", "receipt-retraction", "procedure-supersession"]) {
    const learned = await learnProcedure(root, learning(`cache-${transition}`, {
      title: `Check ${transition}`, goal: `Check ${transition}`, triggers: ["cache lifecycle"],
      steps: [{ tool: "read", args: { path: "src/session.ts" } }],
    }), { now });
    const version = await approveProcedure(root, { procedureId: learned.event.id, actor: "maintainer" }, { now });
    const receipt = await recordProcedureOutcome(root, outcome(version.event.id, `cache-outcome-${transition}`, {
      goal: `Check ${transition}`,
    }), { now });
    const successfulSnapshot = await readMemoryLedgerSnapshot(root);
    const selectors = { procedureIds: [version.event.id] };
    assert.equal((await recallProcedures(root, "", { ...selectors, snapshot: successfulSnapshot })).ready.length, 1);
    await appendMemoryEvent(root, {
      id: `invalidate-${transition}`, kind: "workflow", subjectType: "task",
      title: `Record ${transition}`, body: "Explicit reviewer invalidation of the previous evidence.",
      status: transition === "procedure-supersession" ? "active" : "retracted",
      supersedes: [transition === "receipt-retraction" ? receipt.event.id : version.event.id],
      provenance: { actor: "maintainer", method: "explicit" }, authority: "human",
    }, { now });
    const current = await recallProcedures(root, "", selectors);
    const cached = await recallProcedures(root, "", { ...selectors, snapshot: successfulSnapshot });
    assert.equal(current.ready.length, 0);
    assert.equal(cached.ready.length, 0, `cached success cannot bypass ${transition}`);
    assert.equal(cached.memoryFingerprint, current.memoryFingerprint);
    assert.deepEqual(cached.review, current.review);
    if (transition === "receipt-retraction") assert.equal(cached.review[0].state, "unverified");
    else assert.equal(cached.review.length, 0, "inactive procedure versions cannot enter recall");
  }

  const beforeRejectedWrites = (await readMemoryLedgerSnapshot(root)).rawLedger;
  await assert.rejects(learnProcedure(root, learning("sensitive", { sensitivity: "restricted" }), { now }), /invalid procedure learning/);
  await assert.rejects(learnProcedure(root, learning("secret", { steps: [{ tool: "shell", args: { command: "api_key=abcdefghijklmnop123456" } }] }), { now }), /credential/);
  await assert.rejects(learnProcedure(root, learning("escape", { sources: [{ path: "../outside.txt" }] }), { now }), /inside the repository/);
  await writeFile(join(root, ".env"), "UNUSED_ENV_NAME=fixture\n");
  await assert.rejects(learnProcedure(root, learning("secret-path", { sources: [{ path: ".env" }] }), { now }), /non-secret/);
  await assert.rejects(learnProcedure(root, learning("missing-source", { sources: [{ path: "src/missing.ts" }] }), { now }), /ENOENT/);
  await assert.rejects(learnProcedure(root, learning("imaginary-line", { sources: [{ path: "src/session.ts", startLine: 20 }] }), { now }), /line span exceeds/);
  await writeFile(join(root, "src/non-utf8.txt"), Buffer.from([0xff]));
  await assert.rejects(learnProcedure(root, learning("non-utf8", { sources: [{ path: "src/non-utf8.txt" }] }), { now }), /valid UTF-8/);
  let nested = { value: true };
  for (let index = 0; index < 12; index++) nested = { next: nested };
  await assert.rejects(learnProcedure(root, learning("deep-json", { steps: [{ tool: "shell", args: nested }] }), { now }), /nesting limit/);
  assert.equal((await readMemoryLedgerSnapshot(root)).rawLedger, beforeRejectedWrites);

  const concurrent = await Promise.all([
    learnProcedure(root, learning("concurrent"), { now }), learnProcedure(root, learning("concurrent"), { now }),
  ]);
  assert.equal(concurrent.filter((result) => result.duplicate).length, 1);
  assert.equal((await readMemoryLedgerSnapshot(root)).events.filter((event) => event.id === concurrent[0].event.id).length, 1);
  const noExecution = await learnProcedure(root, learning("no-execution", {
    title: "Inspect marker procedure", goal: "Inspect marker procedure", triggers: ["marker procedure"],
    steps: [{ tool: "shell", args: { command: "write a marker file if this were executed" } }],
  }), { now });
  await recallProcedures(root, "marker procedure", { procedureIds: [noExecution.event.id] });
  await assert.rejects(access(join(root, "marker")), /ENOENT/, "memory recall never executes recorded commands");

  const viewCandidate = await learnProcedure(root, learning("view-candidate", {
    title: "Candidate view reference", goal: "UNAPPROVED_GOAL_SENTINEL stored instructions",
  }), { now });
  const viewApprovedCandidate = await learnProcedure(root, learning("view-approved", {
    title: "Approved view reference", goal: "APPROVED_GOAL_SENTINEL stored instructions",
  }), { now });
  const viewApproved = await approveProcedure(root, { procedureId: viewApprovedCandidate.event.id, actor: "maintainer" }, { now });
  const viewFailure = await recordProcedureOutcome(root, outcome(viewApproved.event.id, "view-failure", {
    goal: "FAILED_GOAL_SENTINEL stored instructions", outcome: "failure",
    verification: [{ check: "Fixture failure", passed: false, evidence: "Synthetic caller attestation." }],
  }), { now });
  await writeFile(join(root, "src/session.ts"), "export const staleViewEvidence = true;\n");
  await refreshRepoBrain(root, { now });
  const workflows = await readFile(join(root, ".provena", "views", "workflows.md"), "utf8");
  const learnings = await readFile(join(root, ".provena", "views", "learnings.md"), "utf8");
  assert(!workflows.includes("UNAPPROVED_GOAL_SENTINEL"));
  assert(!workflows.includes("APPROVED_GOAL_SENTINEL"));
  assert(!learnings.includes("FAILED_GOAL_SENTINEL"));
  assert(learnings.includes(viewFailure.event.id), "outcome references retain their own ledger citation");
  for (const [view, id] of [[workflows, viewCandidate.event.id], [workflows, viewApproved.event.id], [learnings, viewApproved.event.id]]) {
    assert(view.includes(`provena procedure inspect ${id} --json`));
    assert.match(view, /Readiness is not validated by this static view/);
  }

  await appendMemoryEvent(root, {
    id: "invalid-reserved-procedure", kind: "workflow", subjectType: "task", title: "Malformed reserved procedure",
    body: "The payload must not silently enter recall.", structuredData: { procedure: { schemaVersion: 99 } },
    provenance: { actor: "importer", method: "explicit" }, authority: "agent",
  }, { now });
  await assert.rejects(recallProcedures(root, "repair login token"), /invalid procedure payload/);
  await refreshRepoBrain(root, { now });
  const invalidView = await readFile(join(root, ".provena", "views", "workflows.md"), "utf8");
  assert(!invalidView.includes("The payload must not silently enter recall."), "malformed reserved payloads cannot become ordinary workflow advice");
  console.log("procedure lifecycle, abstention, exact-version outcomes, freshness, safety, and budget tests passed");
} finally {
  await rm(root, { recursive: true, force: true });
}
