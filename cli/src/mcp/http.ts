import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { Socket } from "node:net";
import { performance } from "node:perf_hooks";
import type { Duplex } from "node:stream";
import { MIMEType } from "node:util";
import { createMcpExpressApp } from "@modelcontextprotocol/sdk/server/express.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import type { JSONRPCMessage } from "@modelcontextprotocol/sdk/types.js";
import { getGitRoot } from "../config.js";
import { createRepoMcpServer } from "./server.js";

export const REPO_MCP_HTTP_HOST = "127.0.0.1";
export const DEFAULT_REPO_MCP_HTTP_PORT = 18_093;
export const REPO_MCP_HTTP_BODY_LIMIT_BYTES = 100 * 1_024;
const MAX_MCP_BATCH_MESSAGES = 16;

export type RepoMcpHttpLifecycleEvent =
  | "server-created"
  | "transport-created"
  | "transport-closed"
  | "server-closed";

export interface RepoMcpHttpServerOptions {
  /** Port zero is supported programmatically for an ephemeral test listener. */
  port?: number;
  onLifecycle?: (event: RepoMcpHttpLifecycleEvent) => void;
}

export interface RepoMcpHttpServerStartResult {
  host: typeof REPO_MCP_HTTP_HOST;
  port: number;
  mcpUrl: string;
  healthUrl: string;
  startupMs: number;
  close: () => Promise<void>;
}

type ParsedHttpRequest = IncomingMessage & { body?: unknown };

const LOCAL_HOSTNAMES = new Set(["127.0.0.1", "localhost", "[::1]"]);

function emitLifecycle(
  options: RepoMcpHttpServerOptions,
  event: RepoMcpHttpLifecycleEvent,
): void {
  options.onLifecycle?.(event);
}

function hasOneValidLocalHost(request: IncomingMessage): boolean {
  const hostHeaders = request.rawHeaders.filter(
    (value, index) => index % 2 === 0 && value.toLowerCase() === "host",
  );
  const host = request.headers.host;
  if (hostHeaders.length !== 1 || !host || host.length > 255 || /[\u0000-\u0020\u007f]/u.test(host)) {
    return false;
  }
  try {
    const parsed = new URL(`http://${host}`);
    return (
      !parsed.username &&
      !parsed.password &&
      parsed.pathname === "/" &&
      !parsed.search &&
      !parsed.hash &&
      LOCAL_HOSTNAMES.has(parsed.hostname)
    );
  } catch {
    return false;
  }
}

function validOrigin(request: IncomingMessage, port: number): boolean {
  const origin = request.headers.origin;
  return origin === undefined || new Set([
    `http://127.0.0.1:${port}`,
    `http://localhost:${port}`,
    `http://[::1]:${port}`,
  ]).has(origin);
}

function contentLengthStatus(request: IncomingMessage): 400 | 413 | undefined {
  const value = request.headers["content-length"];
  if (value === undefined) return undefined;
  if (!/^\d+$/u.test(value)) return 400;
  const bytes = Number(value);
  return Number.isSafeInteger(bytes) && bytes <= REPO_MCP_HTTP_BODY_LIMIT_BYTES
    ? undefined
    : 413;
}

function hasOneJsonContentType(request: IncomingMessage): boolean {
  const contentTypeHeaders = request.rawHeaders.filter(
    (value, index) => index % 2 === 0 && value.toLowerCase() === "content-type",
  );
  const contentType = request.headers["content-type"];
  if (contentTypeHeaders.length !== 1 || typeof contentType !== "string") return false;
  try {
    return new MIMEType(contentType).essence === "application/json";
  } catch {
    return false;
  }
}

function exactRoutePath(request: IncomingMessage): "/mcp" | "/healthz" | undefined {
  if (!request.url?.startsWith("/")) return undefined;
  const queryAt = request.url.indexOf("?");
  const path = queryAt === -1 ? request.url : request.url.slice(0, queryAt);
  return path === "/mcp" || path === "/healthz" ? path : undefined;
}

function sendJson(
  response: ServerResponse,
  status: number,
  value: Record<string, unknown>,
  headers: Record<string, string> = {},
): void {
  if (response.writableEnded) return;
  const body = JSON.stringify(value);
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    ...headers,
  });
  response.end(body);
}

function sendHttpError(
  response: ServerResponse,
  status: number,
  message: string,
  headers?: Record<string, string>,
): void {
  sendJson(response, status, { error: message }, headers);
}

function sendMcpError(response: ServerResponse, status: number, code: number, message: string): void {
  sendJson(response, status, {
    jsonrpc: "2.0",
    error: { code, message },
    id: null,
  });
}

