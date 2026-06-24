import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const dist = pathToFileURL(join(root, "dist", "index.js")).href;
const fixtureRoot = join(root, "tests", "fixtures", "discover-repo");

const { enumerateFiles } = await import(dist);

const config = {
  version: 1,
  backend: "sqlite",
  database: { path: ".provena/provena.db" },
  store_url: "http://127.0.0.1:18092",
  scope: { tenant_id: "fixture", project_id: "discover-repo" },
  index: {
    include: ["**/*"],
    exclude: ["node_modules/**", ".git/**", "dist/**", "build/**"],
  },
};

const files = await enumerateFiles(fixtureRoot, config);
const paths = files.map((file) => file.path);

assert.ok(!paths.some((path) => path.includes("node_modules")), "node_modules must be skipped");
assert.ok(!paths.includes(".env"), ".env must be skipped via gitignore");
assert.ok(paths.includes("src/foo.ts"), "src/foo.ts must be included");

const foo = files.find((file) => file.path === "src/foo.ts");
assert.ok(foo, "src/foo.ts entry must exist");
assert.equal(foo.language, "typescript");
assert.match(foo.sha256, /^[a-f0-9]{64}$/);
assert.ok(foo.sizeBytes > 0);
assert.ok(foo.absolutePath.endsWith("src/foo.ts") || foo.absolutePath.includes("src\\foo.ts"));

const gitInit = spawnSync("git", ["init"], { cwd: fixtureRoot, encoding: "utf8" });
if (gitInit.status !== 0) {
  console.error(gitInit.stderr || gitInit.stdout);
  process.exit(gitInit.status ?? 1);
}

const withGit = await enumerateFiles(fixtureRoot, config);
const withGitPaths = withGit.map((file) => file.path);
assert.ok(!withGitPaths.some((path) => path.includes("node_modules")));
assert.ok(!withGitPaths.includes(".env"));
assert.ok(withGitPaths.includes("src/foo.ts"));

console.log("discover.test: ok");