import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  neo4jConfigFromEnv,
  neo4jRepositoryId,
  syncGraphToNeo4j,
} from "../dist/index.js";

assert.throws(
  () => neo4jConfigFromEnv({}),
  /PROVENA_NEO4J_URI/,
);
assert.throws(
  () =>
    neo4jConfigFromEnv({
      PROVENA_NEO4J_URI: "neo4j://db.example.test",
      PROVENA_NEO4J_USERNAME: "neo4j",
      PROVENA_NEO4J_PASSWORD: "secret",
    }),
  /encrypted/,
);
assert.doesNotThrow(() =>
  neo4jConfigFromEnv({
    PROVENA_NEO4J_URI: "neo4j://db.example.test",
    PROVENA_NEO4J_USERNAME: "neo4j",
    PROVENA_NEO4J_PASSWORD: "secret",
    PROVENA_NEO4J_ALLOW_INSECURE: "1",
  }),
);
assert.notEqual(
  neo4jRepositoryId("tenant", "repo-uuid-one"),
  neo4jRepositoryId("tenant", "repo-uuid-two"),
);
assert.notEqual(
  neo4jRepositoryId("a:b", "ccc"),
  neo4jRepositoryId("a", "b:ccc"),
  "repository identity encoding must be injective",
);
assert.throws(
  () =>
    neo4jConfigFromEnv({
      PROVENA_NEO4J_URI: "https://example.com",
      PROVENA_NEO4J_USERNAME: "neo4j",
      PROVENA_NEO4J_PASSWORD: "secret",
    }),
  /must use neo4j/,
);
assert.throws(
  () =>
    neo4jConfigFromEnv({
      PROVENA_NEO4J_URI: "neo4j+s://embedded-user:embedded-password@db.example.test",
      PROVENA_NEO4J_USERNAME: "neo4j",
      PROVENA_NEO4J_PASSWORD: "secret",
    }),
  /must not contain credentials/,
);

// This is deliberately independent of production constants and helpers. The
// namespace, integer version, canonical tuple bytes, and expected token are
// frozen here so a coordinated implementation-and-test drift cannot pass.
function independentProjectionFingerprint(sourceFingerprint, memoryFingerprint) {
  const tuple = [
    "provena.neo4j.graph",
    2,
    sourceFingerprint,
    memoryFingerprint,
  ];
  return createHash("sha256")
    .update(`${JSON.stringify(tuple)}\n`, "utf8")
    .digest("hex");
}

assert.equal(
  independentProjectionFingerprint("source-a", "memory-a"),
  "83af31ad5dfdf21d4df00aba8d3c0877789e6a2a21247f68c55b988677a93dae",
  "the independently frozen canonical tuple must remain reproducible",
);

const BODY_SENTINEL = ["sk", "proj", "BODYSECRET123456789012345678"].join("-");
const STRUCTURED_SENTINEL = ["gh", "p_STRUCTUREDSECRET1234567890123456"].join("");
const URI_SENTINEL = ["gh", "p_URISECRET1234567890123456789012"].join("");
const USERNAME_SENTINEL = "neo4j-username-sentinel";
const PASSWORD_SENTINEL = ["sk", "proj", "PASSWORDSECRET12345678901234"].join("-");

function graphV2(sourceFingerprint, memoryFingerprint) {
  return {
    schemaVersion: 2,
    sourceFingerprint,
    memoryFingerprint,
    projectionFingerprint: independentProjectionFingerprint(
      sourceFingerprint,
      memoryFingerprint,
    ),
    timeSemantics: "event-effective-time",
    nodes: [
      {
        id: "repo:root",
        type: "repository",
        label: "demo",
        path: ".",
        metadata: { description: null },
      },
      {
        id: "memory:event-1",
        type: "memory",
        label: "Use the verified workflow",
        metadata: {
          eventId: "event-1",
          title: "Use the verified workflow",
          kind: "workflow",
          subjectType: "repo",
          declaredStatus: "active",
          authority: "human",
          confidence: 0.9,
          importance: 0.8,
          sensitivity: "internal",
          validFrom: "2026-07-13T18:00:00.000Z",
          validTo: null,
          body: BODY_SENTINEL,
          structuredData: { raw: STRUCTURED_SENTINEL },
        },
      },
    ],
    edges: [
      {
        id: "memory-applies-to-repo",
        from: "memory:event-1",
        to: "repo:root",
        type: "applies_to",
        weight: 1,
        effectiveAt: "2026-07-13T18:00:00.000Z",
      },
    ],
  };
}

