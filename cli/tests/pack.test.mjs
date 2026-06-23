import { spawnSync } from "node:child_process";
import { readdirSync, unlinkSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

const dry = spawnSync("npm", ["pack", "--dry-run"], { cwd: root, encoding: "utf8", shell: true });
if (dry.status !== 0) {
  console.error(dry.stderr || dry.stdout);
  process.exit(1);
}

const lines = dry.stdout.split("\n").map((l) => l.trim()).filter(Boolean);
const tarballLine = lines.find((l) => l.endsWith(".tgz"));
if (!tarballLine) {
  console.error("npm pack --dry-run missing tarball line", dry.stdout);
  process.exit(1);
}

for (const line of lines) {
  if (line.startsWith("npm notice") || line === tarballLine) continue;
  const normalized = line.replace(/^npm notice\s+/, "");
  if (!normalized.startsWith("dist/") && normalized !== "package.json") {
    console.error(`unexpected pack file: ${normalized}`);
    process.exit(1);
  }
}

const pack = spawnSync("npm", ["pack"], { cwd: root, encoding: "utf8", shell: true });
if (pack.status !== 0) {
  console.error(pack.stderr || pack.stdout);
  process.exit(1);
}

const created = readdirSync(root).filter((f) => f.endsWith(".tgz"));
for (const f of created) unlinkSync(join(root, f));

console.log("pack-test: ok");