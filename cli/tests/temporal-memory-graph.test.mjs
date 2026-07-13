import assert from "node:assert/strict";
import { createHash } from "node:crypto";

import {
  buildRepoGraph,
  buildRepoGraphWithDiagnostics,
  deriveMemoryTemporalRecords,
  induceRepoGraphAt,
  memoryGraphNodeId,
  memoryTimeline,
  repoGraphProjectionFingerprint,
} from "../dist/graph/index.js";
import {
  activeMemoryEventsAt,
  canonicalMemoryAsOf,
  memoryEventToRecord,
} from "../dist/brain/index.js";
import { canonicalJson } from "../dist/brain/utils.js";

const T0 = "2026-07-13T09:59:59.999Z";
const T1 = "2026-07-13T10:00:00.000Z";
const T2 = "2026-07-13T11:00:00.000Z";
const T3 = "2026-07-13T12:00:00.000Z";
const T4 = "2026-07-13T13:00:00.000Z";

function memory(id, createdAt, overrides = {}) {
  return {
    schemaVersion: 1,
    id,
    kind: "fact",
    subjectType: "repo",
    title: `Memory ${id}`,
    body: `Body ${id}`,
    structuredData: {},
    status: "active",
    appliesTo: [],
    sources: [],
    provenance: { actor: "temporal-test", method: "observed" },
    authority: "tool",
    confidence: 0.8,
    importance: 0.6,
    sensitivity: "internal",
    createdAt,
    updatedAt: createdAt,
    supersedes: [],
    tags: [],
    triggers: [],
    ...overrides,
  };
}

function repoMap(overrides = {}) {
  return {
    schemaVersion: 1,
    repository: { name: "temporal-fixture", description: "A deterministic fixture" },
    sourceFingerprint: "a".repeat(64),
    scan: { complete: true, warnings: [] },
    languages: ["TypeScript"],
    directories: [{ id: "directory:src", path: "src", fileCount: 2 }],
    files: [
      {
        id: "file:a",
        path: "src/a.ts",
        kind: "source",
        language: "TypeScript",
        sizeBytes: 10,
        sha256: "b".repeat(64),
        imports: ["./b.js"],
      },
      {
        id: "file:b",
        path: "src/b.ts",
        kind: "source",
        language: "TypeScript",
        sizeBytes: 20,
        sha256: "c".repeat(64),
        imports: [],
      },
    ],
    symbols: [
      { id: "symbol:a:run", path: "src/a.ts", name: "run", kind: "function", line: 1, exported: true },
      { id: "symbol:b:run", path: "src/b.ts", name: "run", kind: "function", line: 2, exported: true },
    ],
    packages: [],
    commands: [],
    environmentVariables: [],
    ...overrides,
  };
}

function eventIds(events) {
  return events.map((event) => event.id).sort();
}

function graphMemoryEventIds(graph) {
  return graph.nodes
    .filter((node) => node.type === "memory")
    .map((node) => node.metadata.eventId)
    .sort();
}

// T02: v2 namespaces memory identities while preserving every code identity.
const map = repoMap();
const collisionShaped = memory("repo:root", T1);
const codeOnly = buildRepoGraph(map);
const graph = buildRepoGraph(map, [collisionShaped]);
assert.equal(codeOnly.schemaVersion, 2);
assert.equal(codeOnly.memoryFingerprint, createHash("sha256").update("").digest("hex"));
assert.deepEqual(
  codeOnly.nodes.map((node) => node.id),
  [
    "directory:src",
    "file:a",
    "file:b",
    "repo:root",
    "symbol:a:run",
    "symbol:b:run",
  ],
  "schema-v2 must preserve the frozen schema-v1 code node identities",
);
assert.deepEqual(
  codeOnly.edges.map((edge) => edge.id),
  [
    "edge:d2f8f0061c45d00becdc",
    "edge:65ebbff1f3f328df9f6f",
    "edge:8ccdc6bb4537222497a9",
    "edge:c88af1e9b29974b26e54",
    "edge:485e3ac6a7a11a3b6ca5",
    "edge:3388091faddf95d45c32",
  ],
  "schema-v2 must preserve the frozen schema-v1 code edge identities",
);
assert(graph.nodes.some((node) => node.id === "repo:root" && node.type === "repository"));
assert(graph.nodes.some((node) => node.id === "memory:repo:root" && node.type === "memory"));
assert.deepEqual(
  graph.nodes.filter((node) => node.type !== "memory"),
  codeOnly.nodes,
  "adding memory must not alter code nodes",
);
assert.deepEqual(
  graph.edges.filter((edge) => !["supersedes", "cites", "applies_to"].includes(edge.type)),
  codeOnly.edges,
  "adding memory must not alter code edges",
);

