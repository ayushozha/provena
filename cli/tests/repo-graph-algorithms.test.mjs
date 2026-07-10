import assert from "node:assert/strict";
import {
  connectedComponents,
  degreeCentrality,
  neighborhood,
  pageRank,
  shortestPath,
  buildRepoGraph,
} from "../dist/graph/index.js";

const graph = {
  schemaVersion: 1,
  sourceFingerprint: "fixture",
  nodes: ["a", "b", "c", "d"].map((id) => ({
    id,
    type: "file",
    label: id,
    metadata: {},
  })),
  edges: [
    { id: "a-b", from: "a", to: "b", type: "imports", weight: 1 },
    { id: "b-c", from: "b", to: "c", type: "imports", weight: 1 },
  ],
};

assert.deepEqual(connectedComponents(graph), [["a", "b", "c"], ["d"]]);
assert.deepEqual(shortestPath(graph, "a", "c", "out"), ["a", "b", "c"]);
assert.equal(shortestPath(graph, "c", "a", "out"), null);
assert.deepEqual(shortestPath(graph, "c", "a", "both"), ["c", "b", "a"]);
assert.deepEqual(neighborhood(graph, "a", 1, "out"), { nodeIds: ["a", "b"], edgeIds: ["a-b"] });
assert.deepEqual(neighborhood(graph, "missing", 2), { nodeIds: [], edgeIds: [] });

const degree = degreeCentrality(graph);
assert.equal(degree.b.total, 2 / 3);
assert.equal(degree.d.total, 0);
const rank = pageRank(graph);
assert(Math.abs(Object.values(rank).reduce((sum, value) => sum + value, 0) - 1) < 1e-9);
assert(rank.c > rank.a);
assert.deepEqual(rank, pageRank(graph), "graph algorithms must be deterministic");

const directories = Array.from({ length: 5_000 }, (_, index) => ({
  id: `directory-${index}`,
  path: index === 0 ? "d0" : `d0/d${index}`,
  fileCount: 0,
}));
directories.find = () => {
  throw new Error("graph construction must not linearly scan directories");
};
const largeDirectoryGraph = buildRepoGraph({
  schemaVersion: 1,
  repository: { name: "large-directory-map", description: null },
  sourceFingerprint: "large-directory-fixture",
  languages: [],
  directories,
  files: [],
  symbols: [],
  packages: [],
  commands: [],
  environmentVariables: [],
});
assert.equal(largeDirectoryGraph.nodes.length, 5_001);

const polyglotGraph = buildRepoGraph({
  schemaVersion: 1,
  repository: { name: "polyglot", description: null },
  sourceFingerprint: "polyglot-fixture",
  languages: ["go", "rust"],
  directories: [],
  files: [
    { id: "go-main", path: "cmd/api/main.go", kind: "source", language: "go", sizeBytes: 1, sha256: "1", imports: ["example.com/acme/repo/internal/auth"] },
    { id: "go-auth", path: "internal/auth/auth.go", kind: "source", language: "go", sizeBytes: 1, sha256: "2", imports: [] },
    { id: "rust-lib", path: "rust/src/lib.rs", kind: "source", language: "rust", sizeBytes: 1, sha256: "3", imports: ["crate::engine::run"] },
    { id: "rust-engine", path: "rust/src/engine.rs", kind: "source", language: "rust", sizeBytes: 1, sha256: "4", imports: [] },
  ],
  symbols: [],
  packages: [
    { id: "go-package", path: ".", manifestPath: "go.mod", name: "example.com/acme/repo", ecosystem: "go", dependencies: [], commandIds: [] },
    { id: "rust-package", path: "rust", manifestPath: "rust/Cargo.toml", name: "polyglot-rust", ecosystem: "rust", dependencies: [], commandIds: [] },
  ],
  commands: [],
  environmentVariables: [],
});
assert(polyglotGraph.edges.some((edge) => edge.from === "go-main" && edge.to === "go-auth" && edge.type === "imports"));
assert(polyglotGraph.edges.some((edge) => edge.from === "rust-lib" && edge.to === "rust-engine" && edge.type === "imports"));

console.log("repo graph algorithm tests passed");
