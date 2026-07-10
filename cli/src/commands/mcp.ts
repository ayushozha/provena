import { getGitRoot } from "../config.js";
import { installMcpConfigs } from "../integrations/mcp-config.js";
import { installPortableRuntime } from "../integrations/runtime.js";
import { runRepoMcpServer } from "../mcp/server.js";
import { readDaemonStatus, runDaemonCommand, waitForDaemonStop } from "./daemon.js";

export function printMcpHelp(): void {
  console.log("Usage: provena mcp <install|serve>");
  console.log("");
  console.log("Install project-scoped MCP configs or run the stdio repo-memory server.");
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
    await runRepoMcpServer(cwd);
    return 0;
  }
  if (command !== "install") {
    throw new Error(`unknown MCP subcommand: ${command}`);
  }
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
