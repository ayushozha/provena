import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { performance } from "node:perf_hooks";
import { learnProcedure, approveProcedure, recordProcedureOutcome, recallProcedures } from "../dist/procedures/index.js";

// This is a deterministic behavior evaluation, not a claim of model savings or competitor superiority.
const fixtureBytes = await readFile(new URL("./procedure-behavior.json", import.meta.url));
const fixture = JSON.parse(fixtureBytes);
const rows = [];
const clock = { now: () => new Date("2026-10-08T12:00:00.000Z") };
for (const test of fixture.cases) {
  const root = await mkdtemp(join(tmpdir(), "provena-procedure-eval-"));
  try {
    await mkdir(join(root, "src"));
    await writeFile(join(root, "src/session.ts"), "export const validationEnabled = true;\n");
    let { event } = await learnProcedure(root, {
      ...fixture.procedure, episodeId: `learn-${test.id}`, sessionId: `session-${test.id}`, actor: "fixture-harness",
      sources: [{ path: "src/session.ts", startLine: 1 }], prerequisites: test.prerequisites ?? [], verification: [],
    }, clock);
    if (test.approved) ({ event } = await approveProcedure(root, { procedureId: event.id, actor: "fixture-maintainer" }, clock));
    for (const [index, outcome] of test.outcomes.entries()) {
      await recordProcedureOutcome(root, {
        procedureId: event.id, receiptId: `receipt-${test.id}-${index}`, episodeId: `episode-${test.id}-${index}`,
        sessionId: `session-${test.id}`, actor: "fixture-harness", goal: fixture.procedure.goal, outcome,
        verification: outcome === "unknown" ? [] : [{ check: "Synthetic fixture goal attestation", passed: outcome === "success", evidence: "Declared lifecycle test receipt; no agent task was executed." }],
      }, clock);
    }
    if (test.changeSource) await writeFile(join(root, "src/session.ts"), "export const validationChanged = true;\n");
    const start = performance.now();
    const result = await recallProcedures(root, test.query, test.recall);
    const latencyMs = performance.now() - start;
    const observed = { ready: result.ready.length, review: result.review.length };
    if (test.expected.state) observed.state = result.review[0]?.state ?? null;
    if (test.expected.failures !== undefined) observed.failures = [...result.ready, ...result.review][0]?.reliability.failures ?? null;
    if (test.expected.omitted !== undefined) observed.omitted = result.omitted;
    const correct = Object.entries(test.expected).every(([key, value]) => observed[key] === value);
    const budgetCorrect = result.characters === JSON.stringify(result).length &&
      result.characters <= (test.recall?.maxCharacters ?? 16_000) &&
      result.estimatedTokens <= (test.recall?.maxTokens ?? 4_000);
    rows.push({ id: test.id, expected: test.expected, observed, correct: correct && budgetCorrect, latencyMs: +latencyMs.toFixed(3) });
  } catch (error) {
    rows.push({ id: test.id, correct: false, error: error instanceof Error ? error.message : "evaluation error" });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}
const passed = rows.filter((row) => row.correct).length;
console.log(JSON.stringify({
  schemaVersion: 1, evaluation: "procedure eligibility and abstention",
  scope: fixture.description, datasetSha256: createHash("sha256").update(fixtureBytes).digest("hex"),
  runtime: process.version, denominator: fixture.cases.length, evaluated: rows.length, skipped: 0,
  passed, failed: rows.length - passed, externalCompetitorsMeasured: [], rows,
}, null, 2));
if (passed !== fixture.cases.length) process.exitCode = 1;
