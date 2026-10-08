import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { request as nodeRequest } from "node:http";
import { createConnection, createServer as createNetServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import {
  DEFAULT_REPO_MCP_HTTP_PORT,
  REPO_MCP_HTTP_BODY_LIMIT_BYTES,
  REPO_MCP_HTTP_HOST,
  createDefaultConfig,
  readMemoryEvents,
  refreshRepoBrain,
  startRepoMcpHttpServer,
  writeConfig,
} from "../dist/index.js";
import { withRepoMemoryLock } from "../dist/brain/lock.js";

const cliRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const cli = join(cliRoot, "dist", "cli.js");
const expectedTools = [
  "provena_context",
  "provena_graph_neighbors",
  "provena_graph_path",
  "provena_maintenance_context",
  "provena_maintenance_plan",
  "provena_procedure_learn",
  "provena_procedure_outcome",
  "provena_procedure_recall",
  "provena_refresh",
  "provena_remember",
].sort();
const expectedResources = [
  "provena://repo/brain",
  "provena://repo/graph",
  "provena://repo/manifest",
  "provena://repo/map",
  "provena://repo/memories",
].sort();
const trackedArtifacts = [
  ".provena/repo.brain.md",
  ".provena/repo.map.json",
  ".provena/graph.json",
  ".provena/maintenance.plan.json",
  ".provena/manifest.json",
  ".provena/memory/events.jsonl",
];

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(check, label, timeoutMs = 5_000) {
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

function rawRequest({ port, path = "/healthz", method = "GET", headers = {}, body, setHost = true }) {
  return new Promise((resolve, reject) => {
    const request = nodeRequest(
      { host: REPO_MCP_HTTP_HOST, port, path, method, headers, setHost },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => resolve({
          status: response.statusCode,
          headers: response.headers,
          body: Buffer.concat(chunks).toString("utf8"),
        }));
      },
    );
    request.once("error", reject);
    request.end(body);
  });
}

function rawWireRequest(port, lines) {
  return new Promise((resolve, reject) => {
    const socket = createConnection({ host: REPO_MCP_HTTP_HOST, port });
    const chunks = [];
    socket.once("connect", () => socket.end(lines.join("\r\n")));
    socket.on("data", (chunk) => chunks.push(chunk));
    socket.once("error", reject);
    socket.once("end", () => {
      const wire = Buffer.concat(chunks).toString("utf8");
      const boundary = wire.indexOf("\r\n\r\n");
      assert(boundary >= 0, "raw HTTP response must contain headers");
      const head = wire.slice(0, boundary).split("\r\n");
      const status = Number(head[0]?.split(" ")[1]);
      const headers = Object.fromEntries(head.slice(1).map((line) => {
        const colon = line.indexOf(":");
        return [line.slice(0, colon).trim().toLowerCase(), line.slice(colon + 1).trim()];
      }));
      resolve({ status, headers, body: wire.slice(boundary + 4) });
    });
  });
}

function requestHeaders(port, extra = {}) {
  return {
    Host: `127.0.0.1:${port}`,
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
    ...extra,
  };
}

function assertSecurityHeaders(response) {
  assert.equal(response.headers["cache-control"], "no-store");
  assert.equal(response.headers["x-content-type-options"], "nosniff");
  assert.equal(response.headers["access-control-allow-origin"], undefined);
}

function assertNoDisclosure(value, sentinels, root) {
  const text = String(value);
  for (const sentinel of sentinels) {
    assert(!text.includes(sentinel), `response or log reflected ${sentinel}`);
  }
  const jsonEscapedRoot = JSON.stringify(root).slice(1, -1);
  const normalizedText = text.replaceAll("\\\\", "\\").replaceAll("\\", "/");
  for (const path of [root, root.replaceAll("\\", "/"), jsonEscapedRoot]) {
    assert(!text.includes(path), "response or log disclosed the absolute repository path");
  }
  assert(!normalizedText.includes(root.replaceAll("\\", "/")), "response or log disclosed an encoded repository path");
  assert.doesNotMatch(text, /\bat\s+[^\r\n]+:\d+:\d+/u, "response or log disclosed a stack trace");
}

function assertBoundedError(response, sentinels, root, maximum = 4_096) {
  assertSecurityHeaders(response);
  assert(Buffer.byteLength(response.body) <= maximum, "error response must remain bounded");
  assertNoDisclosure(response.body, sentinels, root);
}

function artifactSnapshot(root) {
  return Object.fromEntries(trackedArtifacts.map((relativePath) => {
    const value = readFileSync(join(root, ...relativePath.split("/")));
    return [relativePath, createHash("sha256").update(value).digest("hex")];
  }));
}

function allFiles(root, prefix = "") {
  if (!existsSync(root)) return [];
  const files = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) files.push(...allFiles(join(root, entry.name), relative));
    else files.push(relative);
  }
  return files.sort();
}

