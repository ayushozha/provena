import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const required = ["dist/cli.js", "dist/cli.d.ts", "dist/index.js", "dist/index.d.ts"];

for (const rel of required) {
  if (!existsSync(join(root, rel))) {
    console.error(`missing build artifact: ${rel}`);
    process.exit(1);
  }
}

console.log("build-artifacts: ok");