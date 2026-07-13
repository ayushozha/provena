import { getGitRoot } from "../config.js";
import { installMcpConfigs } from "../integrations/mcp-config.js";
import { installPortableRuntime } from "../integrations/runtime.js";
import {
  DEFAULT_REPO_MCP_HTTP_PORT,
  runRepoMcpHttpServer,
} from "../mcp/http.js";
import { runRepoMcpServer } from "../mcp/server.js";
import { readDaemonStatus, runDaemonCommand, waitForDaemonStop } from "./daemon.js";

export function printMcpHelp(): void {
  console.log("Usage:");
  console.log("  provena mcp install");
  console.log("  provena mcp serve");
  console.log("  provena mcp serve --http [--port <port>]");
  console.log("");
  console.log("Install project-scoped configs, serve MCP over stdio, or opt into loopback HTTP.");
  console.log(`HTTP binds 127.0.0.1 only and defaults to port ${DEFAULT_REPO_MCP_HTTP_PORT}.`);
}

function parseHttpPort(args: string[]): number | undefined {
  if (args[0] !== "serve") throw new Error("invalid MCP serve options");
  const options = args.slice(1);
  if (options.length === 0) return undefined;
  if (options[0] !== "--http" || options.filter((value) => value === "--http").length !== 1) {
    throw new Error("invalid MCP serve options");
  }
  if (options.length === 1) return DEFAULT_REPO_MCP_HTTP_PORT;
  if (options.length !== 3 || options[1] !== "--port" || !/^[1-9]\d{0,4}$/u.test(options[2]!)) {
    throw new Error("invalid MCP serve options");
  }
  const port = Number(options[2]);
  if (!Number.isInteger(port) || port > 65_535) throw new Error("invalid MCP serve options");
  return port;
}

export async function runMcpCommand(
  args: string[],
  cwd = process.cwd(),
): Promise<number> {
  if (args.includes("--help") || args.includes("-h")) {
    printMcpHelp();
    return 0;
  }
  const command = args[0] ?? "serve";
  if (command === "serve") {
    const port = parseHttpPort(args.length === 0 ? ["serve"] : args);
    if (port === undefined) await runRepoMcpServer(cwd);
    else await runRepoMcpHttpServer(cwd, { port });
    return 0;
  }
  if (command !== "install") {
    throw new Error("unknown MCP subcommand");
  }
  if (args.length !== 1) throw new Error("invalid MCP install options");
  const repoRoot = getGitRoot(cwd);
  const daemon = readDaemonStatus(repoRoot);
  const runtime = installPortableRuntime(repoRoot);
  if (!runtime.reused && daemon.running) {
    runDaemonCommand(["stop"], repoRoot);
    if (!waitForDaemonStop(repoRoot)) {
      throw new Error("existing Provena daemon did not stop during runtime upgrade");
    }
    runDaemonCommand(
      daemon.intervalMs
        ? ["start", "--interval", `${daemon.intervalMs}ms`]
        : ["start"],
      repoRoot,
    );
  }
  const results = installMcpConfigs(repoRoot);
  const installed = results.filter((result) => result.action !== "skipped");
  console.log(
    `${installed.length === results.length ? "Provena MCP installed" : "Provena MCP partially installed"} ` +
      `(${runtime.reused ? "runtime reused" : "runtime created"}).`,
  );
  for (const result of results) {
    console.log(`  ${result.action.padEnd(9)} ${result.client}: ${result.path}`);
    if (result.reason) console.log(`             ${result.reason}`);
  }
  if (installed.length === 0) {
    console.error("provena mcp: every project config was preserved/skipped; no MCP client was installed");
    return 1;
  }
  return 0;
}