function sendClientError(socket: Duplex): void {
  if (!socket.writable || socket.destroyed) {
    socket.destroy();
    return;
  }
  const body = JSON.stringify({ error: "Invalid request" });
  socket.end([
    "HTTP/1.1 400 Bad Request",
    "Connection: close",
    "Cache-Control: no-store",
    "X-Content-Type-Options: nosniff",
    "Content-Type: application/json; charset=utf-8",
    `Content-Length: ${Buffer.byteLength(body)}`,
    "",
    body,
  ].join("\r\n"));
}

function requestErrorStatus(error: unknown): number {
  if (!error || typeof error !== "object") return 500;
  const value = error as { type?: unknown; status?: unknown };
  if (value.type === "entity.too.large" || value.status === 413) return 413;
  if (value.type === "entity.parse.failed" || value.status === 400) return 400;
  if (value.status === 415) return 415;
  return 500;
}

function boundedMcpMessage(message: JSONRPCMessage): JSONRPCMessage {
  if ("error" in message) {
    return {
      jsonrpc: "2.0",
      id: message.id,
      error: {
        code: message.error.code,
        message: "Repository memory request failed",
      },
    };
  }
  if (
    "result" in message &&
    message.result &&
    typeof message.result === "object" &&
    "isError" in message.result &&
    message.result.isError === true
  ) {
    return {
      jsonrpc: "2.0",
      id: message.id,
      result: {
        content: [{ type: "text", text: "Repository memory request failed" }],
        isError: true,
      },
    };
  }
  return message;
}

function boundMcpTransportErrors(transport: StreamableHTTPServerTransport): void {
  const send = transport.send.bind(transport);
  transport.send = (message, options) => send(boundedMcpMessage(message), options);
}

function assertProgrammaticPort(port: number): void {
  if (!Number.isInteger(port) || port < 0 || port > 65_535) {
    throw new Error("invalid local MCP HTTP port");
  }
}

