import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  appendMemoryEvent,
  createDefaultConfig,
  readMemoryLedgerSnapshot,
  refreshRepoBrain,
  writeConfig,
} from "../dist/index.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const cli = join(root, "dist", "cli.js");

function run(args, expectStatus, cwd = root) {
  const result = spawnSync(process.execPath, [cli, ...args], {
    encoding: "utf8",
    cwd,
  });
  if (result.status !== expectStatus) {
    console.error(`expected exit ${expectStatus} for provena ${args.join(" ")}, got ${result.status}`);
    console.error(result.stderr || result.stdout);
    process.exit(1);
  }
  return result;
}

run(["index"], 1);
run(["watch"], 1);
run(["not-a-command"], 1);
run(["--help"], 0);
run(["--version"], 0);

const mcpHelp = run(["mcp", "--help"], 0).stdout;
assert.match(mcpHelp, /provena mcp serve --http \[--port <port>\]/);
assert.match(mcpHelp, /127\.0\.0\.1 only and defaults to port 18093/);
const maintainHelp = run(["maintain", "--help"], 0).stdout;
assert.match(maintainHelp, /normal refresh, which may append source-grounded observations/);
assert.doesNotMatch(maintainHelp, /do not[\s\S]*change durable memory/);
const hostileOption = "HTTP_OPTION_SENTINEL\u001b[31m";
for (const args of [
  ["mcp", "serve", "--port", "18094"],
  ["mcp", "serve", "--http", "--port"],
  ["mcp", "serve", "--http", "--http"],
  ["mcp", "serve", "--http", "--port", "18094", "--port", "18095"],
  ["mcp", "serve", "--http", "--port", "+1"],
  ["mcp", "serve", "--http", "--port", "-1"],
  ["mcp", "serve", "--http", "--port", "018093"],
  ["mcp", "serve", "--http", "--port", "1.5"],
  ["mcp", "serve", "--http", "--port", "1e3"],
  ["mcp", "serve", "--http", "--port", "0"],
  ["mcp", "serve", "--http", "--port", "65536"],
  ["mcp", "serve", "--http", "--port", "NaN"],
  ["mcp", "serve", "--http", "--host", "0.0.0.0"],
  ["mcp", "serve", "--http", "--unknown"],
  ["mcp", "serve", "--http", "--port", hostileOption],
]) {
  const result = spawnSync(process.execPath, [cli, ...args], {
    cwd: root,
    encoding: "utf8",
    timeout: 5_000,
  });
  assert.equal(result.status, 1, `${args.join(" ")} must fail before listening`);
  assert.match(result.stderr, /invalid MCP serve options/);
  assert(Buffer.byteLength(result.stderr) < 512, "invalid option errors remain bounded");
  assert(!result.stderr.includes(hostileOption), "invalid options must not be reflected");
  assert.doesNotMatch(result.stderr, /\bat\s+[^\r\n]+:\d+:\d+/u, "invalid option errors omit stacks");
}

