import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import {
  appendMemoryEvent,
  createDefaultConfig,
  readMemoryLedgerSnapshot,
  readMemoryEvents,
  refreshRepoBrain,
  writeConfig,
} from "../dist/index.js";
import { createRepoMcpServer } from "../dist/mcp/server.js";

const root = mkdtempSync(join(tmpdir(), "provena-mcp-"));
let client;
let server;
try {
  mkdirSync(join(root, "src"), { recursive: true });
  writeFileSync(join(root, "package.json"), JSON.stringify({ name: "mcp-fixture", scripts: { test: "node --test" } }));
  writeFileSync(join(root, "src", "auth.ts"), "export function authorize() { return true; }\n");
  writeConfig(root, createDefaultConfig({ cwd: root, gitRoot: root }));
  await refreshRepoBrain(root);
  const first = await appendMemoryEvent(root, {
    kind: "decision",
    subjectType: "file",
    title: "Historical authorization branch",
    body: "Use the historical authorization workflow at this boundary.",
    appliesTo: ["src/auth.ts"],
    sources: [{ path: "src/auth.ts", startLine: 1 }],
    provenance: { actor: "mcp-test", method: "explicit" },
    authority: "human",
    confidence: 1,
    importance: 1,
    sensitivity: "internal",
    createdAt: "2026-01-01T00:00:00.000Z",
    updatedAt: "2026-01-01T00:00:00.000Z",
  });
  const second = await appendMemoryEvent(root, {
    kind: "decision",
    subjectType: "file",
    title: "Current authorization branch",
    body: "Use the successor authorization workflow now.",
    appliesTo: ["src/auth.ts"],
    sources: [{ path: "src/auth.ts", startLine: 1 }],
    provenance: { actor: "mcp-test", method: "explicit" },
    authority: "human",
    confidence: 1,
    importance: 1,
    sensitivity: "internal",
    createdAt: "2026-03-01T00:00:00.000Z",
    updatedAt: "2026-03-01T00:00:00.000Z",
    supersedes: [first.id],
  });
  await refreshRepoBrain(root);

  server = await createRepoMcpServer(root);
  client = new Client({ name: "provena-test-client", version: "1.0.0" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await Promise.all([server.connect(serverTransport), client.connect(clientTransport)]);

  const tools = await client.listTools();
  for (const name of [
    "provena_context",
    "provena_refresh",
    "provena_remember",
    "provena_graph_neighbors",
    "provena_graph_path",
    "provena_maintenance_plan",
    "provena_maintenance_context",
  ]) {
    assert.ok(tools.tools.some((tool) => tool.name === name), `MCP missing ${name}`);
  }

  const brain = await client.readResource({ uri: "provena://repo/brain" });
  assert.match(brain.contents[0]?.text ?? "", /repo brain/i);

  const context = await client.callTool({
    name: "provena_context",
    arguments: { query: "authorize", maxTokens: 256 },
  });
  assert.match(context.content[0]?.text ?? "", /src\/auth\.ts/);

  const boundary = "2026-02-01T00:00:00.000Z";
  const historicalContext = await client.callTool({
    name: "provena_context",
    arguments: { query: "historical authorization", memoryAsOf: boundary, maxTokens: 512 },
  });
  const historicalPacket = JSON.parse(historicalContext.content[1]?.text ?? "{}");
  assert.equal(historicalPacket.memoryAsOf, boundary);
  assert.equal(historicalPacket.repositoryTopology, "current");
  assert.equal(
    historicalPacket.memoryFingerprint,
    (await readMemoryLedgerSnapshot(root)).memoryFingerprint,
  );
  assert.match(historicalContext.content[0]?.text ?? "", /Historical authorization branch/);
  assert.doesNotMatch(historicalContext.content[0]?.text ?? "", /Current authorization branch/);

  const historicalNeighbors = await client.callTool({
    name: "provena_graph_neighbors",
    arguments: { node: first.id, depth: 0, memoryAsOf: boundary },
  });
  const neighborsPayload = JSON.parse(historicalNeighbors.content[0]?.text ?? "{}");
  assert.equal(neighborsPayload.root.metadata.eventId, first.id);
  assert.equal(neighborsPayload.memoryAsOf, boundary);
  assert.equal(neighborsPayload.memoryFingerprint, historicalPacket.memoryFingerprint);

  const historicalPath = await client.callTool({
    name: "provena_graph_path",
    arguments: { from: first.id, to: "src/auth.ts", memoryAsOf: boundary },
  });
  const pathPayload = JSON.parse(historicalPath.content[0]?.text ?? "{}");
  assert.equal(pathPayload.from.metadata.eventId, first.id);
  assert.equal(pathPayload.to.path, "src/auth.ts");
  assert(pathPayload.path.length >= 2);
  assert.equal(pathPayload.memoryFingerprint, historicalPacket.memoryFingerprint);

  const hostileBoundary = "not-a-time-secret\u001b[31m";
  for (const request of [
    { name: "provena_context", arguments: { query: "authorization", memoryAsOf: hostileBoundary } },
    { name: "provena_graph_neighbors", arguments: { node: first.id, memoryAsOf: hostileBoundary } },
    { name: "provena_graph_path", arguments: { from: first.id, to: "src/auth.ts", memoryAsOf: hostileBoundary } },
  ]) {
    const invalid = await client.callTool(request);
    assert.equal(invalid.isError, true);
    const message = invalid.content[0]?.text ?? "";
    assert.match(message, /memoryAsOf|canonical UTC/i);
    assert(!message.includes("not-a-time-secret"));
    assert(!message.includes("\u001b"));
  }

  const mapPath = join(root, ".provena", "repo.map.json");
  const storedMap = readFileSync(mapPath, "utf8");
  writeFileSync(
    join(root, "src", "auth.ts"),
    "export function authorize() { return true; }\nexport const changedAfterRefresh = true;\n",
  );
  await client.callTool({
    name: "provena_context",
    arguments: { query: "authorize", maxTokens: 256 },
  });
  assert.equal(
    readFileSync(mapPath, "utf8"),
    storedMap,
    "read-only MCP tools must not refresh or mutate tracked artifacts",
  );

  const remembered = await client.callTool({
    name: "provena_remember",
    arguments: {
      kind: "decision",
      title: "Keep authorization explicit",
      body: "Authorization behavior remains explicit and testable.",
      sources: [{ path: "src/auth.ts", startLine: 1 }],
    },
  });
  assert.match(remembered.content[0]?.text ?? "", /Keep authorization explicit/);
  const memories = await readMemoryEvents(root);
  assert.equal(
    memories.filter((event) => event.title === "Keep authorization explicit").length,
    1,
    "the explicit MCP decision must be appended once alongside managed observations",
  );

  const rejected = await client.callTool({
      name: "provena_remember",
      arguments: {
        kind: "fact",
        title: "Leaked credential",
        body: "api_key=abcdefghijklmnop123456",
      },
    });
  assert.equal(rejected.isError, true);
  assert.match(rejected.content[0]?.text ?? "", /credential|private key/i);

  const outside = join(root, "outside-resource.txt");
  const mapResource = join(root, ".provena", "repo.map.json");
  const originalMap = readFileSync(mapResource, "utf8");
  writeFileSync(outside, "must not be exposed through MCP\n", "utf8");
  try {
    rmSync(mapResource);
    symlinkSync(outside, mapResource, "file");
    await assert.rejects(
      client.readResource({ uri: "provena://repo/map" }),
      /symbolic link|safe repository path|symlink/i,
    );
  } catch (error) {
    if (!["EPERM", "EACCES", "UNKNOWN"].includes(error?.code)) throw error;
  } finally {
    rmSync(mapResource, { force: true });
    writeFileSync(mapResource, originalMap, "utf8");
  }
} finally {
  await client?.close();
  await server?.close();
  rmSync(root, { recursive: true, force: true });
}

console.log("mcp-server.test: ok");