export async function startRepoMcpHttpServer(
  cwd = process.cwd(),
  options: RepoMcpHttpServerOptions = {},
): Promise<RepoMcpHttpServerStartResult> {
  const startedAt = performance.now();
  const requestedPort = options.port ?? DEFAULT_REPO_MCP_HTTP_PORT;
  assertProgrammaticPort(requestedPort);
  const repoRoot = getGitRoot(cwd);
  const app = createMcpExpressApp({ host: REPO_MCP_HTTP_HOST });
  const activeRequests = new Set<Promise<void>>();
  const activeMcpSockets = new Set<Socket>();
  const sockets = new Set<Socket>();

  app.all("/healthz", (request: ParsedHttpRequest, response: ServerResponse, next: () => void) => {
    if (request.method === "GET") next();
    else sendHttpError(response, 405, "Method not allowed", { Allow: "GET" });
  });
  app.get("/healthz", (_request: ParsedHttpRequest, response: ServerResponse) => {
    sendJson(response, 200, { status: "ok" });
  });
  app.post("/mcp", async (request: ParsedHttpRequest, response: ServerResponse) => {
    if (!hasOneJsonContentType(request)) {
      sendMcpError(response, 415, -32_000, "Unsupported media type");
      return;
    }
    if (Array.isArray(request.body) && request.body.length > MAX_MCP_BATCH_MESSAGES) {
      sendMcpError(response, 400, -32_600, "Invalid request");
      return;
    }

    const socket = request.socket;
    activeMcpSockets.add(socket);
    const task = (async () => {
      let transport: StreamableHTTPServerTransport | undefined;
      let mcpServer: Awaited<ReturnType<typeof createRepoMcpServer>> | undefined;
      try {
        mcpServer = await createRepoMcpServer(repoRoot, { sanitizeErrors: true });
        emitLifecycle(options, "server-created");
        transport = new StreamableHTTPServerTransport({
          sessionIdGenerator: undefined,
          enableJsonResponse: true,
        });
        transport.onerror = () => {};
        boundMcpTransportErrors(transport);
        emitLifecycle(options, "transport-created");
        await mcpServer.connect(transport);
        await transport.handleRequest(request, response, request.body);
      } catch {
        if (!response.headersSent) {
          sendMcpError(response, 500, -32_603, "Internal server error");
        } else if (!response.writableEnded) {
          response.end();
        }
      } finally {
        if (transport) {
          try {
            await transport.close();
          } catch {}
          emitLifecycle(options, "transport-closed");
        }
        if (mcpServer) {
          try {
            await mcpServer.close();
          } catch {}
          emitLifecycle(options, "server-closed");
        }
      }
    })();
    activeRequests.add(task);
    try {
      await task;
    } finally {
      activeRequests.delete(task);
      activeMcpSockets.delete(socket);
    }
  });
  app.all("/mcp", (_request: ParsedHttpRequest, response: ServerResponse) => {
    response.setHeader("Allow", "POST");
    sendMcpError(response, 405, -32_000, "Method not allowed");
  });
  app.use((_request: ParsedHttpRequest, response: ServerResponse) => {
    sendHttpError(response, 404, "Not found");
  });
  app.use((
    error: unknown,
    _request: ParsedHttpRequest,
    response: ServerResponse,
    _next: () => void,
  ) => {
    const status = requestErrorStatus(error);
    sendMcpError(
      response,
      status,
      status === 413 ? -32_000 : status === 400 ? -32_700 : -32_603,
      status === 413 ? "Request body too large" : status === 400 ? "Invalid request" : "Internal server error",
    );
  });

  let actualPort = requestedPort;
  const httpServer = createServer({ requireHostHeader: false }, (request, response) => {
    response.setHeader("Cache-Control", "no-store");
    response.setHeader("X-Content-Type-Options", "nosniff");
    if (!hasOneValidLocalHost(request) || !validOrigin(request, actualPort)) {
      sendHttpError(response, 403, "Forbidden");
      return;
    }
    const route = exactRoutePath(request);
    if (!route) {
      sendHttpError(response, 404, "Not found");
      return;
    }
    if (route === "/healthz" && request.method !== "GET") {
      sendHttpError(response, 405, "Method not allowed", { Allow: "GET" });
      return;
    }
    if (route === "/mcp" && request.method !== "POST") {
      response.setHeader("Allow", "POST");
      sendMcpError(response, 405, -32_000, "Method not allowed");
      return;
    }
    const contentStatus = contentLengthStatus(request);
    if (contentStatus !== undefined) {
      sendHttpError(
        response,
        contentStatus,
        contentStatus === 413 ? "Request body too large" : "Invalid request",
      );
      return;
    }
    if (route === "/mcp" && request.method === "POST" && !hasOneJsonContentType(request)) {
      sendMcpError(response, 415, -32_000, "Unsupported media type");
      return;
    }
    app(request, response);
  });
  httpServer.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => {
      sockets.delete(socket);
      activeMcpSockets.delete(socket);
    });
  });
  httpServer.on("clientError", (_error, socket) => sendClientError(socket));

  await new Promise<void>((resolve, reject) => {
    const cleanup = () => {
      httpServer.off("error", onError);
      httpServer.off("listening", onListening);
    };
    const onError = () => {
      cleanup();
      reject(new Error("local MCP HTTP server failed to listen"));
    };
    const onListening = () => {
      cleanup();
      resolve();
    };
    httpServer.once("error", onError);
    httpServer.once("listening", onListening);
    httpServer.listen(requestedPort, REPO_MCP_HTTP_HOST);
  });

  const address = httpServer.address();
  if (!address || typeof address === "string") {
    httpServer.close();
    throw new Error("local MCP HTTP server failed to determine its port");
  }
  actualPort = address.port;
  const mcpUrl = `http://${REPO_MCP_HTTP_HOST}:${actualPort}/mcp`;
  let closing: Promise<void> | undefined;
  const close = () => {
    closing ??= (async () => {
      if (!httpServer.listening) return;
      const listenerClosed = new Promise<void>((resolve, reject) => {
        httpServer.close((error) => {
          if (error) reject(new Error("local MCP HTTP server failed to close"));
          else resolve();
        });
      });
      httpServer.closeIdleConnections();
      for (const socket of sockets) {
        if (!activeMcpSockets.has(socket)) socket.destroy();
      }
      while (activeRequests.size > 0) {
        await Promise.allSettled([...activeRequests]);
      }
      httpServer.closeIdleConnections();
      for (const socket of sockets) socket.destroy();
      await listenerClosed;
    })();
    return closing;
  };

  return {
    host: REPO_MCP_HTTP_HOST,
    port: actualPort,
    mcpUrl,
    healthUrl: `http://${REPO_MCP_HTTP_HOST}:${actualPort}/healthz`,
    startupMs: Math.max(0, performance.now() - startedAt),
    close,
  };
}

export async function runRepoMcpHttpServer(
  cwd = process.cwd(),
  options: RepoMcpHttpServerOptions = {},
): Promise<void> {
  const started = await startRepoMcpHttpServer(cwd, options);
  console.log(
    `Provena MCP HTTP listening host=${started.host} port=${started.port} ` +
      `mcp=${started.mcpUrl} startupMs=${started.startupMs.toFixed(3)}`,
  );
  await new Promise<void>((resolve, reject) => {
    let shuttingDown = false;
    const shutdown = () => {
      if (shuttingDown) return;
      shuttingDown = true;
      process.off("SIGINT", shutdown);
      process.off("SIGTERM", shutdown);
      void started.close().then(resolve, reject);
    };
    process.once("SIGINT", shutdown);
    process.once("SIGTERM", shutdown);
  });
}
