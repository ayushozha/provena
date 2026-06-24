import { loadConfig } from "../config.js";
import { runIndex } from "../indexer/run.js";

function hasFlag(args: string[], ...flags: string[]): boolean {
  return flags.some((flag) => args.includes(flag));
}

function optionValue(args: string[], flag: string): string | undefined {
  const index = args.indexOf(flag);
  if (index === -1) {
    return undefined;
  }
  const value = args[index + 1];
  if (!value || value.startsWith("-")) {
    return undefined;
  }
  return value;
}

export function printIndexHelp(): void {
  console.log("Usage: provena index [options]");
  console.log("");
  console.log("Options:");
  console.log("  --dry-run        List TS/JS files without writing");
  console.log("  --path <subdir>  Limit indexing to a repo subtree");
  console.log("  --help, -h       Show this help");
}

export async function runIndexCommand(args: string[]): Promise<number> {
  if (hasFlag(args, "--help", "-h")) {
    printIndexHelp();
    return 0;
  }

  const dryRun = hasFlag(args, "--dry-run");
  const pathPrefix = optionValue(args, "--path");

  const { config, projectRoot } = loadConfig();

  const result = await runIndex(config, projectRoot, {
    dryRun,
    pathPrefix,
    cwd: process.cwd(),
  });

  if (!dryRun) {
    const { summary } = result;
    const memories = summary.memoriesCreated + summary.memoriesSkipped;
    console.log(
      `Indexed ${summary.filesIndexed}/${summary.filesDiscovered} files, ${memories} memories, ${summary.relationsCreated} relations`,
    );
    if (summary.filesFailed > 0) {
      console.error(
        `provena index: ${summary.filesFailed} file(s) failed (see .provena/${"index-errors.log"})`,
      );
    }
  }

  return result.exitCode;
}