const reorderedMap = repoMap({
  directories: [...map.directories].reverse(),
  files: [...map.files].reverse(),
  symbols: [...map.symbols].reverse(),
  languages: [...map.languages].reverse(),
});
assert.deepEqual(buildRepoGraph(reorderedMap, [collisionShaped]), graph);

assert.throws(
  () => buildRepoGraph(repoMap({
    files: [{ ...map.files[0], id: "memory:repo:root" }, map.files[1]],
  }), [collisionShaped]),
  /node id collision/,
);
assert.throws(
  () => buildRepoGraph(repoMap({
    files: [map.files[0], { ...map.files[1], id: map.files[0].id }],
  })),
  /node id collision/,
);
assert.throws(
  () => buildRepoGraph(repoMap({
    files: [{ ...map.files[0], imports: ["./b.js", "./b.js"] }, map.files[1]],
  })),
  /edge id collision/,
);
const duplicateCommand = {
  id: "command:duplicate",
  name: "duplicate",
  command: "npm test",
  cwd: ".",
  source: "package.json",
};
assert.throws(
  () => buildRepoGraph(repoMap({
    commands: [duplicateCommand, { ...duplicateCommand }],
  })),
  /command id collision/,
);

const sharedDependency = buildRepoGraph(repoMap({
  packages: [
    { id: "package:a", path: ".", manifestPath: "a.json", name: "a", ecosystem: "node", dependencies: ["shared"], commandIds: [] },
    { id: "package:b", path: ".", manifestPath: "b.json", name: "b", ecosystem: "node", dependencies: ["shared"], commandIds: [] },
  ],
}));
assert.equal(
  sharedDependency.nodes.filter((node) => node.type === "dependency" && node.label === "shared").length,
  1,
  "the one intentional node-reuse path is a canonical shared dependency",
);
assert.equal(sharedDependency.edges.filter((edge) => edge.type === "depends_on").length, 2);

const canonicalLine = canonicalJson(memoryEventToRecord(collisionShaped)).slice(0, -1);
const rawLedger = `${canonicalLine}\r\n\r\n`;
const attestedSnapshot = {
  events: [collisionShaped],
  rawLedger,
  bytes: Buffer.byteLength(rawLedger),
  memoryFingerprint: createHash("sha256").update(rawLedger).digest("hex"),
};
assert.equal(buildRepoGraph(map, attestedSnapshot).memoryFingerprint, attestedSnapshot.memoryFingerprint);
assert.throws(
  () => buildRepoGraph(map, { ...attestedSnapshot, events: [memory("other", T1)] }),
  /events do not match/,
);

const independentProjection = createHash("sha256")
  .update(canonicalJson(["provena.neo4j.graph", 2, map.sourceFingerprint, graph.memoryFingerprint]))
  .digest("hex");
assert.equal(graph.projectionFingerprint, independentProjection);
assert.equal(
  repoGraphProjectionFingerprint(map.sourceFingerprint, graph.memoryFingerprint),
  independentProjection,
);

