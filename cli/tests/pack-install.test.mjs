/**
 * Simulates a fresh consumer: npm pack → install tarball → run --version.
 * Does not hit the npm registry (plan 26 pre-publish gate).
 */
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  appendFileSync,
  existsSync,
  lstatSync,
  mkdtempSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { createServer as createNetServer } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";

const cliRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const npmCli =
  process.env.npm_execpath ??
  join(dirname(process.execPath), "node_modules", "npm", "bin", "npm-cli.js");

function run(command, args, cwd) {
  return spawnSync(command === "npm" ? process.execPath : command, command === "npm" ? [npmCli, ...args] : args, {
    cwd,
    encoding: "utf8",
    shell: false,
  });
}

function processAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function waitFor(check, attempts = 20) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      if (check()) return true;
    } catch {}
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 250);
  }
  try {
    return check();
  } catch {
    return false;
  }
}

function waitForExit(pids, attempts = 20) {
  return waitFor(() => ![...pids].some(processAlive), attempts);
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function unusedPort() {
  const server = createNetServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert(address && typeof address !== "string");
  const port = address.port;
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  return port;
}

async function assertPortReleased(port) {
  const server = createNetServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}

async function waitForAsync(check, label, timeoutMs = 5_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      if (await check()) return;
    } catch (error) {
      lastError = error;
    }
    await delay(25);
  }
  throw new Error(`${label} timed out${lastError ? `: ${lastError.message}` : ""}`);
}

async function waitForChildExit(child, timeoutMs = 5_000) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("HTTP launcher did not exit")), timeoutMs);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

function assertGenericLauncherError(result, cwd, hostile, pattern) {
  assert.equal(result.status, 1, result.stderr || result.stdout);
  assert.match(result.stderr, pattern);
  assert(Buffer.byteLength(result.stderr) < 1_024);
  assert(!result.stderr.includes(cwd), "launcher error must omit absolute repository path");
  assert(!result.stderr.includes(hostile), "launcher error must not reflect hostile input");
  assert.doesNotMatch(result.stderr, /\bat\s+[^\r\n]+:\d+:\d+/u, "launcher error must omit stacks");
}

function assertInvalidHttpLauncher(executable, prefixArgs, cwd, label) {
  const hostile = `PACKED_HTTP_OPTION_SENTINEL_${label}\u001b[31m`;
  const result = spawnSync(
    executable,
    [...prefixArgs, "mcp", "serve", "--http", "--port", hostile],
    {
      cwd,
      encoding: "utf8",
      shell: false,
      timeout: 5_000,
      env: { ...process.env, PROVENA_NPM_REGISTRY: "http://127.0.0.1:1" },
    },
  );
  assertGenericLauncherError(result, cwd, hostile, /invalid MCP serve options|MCP command failed/);
}

