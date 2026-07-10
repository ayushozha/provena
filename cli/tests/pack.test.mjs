import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, unlinkSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const npmCli =
  process.env.npm_execpath ??
  join(dirname(process.execPath), "node_modules", "npm", "bin", "npm-cli.js");
const npm = (args) =>
  spawnSync(process.execPath, [npmCli, ...args], {
    cwd: root,
    encoding: "utf8",
    shell: false,
  });

const parsePackJson = (result, label) => {
  try {
    const parsed = JSON.parse(result.stdout);
    if (!Array.isArray(parsed) || parsed.length !== 1 || !Array.isArray(parsed[0].files)) {
      throw new Error("expected one npm pack result with a files array");
    }
    return parsed[0];
  } catch (error) {
    console.error(`${label} did not return valid npm pack JSON: ${error.message}`);
    console.error(result.stdout || result.stderr);
    process.exit(1);
  }
};

const dry = npm(["pack", "--dry-run", "--json", "--silent"]);
if (dry.status !== 0) {
  console.error(dry.stderr || dry.stdout);
  process.exit(1);
}

const dryResult = parsePackJson(dry, "npm pack --dry-run");
const packageJson = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
const bundled = new Set(dryResult.bundled ?? []);
for (const dependency of Object.keys(packageJson.dependencies ?? {})) {
  if (!bundled.has(dependency)) {
    console.error(`production dependency is not bundled: ${dependency}`);
    process.exit(1);
  }
}
for (const file of dryResult.files) {
  if (
    !file.path.startsWith("dist/") &&
    !file.path.startsWith("node_modules/") &&
    file.path !== "package.json" &&
    file.path !== "README.md"
  ) {
    console.error(`unexpected pack file: ${file.path}`);
    process.exit(1);
  }
}

const pack = npm(["pack", "--json", "--silent"]);
if (pack.status !== 0) {
  console.error(pack.stderr || pack.stdout);
  process.exit(1);
}

const packResult = parsePackJson(pack, "npm pack");
const tarball = join(root, packResult.filename);
if (!existsSync(tarball)) {
  console.error(`npm pack did not create ${packResult.filename}`);
  process.exit(1);
}
unlinkSync(tarball);

console.log("pack-test: ok");