// T03: memory graph metadata is an explicit bounded allowlist.
const sentinels = {
  title: "ALLOWED_TITLE_SENTINEL",
  body: "OMITTED_BODY_SENTINEL",
  structured: "OMITTED_STRUCTURED_SENTINEL",
  provenance: "OMITTED_PROVENANCE_SENTINEL",
  tag: "OMITTED_TAG_SENTINEL",
  trigger: "OMITTED_TRIGGER_SENTINEL",
  blob: "OMITTED_BLOB_SENTINEL",
};
const boundedEvent = memory("bounded", T1, {
  title: sentinels.title,
  body: sentinels.body,
  structuredData: { nested: { value: sentinels.structured } },
  provenance: { actor: sentinels.provenance, method: "observed" },
  tags: [sentinels.tag],
  triggers: [sentinels.trigger],
  sources: [{ path: "src/a.ts", blob: sentinels.blob }],
});
const boundedGraph = buildRepoGraph(map, [boundedEvent]);
const boundedNode = boundedGraph.nodes.find((node) => node.id === "memory:bounded");
assert(boundedNode);
assert.deepEqual(Object.keys(boundedNode.metadata).sort(), [
  "authority",
  "confidence",
  "declaredStatus",
  "eventId",
  "importance",
  "kind",
  "sensitivity",
  "subjectType",
  "title",
  "validFrom",
  "validTo",
]);
const boundedBytes = canonicalJson(boundedGraph);
assert(boundedBytes.includes(sentinels.title));
for (const value of Object.values(sentinels).filter((value) => value !== sentinels.title)) {
  assert(!boundedBytes.includes(value), `${value} must not enter graph bytes`);
}

// T04: intervals use direct-successor producer time and exact half-open bounds.
const intervalEvents = [
  memory("A", T1),
  memory("B", T2, { supersedes: ["A"] }),
  memory("R", T3, { status: "retracted", supersedes: ["B"] }),
  memory("declared-superseded", T1, { status: "superseded" }),
  memory("equal-predecessor", T2),
  memory("equal-successor", T2, { supersedes: ["equal-predecessor"] }),
  memory("branch-root", T1),
  memory("branch-early", T2, { supersedes: ["branch-root"] }),
  memory("branch-late", T3, { supersedes: ["branch-root"] }),
  memory("branch-descendant", T4, { supersedes: ["branch-late"] }),
];
const intervalProjection = deriveMemoryTemporalRecords(intervalEvents);
const intervalById = new Map(intervalProjection.records.map((record) => [record.eventId, record]));
assert.deepEqual(
  [intervalById.get("A").validFrom, intervalById.get("A").validTo],
  [T1, T2],
);
assert.deepEqual(
  [intervalById.get("B").validFrom, intervalById.get("B").validTo],
  [T2, T3],
);
assert.equal(intervalById.get("R").validTo, T3);
assert.equal(intervalById.get("declared-superseded").validTo, T1);
assert.equal(intervalById.get("equal-predecessor").validTo, T2);
assert.equal(intervalById.get("equal-successor").validTo, null);
assert.equal(intervalById.get("branch-root").validTo, T2);
assert.deepEqual(intervalById.get("branch-root").successors, ["branch-early", "branch-late"]);
assert.equal(intervalById.get("branch-late").validTo, T4);
assert.deepEqual(eventIds(activeMemoryEventsAt(intervalEvents.slice(0, 3), T1)), ["A"]);
assert.deepEqual(eventIds(activeMemoryEventsAt(intervalEvents.slice(0, 3), T2)), ["B"]);
assert.deepEqual(eventIds(activeMemoryEventsAt(intervalEvents.slice(0, 3), T3)), []);

const attestedOrderEvents = [
  memory("order-root", T1),
  memory("order-z", T2, { supersedes: ["order-root"] }),
  memory("order-a", T3, { supersedes: ["order-root"] }),
  memory("predecessor-z", T1),
  memory("predecessor-a", T1),
  memory("ordered-join", T4, { supersedes: ["predecessor-z", "predecessor-a"] }),
];
const attestedOrder = deriveMemoryTemporalRecords(attestedOrderEvents).records;
assert.deepEqual(
  attestedOrder.map((record) => record.eventId),
  attestedOrderEvents.map((event) => event.id),
  "temporal derivation must retain attested ledger order",
);
const attestedOrderById = new Map(attestedOrder.map((record) => [record.eventId, record]));
assert.deepEqual(attestedOrderById.get("order-root").successors, ["order-z", "order-a"]);
assert.deepEqual(
  attestedOrderById.get("ordered-join").predecessors,
  ["predecessor-z", "predecessor-a"],
  "temporal derivation must retain attested reference order",
);

