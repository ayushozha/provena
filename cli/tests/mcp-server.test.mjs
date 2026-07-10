import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import {
  createDefaultConfig,
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
  assert.equal((await readMemoryEvents(root)).length, 1);

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
