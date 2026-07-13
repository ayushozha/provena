import assert from "node:assert/strict";
import { appendFile, mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  activeMemoryEvents,
  appendMemoryEvent,
  readMemoryEvents,
  refreshRepoBrain,
} from "../dist/brain/index.js";

const root = await mkdtemp(join(tmpdir(), "provena-events-"));
try {
  await mkdir(join(root, "src"), { recursive: true });
  await writeFile(join(root, "package.json"), '{"name":"memory-events"}\n');
  await writeFile(join(root, "src", "app.ts"), "export const app = true;\n");
  const now = () => new Date("2026-07-09T12:00:00.000Z");
  await appendMemoryEvent(root, {
    id: "fact-001",
    kind: "fact",
    subjectType: "file",
    title: "App entrypoint",
    body: "src/app.ts is the application entrypoint.",
    provenance: { actor: "indexer", method: "observed" },
    authority: "tool",
    confidence: 0.8,
    importance: 0.7,
    sources: [{ path: "src/app.ts", startLine: 1, endLine: 1 }],
  }, { now });
  await appendMemoryEvent(root, {
    id: "decision-001",
    kind: "decision",
    subjectType: "architecture",
    title: "Use event sourcing",
    body: "Keep durable memory in an append-only ledger.",
    structuredData: { rationale: "History must remain auditable." },
    provenance: { actor: "maintainer", method: "explicit" },
    authority: "human",
    appliesTo: [".provena/memory/events.jsonl"],
    sources: [{ path: "package.json" }],
  }, { now });
  await appendMemoryEvent(root, {
    id: "decision-002",
    kind: "decision",
    subjectType: "architecture",
    title: "Use canonical event sourcing",
    body: "The canonical JSONL ledger supersedes the earlier sketch.",
    provenance: { actor: "maintainer", method: "explicit" },
    authority: "human",
    supersedes: ["decision-001"],
    sources: [{ path: "package.json" }],
  }, { now });

  await assert.rejects(
    appendMemoryEvent(root, {
      id: "preference-001",
      kind: "preference",
      subjectType: "repo",
      title: "Imagined preference",
      body: "This must be rejected.",
      provenance: { actor: "classifier", method: "observed" },
      authority: "tool",
    }, { now }),
    /explicitly recorded/,
  );
  await assert.rejects(
    appendMemoryEvent(root, {
      id: "fact-escape",
      kind: "fact",
      subjectType: "file",
      title: "Escaping source",
      body: "This must be rejected.",
      provenance: { actor: "classifier", method: "observed" },
      authority: "tool",
      sources: [{ path: "../outside.txt" }],
    }, { now }),
    /inside the repository/,
  );
  await assert.rejects(
    appendMemoryEvent(root, {
      id: "restricted-append",
      kind: "fact",
      subjectType: "repo",
      title: "Restricted local memory",
      body: "This must use the governed store instead of Git.",
      provenance: { actor: "maintainer", method: "explicit" },
      authority: "human",
      sensitivity: "restricted",
    }, { now }),
    /Git-tracked repo ledger/,
  );
  await assert.rejects(
    appendMemoryEvent(root, {
      id: "secret-append",
      kind: "handoff",
      subjectType: "task",
      title: "Unsafe checkpoint",
      body: "api_key=abcdefghijklmnop123456",
      provenance: { actor: "maintainer", method: "explicit" },
      authority: "human",
    }, { now }),
    /credential|private key/,
  );
  await assert.rejects(
    appendMemoryEvent(root, {
      id: "agent-supersedes-human",
      kind: "decision",
      subjectType: "architecture",
      title: "Agent override",
      body: "An agent must not silently replace a human decision.",
      provenance: { actor: "agent", method: "explicit" },
      authority: "agent",
      supersedes: ["decision-002"],
    }, { now }),
    /agent memory cannot supersede human memory/,
  );

  const all = await readMemoryEvents(root);
  assert.equal(all.length, 3);
  assert.deepEqual(activeMemoryEvents(all).map((event) => event.id).sort(), ["decision-002", "fact-001"]);
  const ledger = await readFile(join(root, ".provena", "memory", "events.jsonl"), "utf8");
  assert.equal(ledger.trim().split(/\r?\n/).length, 3, "one complete JSON object must be appended per line");
  const stored = JSON.parse(ledger.trim().split(/\r?\n/)[0]);
  assert.equal(stored.schema_version, 1);
  assert.equal(stored.subject_type, "file");
  assert.equal(stored.created_at, "2026-07-09T12:00:00.000Z");
  assert(!("schemaVersion" in stored), "the cross-runtime ledger uses canonical snake_case fields");
  await assert.rejects(
    appendMemoryEvent(root, {
      id: "absolute-applies-to",
      kind: "fact",
      subjectType: "file",
      title: "Unsafe applicability",
      body: "Absolute applicability paths must not enter the portable ledger.",
      appliesTo: [join(root, "src", "app.ts")],
      provenance: { actor: "indexer", method: "observed" },
      authority: "tool",
    }, { now }),
    /repository-relative|inside the repository/,
  );

  const ledgerPath = join(root, ".provena", "memory", "events.jsonl");
  const canonicalLedger = await readFile(ledgerPath, "utf8");
  const withUnknownField = { ...stored, unexpected: true };
  await appendFile(ledgerPath, `${JSON.stringify(withUnknownField)}\n`);
  await assert.rejects(readMemoryEvents(root), /unsupported fields/);
  await writeFile(ledgerPath, canonicalLedger, "utf8");

  const nonCanonicalTimestamp = {
    ...stored,
    id: "non-canonical-time",
    created_at: "2026-07-09T12:00:00Z",
  };
  await appendFile(ledgerPath, `${JSON.stringify(nonCanonicalTimestamp)}\n`);
  await assert.rejects(readMemoryEvents(root), /canonical UTC ISO timestamp/);
  await writeFile(ledgerPath, canonicalLedger, "utf8");

  const concurrentInput = {
    id: "fact-concurrent",
    kind: "fact",
    subjectType: "repo",
    title: "Concurrent fact",
    body: "Only one writer may append this stable memory ID.",
    provenance: { actor: "indexer", method: "observed" },
    authority: "tool",
  };
  const concurrent = await Promise.allSettled([
    appendMemoryEvent(root, concurrentInput, { now }),
    appendMemoryEvent(root, concurrentInput, { now }),
  ]);
  assert.equal(concurrent.filter((result) => result.status === "fulfilled").length, 1);
  assert.equal(concurrent.filter((result) => result.status === "rejected").length, 1);
  assert.equal((await readMemoryEvents(root)).filter((event) => event.id === "fact-concurrent").length, 1);

  await appendMemoryEvent(root, {
    id: "fact-whitespace-optionals",
    kind: "fact",
    subjectType: "file",
    title: "Whitespace optional fields",
    body: "Whitespace-only optional values are omitted canonically.",
    sources: [{ path: "src/app.ts", symbol: "   " }],
    provenance: { actor: "indexer", method: "observed", agent: "   " },
    authority: "tool",
  }, { now });
  const whitespaceEvent = (await readMemoryEvents(root)).find(
    (event) => event.id === "fact-whitespace-optionals",
  );
  assert.equal(whitespaceEvent?.sources[0]?.symbol, undefined);
  assert.equal(whitespaceEvent?.provenance.agent, undefined);

  await assert.rejects(
    appendMemoryEvent(root, {
      id: "secret-in-provenance",
      kind: "fact",
      subjectType: "file",
      title: "Unsafe provenance",
      body: "The body itself is harmless.",
      sources: [{
        path: "src/app.ts",
        symbol: ["xoxb", "1234567890", "abcdefghijklmnop"].join("-"),
      }],
      provenance: { actor: "indexer", method: "observed" },
      authority: "tool",
    }, { now }),
    /credential|private key/,
  );

  await appendFile(
    ledgerPath,
    `${JSON.stringify({
      schema_version: 1,
      id: "restricted-imported",
      kind: "fact",
      subject_type: "repo",
      title: "Imported restricted memory",
      body: "This body must never appear in tracked derived views.",
      structured_data: {},
      status: "active",
      applies_to: [],
      sources: [],
      provenance: { actor: "migration", method: "imported" },
      authority: "system",
      confidence: 1,
      importance: 1,
      sensitivity: "restricted",
      created_at: "2026-07-09T12:00:00.000Z",
      updated_at: "2026-07-09T12:00:00.000Z",
      supersedes: [],
      tags: [],
      triggers: [],
    })}\n`,
  );

  await refreshRepoBrain(root);
  const decisions = await readFile(join(root, ".provena", "views", "decisions.md"), "utf8");
  assert(decisions.includes("Use canonical event sourcing"));
  assert(!decisions.includes("## Use event sourcing"));
  assert(decisions.includes("memory:decision-002") || decisions.includes("package.json"));
  const learnings = await readFile(join(root, ".provena", "views", "learnings.md"), "utf8");
  assert(!learnings.includes("This body must never appear"));

  console.log("repo brain event tests passed");
} finally {
  await rm(root, { recursive: true, force: true });
}