function recordingDriver(connectivityError) {
  const calls = [];
  const run = async (query, params = {}) => {
    calls.push({ query, params });
    return {};
  };
  const session = {
    run,
    executeWrite: async (callback) => callback({ run }),
    close: async () => undefined,
  };
  const driver = {
    verifyConnectivity: async () => {
      if (connectivityError) throw connectivityError;
    },
    session: () => session,
    close: async () => {
      throw new Error("supplied driver must not be closed");
    },
  };
  return { calls, driver };
}

const config = {
  uri: `neo4j://localhost:7687?marker=${URI_SENTINEL}`,
  username: USERNAME_SENTINEL,
  password: PASSWORD_SENTINEL,
};

async function syncFixture(sourceFingerprint, memoryFingerprint) {
  const { calls, driver } = recordingDriver();
  const graph = graphV2(sourceFingerprint, memoryFingerprint);
  const result = await syncGraphToNeo4j(graph, "demo-repo", config, driver);
  return { calls, graph, result };
}

const baseline = await syncFixture("source-a", "memory-a");
const unchanged = await syncFixture("source-a", "memory-a");
const sourceChanged = await syncFixture("source-b", "memory-a");
const memoryChanged = await syncFixture("source-a", "memory-b");

assert.deepEqual(baseline.result, {
  repoId: "demo-repo",
  sourceFingerprint: "source-a",
  memoryFingerprint: "memory-a",
  projectionFingerprint:
    "83af31ad5dfdf21d4df00aba8d3c0877789e6a2a21247f68c55b988677a93dae",
  nodes: 2,
  edges: 1,
});
assert.equal(
  unchanged.result.projectionFingerprint,
  baseline.result.projectionFingerprint,
  "unchanged source and memory inputs must reproduce the same projection",
);
assert.equal(
  sourceChanged.result.projectionFingerprint,
  "0c1fab79c5e705665268a1d0caeb655f0907e9e5a3d805317bf24a47d806289f",
);
assert.equal(
  memoryChanged.result.projectionFingerprint,
  "2cf80c010fac06ecd9943eed2ff183f9c6e8412e688f77e21856815bfc6e2f7c",
);
assert.notEqual(
  sourceChanged.result.projectionFingerprint,
  baseline.result.projectionFingerprint,
  "a source-only change must invalidate the Neo4j projection",
);
assert.notEqual(
  memoryChanged.result.projectionFingerprint,
  baseline.result.projectionFingerprint,
  "a memory-only change must invalidate the Neo4j projection",
);

