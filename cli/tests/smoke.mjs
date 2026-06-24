import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const cli = join(root, "dist", "cli.js");

function run(args) {
  const result = spawnSync(process.execPath, [cli, ...args], {
    encoding: "utf8",
    cwd: root,
  });
  if (result.status !== 0) {
    console.error(result.stderr || result.stdout);
    process.exit(result.status ?? 1);
  }
  return result.stdout;
}

const version = run(["--version"]).trim();
if (!/^\d+\.\d+\.\d+$/.test(version)) {
  console.error(
    `version must be full semver MAJOR.MINOR.PATCH (e.g. 0.1.0), got: ${version}`,
  );
  process.exit(1);
}
if (!/^0\.1\.\d+$/.test(version)) {
  console.error(
    `CLI must stay on 0.1.x unless a major release explicitly bumps minor ` +
      `(0.2.0+ requires updating this test); got: ${version}`,
  );
  process.exit(1);
}

const help = run(["--help"]);
for (const cmd of ["init", "index", "search", "status"]) {
  if (!help.includes(cmd)) {
    console.error(`help missing command: ${cmd}`);
    process.exit(1);
  }
}

console.log("cli-smoke: ok");