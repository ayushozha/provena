import { mkdirSync } from "node:fs";
import {
  configExists,
  createDefaultConfig,
  ensureGitignore,
  getGitRoot,
  provenaDir,
  writeConfig,
} from "../config.js";

export interface InitOptions {
  cwd?: string;
  force?: boolean;
}

export function runInit(options: InitOptions = {}): number {
  const cwd = options.cwd ?? process.cwd();
  const gitRoot = getGitRoot(cwd);
  const force = options.force ?? false;

  mkdirSync(provenaDir(gitRoot), { recursive: true });

  const exists = configExists(gitRoot);
  if (exists && !force) {
    console.log("Provena already initialized (.provena/config.json exists).");
  } else {
    const config = createDefaultConfig({ cwd, gitRoot });
    writeConfig(gitRoot, config);
    if (exists && force) {
      console.log("Provena config overwritten (--force).");
    } else {
      console.log("Provena initialized.");
    }
    console.log(`  config: ${provenaDir(gitRoot)}/config.json`);
    console.log(`  database: ${config.database.path}`);
  }

  if (ensureGitignore(gitRoot)) {
    console.log("  added .provena/ to .gitignore");
  }

  console.log("");
  console.log("Next step: provena index");

  return 0;
}