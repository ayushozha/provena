import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  MEMORY_EVENT_SCHEMA_PATH,
  MEMORY_LEDGER_PATH,
  REPO_BRAIN_PATH,
  REPO_BRAIN_MANAGED_ARTIFACT_PATHS,
  REPO_GRAPH_PATH,
  REPO_MANIFEST_PATH,
  memoryEventToRecord,
  readRepoBrainArtifacts,
  repoMapSourceFingerprint,
  refreshRepoBrain,
} from "../dist/brain/index.js";
import { canonicalJson } from "../dist/brain/utils.js";
import { buildContextPacket, renderContextPacketMarkdown } from "../dist/context/index.js";
import { buildRepoGraph } from "../dist/graph/index.js";

const map = {
  schemaVersion: 1,
  repository: { name: "context-fixture", description: null },
  sourceFingerprint: "",
  scan: { complete: true, warnings: [] },
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
  environmentVariables: [],
};
map.sourceFingerprint = repoMapSourceFingerprint(map);
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
const memory = ledgerSnapshot(events);
const graph = buildRepoGraph(map, memory);

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
assert.equal(packet.repositoryTopology, "current");
assert.equal("memoryAsOf" in packet, false, "current packets must not inject a wall-clock boundary");
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
assert(markdown.includes("Memory: current; repository topology: current"));
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

const legacyEmpty = buildContextPacket(map, buildRepoGraph(map, []), [], {
  maxCharacters: 256,
  maxTokens: 64,
});
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

const graphV1 = {
  schemaVersion: 1,
  sourceFingerprint: graph.sourceFingerprint,
  nodes: graph.nodes.filter((node) => node.type !== "memory"),
  edges: graph.edges.filter((edge) => !["supersedes", "cites", "applies_to"].includes(edge.type)),
};
assert.equal(
  buildContextPacket(map, graphV1, memory, { maxCharacters: 4_000, maxTokens: 1_000 })
    .memoryFingerprint,
  memory.memoryFingerprint,
  "a source-attested code-only graph v1 remains readable with ledger-backed context",
);

let attestationError;
for (const [candidateMap, candidateGraph, candidateMemory] of [
  [{ ...map, sourceFingerprint: "map-source-mismatch" }, graph, memory],
  [map, { ...graph, sourceFingerprint: "graph-source-mismatch" }, memory],
  [map, { ...graph, memoryFingerprint: "0".repeat(64) }, memory],
  [map, { ...graph, projectionFingerprint: "1".repeat(64) }, memory],
  [map, graph, { ...memory, memoryFingerprint: "2".repeat(64) }],
  [map, graph, { ...memory, rawLedger: `${memory.rawLedger}\n`, bytes: memory.bytes + 1 }],
]) {
  assert.throws(
    () => buildContextPacket(candidateMap, candidateGraph, candidateMemory),
    (error) => {
      attestationError ??= error.message;
      assert.equal(error.message, attestationError);
      assert.equal(error.message, "context inputs do not describe one attested repo generation");
      assert(!/mismatch|0{16}|1{16}|2{16}/i.test(error.message));
      return true;
    },
  );
}

function ledgerSnapshot(ledgerEvents) {
  const rawLedger = ledgerEvents
    .map((event) => canonicalJson(memoryEventToRecord(event)))
    .join("");
  return {
    events: ledgerEvents,
    rawLedger,
    bytes: Buffer.byteLength(rawLedger, "utf8"),
    memoryFingerprint: createHash("sha256").update(rawLedger).digest("hex"),
  };
}

const history = {
  ...events[0],
  id: "history-high-degree",
  kind: "decision",
  title: "Historical branch",
  body: "This predecessor should appear only at its effective boundary.",
  appliesTo: ["src/app.ts", "src/util.ts", "package.json"],
  createdAt: "2026-01-01T00:00:00.000Z",
  updatedAt: "2026-01-01T00:00:00.000Z",
  supersedes: [],
};
const current = {
  ...events[0],
  id: "current-head",
  kind: "decision",
  title: "Current branch",
  body: "This successor is the current active head.",
  appliesTo: ["src/app.ts"],
  createdAt: "2026-03-01T00:00:00.000Z",
  updatedAt: "2026-03-01T00:00:00.000Z",
  supersedes: [history.id],
};
const fullTemporalMemory = ledgerSnapshot([history, current]);
const minimalCurrentMemory = ledgerSnapshot([{ ...current, supersedes: [] }]);
const fullTemporalGraph = buildRepoGraph(map, fullTemporalMemory);
const minimalCurrentGraph = buildRepoGraph(map, minimalCurrentMemory);
const currentFromHistory = buildContextPacket(map, fullTemporalGraph, fullTemporalMemory, {
  maxCharacters: 12_000,
  maxTokens: 3_000,
});
const currentWithoutHistory = buildContextPacket(map, minimalCurrentGraph, minimalCurrentMemory, {
  maxCharacters: 12_000,
  maxTokens: 3_000,
});
assert(currentFromHistory.items.some((item) => item.id === current.id));
assert(!currentFromHistory.items.some((item) => item.id === history.id));
assert.deepEqual(
  currentFromHistory.items,
  currentWithoutHistory.items,
  "inactive memory history must not change current scores, graph boosts, or ordering",
);
assert.equal(
  currentFromHistory.memoryFingerprint,
  fullTemporalMemory.memoryFingerprint,
  "current context must attest the complete raw ledger rather than its active subset",
);