async function unusedPort() {
  const server = createNetServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, REPO_MCP_HTTP_HOST, resolve);
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
    server.listen(port, REPO_MCP_HTTP_HOST, resolve);
  });
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}

function exactJsonBody(targetBytes) {
  const envelope = {
    jsonrpc: "2.0",
    id: "body-boundary",
    method: "unknown/body-boundary",
    params: { padding: "" },
  };
  const empty = JSON.stringify(envelope);
  const paddingBytes = targetBytes - Buffer.byteLength(empty);
  assert(paddingBytes >= 0);
  envelope.params.padding = "x".repeat(paddingBytes);
  const result = JSON.stringify(envelope);
  assert.equal(Buffer.byteLength(result), targetBytes);
  return result;
}

async function connectClient(url, name, responses = []) {
  const transport = new StreamableHTTPClientTransport(new URL(url), {
    fetch: async (input, init) => {
      const response = await fetch(input, init);
      responses.push({
        method: init?.method ?? "GET",
        status: response.status,
        headers: Object.fromEntries(response.headers),
      });
      return response;
    },
  });
  const client = new Client({ name, version: "1.0.0" });
  await client.connect(transport);
  return { client, transport };
}

function spawnCli(args, cwd, env = {}) {
  const child = spawn(process.execPath, [cli, ...args], {
    cwd,
    env: { ...process.env, ...env },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  const output = { stdout: "", stderr: "" };
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => { output.stdout += chunk; });
  child.stderr.on("data", (chunk) => { output.stderr += chunk; });
  return { child, output };
}

async function within(promise, timeoutMs, label) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} timed out`)), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function waitForExit(child, timeoutMs = 5_000) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return { code: child.exitCode, signal: child.signalCode };
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("child process did not exit")), timeoutMs);
    child.once("exit", (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal });
    });
  });
}

async function waitForHealth(port, child) {
  await waitFor(async () => {
    if (child.exitCode !== null) throw new Error(`server exited ${child.exitCode}`);
    const response = await rawRequest({ port });
    return response.status === 200;
  }, `HTTP server on ${port}`);
}

async function runSignalCase({ root, cwd, signal, port, useDefault = false, incomplete = false, sentinels }) {
  const before = artifactSnapshot(root);
  const beforeFiles = allFiles(join(root, ".provena"));
  const args = ["mcp", "serve", "--http", ...(useDefault ? [] : ["--port", String(port)])];
  const { child, output } = spawnCli(args, cwd, {
    PROVENA_HTTP_TEST_ENV: sentinels.environment,
  });
  let exited;
  let incompleteRequest;
  try {
    await waitForHealth(port, child);
    await waitFor(() => output.stdout.includes("Provena MCP HTTP listening"), "CLI startup line");
    const lines = output.stdout.trim().split(/\r?\n/u).filter(Boolean);
    assert.equal(lines.length, 1, "foreground HTTP prints exactly one startup line");
    assert.match(lines[0], new RegExp(`host=127\\.0\\.0\\.1 port=${port} .*mcp=http://127\\.0\\.0\\.1:${port}/mcp startupMs=\\d+(?:\\.\\d+)?`));
    assertNoDisclosure(lines[0], Object.values(sentinels), root);

    const malformed = await rawRequest({
      port,
      path: "/mcp",
      method: "POST",
      headers: requestHeaders(port, {
        Authorization: `Bearer ${sentinels.credential}`,
        "Content-Length": Buffer.byteLength(`{"value":"${sentinels.body}"`),
      }),
      body: `{"value":"${sentinels.body}"`,
    });
    assert.equal(malformed.status, 400);
    assertBoundedError(malformed, Object.values(sentinels), root);

    if (incomplete) {
      incompleteRequest = nodeRequest({
        host: REPO_MCP_HTTP_HOST,
        port,
        path: "/mcp",
        method: "POST",
        headers: requestHeaders(port, {
          "Content-Length": 256,
        }),
      });
      incompleteRequest.on("error", () => {});
      await new Promise((resolve, reject) => {
        incompleteRequest.once("socket", (socket) => {
          if (!socket.connecting) resolve();
          else {
            socket.once("connect", resolve);
            socket.once("error", reject);
          }
        });
      });
      incompleteRequest.write("{");
      await delay(25);
    }

    assert.equal(child.kill(signal), true);
    exited = await within(waitForExit(child, 3_000), 3_500, `${signal} shutdown`);
    if (process.platform !== "win32") assert.equal(exited.code, 0, `${signal} should exit cleanly`);
  } finally {
    incompleteRequest?.destroy();
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGKILL");
      await waitForExit(child).catch(() => {});
    }
  }
  assertNoDisclosure(output.stderr, Object.values(sentinels), root);
  assert.equal(output.stderr, "", "normal shutdown must not log errors");
  await assertPortReleased(port);
  assert.deepEqual(artifactSnapshot(root), before, "foreground lifecycle must not rewrite repo memory");
  assert.deepEqual(allFiles(join(root, ".provena")), beforeFiles, "foreground lifecycle leaves no HTTP state");
  assert(!allFiles(join(root, ".provena")).some((path) => /(?:^|\/)(?:daemon\.(?:pid|heartbeat|stop)|.*(?:session|token|\.tmp|\.lock).*)$/iu.test(path)));
  return exited;
}

