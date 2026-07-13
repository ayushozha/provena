import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, truncate, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  buildRepoGraph,
  inspectRepoBrainArtifactIntegrity,
  refreshRepoBrain,
  verifyRepoMemory,
  verifyStoredRepoMemory,
} from "../dist/index.js";

const sha256 = (value) => createHash("sha256").update(value).digest("hex");
const clone = (value) => structuredClone(value);
const failed = (report, name) => report.checks.some(
  (check) => check.name === name && (check.passed === false || check.status === "fail"),
);

const root = await mkdtemp(join(tmpdir(), "provena-temporal-harness-"));
try {
  await writeFile(
    join(root, "package.json"),
    `${JSON.stringify({
      name: "temporal-harness-fixture",
      scripts: { test: "node test" },
    }, null, 2)}\n`,
    "utf8",
  );
  const generation = await refreshRepoBrain(root, {
    now: () => new Date("2026-07-13T12:00:00.000Z"),
  });
  assert.equal(generation.graph.schemaVersion, 2);
  const baseline = inspectRepoBrainArtifactIntegrity(
    generation.map,
    generation.graph,
    generation.manifest,
    generation.memory,
  );
  assert.equal(baseline.passed, true);

  const memoryIndex = generation.graph.nodes.findIndex((node) => node.type === "memory");
  assert.notEqual(memoryIndex, -1, "fixture refresh must create a managed memory node");

  const inspectGraph = (mutate) => {
    const graph = clone(generation.graph);
    mutate(graph, graph.nodes[memoryIndex]);
    return inspectRepoBrainArtifactIntegrity(
      generation.map,
      graph,
      generation.manifest,
      generation.memory,
    );
  };

  const fingerprintMismatch = inspectGraph((graph) => {
    graph.memoryFingerprint = "0".repeat(64);
  });
  assert.equal(fingerprintMismatch.passed, false);
  assert(failed(fingerprintMismatch, "stored-memory-fingerprint"));

  const sourceMismatch = inspectRepoBrainArtifactIntegrity(
    { ...generation.map, sourceFingerprint: "1".repeat(64) },
    generation.graph,
    generation.manifest,
    generation.memory,
  );
  assert.equal(sourceMismatch.passed, false);
  assert(failed(sourceMismatch, "stored-source-fingerprint"));

  const selfMismatchedMap = clone(generation.map);
  selfMismatchedMap.repository.name = "coordinated map tamper";
  const rebuiltFromTamper = buildRepoGraph(selfMismatchedMap, generation.memory);
  const mapSelfMismatch = inspectRepoBrainArtifactIntegrity(
    selfMismatchedMap,
    rebuiltFromTamper,
    generation.manifest,
    generation.memory,
  );
  assert.equal(mapSelfMismatch.passed, false);
  assert(failed(mapSelfMismatch, "stored-source-fingerprint"));

  for (const [name, mutate, expectedCheck] of [
    ["earlier validTo", (_graph, node) => { node.metadata.validTo = "2020-01-01T00:00:00.000Z"; }, "stored-temporal-nodes"],
    ["noncanonical validFrom", (_graph, node) => { node.metadata.validFrom = "2026-07-13T12:00:00Z"; }, "stored-temporal-nodes"],
    ["null nonactive interval", (_graph, node) => {
      node.metadata.declaredStatus = "retracted";
      node.metadata.validTo = null;
    }, "stored-temporal-nodes"],
    ["invalid kind", (_graph, node) => { node.metadata.kind = "invented"; }, "stored-temporal-nodes"],
    ["invalid subject type", (_graph, node) => { node.metadata.subjectType = "invented"; }, "stored-temporal-nodes"],
    ["invalid declared status", (_graph, node) => { node.metadata.declaredStatus = "invented"; }, "stored-temporal-nodes"],
    ["invalid authority", (_graph, node) => { node.metadata.authority = "invented"; }, "stored-temporal-nodes"],
    ["invalid sensitivity", (_graph, node) => { node.metadata.sensitivity = "invented"; }, "stored-temporal-nodes"],
    ["invalid confidence range", (_graph, node) => { node.metadata.confidence = 2; }, "stored-temporal-nodes"],
    ["invalid importance range", (_graph, node) => { node.metadata.importance = -1; }, "stored-temporal-nodes"],
    ["invalid namespaced event id", (_graph, node) => { node.metadata.eventId = "other-memory"; }, "stored-temporal-nodes"],
    ["unexpected memory metadata", (_graph, node) => { node.metadata.body = "must not be projected"; }, "stored-temporal-nodes"],
    ["dangling endpoint", (graph) => { graph.edges[0].to = "missing:node"; }, "stored-graph-references"],
  ]) {
    const report = inspectGraph(mutate);
    assert.equal(report.passed, false, name);
    assert(failed(report, expectedCheck), `${name} must fail ${expectedCheck}`);
  }

  const ledgerMismatchManifest = clone(generation.manifest);
  ledgerMismatchManifest.artifacts.find(
    (artifact) => artifact.path === ".provena/memory/events.jsonl",
  ).sha256 = "2".repeat(64);
  const ledgerMismatch = inspectRepoBrainArtifactIntegrity(
    generation.map,
    generation.graph,
    ledgerMismatchManifest,
    generation.memory,
  );
  assert.equal(ledgerMismatch.passed, false);
  assert(failed(ledgerMismatch, "stored-ledger-attestation"));

  const snapshotMismatch = inspectRepoBrainArtifactIntegrity(
    generation.map,
    generation.graph,
    generation.manifest,
    { ...generation.memory, rawLedger: `${generation.memory.rawLedger}\n` },
  );
  assert.equal(snapshotMismatch.passed, false);
  assert(failed(snapshotMismatch, "stored-memory-snapshot"));

  const aggregateManifest = clone(generation.manifest);
  aggregateManifest.artifacts.find(
    (artifact) => artifact.path === ".provena/repo.map.json",
  ).bytes = 64 * 1024 * 1024;
  aggregateManifest.artifacts.find(
    (artifact) => artifact.path === ".provena/graph.json",
  ).bytes = 64 * 1024 * 1024;
  const aggregateManifestFailure = inspectRepoBrainArtifactIntegrity(
    generation.map,
    generation.graph,
    aggregateManifest,
    generation.memory,
  );
  assert.equal(aggregateManifestFailure.passed, false);
  assert(failed(aggregateManifestFailure, "stored-artifact-shapes"));

  const coordinatedGraph = inspectGraph((_graph, node) => {
    node.label = "coordinated graph tamper";
  });
  assert.equal(coordinatedGraph.passed, false);
  assert(failed(coordinatedGraph, "stored-canonical-graph"));

  for (const malformed of [
    [{}, {}, {}, {}],
    [generation.map, { ...generation.graph, nodes: null }, generation.manifest, generation.memory],
    [generation.map, generation.graph, { ...generation.manifest, artifacts: null }, generation.memory],
    [generation.map, generation.graph, generation.manifest, { ...generation.memory, events: null }],
  ]) {
    assert.doesNotThrow(() => inspectRepoBrainArtifactIntegrity(...malformed));
    assert.equal(inspectRepoBrainArtifactIntegrity(...malformed).passed, false);
  }

  const graphPath = join(root, ".provena", "graph.json");
  const mapPath = join(root, ".provena", "repo.map.json");
  const manifestPath = join(root, ".provena", "manifest.json");
  const originalGraph = await readFile(graphPath);
  const originalMap = await readFile(mapPath);
  const originalManifest = await readFile(manifestPath);
  const restore = async () => {
    await writeFile(graphPath, originalGraph);
    await writeFile(mapPath, originalMap);
    await writeFile(manifestPath, originalManifest);
  };
  const writeManifest = (manifest) => writeFile(
    manifestPath,
    `${JSON.stringify(manifest, null, 2)}\n`,
    "utf8",
  );

  for (const [name, artifacts] of [
    ["empty", []],
    ["duplicate", [
      ...generation.manifest.artifacts,
      generation.manifest.artifacts[0],
    ]],
    ["missing", generation.manifest.artifacts.filter(
      (artifact) => artifact.path !== ".provena/graph.json",
    )],
    ["extra", [
      ...generation.manifest.artifacts,
      { path: ".provena/extra.json", sha256: "0".repeat(64), bytes: 0 },
    ]],
  ]) {
    await writeManifest({ ...generation.manifest, artifacts });
    const report = await verifyStoredRepoMemory(root);
    assert.equal(report.passed, false, `${name} manifest artifact set must fail`);
    await restore();
  }

  const tamperedGraph = clone(generation.graph);
  tamperedGraph.nodes[memoryIndex].label = "coordinated graph tamper";
  const tamperedGraphBytes = Buffer.from(`${JSON.stringify(tamperedGraph, null, 2)}\n`);
  const coordinatedManifest = clone(generation.manifest);
  const graphArtifact = coordinatedManifest.artifacts.find(
    (artifact) => artifact.path === ".provena/graph.json",
  );
  graphArtifact.bytes = tamperedGraphBytes.byteLength;
  graphArtifact.sha256 = sha256(tamperedGraphBytes);
  await writeFile(graphPath, tamperedGraphBytes);
  await writeManifest(coordinatedManifest);
  const coordinatedFailure = await verifyStoredRepoMemory(root);
  assert.equal(coordinatedFailure.passed, false);
  assert(failed(coordinatedFailure, "stored-artifact-parse"));
  await restore();

  await writeFile(graphPath, '{"schemaVersion":2,"nodes":null}\n', "utf8");
  await assert.doesNotReject(() => verifyStoredRepoMemory(root));
  assert.equal((await verifyStoredRepoMemory(root)).passed, false);
  await restore();

  await truncate(graphPath, 64 * 1024 * 1024 + 1);
  const oversized = await verifyStoredRepoMemory(root);
  assert.equal(oversized.passed, false);
  assert(failed(oversized, "stored-artifact-parse"));
  await restore();

  await truncate(graphPath, 64 * 1024 * 1024);
  await truncate(mapPath, 64 * 1024 * 1024);
  const aggregateOversized = await verifyStoredRepoMemory(root);
  assert.equal(aggregateOversized.passed, false);
  assert(failed(aggregateOversized, "stored-artifact-parse"));
  await restore();

  const fingerprintGraph = clone(generation.graph);
  fingerprintGraph.memoryFingerprint = "0".repeat(64);
  await writeFile(graphPath, `${JSON.stringify(fingerprintGraph, null, 2)}\n`, "utf8");
  const corruptBytes = await readFile(graphPath, "utf8");
  assert.equal((await verifyStoredRepoMemory(root)).passed, false);
  const harnessFailure = await verifyRepoMemory(root);
  assert.equal(harnessFailure.passed, false);
  assert.equal(
    await readFile(graphPath, "utf8"),
    corruptBytes,
    "harness must not repair a corrupt generation before reporting it",
  );

  await restore();
  assert.equal((await verifyStoredRepoMemory(root)).passed, true);
  console.log("temporal memory harness tests passed");
} finally {
  await rm(root, { recursive: true, force: true });
}