const intervalGraph = buildRepoGraph(map, intervalEvents);
const temporalEdges = intervalGraph.edges.filter((edge) =>
  ["supersedes", "cites", "applies_to"].includes(edge.type),
);
assert(temporalEdges.every((edge) => canonicalMemoryAsOf(edge.effectiveAt) === edge.effectiveAt));
assert(intervalGraph.edges.some((edge) =>
  edge.type === "supersedes" && edge.from === "memory:B" && edge.to === "memory:A",
));

// T05: only exact current repo, file, and unique path-qualified symbol targets resolve.
const linkedEvent = memory("linked", T2, {
  appliesTo: [".", "src/b.ts", "src", "old/missing.ts"],
  sources: [
    { path: "src/a.ts", symbol: "run" },
    { path: "src/a.ts", symbol: "missing" },
    { path: "SRC/A.TS", symbol: "run" },
    { path: "old/missing.ts", symbol: "run" },
  ],
});
const linkedGraph = buildRepoGraph(map, [linkedEvent]);
const linkedEdges = linkedGraph.edges.filter((edge) => edge.from === "memory:linked");
assert.deepEqual(
  linkedEdges.filter((edge) => edge.type === "cites").map((edge) => edge.to).sort(),
  ["file:a", "symbol:a:run"],
);
assert.deepEqual(
  linkedEdges.filter((edge) => edge.type === "applies_to").map((edge) => edge.to).sort(),
  ["file:b", "repo:root"],
);
const linkedNodeIds = new Set(linkedGraph.nodes.map((node) => node.id));
assert(linkedGraph.edges.every((edge) => linkedNodeIds.has(edge.from) && linkedNodeIds.has(edge.to)));

// T06: diagnostics prove bounded linear work without timing claims or recursion.
function chainEvents(count) {
  return Array.from({ length: count }, (_, index) => {
    const id = `chain-${String(index).padStart(5, "0")}`;
    return memory(id, new Date(Date.UTC(2026, 0, 1, 0, 0, index)).toISOString(), {
      supersedes: index === 0 ? [] : [`chain-${String(index - 1).padStart(5, "0")}`],
    });
  });
}

for (const count of [1_200, 2_400]) {
  const events = chainEvents(count);
  const first = buildRepoGraphWithDiagnostics(repoMap(), events);
  const second = buildRepoGraphWithDiagnostics(repoMap(), events);
  assert.deepEqual(first.diagnostics, {
    eventIndexVisits: count,
    supersessionReferenceVisits: count - 1,
    successorIntervalVisits: count - 1,
    emittedTemporalRecords: count,
  });
  assert.deepEqual(second.diagnostics, first.diagnostics);
  assert.deepEqual(second.graph, first.graph);
}

const fanout = [
  memory("fanout-root", T1),
  ...Array.from({ length: 1_000 }, (_, index) => memory(
    `fanout-${String(index).padStart(4, "0")}`,
    T2,
    { supersedes: ["fanout-root"] },
  )),
];
assert.deepEqual(buildRepoGraphWithDiagnostics(repoMap(), fanout).diagnostics, {
  eventIndexVisits: 1_001,
  supersessionReferenceVisits: 1_000,
  successorIntervalVisits: 1_000,
  emittedTemporalRecords: 1_001,
});