const boundary = "2026-02-01T00:00:00.000Z";
const historical = buildContextPacket(map, fullTemporalGraph, fullTemporalMemory, {
  memoryAsOf: boundary,
  maxCharacters: 12_000,
  maxTokens: 3_000,
});
assert.equal(historical.memoryAsOf, boundary);
assert.equal(historical.repositoryTopology, "current");
assert.equal(historical.memoryFingerprint, fullTemporalMemory.memoryFingerprint);
assert(historical.items.some((item) => item.id === history.id));
assert(!historical.items.some((item) => item.id === current.id));
assert.match(
  renderContextPacketMarkdown(historical),
  /Memory: historical as of `2026-02-01T00:00:00\.000Z`; repository topology: current/,
);

const hostileBoundary = "not-a-time-secret\u001b[31m";
assert.throws(
  () => buildContextPacket(map, fullTemporalGraph, fullTemporalMemory, {
    memoryAsOf: hostileBoundary,
  }),
  (error) => {
    assert.match(error.message, /memoryAsOf|canonical UTC/i);
    assert(!error.message.includes("not-a-time-secret"));
    assert(!error.message.includes("\u001b"));
    return true;
  },
);

const repoRoot = await mkdtemp(join(tmpdir(), "provena-context-generation-"));
try {
  await writeFile(
    join(repoRoot, "package.json"),
    `${JSON.stringify({
      name: "context-generation-fixture",
      scripts: { test: "node --test" },
    }, null, 2)}\n`,
    "utf8",
  );
  const fixedNow = () => new Date("2026-07-13T12:00:00.000Z");
  const firstGeneration = await refreshRepoBrain(repoRoot, { now: fixedNow });
  const firstGraphBytes = await readFile(
    join(repoRoot, ...REPO_GRAPH_PATH.split("/")),
    "utf8",
  );
  const stored = await readRepoBrainArtifacts(repoRoot);
  const ledgerDigest = createHash("sha256")
    .update(stored.memory.rawLedger)
    .digest("hex");
  const committedPacket = buildContextPacket(
    stored.map,
    stored.graph,
    stored.memory,
  );
  assert.equal(stored.map.sourceFingerprint, stored.graph.sourceFingerprint);
  assert.equal(stored.map.sourceFingerprint, stored.manifest.sourceFingerprint);
  assert.equal(stored.memory.memoryFingerprint, ledgerDigest);
  assert.equal(stored.graph.memoryFingerprint, ledgerDigest);
  assert.equal(stored.manifest.memoryFingerprint, ledgerDigest);
  assert.equal(committedPacket.memoryFingerprint, ledgerDigest);
  assert(Object.isFrozen(REPO_BRAIN_MANAGED_ARTIFACT_PATHS));
  assert.deepEqual(REPO_BRAIN_MANAGED_ARTIFACT_PATHS, [
    ".provena/repo.brain.md",
    ".provena/repo.map.json",
    ".provena/graph.json",
    ".provena/maintenance.plan.json",
    ".provena/schema/memory-event.schema.json",
    ".provena/views/decisions.md",
    ".provena/views/workflows.md",
    ".provena/views/learnings.md",
    ".provena/memory/events.jsonl",
  ]);
  assert.deepEqual(
    stored.manifest.artifacts.map((artifact) => artifact.path),
    [...REPO_BRAIN_MANAGED_ARTIFACT_PATHS],
  );

  const secondGeneration = await refreshRepoBrain(repoRoot, { now: fixedNow });
  assert.equal(
    await readFile(join(repoRoot, ...REPO_GRAPH_PATH.split("/")), "utf8"),
    firstGraphBytes,
    "a no-op locked refresh must preserve graph bytes exactly",
  );
  assert.equal(secondGeneration.graph.projectionFingerprint, firstGeneration.graph.projectionFingerprint);

  const ledgerPath = join(repoRoot, ...MEMORY_LEDGER_PATH.split("/"));
  const schemaPath = join(repoRoot, ...MEMORY_EVENT_SCHEMA_PATH.split("/"));
  const brainPath = join(repoRoot, ...REPO_BRAIN_PATH.split("/"));
  const graphPath = join(repoRoot, ...REPO_GRAPH_PATH.split("/"));
  const manifestPath = join(repoRoot, ...REPO_MANIFEST_PATH.split("/"));
  const ledgerBeforeUpgrade = await readFile(ledgerPath, "utf8");
  const schemaBeforeUpgrade = await readFile(schemaPath, "utf8");
  const syncBytes = (snapshot) => canonicalJson({
    schema_version: 1,
    ledger_path: MEMORY_LEDGER_PATH,
    memory_fingerprint: snapshot.memoryFingerprint,
    ledger_bytes: snapshot.bytes,
    ledger: snapshot.rawLedger,
  });
  const syncBeforeUpgrade = syncBytes(stored.memory);
  const legacyGraph = {
    schemaVersion: 1,
    sourceFingerprint: stored.graph.sourceFingerprint,
    nodes: stored.graph.nodes.filter((node) => node.type !== "memory"),
    edges: stored.graph.edges.filter(
      (edge) => !["supersedes", "cites", "applies_to"].includes(edge.type),
    ),
  };
  const legacyGraphBytes = canonicalJson(legacyGraph, true);
  const currentBrainBytes = await readFile(brainPath, "utf8");
  const currentGraphSummary = `${stored.graph.nodes.length} nodes, ${stored.graph.edges.length} edges`;
  const legacyGraphSummary = `${legacyGraph.nodes.length} nodes, ${legacyGraph.edges.length} edges`;
  assert(currentBrainBytes.includes(currentGraphSummary));
  const legacyBrainBytes = currentBrainBytes.replace(currentGraphSummary, legacyGraphSummary);
  const legacyManifest = structuredClone(stored.manifest);
  const graphEntry = legacyManifest.artifacts.find(
    (artifact) => artifact.path === REPO_GRAPH_PATH,
  );
  assert(graphEntry);
  graphEntry.sha256 = createHash("sha256").update(legacyGraphBytes).digest("hex");
  graphEntry.bytes = Buffer.byteLength(legacyGraphBytes, "utf8");
  const brainEntry = legacyManifest.artifacts.find(
    (artifact) => artifact.path === REPO_BRAIN_PATH,
  );
  assert(brainEntry);
  brainEntry.sha256 = createHash("sha256").update(legacyBrainBytes).digest("hex");
  brainEntry.bytes = Buffer.byteLength(legacyBrainBytes, "utf8");
  await writeFile(brainPath, legacyBrainBytes, "utf8");
  await writeFile(graphPath, legacyGraphBytes, "utf8");
  await writeFile(manifestPath, canonicalJson(legacyManifest, true), "utf8");

  const legacyStored = await readRepoBrainArtifacts(repoRoot);
  assert.equal(legacyStored.graph.schemaVersion, 1);
  assert.equal(legacyStored.memory.rawLedger, ledgerBeforeUpgrade);

  const tamperedLegacyGraph = structuredClone(legacyGraph);
  tamperedLegacyGraph.nodes[0].label = "coordinated legacy tamper";
  const tamperedLegacyBytes = canonicalJson(tamperedLegacyGraph, true);
  const tamperedLegacyManifest = structuredClone(legacyManifest);
  const tamperedLegacyEntry = tamperedLegacyManifest.artifacts.find(
    (artifact) => artifact.path === REPO_GRAPH_PATH,
  );
  assert(tamperedLegacyEntry);
  tamperedLegacyEntry.sha256 = createHash("sha256").update(tamperedLegacyBytes).digest("hex");
  tamperedLegacyEntry.bytes = Buffer.byteLength(tamperedLegacyBytes, "utf8");
  await writeFile(graphPath, tamperedLegacyBytes, "utf8");
  await writeFile(manifestPath, canonicalJson(tamperedLegacyManifest, true), "utf8");
  await assert.rejects(
    readRepoBrainArtifacts(repoRoot),
    (error) => error.message ===
      "stored repo brain artifacts do not describe one committed generation; run `provena refresh`",
  );
  await writeFile(graphPath, legacyGraphBytes, "utf8");
  await writeFile(manifestPath, canonicalJson(legacyManifest, true), "utf8");

  const upgraded = await refreshRepoBrain(repoRoot, { now: fixedNow });
  assert.equal(upgraded.graph.schemaVersion, 2);
  assert.equal(await readFile(ledgerPath, "utf8"), ledgerBeforeUpgrade);
  assert.equal(await readFile(schemaPath, "utf8"), schemaBeforeUpgrade);
  assert.equal(syncBytes(upgraded.memory), syncBeforeUpgrade);
  assert.equal(
    await readFile(join(repoRoot, ...REPO_GRAPH_PATH.split("/")), "utf8"),
    firstGraphBytes,
    "legacy graph v1 must deterministically regenerate the original v2 projection",
  );
  const validManifestText = await readFile(manifestPath, "utf8");
  const malformedManifest = JSON.parse(validManifestText);
  malformedManifest.artifacts[0] = null;
  await writeFile(manifestPath, `${JSON.stringify(malformedManifest)}\n`, "utf8");
  await assert.rejects(
    readRepoBrainArtifacts(repoRoot),
    (error) => error.message ===
      "stored repo brain artifacts do not describe one committed generation; run `provena refresh`",
  );
  await writeFile(manifestPath, validManifestText, "utf8");
} finally {
  await rm(repoRoot, { recursive: true, force: true });
}

console.log("context packet tests passed");
