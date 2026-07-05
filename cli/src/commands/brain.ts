import { loadConfig, provenaDir } from "../config.js";
import { buildRepoMap, writeBrain } from "../brain/brain.js";
import { detectRepoIntelligence } from "../brain/detect.js";
import { classifyFiles } from "../indexer/classify.js";
import { discoverRepo } from "../indexer/discover.js";

function hasFlag(args: string[], ...flags: string[]): boolean {
  return flags.some((flag) => args.includes(flag));
}

export function printBrainHelp(): void {
  console.log("Usage: provena brain [--json]");
  console.log("");
  console.log("Generate .provena/repo.brain.md (agent bootloader) + repo.map.json");
  console.log("from the current repo. Run `provena init` first.");
  console.log("");
  console.log("Options:");
  console.log("  --json       Print the repo map as JSON instead of a summary");
  console.log("  --help, -h   Show this help");
}

/** discover -> classify -> detect -> build map -> write brain + map. */
export async function runBrainCommand(args: string[]): Promise<number> {
  if (hasFlag(args, "--help", "-h")) {
    printBrainHelp();
    return 0;
  }

  const { config, projectRoot } = loadConfig();

  const files = await discoverRepo(config, {
    cwd: projectRoot,
    repoRoot: projectRoot,
    warn: (message) => console.error(`provena brain: ${message}`),
  });

  if (files.length === 0) {
    console.error("provena brain: no indexable files discovered");
    return 1;
  }

  const classified = classifyFiles(files);
  const intel = detectRepoIntelligence(projectRoot, classified);
  const map = buildRepoMap(projectRoot, classified, intel, new Date().toISOString());
  const result = writeBrain(provenaDir(projectRoot), map);

  if (hasFlag(args, "--json")) {
    process.stdout.write(`${JSON.stringify(map, null, 2)}\n`);
    return 0;
  }

  console.log("Provena brain generated.");
  console.log(`  brain:   ${result.brainPath} (${result.bytes} bytes)`);
  console.log(`  map:     ${result.mapPath}`);
  console.log(`  files:   ${map.fileCount}`);
  console.log(`  langs:   ${map.languages.join(", ") || "unknown"}`);
  console.log(`  entries: ${map.entrypoints.length} entrypoints, ${map.services.length} services, ${map.envVars.length} env vars`);
  return 0;
}
