import assert from "node:assert/strict";
import { neo4jConfigFromEnv, neo4jRepositoryId, syncGraphToNeo4j } from "../dist/index.js";

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
  verifyConnectivity: async () => undefined,
  session: () => session,
  close: async () => {
    throw new Error("supplied driver must not be closed");
  },
};
const graph = {
  schemaVersion: 1,
  sourceFingerprint: "fingerprint-1",
  nodes: [
    { id: "repo", type: "repository", label: "demo", metadata: {} },
    { id: "file", type: "file", label: "index.ts", path: "src/index.ts", metadata: { language: "typescript" } },
  ],
  edges: [{ id: "contains", from: "repo", to: "file", type: "contains", weight: 1 }],
};

const result = await syncGraphToNeo4j(
  graph,
  "demo-repo",
  { uri: "neo4j://localhost", username: "neo4j", password: "not-used" },
  driver,
);
assert.deepEqual(result, {
  repoId: "demo-repo",
  sourceFingerprint: "fingerprint-1",
  nodes: 2,
  edges: 1,
});
assert.ok(calls.some((call) => call.query.includes("CREATE CONSTRAINT")));
assert.ok(calls.some((call) => call.query.includes("UNWIND $nodes")));
assert.ok(calls.some((call) => call.query.includes("UNWIND $edges")));
assert.ok(calls.some((call) => call.query.includes("DETACH DELETE")));
assert.equal(
  calls.some((call) => JSON.stringify(call.params).includes("not-used")),
  false,
  "password is never sent as a Cypher parameter",
);

console.log("neo4j-adapter.test: ok");
