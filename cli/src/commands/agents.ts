import { getGitRoot } from "../config.js";
import { installAgentInstructions } from "../integrations/agents.js";

export function printAgentsHelp(): void {
  console.log("Usage: provena agents install");
  console.log("");
  console.log("Install managed repo-memory instructions for Codex, Claude Code,");
  console.log("Cursor, and GitHub Copilot without replacing existing instructions.");
}
export function runAgentsCommand(args: string[], cwd = process.cwd()): number {
  if (args.includes("--help") || args.includes("-h")) {
    printAgentsHelp();
    return 0;
  }
  const subcommand = args[0] ?? "install";
  if (subcommand !== "install") {
    console.error(`provena agents: unknown subcommand ${subcommand}`);
    printAgentsHelp();
    return 1;
  }

  const repoRoot = getGitRoot(cwd);
  const results = installAgentInstructions(repoRoot);
  console.log("Provena agent boot instructions installed.");
  for (const result of results) {
    console.log(`  ${result.action.padEnd(9)} ${result.path}`);
  }
  return 0;
}