for (const fixture of [baseline, unchanged, sourceChanged, memoryChanged]) {
  assert.equal(
    fixture.result.projectionFingerprint,
    fixture.graph.projectionFingerprint,
  );
  assert.ok(fixture.calls.some((call) => call.query.includes("CREATE CONSTRAINT")));
  const nodeCall = fixture.calls.find((call) => call.query.includes("UNWIND $nodes"));
  const edgeCall = fixture.calls.find((call) => call.query.includes("UNWIND $edges"));
  assert.ok(nodeCall);
  assert.ok(edgeCall);
  assert.ok(nodeCall.query.includes("node.projection_fingerprint = $projectionFingerprint"));
  assert.ok(edgeCall.query.includes("edge.projection_fingerprint = $projectionFingerprint"));
  assert.ok(edgeCall.query.includes("edge.type = item.type"));
  assert.ok(edgeCall.query.includes("edge.effective_at = item.effectiveAt"));
  assert.ok(edgeCall.query.includes("[edge:PROVENA_RELATION"));
  assert.equal(edgeCall.query.includes(":applies_to"), false);
  assert.equal(edgeCall.query.includes(":supersedes"), false);

  const projectedMemory = nodeCall.params.nodes.find(
    (node) => node.type === "memory",
  );
  assert.deepEqual(Object.keys(projectedMemory.memory).sort(), [
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
  assert.equal(projectedMemory.metadataJson, null);
  assert.equal(
    edgeCall.params.edges[0].effectiveAt,
    "2026-07-13T18:00:00.000Z",
  );

  const dataCalls = fixture.calls.filter(
    (call) => call.query.includes("UNWIND $") || call.query.includes("projection_fingerprint IS NULL"),
  );
  assert.ok(dataCalls.length >= 4);
  for (const call of dataCalls) {
    assert.equal(
      call.params.projectionFingerprint,
      fixture.result.projectionFingerprint,
    );
    assert.equal(call.params.sourceFingerprint, fixture.result.sourceFingerprint);
    assert.equal(call.params.memoryFingerprint, fixture.result.memoryFingerprint);
    assert.equal(call.params.projectionNamespace, "provena.neo4j.graph");
    assert.equal(call.params.projectionVersion, 2);
  }
  const cleanupCalls = fixture.calls.filter((call) =>
    call.query.includes("projection_fingerprint IS NULL"),
  );
  assert.equal(cleanupCalls.length, 2);
  for (const call of cleanupCalls) {
    assert.ok(call.query.includes("<> $projectionFingerprint"));
    assert.equal(call.query.includes("source_fingerprint <>"), false);
    assert.equal(call.query.includes("memory_fingerprint <>"), false);
  }

  const observable = JSON.stringify({ calls: fixture.calls, result: fixture.result });
  for (const sentinel of [
    BODY_SENTINEL,
    STRUCTURED_SENTINEL,
    URI_SENTINEL,
    USERNAME_SENTINEL,
    PASSWORD_SENTINEL,
  ]) {
    assert.equal(
      observable.includes(sentinel),
      false,
      `${sentinel} must not reach Cypher, parameters, or results`,
    );
  }
}

const legacyMemoryFingerprint =
  "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const legacyGraph = {
  schemaVersion: 1,
  sourceFingerprint: "fingerprint-1",
  nodes: [
    { id: "repo", type: "repository", label: "demo", metadata: {} },
    {
      id: "file",
      type: "file",
      label: "index.ts",
      path: "src/index.ts",
      metadata: { language: "typescript" },
    },
  ],
  edges: [
    { id: "contains", from: "repo", to: "file", type: "contains", weight: 1 },
  ],
};
const legacyRecording = recordingDriver();
const legacyResult = await syncGraphToNeo4j(
  legacyGraph,
  "legacy-repo",
  config,
  legacyRecording.driver,
);
assert.deepEqual(legacyResult, {
  repoId: "legacy-repo",
  sourceFingerprint: "fingerprint-1",
  memoryFingerprint: legacyMemoryFingerprint,
  projectionFingerprint:
    "457ec9b2144fb9e14a0f683118e8020e8be9d1a164572572fc09147c9173a8d5",
  nodes: 2,
  edges: 1,
});

const failing = recordingDriver(
  new Error(`driver leaked ${PASSWORD_SENTINEL} and ${URI_SENTINEL}`),
);
await assert.rejects(
  syncGraphToNeo4j(
    graphV2("source-a", "memory-a"),
    "demo-repo",
    config,
    failing.driver,
  ),
  (error) => {
    assert.equal(error.message, "Neo4j graph sync failed");
    assert.equal(error.message.includes(PASSWORD_SENTINEL), false);
    assert.equal(error.message.includes(URI_SENTINEL), false);
    return true;
  },
);

await assert.rejects(
  syncGraphToNeo4j(
    {
      ...graphV2("source-a", "memory-a"),
      projectionFingerprint: "forged-projection-fingerprint",
    },
    "demo-repo",
    config,
    recordingDriver().driver,
  ),
  /does not match its attested inputs/,
);

console.log("neo4j-adapter.test: ok (mock driver; no live Neo4j claim)");
