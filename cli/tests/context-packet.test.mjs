import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { memoryEventToRecord } from "../dist/brain/events.js";
import { canonicalJson } from "../dist/brain/utils.js";
import { buildContextPacket, renderContextPacketMarkdown } from "../dist/context/index.js";
import { buildRepoGraph } from "../dist/graph/index.js";

const map = {
  schemaVersion: 1,
  repository: { name: "context-fixture", description: null },
  sourceFingerprint: "source-fingerprint",
  languages: ["typescript"],
  directories: [{ id: "dir-src", path: "src", fileCount: 2 }],
  files: [
    { id: "file-app", path: "src/app.ts", kind: "source", language: "typescript", sizeBytes: 100, sha256: "a", imports: ["./util.js"] },
    { id: "file-util", path: "src/util.ts", kind: "source", language: "typescript", sizeBytes: 80, sha256: "b", imports: [] },
    { id: "file-package", path: "package.json", kind: "manifest", language: "json", sizeBytes: 50, sha256: "c", imports: [] },
  ],
  symbols: [{ id: "symbol-run", path: "src/app.ts", name: "run", kind: "function", line: 4, exported: true }],
  commands: [{ id: "cmd:test", name: "test", command: "npm run test", cwd: ".", source: "package.json" }],
  packages: [{ id: "package-root", path: ".", manifestPath: "package.json", name: "context-fixture", ecosystem: "node", dependencies: [], commandIds: ["cmd:test"] }],
};
const graph = buildRepoGraph(map);
const events = [{
  schemaVersion: 1,
  id: "memory-workflow",
  kind: "workflow",
  subjectType: "test",
  title: "Run focused tests",
  body: "Use npm run test before handing off a change.",
  structuredData: {},
  status: "active",
  appliesTo: ["src/app.ts"],
  sources: [{ path: "package.json" }],
  provenance: { actor: "maintainer", method: "explicit" },
  authority: "human",
  confidence: 1,
  importance: 0.9,
  sensitivity: "internal",
  createdAt: "2026-07-09T12:00:00.000Z",
  updatedAt: "2026-07-09T12:00:00.000Z",
  supersedes: [],
  tags: ["testing"],
  triggers: ["handoff"],
}];
const memory = {
  events,
  memoryFingerprint: "raw-ledger-fingerprint",
  bytes: 1,
};

const packet = buildContextPacket(map, graph, memory, {
  query: "run test app",
  paths: ["src/app.ts"],
  symbols: ["run"],
  commands: ["npm run test"],
  maxCharacters: 1_200,
  maxTokens: 300,
  graphHops: 1,
});
assert.equal(packet.memoryFingerprint, memory.memoryFingerprint);
assert.equal(packet.items[0].id, "file-app", "exact path matches outrank all fuzzy matches");
assert(packet.items.some((item) => item.id === "symbol-run"));
assert(packet.items.some((item) => item.id === "cmd:test"));
assert(packet.items.some((item) => item.id === "memory-workflow"));
assert(packet.items.every((item) => item.citations.length > 0));
assert(packet.budget.usedCharacters <= packet.budget.maxCharacters);
assert(packet.budget.estimatedTokens <= packet.budget.maxTokens);
const markdown = renderContextPacketMarkdown(packet);
assert.equal(packet.budget.usedCharacters, Buffer.byteLength(markdown, "utf8"));
assert(Buffer.byteLength(markdown, "utf8") <= packet.budget.maxCharacters);
assert(markdown.includes("src/app.ts"));
assert(markdown.includes("package.json"));
assert(!markdown.includes("C:\\"));

const clipped = buildContextPacket(map, graph, memory, {
  query: "run test app",
  maxCharacters: 256,
  maxTokens: 64,
});
assert(clipped.budget.truncated);
assert(clipped.budget.usedCharacters <= 256);
const clippedMarkdown = renderContextPacketMarkdown(clipped);
assert.equal(clipped.budget.usedCharacters, Buffer.byteLength(clippedMarkdown, "utf8"));
assert(Buffer.byteLength(clippedMarkdown, "utf8") <= 256);
assert(Math.ceil(Buffer.byteLength(clippedMarkdown, "utf8") / 4) <= 64);

const longQuery = buildContextPacket(map, graph, memory, {
  query: "#[]*`<long-query> ".repeat(500),
  maxCharacters: 256,
  maxTokens: 64,
});
const longQueryMarkdown = renderContextPacketMarkdown(longQuery);
assert(longQuery.budget.truncated);
assert(longQuery.query.endsWith("…"));
assert.equal(longQuery.budget.usedCharacters, Buffer.byteLength(longQueryMarkdown, "utf8"));
assert(longQuery.budget.usedCharacters <= 256);
assert(longQuery.budget.estimatedTokens <= 64);

const legacyEmpty = buildContextPacket(map, graph, [], { maxCharacters: 256, maxTokens: 64 });
assert.equal(
  legacyEmpty.memoryFingerprint,
  createHash("sha256").update("").digest("hex"),
  "the backward-compatible array API must use canonical empty-ledger bytes",
);

const legacyNonempty = buildContextPacket(map, graph, events, {
  query: "handoff",
  maxCharacters: 4_000,
  maxTokens: 1_000,
});
const canonicalLedger = events
  .map((event) => canonicalJson(memoryEventToRecord(event)))
  .join("");
assert.equal(
  legacyNonempty.memoryFingerprint,
  createHash("sha256").update(canonicalLedger).digest("hex"),
  "the backward-compatible array API must fingerprint canonical JSONL records",
);
assert(
  legacyNonempty.items.some((item) => item.id === events[0].id),
  "the backward-compatible array API must preserve memory selection",
);

console.log("context packet tests passed");