// T07: one generic parser accepts only canonical UTC milliseconds.
assert.equal(canonicalMemoryAsOf(T2), T2);
assert.equal(canonicalMemoryAsOf(), undefined);
const invalidBoundaries = [
  "2026-07-13T11:00:00Z",
  "2026-07-13T11:00:00.00Z",
  "2026-07-13T11:00:00.0000Z",
  "2026-07-13T04:00:00.000-07:00",
  "2026-07-13T11:00:00.000z",
  "2026-02-30T11:00:00.000Z",
  ` ${T2}`,
  `${T2} `,
  `${T2}\u001b[31m`,
  `${T2}\u202E`,
  "x".repeat(10_000),
  "Bearer secret-value-that-must-not-be-reflected",
  42,
  null,
];
let canonicalError;
for (const value of invalidBoundaries) {
  assert.throws(
    () => canonicalMemoryAsOf(value),
    (error) => {
      canonicalError ??= error.message;
      assert.equal(error.message, canonicalError);
      assert(!error.message.includes(String(value)));
      assert(!/[\u001b\u202E]/u.test(error.message));
      return true;
    },
  );
  assert.throws(() => induceRepoGraphAt(intervalGraph, value), (error) => error.message === canonicalError);
}

const originalDateNow = Date.now;
Date.now = () => { throw new Error("wall clock must not be read"); };
try {
  assert.equal(canonicalMemoryAsOf(), undefined);
  assert.deepEqual(activeMemoryEventsAt([collisionShaped]), [collisionShaped]);
  assert.doesNotThrow(() => induceRepoGraphAt(graph));
} finally {
  Date.now = originalDateNow;
}

// T08: selection is an effective-time prefix, including later backdated appends.
const A = memory("backdated-A", T1);
const B = memory("backdated-B", T3, { supersedes: [A.id] });
const beforeBackdate = [A, B];
assert.deepEqual(eventIds(activeMemoryEventsAt(beforeBackdate, T0)), []);
assert.deepEqual(eventIds(activeMemoryEventsAt(beforeBackdate, T1)), [A.id]);
assert.deepEqual(eventIds(activeMemoryEventsAt(beforeBackdate, T2)), [A.id]);
assert.deepEqual(eventIds(activeMemoryEventsAt(beforeBackdate, T3)), [B.id]);

const C = memory("backdated-C", T2, { supersedes: [A.id] });
const afterBackdate = [...beforeBackdate, C];
assert.deepEqual(eventIds(activeMemoryEventsAt(afterBackdate, T2)), [C.id]);
assert.deepEqual(eventIds(activeMemoryEventsAt(afterBackdate, T3)), [B.id, C.id].sort());
const retraction = memory("backdated-R", T4, {
  status: "retracted",
  supersedes: [B.id, C.id],
});
const completeHistory = [...afterBackdate, retraction];
assert.deepEqual(eventIds(activeMemoryEventsAt(completeHistory, T4)), []);

const historyGraph = buildRepoGraph(map, completeHistory);
assert.deepEqual(graphMemoryEventIds(induceRepoGraphAt(historyGraph, T2)), [C.id]);
assert.deepEqual(graphMemoryEventIds(induceRepoGraphAt(historyGraph, T3)), [B.id, C.id].sort());
assert.deepEqual(graphMemoryEventIds(induceRepoGraphAt(historyGraph, T4)), []);
assert.deepEqual(graphMemoryEventIds(induceRepoGraphAt(historyGraph)), []);

const timelineByRawId = memoryTimeline(historyGraph, A.id);
assert.deepEqual(memoryTimeline(historyGraph, memoryGraphNodeId(A.id)), timelineByRawId);
assert.deepEqual(
  timelineByRawId.entries.map((entry) => entry.eventId).sort(),
  [A.id, B.id, C.id, retraction.id].sort(),
);
assert.deepEqual(
  timelineByRawId.entries.find((entry) => entry.eventId === A.id).successors,
  [memoryGraphNodeId(B.id), memoryGraphNodeId(C.id)].sort(),
);
assert.throws(() => memoryTimeline(historyGraph, "missing"), /not found or is ambiguous/);
const ambiguousGraph = buildRepoGraph(map, [memory("x", T1), memory("memory:x", T2)]);
assert.throws(() => memoryTimeline(ambiguousGraph, "memory:x"), /not found or is ambiguous/);

console.log("temporal memory graph tests passed");
