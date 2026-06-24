import {
  configExists,
  createDefaultConfig,
  getGitRoot,
  readConfig,
} from "../config.js";
import { discoverRepo } from "../indexer/discover.js";

export async function runDiscover(args: string[]): Promise<void> {
  const json = args.includes("--json");
  const cwd = process.cwd();
  const gitRoot = getGitRoot(cwd);

  const config = configExists(gitRoot)
    ? readConfig(gitRoot)
    : createDefaultConfig({ cwd, gitRoot });

  const files = await discoverRepo(config, {
    cwd,
    repoRoot: gitRoot,
    warn: (message) => console.error(`provena discover: ${message}`),
  });

  if (json) {
    console.log(JSON.stringify(files, null, 2));
    return;
  }

  for (const file of files) {
    console.log(
      `${file.path}\t${file.sizeBytes}\t${file.language ?? "-"}\t${file.sha256.slice(0, 12)}`,
    );
  }
  console.error(`provena discover: ${files.length} file(s)`);
}