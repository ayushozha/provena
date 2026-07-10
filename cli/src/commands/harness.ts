import { getGitRoot, loadConfig } from "../config.js";
import { verifyRepoMemory } from "../harness/index.js";

export async function runHarnessCommand(
  args: string[],
  cwd = process.cwd(),
): Promise<number> {
  if (args.includes("--help") || args.includes("-h")) {
    console.log("Usage: provena harness verify [--json]");
    console.log("");
    console.log("Verify determinism, graph integrity, provenance, and context budgets.");
    return 0;
  }
  const command = args[0] ?? "verify";
  if (command !== "verify") throw new Error(`unknown harness subcommand: ${command}`);
  const repoRoot = getGitRoot(cwd);
  loadConfig(repoRoot);
  const report = await verifyRepoMemory(repoRoot);
  if (args.includes("--json")) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    console.log(`Provena memory harness: ${report.passed ? "PASS" : "FAIL"}`);
    for (const item of report.checks) {
      console.log(`  ${item.status.toUpperCase().padEnd(4)} ${item.name}: ${item.detail}`);
    }
  }
  return report.passed ? 0 : 1;
}
