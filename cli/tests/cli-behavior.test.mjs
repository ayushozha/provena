import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const cli = join(root, "dist", "cli.js");

function run(args, expectStatus) {
  const result = spawnSync(process.execPath, [cli, ...args], {
    encoding: "utf8",
    cwd: root,
  });
  if (result.status !== expectStatus) {
    console.error(`expected exit ${expectStatus} for provena ${args.join(" ")}, got ${result.status}`);
    console.error(result.stderr || result.stdout);
    process.exit(1);
  }
  return result;
}

run(["index"], 1);
run(["watch"], 1);
run(["not-a-command"], 1);
run(["--help"], 0);
run(["--version"], 0);

console.log("cli-behavior: ok");