async function exerciseHttpLauncher(executable, prefixArgs, cwd, label) {
  const port = await unusedPort();
  const args = [...prefixArgs, "mcp", "serve", "--http", "--port", String(port)];
  const child = spawn(executable, args, {
    cwd,
    env: { ...process.env, PROVENA_NPM_REGISTRY: "http://127.0.0.1:1" },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (value) => { stdout += value; });
  child.stderr.on("data", (value) => { stderr += value; });
  let client;
  try {
    await waitForAsync(async () => {
      if (child.exitCode !== null) throw new Error(`launcher exited ${child.exitCode}: ${stderr}`);
      const response = await fetch(`http://127.0.0.1:${port}/healthz`, { cache: "no-store" });
      return response.status === 200;
    }, `${label} HTTP health`);
    await waitForAsync(() => stdout.includes("Provena MCP HTTP listening"), `${label} startup line`);
    assert.match(stdout, new RegExp(`host=127\\.0\\.0\\.1 port=${port} .*mcp=http://127\\.0\\.0\\.1:${port}/mcp startupMs=\\d+(?:\\.\\d+)?`));
    assert(!stdout.includes(cwd));

    const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`));
    client = new Client({ name: `packed-http-${label}`, version: "1.0.0" });
    await client.connect(transport);
    assert.deepEqual(
      (await client.listTools()).tools.map((tool) => tool.name).sort(),
      ["provena_context", "provena_graph_neighbors", "provena_graph_path", "provena_maintenance_context", "provena_maintenance_plan", "provena_procedure_learn", "provena_procedure_outcome", "provena_procedure_recall", "provena_refresh", "provena_remember"].sort(),
    );
    const brain = await client.readResource({ uri: "provena://repo/brain" });
    assert.match(brain.contents[0]?.text ?? "", /repo brain/i);
    const context = await client.callTool({
      name: "provena_context",
      arguments: { query: "authenticate", maxTokens: 256 },
    });
    assert.match(context.content[0]?.text ?? "", /src\/index\.ts/);

    const collision = spawnSync(executable, args, {
      cwd,
      encoding: "utf8",
      shell: false,
      timeout: 5_000,
      env: { ...process.env, PROVENA_NPM_REGISTRY: "http://127.0.0.1:1" },
    });
    assertGenericLauncherError(collision, cwd, "PACKED_HTTP_COLLISION_SENTINEL", /failed to listen|MCP command failed/);
    assert.equal((await fetch(`http://127.0.0.1:${port}/healthz`)).status, 200);
  } finally {
    await client?.close().catch(() => {});
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
    try {
      await waitForChildExit(child);
    } catch {
      child.kill("SIGKILL");
      await waitForChildExit(child).catch(() => {});
    }
  }
  assert.equal(stderr, "", `${label} HTTP launcher must not emit errors`);
  await assertPortReleased(port);
}

const pack = run("npm", ["pack", "--silent"], cliRoot);
assert.equal(pack.status, 0, pack.stderr || pack.stdout);

const tarball = readdirSync(cliRoot).find((f) => f.endsWith(".tgz"));
assert.ok(tarball, "npm pack did not create a tarball");

const scratch = mkdtempSync(join(tmpdir(), "provena-pack-install-"));
const cliBin = join(scratch, "node_modules", "@provena", "cli", "dist", "cli.js");
const daemonPids = new Set();
let heldRepoMemoryLock;
let testError;
try {
  run("npm", ["init", "-y"], scratch);
  assert.equal(run("git", ["init", "--quiet"], scratch).status, 0);
  writeFileSync(join(scratch, ".gitignore"), "node_modules/\ndist/\n", "utf8");
  mkdirSync(join(scratch, "src"), { recursive: true });
  writeFileSync(
    join(scratch, "src", "index.ts"),
    "export function authenticate(user: string) { return Boolean(user); }\n",
    "utf8",
  );
  const consumerPackage = JSON.parse(readFileSync(join(scratch, "package.json"), "utf8"));
  consumerPackage.description = "Packed-install repo brain fixture";
  consumerPackage.scripts = { test: "node --test", build: "tsc" };
  writeFileSync(join(scratch, "package.json"), `${JSON.stringify(consumerPackage, null, 2)}\n`);

  const install = run("npm", ["install", join(cliRoot, tarball)], scratch);
  assert.equal(install.status, 0, install.stderr || install.stdout);

  const pkgJson = JSON.parse(
    readFileSync(join(scratch, "package.json"), "utf8"),
  );
  assert.ok(
    pkgJson.dependencies?.["@provena/cli"] || pkgJson.devDependencies?.["@provena/cli"],
    "tarball install did not add @provena/cli",
  );

  const versionRun = spawnSync(process.execPath, [cliBin, "--version"], {
    cwd: scratch,
    encoding: "utf8",
  });
  assert.equal(versionRun.status, 0, versionRun.stderr);
  assert.match(versionRun.stdout.trim(), /^0\.1\.\d+$/);

  const helpRun = spawnSync(process.execPath, [cliBin, "--help"], {
    cwd: scratch,
    encoding: "utf8",
  });
  assert.equal(helpRun.status, 0, helpRun.stderr);
  for (const cmd of ["init", "refresh", "context", "maintain", "remember", "graph", "mcp", "sync"]) {
    assert.ok(helpRun.stdout.includes(cmd), `help missing ${cmd}`);
  }
  const contextHelp = run(process.execPath, [cliBin, "context", "--help"], scratch);
  assert.equal(contextHelp.status, 0, contextHelp.stderr || contextHelp.stdout);
  assert.match(contextHelp.stdout, /--memory-as-of requires YYYY-MM-DDTHH:mm:ss\.sssZ/);
  const graphHelp = run(process.execPath, [cliBin, "graph", "--help"], scratch);
  assert.equal(graphHelp.status, 0, graphHelp.stderr || graphHelp.stdout);
  assert.match(graphHelp.stdout, /timeline[\s\S]*--memory-as-of YYYY-MM-DDTHH:mm:ss\.sssZ/);
  const mcpHelp = run(process.execPath, [cliBin, "mcp", "--help"], scratch);
  assert.equal(mcpHelp.status, 0, mcpHelp.stderr || mcpHelp.stdout);
  assert.match(mcpHelp.stdout, /provena mcp serve --http \[--port <port>\]/);
  assert.match(mcpHelp.stdout, /127\.0\.0\.1 only and defaults to port 18093/);
  assertInvalidHttpLauncher(process.execPath, [cliBin], scratch, "installed");

  const initRun = run(process.execPath, [cliBin, "init", "--no-daemon"], scratch);
  assert.equal(initRun.status, 0, initRun.stderr || initRun.stdout);
  assert.match(initRun.stdout, /Ready:/);
  for (const path of [
    ".provena/config.json",
    ".provena/repo.brain.md",
    ".provena/repo.map.json",
    ".provena/graph.json",
    ".provena/maintenance.plan.json",
    ".provena/manifest.json",
    ".provena/memory/events.jsonl",
    ".provena/views/decisions.md",
    ".provena/agent-instructions.md",
    ".provena/runtime/runtime.mjs",
    "AGENTS.md",
    "CLAUDE.md",
    ".cursor/rules/provena-memory.mdc",
    ".cursor/mcp.json",
    ".codex/config.toml",
    ".mcp.json",
  ]) {
    assert.ok(existsSync(join(scratch, ...path.split("/"))), `init missing ${path}`);
  }
  const syncDryRun = run(
    process.execPath,
    [cliBin, "sync", "store", "--dry-run", "--json"],
    scratch,
  );
  assert.equal(syncDryRun.status, 0, syncDryRun.stderr || syncDryRun.stdout);
  const syncDryRunResult = JSON.parse(syncDryRun.stdout);
  assert.equal(syncDryRunResult.dry_run, true);
  assert.equal(
    syncDryRunResult.received_events,
    3,
    "init projects one package fact and two declared workflows into the ledger",
  );
  assert.match(syncDryRunResult.ledger_fingerprint, /^[0-9a-f]{64}$/);
  const ignore = readFileSync(join(scratch, ".gitignore"), "utf8");
  assert.match(ignore, /^node_modules\/$/m, "preserves consumer ignore rules");
  assert.match(ignore, /^\.provena\/\*$/m, "unknown and local .provena state is ignored");
  assert.match(ignore, /^!\.provena\/agent-instructions\.md$/m);
  assert.doesNotMatch(ignore, /^\.provena\/$/m, "tracked brain is not hidden");
  for (const durable of [
    ".provena/config.json",
    ".provena/repo.brain.md",
    ".provena/repo.map.json",
    ".provena/graph.json",
    ".provena/maintenance.plan.json",
    ".provena/manifest.json",
    ".provena/memory/events.jsonl",
    ".provena/schema/memory-event.schema.json",
    ".provena/views/decisions.md",
    ".provena/agent-instructions.md",
  ]) {
    assert.notEqual(
      run("git", ["check-ignore", "--quiet", durable], scratch).status,
      0,
      `${durable} must remain trackable`,
    );
  }

  const statusRun = run(process.execPath, [cliBin, "status", "--json"], scratch);
  assert.equal(statusRun.status, 0, statusRun.stderr);
  const status = JSON.parse(statusRun.stdout);
  assert.equal(status.current, true);
  assert.equal(status.integrations.portableRuntime, true);
  assert.equal(status.integrations.agents, true);
  assert.equal(status.integrations.mcp, true);

  const ledgerPath = join(scratch, ".provena", "memory", "events.jsonl");
  const manifestPath = join(scratch, ".provena", "manifest.json");
  const ledgerFingerprint = () =>
    createHash("sha256").update(readFileSync(ledgerPath)).digest("hex");
  const assertPacketMatchesLedger = (packet, message) => {
    const expected = ledgerFingerprint();
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
    assert.equal(packet.memoryFingerprint, expected, message);
    assert.equal(manifest.memoryFingerprint, expected, `${message} (manifest)`);
  };

  const initialLedger = readFileSync(ledgerPath, "utf8");
  const initialEvents = initialLedger.trim().split(/\r?\n/).map((line) => JSON.parse(line));
  assert.equal(initialEvents.length, 3, "init creates package and workflow observations");
  assert.equal(initialEvents.filter((event) => event.kind === "fact").length, 1);
  assert.equal(initialEvents.filter((event) => event.kind === "workflow").length, 2);
  const initialContextRun = run(
    process.execPath,
    [cliBin, "context", "authentication", "--json", "--max-tokens", "256"],
    scratch,
  );
  assert.equal(initialContextRun.status, 0, initialContextRun.stderr);
  assertPacketMatchesLedger(
    JSON.parse(initialContextRun.stdout),
    "managed-memory packet fingerprint must hash exact ledger bytes",
  );

  appendFileSync(ledgerPath, "\n", "utf8");
  const staleMemoryStatus = run(process.execPath, [cliBin, "status", "--json"], scratch);
  assert.equal(staleMemoryStatus.status, 0, staleMemoryStatus.stderr);
  assert.equal(
    JSON.parse(staleMemoryStatus.stdout).current,
    false,
    "ledger changes must make derived views and manifest stale",
  );
  assert.equal(run(process.execPath, [cliBin, "refresh", "--quiet"], scratch).status, 0);

  const contextRun = run(
    process.execPath,
    [cliBin, "context", "authentication", "--json", "--max-tokens", "256"],
    scratch,
  );
  assert.equal(contextRun.status, 0, contextRun.stderr);
  const contextPacket = JSON.parse(contextRun.stdout);
  assertPacketMatchesLedger(
    contextPacket,
    "packet fingerprint must preserve raw blank ledger lines",
  );
  assert.ok(
    contextPacket.items.some((item) => item.citations.some((citation) => citation.path === "src/index.ts")),
    "context packet cites the relevant source file",
  );

  const rememberRun = run(
    process.execPath,
    [
      cliBin,
      "remember",
      "decision",
      "Keep authentication explicit",
      "--body",
      "Authentication behavior stays explicit and source-cited.",
      "--source",
      "src/index.ts:1",
      "--tag",
      "architecture",
      "--authority",
      "human",
    ],
    scratch,
  );
  assert.equal(rememberRun.status, 0, rememberRun.stderr);
  assert.match(readFileSync(join(scratch, ".provena", "views", "decisions.md"), "utf8"), /Keep authentication explicit/);

  const installedPackage = join(scratch, "node_modules", "@provena", "cli");
  const installedApi = await import(
    pathToFileURL(join(installedPackage, "dist", "index.js")).href
  );
  const packedHistory = await installedApi.appendMemoryEvent(scratch, {
    id: "packed-temporal-history",
    kind: "decision",
    subjectType: "file",
    title: "Packed historical authentication",
    body: "Use the historical authentication rule at this effective boundary.",
    appliesTo: ["src/index.ts"],
    sources: [{ path: "src/index.ts", startLine: 1 }],
    provenance: { actor: "pack-install-test", method: "explicit" },
    authority: "human",
    confidence: 1,
    importance: 1,
    sensitivity: "internal",
    createdAt: "2026-01-01T00:00:00.000Z",
    updatedAt: "2026-01-01T00:00:00.000Z",
  });
  const packedCurrent = await installedApi.appendMemoryEvent(scratch, {
    id: "packed-temporal-current",
    kind: "decision",
    subjectType: "file",
    title: "Packed current authentication",
    body: "Use the successor authentication rule now.",
    appliesTo: ["src/index.ts"],
    sources: [{ path: "src/index.ts", startLine: 1 }],
    provenance: { actor: "pack-install-test", method: "explicit" },
    authority: "human",
    confidence: 1,
    importance: 1,
    sensitivity: "internal",
    createdAt: "2026-03-01T00:00:00.000Z",
    updatedAt: "2026-03-01T00:00:00.000Z",
    supersedes: [packedHistory.id],
  });
  await installedApi.refreshRepoBrain(scratch);
  const packedBoundary = "2026-02-01T00:00:00.000Z";
  const temporalContextRun = run(
    process.execPath,
    [
      cliBin,
      "context",
      "packed historical authentication",
      "--memory-as-of",
      packedBoundary,
      "--json",
      "--max-tokens",
      "512",
    ],
    scratch,
  );
  assert.equal(temporalContextRun.status, 0, temporalContextRun.stderr || temporalContextRun.stdout);
  const temporalPacket = JSON.parse(temporalContextRun.stdout);
  assert.equal(temporalPacket.memoryAsOf, packedBoundary);
  assert.equal(temporalPacket.repositoryTopology, "current");
  assert(temporalPacket.items.some((item) => item.id === packedHistory.id));
  assert(!temporalPacket.items.some((item) => item.id === packedCurrent.id));
  assertPacketMatchesLedger(temporalPacket, "packed temporal context must attest the full ledger");

  const rawTimelineRun = run(
    process.execPath,
    [cliBin, "graph", "timeline", packedHistory.id, "--json"],
    scratch,
  );
  const namespacedTimelineRun = run(
    process.execPath,
    [cliBin, "graph", "timeline", `memory:${packedHistory.id}`, "--json"],
    scratch,
  );
  assert.equal(rawTimelineRun.status, 0, rawTimelineRun.stderr || rawTimelineRun.stdout);
  assert.equal(namespacedTimelineRun.status, 0, namespacedTimelineRun.stderr || namespacedTimelineRun.stdout);
  assert.deepEqual(JSON.parse(rawTimelineRun.stdout), JSON.parse(namespacedTimelineRun.stdout));
  assert(
    JSON.parse(rawTimelineRun.stdout).entries.some((entry) => entry.eventId === packedCurrent.id),
  );

  const temporalGraphRun = run(
    process.execPath,
    [cliBin, "graph", "stats", "--memory-as-of", packedBoundary, "--json"],
    scratch,
  );
  assert.equal(temporalGraphRun.status, 0, temporalGraphRun.stderr || temporalGraphRun.stdout);
  const temporalGraph = JSON.parse(temporalGraphRun.stdout);
  assert.equal(temporalGraph.memoryAsOf, packedBoundary);
  assert.equal(temporalGraph.memoryFingerprint, temporalPacket.memoryFingerprint);

  const installedMcp = await import(
    pathToFileURL(join(installedPackage, "dist", "mcp", "server.js")).href
  );
  const packedServer = await installedMcp.createRepoMcpServer(scratch);
  const packedClient = new Client({ name: "packed-temporal-client", version: "1.0.0" });
  const [packedClientTransport, packedServerTransport] = InMemoryTransport.createLinkedPair();
  try {
    await Promise.all([
      packedServer.connect(packedServerTransport),
      packedClient.connect(packedClientTransport),
    ]);
    const mcpContext = await packedClient.callTool({
      name: "provena_context",
      arguments: {
        query: "packed historical authentication",
        memoryAsOf: packedBoundary,
        maxTokens: 512,
      },
    });
    assert.match(mcpContext.content[0]?.text ?? "", /Packed historical authentication/);
    const mcpAttestation = JSON.parse(mcpContext.content[1]?.text ?? "{}");
    assert.equal(mcpAttestation.memoryAsOf, packedBoundary);
    assert.equal(mcpAttestation.memoryFingerprint, temporalPacket.memoryFingerprint);
    const mcpNeighbors = await packedClient.callTool({
      name: "provena_graph_neighbors",
      arguments: { node: packedHistory.id, depth: 0, memoryAsOf: packedBoundary },
    });
    const mcpGraph = JSON.parse(mcpNeighbors.content[0]?.text ?? "{}");
    assert.equal(mcpGraph.root.metadata.eventId, packedHistory.id);
    assert.equal(mcpGraph.memoryFingerprint, temporalPacket.memoryFingerprint);
  } finally {
    await packedClient.close();
    await packedServer.close();
  }
  await exerciseHttpLauncher(process.execPath, [cliBin], scratch, "installed");

  const graphRun = run(process.execPath, [cliBin, "graph", "stats", "--json"], scratch);
  assert.equal(graphRun.status, 0, graphRun.stderr);
  const graphStats = JSON.parse(graphRun.stdout);
  assert.ok(graphStats.nodes > 0);
  assert.ok(graphStats.edges > 0);

  const sessionRun = run(
    process.execPath,
    [cliBin, "session", "start", "authentication", "--agent", "codex", "--json"],
    scratch,
  );
  assert.equal(sessionRun.status, 0, sessionRun.stderr);
  const sessionResult = JSON.parse(sessionRun.stdout);
  assertPacketMatchesLedger(
    sessionResult.packet,
    "session packet fingerprint must hash the non-empty ledger bytes",
  );
  assert.equal(
    sessionResult.session.memoryFingerprint,
    sessionResult.packet.memoryFingerprint,
    "session metadata and its packet must attest to the same ledger snapshot",
  );

  const firstRefresh = run(process.execPath, [cliBin, "refresh", "--json"], scratch);
  assert.equal(firstRefresh.status, 0, firstRefresh.stderr);
  const secondRefresh = run(process.execPath, [cliBin, "refresh", "--json"], scratch);
  assert.equal(secondRefresh.status, 0, secondRefresh.stderr);
  assert.deepEqual(JSON.parse(secondRefresh.stdout).written, [], "refresh is byte-stable when inputs do not change");
  const harnessRun = run(process.execPath, [cliBin, "harness", "verify", "--json"], scratch);
  assert.equal(harnessRun.status, 0, harnessRun.stderr || harnessRun.stdout);
  assert.equal(JSON.parse(harnessRun.stdout).passed, true);

  const persistedPackage = join(scratch, ".provena", "runtime", "node_modules", "@provena", "cli");
  assert.equal(lstatSync(persistedPackage).isSymbolicLink(), false, "portable runtime owns a package copy");
  const offlineInit = spawnSync(process.execPath, [cliBin, "init", "--no-daemon"], {
    cwd: scratch,
    encoding: "utf8",
    env: {
      ...process.env,
      PROVENA_NPM_REGISTRY: "http://127.0.0.1:1",
      npm_execpath: join(scratch, "missing-npm-cli.js"),
    },
  });
  assert.equal(offlineInit.status, 0, offlineInit.stderr || offlineInit.stdout);
  assert.match(offlineInit.stdout, /runtime: current/, "verified runtime reuse must need no npm or registry");

  const integrityPath = join(scratch, ".provena", "runtime", "integrity.json");
  const beforeUpgrade = JSON.parse(readFileSync(integrityPath, "utf8"));
  const invokingBrainModule = join(dirname(cliBin), "brain", "index.js");
  const sameVersionMarker = "// same-version packed release upgrade";
  appendFileSync(invokingBrainModule, `\n${sameVersionMarker}\n`, "utf8");
  const upgradeRuntime = run(process.execPath, [cliBin, "init", "--no-daemon"], scratch);
  assert.equal(upgradeRuntime.status, 0, upgradeRuntime.stderr || upgradeRuntime.stdout);
  const afterUpgrade = JSON.parse(readFileSync(integrityPath, "utf8"));
  assert.equal(afterUpgrade.packageVersion, beforeUpgrade.packageVersion, "release code can change while the prerelease package version stays the same");
  assert.notEqual(afterUpgrade.sourcePackageSha256, beforeUpgrade.sourcePackageSha256);
  assert(readFileSync(join(persistedPackage, "dist", "brain", "index.js"), "utf8").includes(sameVersionMarker), "an intact older runtime must be replaced by the invoking same-version build");
  assert.equal(afterUpgrade.schemaVersion, 3);

  const daemonStart = run(process.execPath, [cliBin, "daemon", "start", "--interval", "10s"], scratch);
  assert.equal(daemonStart.status, 0, daemonStart.stderr || daemonStart.stdout);
  const daemonBeforeRepair = JSON.parse(
    readFileSync(join(scratch, ".provena", "daemon.pid"), "utf8"),
  );
  assert.ok(Number.isSafeInteger(daemonBeforeRepair.pid) && daemonBeforeRepair.pid > 0);
  daemonPids.add(daemonBeforeRepair.pid);
  const daemonLog = join(scratch, ".provena", "daemon.log");
  assert.equal(
    waitFor(() => / refreshed memory .*\r?\n/.test(readFileSync(daemonLog, "utf8")), 40),
    true,
    "daemon must complete its first refresh before the blocked-restart fixture",
  );
  heldRepoMemoryLock = join(
    scratch,
    ".provena",
    "cache",
    "locks",
    "repo-memory.lock",
  );
  mkdirSync(dirname(heldRepoMemoryLock), { recursive: true });
  mkdirSync(heldRepoMemoryLock);
  writeFileSync(
    join(heldRepoMemoryLock, "owner.json"),
    `${JSON.stringify({
      token: "packed-install-live-owner",
      pid: process.pid,
      acquiredAt: new Date().toISOString(),
    })}\n`,
    "utf8",
  );
  const persistedBrainModule = join(persistedPackage, "dist", "brain", "index.js");
  writeFileSync(
    persistedBrainModule,
    `${readFileSync(persistedBrainModule, "utf8")}\n// injected runtime tamper\n`,
    "utf8",
  );
  const repairRuntime = run(process.execPath, [cliBin, "mcp", "install"], scratch);
  assert.equal(repairRuntime.status, 0, repairRuntime.stderr || repairRuntime.stdout);
  assert.doesNotMatch(
    readFileSync(persistedBrainModule, "utf8"),
    /injected runtime tamper/,
    "runtime integrity mismatch must trigger a clean offline reinstall",
  );
  const daemonAfterRepair = JSON.parse(
    readFileSync(join(scratch, ".provena", "daemon.pid"), "utf8"),
  );
  assert.ok(Number.isSafeInteger(daemonAfterRepair.pid) && daemonAfterRepair.pid > 0);
  daemonPids.add(daemonAfterRepair.pid);
  assert.notEqual(
    daemonAfterRepair.pid,
    daemonBeforeRepair.pid,
    "a live daemon must restart onto the repaired runtime",
  );
  assert.equal(daemonAfterRepair.intervalMs, 10_000, "runtime upgrade preserves cadence");
  assert.equal(
    processAlive(daemonBeforeRepair.pid),
    false,
    "runtime repair must stop the prior daemon process",
  );
  daemonPids.delete(daemonBeforeRepair.pid);
  assert.equal(run(process.execPath, [cliBin, "daemon", "status"], scratch).status, 0);
  assert.equal(run(process.execPath, [cliBin, "daemon", "stop"], scratch).status, 0);
  const stoppedDuringInitialRefresh = waitForExit(new Set([daemonAfterRepair.pid]));
  rmSync(heldRepoMemoryLock, { recursive: true, force: true });
  heldRepoMemoryLock = undefined;
  assert.equal(
    stoppedDuringInitialRefresh,
    true,
    "daemon must honor stop while its initial refresh is blocked",
  );
  for (const stateFile of ["daemon.pid", "daemon.heartbeat", "daemon.stop"]) {
    assert.equal(
      existsSync(join(scratch, ".provena", stateFile)),
      false,
      `${stateFile} must be removed after daemon stop`,
    );
  }
  assert.equal(run(process.execPath, [cliBin, "daemon", "status"], scratch).status, 1);
  daemonPids.delete(daemonAfterRepair.pid);
  renameSync(join(scratch, "node_modules", "@provena", "cli"), join(scratch, "node_modules", "@provena", "cli-disabled"));
  const portableRunner = join(scratch, ".provena", "runtime", "runtime.mjs");
  assertInvalidHttpLauncher(process.execPath, [portableRunner], scratch, "persisted-runtime");
  await exerciseHttpLauncher(process.execPath, [portableRunner], scratch, "persisted-runtime");
  const portableRun = run(
    process.execPath,
    [portableRunner, "refresh", "--quiet"],
    scratch,
  );
  assert.equal(portableRun.status, 0, portableRun.stderr || portableRun.stdout);
} catch (error) {
  testError = error;
}

const cleanupErrors = [];
try {
  if (heldRepoMemoryLock) {
    rmSync(heldRepoMemoryLock, { recursive: true, force: true });
    heldRepoMemoryLock = undefined;
  }
} catch (error) {
  cleanupErrors.push(error);
}
try {
  const daemonState = join(scratch, ".provena", "daemon.pid");
  if (existsSync(daemonState)) {
    const pid = JSON.parse(readFileSync(daemonState, "utf8")).pid;
    if (!Number.isSafeInteger(pid) || pid <= 0) {
      throw new Error(`packed-install daemon state has invalid pid: ${String(pid)}`);
    }
    daemonPids.add(pid);
  }
  if (existsSync(cliBin) && [...daemonPids].some(processAlive)) {
    spawnSync(process.execPath, [cliBin, "daemon", "stop"], {
      cwd: scratch,
      encoding: "utf8",
      shell: false,
      timeout: 5_000,
    });
  }
  if (!waitForExit(daemonPids)) {
    throw new Error(`packed-install daemon remained alive: ${[...daemonPids].filter(processAlive).join(", ")}`);
  }
  rmSync(scratch, {
    recursive: true,
    force: true,
    maxRetries: 10,
    retryDelay: 50,
  });
} catch (error) {
  cleanupErrors.push(error);
}
try {
  rmSync(join(cliRoot, tarball), { force: true });
} catch (error) {
  cleanupErrors.push(error);
}
const cleanupError = cleanupErrors.length > 1
  ? new AggregateError(cleanupErrors, "packed-install cleanup failed")
  : cleanupErrors[0];
if (testError && cleanupError) {
  throw new AggregateError([testError, cleanupError], "packed-install test and cleanup failed");
}
if (testError) throw testError;
if (cleanupError) throw cleanupError;

console.log("pack-install.test: ok");