const sandbox = mkdtempSync(join(tmpdir(), "provena-mcp-http-"));
const repoA = join(sandbox, "repo-a-sensitive-path");
const repoB = join(sandbox, "repo-b-sensitive-path");
const subdirectory = join(repoA, "src", "nested");
const sentinels = {
  body: "HTTP_BODY_SENTINEL_7cc612",
  credential: "HTTP_CREDENTIAL_SENTINEL_2b906f",
  environment: "HTTP_ENV_SENTINEL_e4a94d",
  header: "HTTP_HEADER_SENTINEL_9f15c1",
  memory: "HTTP_MEMORY_SENTINEL_6779da",
};
const lifecycle = [];
const capturedLogs = [];
const originalError = console.error;
const originalWarn = console.warn;
const previousEnvironment = process.env.PROVENA_HTTP_TEST_ENV;
let started;
const clients = [];
try {
  for (const root of [repoA, repoB]) {
    mkdirSync(join(root, "src", "nested"), { recursive: true });
    assert.equal(spawnSync("git", ["init", "--quiet"], { cwd: root }).status, 0);
    writeConfig(root, createDefaultConfig({ cwd: root, gitRoot: root }));
  }
  writeFileSync(join(repoA, "package.json"), JSON.stringify({ name: "http-repo-a", scripts: { test: "node --test" } }));
  writeFileSync(join(repoA, "src", "repo-a-only.ts"), "export const HTTP_REPO_A_MARKER = true;\n");
  writeFileSync(join(repoB, "package.json"), JSON.stringify({ name: "http-repo-b", scripts: { test: "node --test" } }));
  writeFileSync(join(repoB, "src", "repo-b-only.ts"), "export const HTTP_REPO_B_MARKER = true;\n");
  await refreshRepoBrain(repoA);
  await refreshRepoBrain(repoB);
  const startupSnapshot = artifactSnapshot(repoA);
  process.env.PROVENA_HTTP_TEST_ENV = sentinels.environment;
  console.error = (...values) => { capturedLogs.push(values.join(" ")); };
  console.warn = (...values) => { capturedLogs.push(values.join(" ")); };

  started = await startRepoMcpHttpServer(subdirectory, {
    port: 0,
    onLifecycle: (event) => lifecycle.push(event),
  });
  assert.equal(started.host, REPO_MCP_HTTP_HOST);
  assert(Number.isInteger(started.port) && started.port > 0 && started.port <= 65_535);
  assert.equal(started.mcpUrl, `http://127.0.0.1:${started.port}/mcp`);
  assert.equal(started.healthUrl, `http://127.0.0.1:${started.port}/healthz`);
  assert(Number.isFinite(started.startupMs) && started.startupMs >= 0);
  assert.deepEqual(artifactSnapshot(repoA), startupSnapshot, "HTTP startup must not refresh tracked artifacts");

  const health = await rawRequest({ port: started.port });
  assert.equal(health.status, 200);
  assert.deepEqual(JSON.parse(health.body), { status: "ok" });
  assert(Buffer.byteLength(health.body) < 256);
  assertSecurityHeaders(health);
  assertNoDisclosure(health.body, Object.values(sentinels), repoA);

  for (const [method, path, status, allow] of [
    ["POST", "/healthz", 405, "GET"],
    ["HEAD", "/healthz", 405, "GET"],
    ["GET", "/mcp", 405, "POST"],
    ["DELETE", "/mcp", 405, "POST"],
    ["OPTIONS", "/mcp", 405, "POST"],
    ["GET", "/missing", 404, undefined],
    ["POST", "/missing", 404, undefined],
    ["GET", "/HEALTHZ", 404, undefined],
    ["GET", "/healthz/", 404, undefined],
    ["POST", "/MCP", 404, undefined],
    ["POST", "/mcp/", 404, undefined],
    ["POST", "/m%63p", 404, undefined],
  ]) {
    const response = await rawRequest({ port: started.port, method, path });
    assert.equal(response.status, status, `${method} ${path}`);
    assert.equal(response.headers.allow, allow, `${method} ${path} Allow`);
    assertBoundedError(response, Object.values(sentinels), repoA);
  }
  for (const [method, path, allow] of [
    ["POST", "/healthz", "GET"],
    ["GET", "/mcp", "POST"],
    ["DELETE", "/mcp", "POST"],
  ]) {
    for (const body of [
      `{"sentinel":"${sentinels.body}"`,
      "x".repeat(REPO_MCP_HTTP_BODY_LIMIT_BYTES + 1),
    ]) {
      const before = lifecycle.length;
      const response = await rawRequest({
        port: started.port,
        path,
        method,
        headers: requestHeaders(started.port, { "Content-Length": Buffer.byteLength(body) }),
        body,
      });
      assert.equal(response.status, 405, `${method} ${path} rejects method before reading a body`);
      assert.equal(response.headers.allow, allow);
      assertBoundedError(response, Object.values(sentinels), repoA);
      assert.equal(lifecycle.length, before);
    }
  }

  const collisionStartedAt = Date.now();
  await assert.rejects(
    startRepoMcpHttpServer(repoB, { port: started.port }),
    (error) => {
      assert.match(error.message, /failed to listen/i);
      assertNoDisclosure(error.message, Object.values(sentinels), repoA);
      assertNoDisclosure(error.message, Object.values(sentinels), repoB);
      return true;
    },
  );
  assert(Date.now() - collisionStartedAt < 2_000, "bind collision must fail promptly");
  assert.equal((await rawRequest({ port: started.port })).status, 200, "collision must not disturb owner");

  const rejectedBefore = lifecycle.length;
  for (const [label, request] of [
    ["missing", { setHost: false }],
    ["non-local", { headers: { Host: `evil.example:${started.port}` } }],
    ["credential-bearing", { headers: { Host: `user@127.0.0.1:${started.port}` } }],
    ["malformed", { headers: { Host: `bad host ${sentinels.header}` } }],
  ]) {
    const response = await rawRequest({ port: started.port, ...request });
    assert.equal(response.status, 403, `${label} Host must fail closed`);
    assertBoundedError(response, Object.values(sentinels), repoA);
  }
  const duplicateHost = await rawWireRequest(started.port, [
    "GET /healthz HTTP/1.1",
    `Host: 127.0.0.1:${started.port}`,
    `Host: evil.example:${started.port}`,
    "Connection: close",
    "",
    "",
  ]);
  assert.equal(duplicateHost.status, 403, "duplicate Host must fail closed");
  assertBoundedError(duplicateHost, Object.values(sentinels), repoA);
  assert.equal(lifecycle.length, rejectedBefore, "rejected Host must not construct MCP state");

  const minimalRpc = JSON.stringify({});
  for (const host of [
    `127.0.0.1:${started.port}`,
    `localhost:${started.port}`,
    `[::1]:${started.port}`,
  ]) {
    const response = await rawRequest({
      port: started.port,
      path: "/mcp",
      method: "POST",
      headers: requestHeaders(started.port, { Host: host, "Content-Length": Buffer.byteLength(minimalRpc) }),
      body: minimalRpc,
    });
    assert.notEqual(response.status, 403, `local Host ${host} must pass rebinding protection`);
    assertSecurityHeaders(response);
  }

  const allowedOrigins = [
    undefined,
    `http://127.0.0.1:${started.port}`,
    `http://localhost:${started.port}`,
    `http://[::1]:${started.port}`,
  ];
  for (const origin of allowedOrigins) {
    const headers = requestHeaders(started.port, { "Content-Length": Buffer.byteLength(minimalRpc) });
    if (origin !== undefined) headers.Origin = origin;
    const response = await rawRequest({ port: started.port, path: "/mcp", method: "POST", headers, body: minimalRpc });
    assert.notEqual(response.status, 403, `allowed Origin ${origin ?? "absent"}`);
    assertSecurityHeaders(response);
  }
  for (const origin of [
    "null",
    `https://127.0.0.1:${started.port}`,
    `http://127.0.0.1:${started.port + 1}`,
    `http://user@127.0.0.1:${started.port}`,
    `http://evil.example:${started.port}`,
    `http://localhost:${started.port}/`,
    `not-an-origin-${sentinels.header}`,
    [`http://127.0.0.1:${started.port}`, `http://localhost:${started.port}`],
  ]) {
    const before = lifecycle.length;
    const response = await rawRequest({
      port: started.port,
      path: "/mcp",
      method: "POST",
      headers: requestHeaders(started.port, {
        Origin: origin,
        "Content-Length": Buffer.byteLength(minimalRpc),
      }),
      body: minimalRpc,
    });
    assert.equal(response.status, 403, `Origin ${origin} must fail closed`);
    assertBoundedError(response, Object.values(sentinels), repoA);
    assert.equal(lifecycle.length, before, "rejected Origin must not construct MCP state");
  }

  const boundaryBody = exactJsonBody(REPO_MCP_HTTP_BODY_LIMIT_BYTES);
  const boundaryBefore = lifecycle.length;
  const boundary = await rawRequest({
    port: started.port,
    path: "/mcp",
    method: "POST",
    headers: requestHeaders(started.port, { "Content-Length": Buffer.byteLength(boundaryBody) }),
    body: boundaryBody,
  });
  assert.notEqual(boundary.status, 413, "exactly 100 KiB reaches MCP parsing");
  assertSecurityHeaders(boundary);
  await waitFor(() => lifecycle.length >= boundaryBefore + 4, "boundary request cleanup");
  assert.deepEqual(lifecycle.slice(boundaryBefore, boundaryBefore + 4), [
    "server-created",
    "transport-created",
    "transport-closed",
    "server-closed",
  ]);

  const oversizedBody = exactJsonBody(REPO_MCP_HTTP_BODY_LIMIT_BYTES + 1);
  const oversizedBefore = lifecycle.length;
  const oversized = await rawRequest({
    port: started.port,
    path: "/mcp",
    method: "POST",
    headers: requestHeaders(started.port, { "Content-Length": Buffer.byteLength(oversizedBody) }),
    body: oversizedBody,
  });
  assert.equal(oversized.status, 413);
  assertBoundedError(oversized, Object.values(sentinels), repoA);
  assert.equal(lifecycle.length, oversizedBefore, "oversized body must fail before MCP construction");

  const chunkedBoundaryBefore = lifecycle.length;
  const chunkedBoundary = await rawRequest({
    port: started.port,
    path: "/mcp",
    method: "POST",
    headers: requestHeaders(started.port, { "Transfer-Encoding": "chunked" }),
    body: boundaryBody,
  });
  assert.notEqual(chunkedBoundary.status, 413, "chunked exact 100 KiB reaches MCP parsing");
  assertSecurityHeaders(chunkedBoundary);
  await waitFor(() => lifecycle.length >= chunkedBoundaryBefore + 4, "chunked boundary cleanup");

  const chunkedOversizedBefore = lifecycle.length;
  const chunkedOversized = await rawRequest({
    port: started.port,
    path: "/mcp",
    method: "POST",
    headers: requestHeaders(started.port, { "Transfer-Encoding": "chunked" }),
    body: oversizedBody,
  });
  assert.equal(chunkedOversized.status, 413, "chunked input is bounded without Content-Length");
  assertBoundedError(chunkedOversized, Object.values(sentinels), repoA);
  assert.equal(lifecycle.length, chunkedOversizedBefore, "chunked oversize fails before MCP construction");

  const malformedBody = `{"sentinel":"${sentinels.body}"`;
  const malformed = await rawRequest({
    port: started.port,
    path: "/mcp",
    method: "POST",
    headers: requestHeaders(started.port, {
      Authorization: `Bearer ${sentinels.credential}`,
      "Content-Length": Buffer.byteLength(malformedBody),
    }),
    body: malformedBody,
  });
  assert.equal(malformed.status, 400);
  assertBoundedError(malformed, Object.values(sentinels), repoA);

  for (const contentType of [
    "text/plain",
    "text/application/jsonjunk",
    "fooapplication/jsonbar",
    "application/json-patch+json",
    ["application/json", "text/plain"],
  ]) {
    const before = lifecycle.length;
    const unsupported = await rawRequest({
      port: started.port,
      path: "/mcp",
      method: "POST",
      headers: {
        Host: `127.0.0.1:${started.port}`,
        "Content-Type": contentType,
        "Content-Length": Buffer.byteLength(sentinels.body),
      },
      body: sentinels.body,
    });
    assert.equal(unsupported.status, 415, `reject invalid Content-Type ${String(contentType)}`);
    assertBoundedError(unsupported, Object.values(sentinels), repoA);
    assert.equal(lifecycle.length, before, "invalid media type fails before MCP construction");
  }
  const charsetBody = JSON.stringify({});
  const charsetBefore = lifecycle.length;
  const charsetJson = await rawRequest({
    port: started.port,
    path: "/mcp",
    method: "POST",
    headers: requestHeaders(started.port, {
      "Content-Type": "application/json; charset=utf-8",
      "Content-Length": Buffer.byteLength(charsetBody),
    }),
    body: charsetBody,
  });
  assert.notEqual(charsetJson.status, 415, "application/json with a charset remains valid");
  await waitFor(() => lifecycle.length >= charsetBefore + 4, "charset request cleanup");

  const responses = [];
  const primary = await connectClient(started.mcpUrl, "provena-http-primary", responses);
  clients.push(primary.client);
  assert.equal(primary.transport.sessionId, undefined, "stateless HTTP must not assign an MCP session");
  const tools = await primary.client.listTools();
  assert.deepEqual(tools.tools.map((tool) => tool.name).sort(), expectedTools);
  const resources = await primary.client.listResources();
  assert.deepEqual(resources.resources.map((resource) => resource.uri).sort(), expectedResources);
  for (const uri of expectedResources.filter((uri) => uri !== "provena://repo/memories")) {
    const resource = await primary.client.readResource({ uri });
    assert.equal(resource.contents.length, 1);
    assert((resource.contents[0]?.text ?? "").length > 0, `${uri} must be readable`);
  }
  const map = await primary.client.readResource({ uri: "provena://repo/map" });
  assert.match(map.contents[0]?.text ?? "", /src\/repo-a-only\.ts/u);
  assert.doesNotMatch(map.contents[0]?.text ?? "", /src\/repo-b-only\.ts/u);
  const context = await primary.client.callTool({
    name: "provena_context",
    arguments: {
      query: "HTTP_REPO_A_MARKER",
      paths: ["src/repo-a-only.ts"],
      maxTokens: 512,
      cwd: repoB,
      repoRoot: repoB,
    },
  });
  assert.match(context.content[0]?.text ?? "", /repo-a-only\.ts/u);
  assert.doesNotMatch(context.content[0]?.text ?? "", /repo-b-only\.ts/u);

  const queryResponses = [];
  const queryClient = await connectClient(`${started.mcpUrl}?repoRoot=${encodeURIComponent(repoB)}`, "provena-http-query-root", queryResponses);
  clients.push(queryClient.client);
  const queryMap = await queryClient.client.readResource({ uri: "provena://repo/map" });
  assert.match(queryMap.contents[0]?.text ?? "", /src\/repo-a-only\.ts/u);
  assert.doesNotMatch(queryMap.contents[0]?.text ?? "", /src\/repo-b-only\.ts/u);

  const beforeRemember = artifactSnapshot(repoA);
  const remembered = await primary.client.callTool({
    name: "provena_remember",
    arguments: {
      kind: "decision",
      title: "HTTP transport memory",
      body: sentinels.memory,
      sources: [{ path: "src/repo-a-only.ts", startLine: 1 }],
    },
  });
  assert.match(remembered.content[0]?.text ?? "", /HTTP transport memory/u);
  assert.notDeepEqual(artifactSnapshot(repoA), beforeRemember, "mutation tool must update repo A");
  assert.equal((await readMemoryEvents(repoA)).filter((event) => event.title === "HTTP transport memory").length, 1);
  assert.equal((await readMemoryEvents(repoB)).filter((event) => event.title === "HTTP transport memory").length, 0);

  const ledgerBeforeRejectedDomainError = readFileSync(join(repoA, ".provena", "memory", "events.jsonl"), "utf8");
  const rejectedDomainBody = JSON.stringify({
    jsonrpc: "2.0",
    id: "forced-domain-error",
    method: "tools/call",
    params: {
      name: "provena_remember",
      arguments: {
        kind: "decision",
        title: "Rejected HTTP supersedes reference",
        body: "This write must remain atomic when its predecessor is unknown.",
        supersedes: [sentinels.body],
      },
    },
  });
  const rejectedDomain = await rawRequest({
    port: started.port,
    path: "/mcp",
    method: "POST",
    headers: requestHeaders(started.port, {
      Authorization: `Bearer ${sentinels.credential}`,
      "Content-Length": Buffer.byteLength(rejectedDomainBody),
    }),
    body: rejectedDomainBody,
  });
  assert.equal(rejectedDomain.status, 200);
  assertBoundedError(rejectedDomain, Object.values(sentinels), repoA);
  assert.match(rejectedDomain.body, /internal|error/iu);
  assert.equal(
    readFileSync(join(repoA, ".provena", "memory", "events.jsonl"), "utf8"),
    ledgerBeforeRejectedDomainError,
    "rejected domain errors must not partially append the ledger",
  );

  const amplifiedValidationBody = JSON.stringify({
    jsonrpc: "2.0",
    id: "validation-amplification",
    method: "tools/call",
    params: {
      name: "provena_context",
      arguments: { paths: Array(5_000).fill(1) },
    },
  });
  assert(Buffer.byteLength(amplifiedValidationBody) < 20_000, "amplification request fixture stays small");
  const amplifiedValidation = await rawRequest({
    port: started.port,
    path: "/mcp",
    method: "POST",
    headers: requestHeaders(started.port, {
      "Content-Length": Buffer.byteLength(amplifiedValidationBody),
    }),
    body: amplifiedValidationBody,
  });
  assert.equal(amplifiedValidation.status, 200);
  assertBoundedError(amplifiedValidation, Object.values(sentinels), repoA);
  assert.match(amplifiedValidation.body, /internal|invalid|error/iu);
  assert.doesNotMatch(amplifiedValidation.body, /invalid_type|Zod|"expected"|\[4999\]/iu, "validation internals must not be serialized");

  const invalidBatchBody = JSON.stringify(Array.from({ length: 64 }, (_, index) => ({
    jsonrpc: "2.0",
    id: `invalid-batch-${index}`,
    method: "tools/call",
    params: { name: "provena_context", arguments: { paths: [1, 2, 3] } },
  })));
  assert(Buffer.byteLength(invalidBatchBody) < REPO_MCP_HTTP_BODY_LIMIT_BYTES);
  const invalidBatch = await rawRequest({
    port: started.port,
    path: "/mcp",
    method: "POST",
    headers: requestHeaders(started.port, {
      "Content-Length": Buffer.byteLength(invalidBatchBody),
    }),
    body: invalidBatchBody,
  });
  assert([200, 400].includes(invalidBatch.status), "batch rejection must remain protocol appropriate");
  assertBoundedError(invalidBatch, Object.values(sentinels), repoA, 32 * 1_024);
  assert.doesNotMatch(invalidBatch.body, /invalid_type|Zod|"expected"/iu, "batch errors must not serialize validation internals");

  const secondResponses = [];
  const secondary = await connectClient(started.mcpUrl, "provena-http-secondary", secondResponses);
  clients.push(secondary.client);
  const concurrentTitles = ["Concurrent HTTP memory A", "Concurrent HTTP memory B"];
  const concurrent = await Promise.all(concurrentTitles.map((title, index) =>
    [primary.client, secondary.client][index].callTool({
      name: "provena_remember",
      arguments: {
        kind: "fact",
        title,
        body: `Concurrent lock-safe body ${index}`,
        sources: [{ path: "src/repo-a-only.ts", startLine: 1 }],
      },
    }),
  ));
  assert(concurrent.every((result) => result.isError !== true));
  const memories = await readMemoryEvents(repoA);
  for (const title of concurrentTitles) {
    assert.equal(memories.filter((event) => event.title === title).length, 1);
  }
  assert(!allFiles(join(repoA, ".provena")).some((path) => /(?:\.tmp|\.lock)(?:\/|$)/u.test(path)), "concurrent mutation cleanup must remove locks and temporaries");

  const forcedBrain = join(repoA, ".provena", "repo.brain.md");
  const hiddenBrain = `${forcedBrain}.http-test-hidden`;
  renameSync(forcedBrain, hiddenBrain);
  try {
    const requestBody = JSON.stringify({
      jsonrpc: "2.0",
      id: "forced-internal-error",
      method: "resources/read",
      params: { uri: "provena://repo/brain" },
    });
    const forced = await rawRequest({
      port: started.port,
      path: "/mcp",
      method: "POST",
      headers: requestHeaders(started.port, {
        Authorization: `Bearer ${sentinels.credential}`,
        "Content-Length": Buffer.byteLength(requestBody),
      }),
      body: requestBody,
    });
    assert.equal(forced.status, 200, "MCP protocol errors remain JSON-RPC responses");
    assertBoundedError(forced, Object.values(sentinels), repoA);
    assert.match(forced.body, /internal|error/iu);
  } finally {
    renameSync(hiddenBrain, forcedBrain);
  }

  const forcedGraph = join(repoA, ".provena", "graph.json");
  const hiddenGraph = `${forcedGraph}.http-test-hidden`;
  renameSync(forcedGraph, hiddenGraph);
  try {
    const requestBody = JSON.stringify({
      jsonrpc: "2.0",
      id: "forced-tool-error",
      method: "tools/call",
      params: { name: "provena_context", arguments: { query: sentinels.memory } },
    });
    const forced = await rawRequest({
      port: started.port,
      path: "/mcp",
      method: "POST",
      headers: requestHeaders(started.port, {
        Authorization: `Bearer ${sentinels.credential}`,
        "Content-Length": Buffer.byteLength(requestBody),
      }),
      body: requestBody,
    });
    assert.equal(forced.status, 200, "tool failures remain JSON-RPC responses");
    assertBoundedError(forced, Object.values(sentinels), repoA);
    assert.match(forced.body, /internal|error/iu);
  } finally {
    renameSync(hiddenGraph, forcedGraph);
  }

  await Promise.all(clients.splice(0).map((client) => client.close()));
  await waitFor(() => {
    const counts = Object.fromEntries([
      "server-created",
      "transport-created",
      "transport-closed",
      "server-closed",
    ].map((event) => [event, lifecycle.filter((value) => value === event).length]));
    return counts["server-created"] === counts["transport-created"] &&
      counts["server-created"] === counts["transport-closed"] &&
      counts["server-created"] === counts["server-closed"];
  }, "per-request lifecycle balance");
  assert(responses.concat(queryResponses, secondResponses).every((response) => response.headers["mcp-session-id"] === undefined));
  assert(responses.concat(queryResponses, secondResponses).every((response) => response.headers["cache-control"] === "no-store"));
  assert(responses.concat(queryResponses, secondResponses).every((response) => response.headers["x-content-type-options"] === "nosniff"));

  const programmaticPort = started.port;
  await started.close();
  await started.close();
  started = undefined;
  await assertPortReleased(programmaticPort);

  const slow = await startRepoMcpHttpServer(repoA, { port: 0 });
  const incompleteRequest = nodeRequest({
    host: REPO_MCP_HTTP_HOST,
    port: slow.port,
    path: "/mcp",
    method: "POST",
    headers: requestHeaders(slow.port, { "Content-Length": 256 }),
  });
  incompleteRequest.on("error", () => {});
  await new Promise((resolve, reject) => {
    incompleteRequest.once("socket", (socket) => {
      if (!socket.connecting) resolve();
      else {
        socket.once("connect", resolve);
        socket.once("error", reject);
      }
    });
  });
  incompleteRequest.write("{");
  await delay(25);
  const slowClose = slow.close();
  let slowCloseError;
  try {
    await within(slowClose, 2_000, "close with incomplete request");
  } catch (error) {
    slowCloseError = error;
  } finally {
    incompleteRequest.destroy();
    await slowClose.catch(() => {});
  }
  if (slowCloseError) throw slowCloseError;
  await assertPortReleased(slow.port);

  let releaseHeldLock;
  let markLockAcquired;
  const lockAcquired = new Promise((resolve) => { markLockAcquired = resolve; });
  const releaseLock = new Promise((resolve) => { releaseHeldLock = resolve; });
  const heldLock = withRepoMemoryLock(repoA, async () => {
    markLockAcquired();
    await releaseLock;
  });
  const activeLifecycle = [];
  let active;
  let activeClient;
  try {
    await lockAcquired;
    active = await startRepoMcpHttpServer(repoA, {
      port: 0,
      onLifecycle: (event) => activeLifecycle.push(event),
    });
    activeClient = await connectClient(active.mcpUrl, "provena-http-active-close");
    const lifecycleBeforeRefresh = activeLifecycle.length;
    const refreshCall = activeClient.client.callTool({ name: "provena_refresh", arguments: {} });
    await waitFor(
      () => activeLifecycle.slice(lifecycleBeforeRefresh).includes("transport-created"),
      "active refresh request",
    );
    let activeCloseResolved = false;
    const activeClose = active.close().then(() => { activeCloseResolved = true; });
    await delay(250);
    assert.equal(activeCloseResolved, false, "close must not return while an accepted mutation is waiting");
    await assert.rejects(rawRequest({ port: active.port }), "close must stop accepting new requests promptly");
    releaseHeldLock();
    await heldLock;
    const refreshResult = await within(refreshCall, 5_000, "active refresh completion");
    assert.notEqual(refreshResult.isError, true, "accepted mutation completes before shutdown");
    await within(activeClose, 2_000, "active request close");
    const activeCounts = Object.fromEntries([
      "server-created",
      "transport-created",
      "transport-closed",
      "server-closed",
    ].map((event) => [event, activeLifecycle.filter((value) => value === event).length]));
    assert.equal(activeCounts["server-created"], activeCounts["transport-created"]);
    assert.equal(activeCounts["server-created"], activeCounts["transport-closed"]);
    assert.equal(activeCounts["server-created"], activeCounts["server-closed"]);
    await assertPortReleased(active.port);
    assert(!allFiles(join(repoA, ".provena")).some((path) => /(?:\.tmp|\.lock)(?:\/|$)/u.test(path)), "shutdown after an active mutation removes lock and temp state");
  } finally {
    releaseHeldLock?.();
    await heldLock.catch(() => {});
    await activeClient?.client.close().catch(() => {});
    await active?.close().catch(() => {});
  }

  const stdio = spawnCli(["mcp", "serve"], subdirectory);
  try {
    await delay(250);
    assert.equal(stdio.child.exitCode, null, "stdio MCP remains a foreground process");
    await assert.rejects(rawRequest({ port: DEFAULT_REPO_MCP_HTTP_PORT }));
    assert.equal(stdio.output.stdout, "", "stdio MCP keeps stdout protocol-only and silent before traffic");
  } finally {
    stdio.child.kill("SIGKILL");
    await waitForExit(stdio.child).catch(() => {});
  }

  await assertPortReleased(DEFAULT_REPO_MCP_HTTP_PORT);
  await runSignalCase({
    root: repoA,
    cwd: subdirectory,
    signal: "SIGINT",
    port: DEFAULT_REPO_MCP_HTTP_PORT,
    useDefault: true,
    sentinels,
  });
  await runSignalCase({
    root: repoA,
    cwd: subdirectory,
    signal: "SIGTERM",
    port: await unusedPort(),
    incomplete: true,
    sentinels,
  });

  assertNoDisclosure(capturedLogs.join("\n"), Object.values(sentinels), repoA);
  assert.equal(capturedLogs.join("\n"), "", "programmatic server must not emit adversarial request logs");
} finally {
  console.error = originalError;
  console.warn = originalWarn;
  if (previousEnvironment === undefined) delete process.env.PROVENA_HTTP_TEST_ENV;
  else process.env.PROVENA_HTTP_TEST_ENV = previousEnvironment;
  await Promise.allSettled(clients.map((client) => client.close()));
  await started?.close().catch(() => {});
  rmSync(sandbox, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
}

console.log("mcp-http-server.test: ok");
