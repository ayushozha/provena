import {
  configExists,
  configPath,
  getGitRoot,
  readConfig,
} from "../config.js";

export function runConfigShow(cwd: string = process.cwd()): number {
  const gitRoot = getGitRoot(cwd);
  const path = configPath(gitRoot);

  if (!configExists(gitRoot)) {
    console.error(`No Provena config found at ${path}`);
    console.error("Run `provena init` first.");
    return 1;
  }

  const config = readConfig(gitRoot);
  console.log(JSON.stringify(config, null, 2));
  return 0;
}