const fixture = mkdtempSync(join(tmpdir(), "provena-cli-temporal-"));
try {
  mkdirSync(join(fixture, "src"), { recursive: true });
  writeFileSync(
    join(fixture, "package.json"),
    JSON.stringify({ name: "cli-temporal-fixture", scripts: { test: "node --test" } }),
  );
  writeFileSync(join(fixture, "src", "auth.ts"), "export const authorize = true;\n");
  writeConfig(fixture, createDefaultConfig({ cwd: fixture, gitRoot: fixture }));
  await refreshRepoBrain(fixture);

  const first = await appendMemoryEvent(fixture, {
    kind: "decision",
    subjectType: "file",
    title: "Historical authorization branch",
    body: "Use the first authorization workflow at this boundary.",
    appliesTo: ["src/auth.ts"],
    sources: [{ path: "src/auth.ts", startLine: 1 }],
    provenance: { actor: "cli-test", method: "explicit" },
    authority: "human",
    confidence: 1,
    importance: 1,
    sensitivity: "internal",
    createdAt: "2026-01-01T00:00:00.000Z",
    updatedAt: "2026-01-01T00:00:00.000Z",
  });
  const second = await appendMemoryEvent(fixture, {
    kind: "decision",
    subjectType: "file",
    title: "Current authorization branch",
    body: "Use the successor authorization workflow now.",
    appliesTo: ["src/auth.ts"],
    sources: [{ path: "src/auth.ts", startLine: 1 }],
    provenance: { actor: "cli-test", method: "explicit" },
    authority: "human",
    confidence: 1,
    importance: 1,
    sensitivity: "internal",
    createdAt: "2026-03-01T00:00:00.000Z",
    updatedAt: "2026-03-01T00:00:00.000Z",
    supersedes: [first.id],
  });
  const refreshed = await refreshRepoBrain(fixture);
  const namespacedFirst = refreshed.graph.nodes.find(
    (node) => node.type === "memory" && node.metadata.eventId === first.id,
  )?.id;
  assert(namespacedFirst, "the temporal graph must namespace the first memory event");
  const boundary = "2026-02-01T00:00:00.000Z";

  const context = JSON.parse(run([
    "context",
    "historical authorization branch",
    "--memory-as-of",
    boundary,
    "--json",
  ], 0, fixture).stdout);
  assert.equal(context.memoryAsOf, boundary);
  assert.equal(context.repositoryTopology, "current");
  assert(context.items.some((item) => item.id === first.id));
  assert(!context.items.some((item) => item.id === second.id));
  assert.equal(context.memoryFingerprint, (await readMemoryLedgerSnapshot(fixture)).memoryFingerprint);

  const stats = JSON.parse(run([
    "graph", "stats", "--memory-as-of", boundary, "--json",
  ], 0, fixture).stdout);
  assert.equal(stats.memoryAsOf, boundary);
  assert.equal(stats.repositoryTopology, "current");
  assert.equal(stats.memoryFingerprint, context.memoryFingerprint);

  const neighbors = JSON.parse(run([
    "graph", "neighbors", first.id, "--hops", "0", "--memory-as-of", boundary, "--json",
  ], 0, fixture).stdout);
  assert.equal(neighbors.root.id, namespacedFirst);
  assert.equal(neighbors.memoryAsOf, boundary);

  const path = JSON.parse(run([
    "graph", "path", first.id, "src/auth.ts", "--memory-as-of", boundary, "--json",
  ], 0, fixture).stdout);
  assert.equal(path.from.id, namespacedFirst);
  assert.equal(path.to.path, "src/auth.ts");
  assert(path.path.length >= 2);

  const components = JSON.parse(run([
    "graph", "components", "--memory-as-of", boundary, "--json",
  ], 0, fixture).stdout);
  assert.equal(components.memoryAsOf, boundary);
  assert(Array.isArray(components.components));

  const rawTimeline = JSON.parse(run(["graph", "timeline", first.id, "--json"], 0, fixture).stdout);
  const namespacedTimeline = JSON.parse(run([
    "graph", "timeline", namespacedFirst, "--json",
  ], 0, fixture).stdout);
  assert.deepEqual(rawTimeline, namespacedTimeline);
  assert(rawTimeline.entries.some((entry) => entry.eventId === second.id));
  assert(!JSON.stringify(rawTimeline).includes(first.body));

  const unknownId = "unknown-memory-secret";
  const unknown = run(["graph", "timeline", unknownId], 1, fixture);
  assert.match(unknown.stderr, /memory graph node not found|memory timeline/i);
  assert(!unknown.stderr.includes(unknownId));

  const hostileBoundary = "not-a-time-secret\u001b[31m";
  for (const args of [
    ["context", "authorization", "--memory-as-of", hostileBoundary, "--json"],
    ["graph", "stats", "--memory-as-of", hostileBoundary, "--json"],
  ]) {
    const invalid = run(args, 1, fixture);
    assert.match(invalid.stderr, /memoryAsOf|canonical UTC/i);
    assert(!invalid.stderr.includes("not-a-time-secret"));
    assert(!invalid.stderr.includes("\u001b"));
  }

  assert.match(
    run(["context", "--help"], 0, fixture).stdout,
    /--memory-as-of requires YYYY-MM-DDTHH:mm:ss\.sssZ/,
  );
  assert.match(
    run(["graph", "--help"], 0, fixture).stdout,
    /timeline[\s\S]*--memory-as-of YYYY-MM-DDTHH:mm:ss\.sssZ/,
  );
} finally {
  rmSync(fixture, { recursive: true, force: true });
}

console.log("cli-behavior: ok");
