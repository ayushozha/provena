import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { refreshRepoBrain, scanRepo } from "../dist/brain/index.js";

async function put(root, path, content) {
  const target = join(root, ...path.split("/"));
  await mkdir(join(target, ".."), { recursive: true });
  await writeFile(target, content);
}

async function fixture(prefix) {
  const root = await mkdtemp(join(tmpdir(), prefix));
  await put(root, "package.json", `${JSON.stringify({
    name: "portable-brain-fixture",
    description: "A deterministic repo brain fixture.",
    scripts: { build: "tsc", test: "node --test" },
    dependencies: { zod: "1.0.0" },
  }, null, 2)}\n`);
  await put(root, "src/index.ts", 'import { helper } from "./helper.js";\nexport function run() { return process.env.API_URL ? helper() : 0; }\n');
  await put(root, "src/helper.ts", "export const helper = () => 42;\n");
  await put(root, "tests/index.test.ts", "export function testRun() { return true; }\n");
  await put(root, "README.md", "# Fixture\n");
  await put(root, ".gitignore", "ignored.txt\n");
  await put(root, "ignored.txt", "ignore me\n");
  await put(root, ".env", "SECRET=never-index-this\n");
  await put(root, ".env.example", "API_URL=\nSESSION_TTL=\n");
  await put(root, ".provena/cache/local.json", "private cache\n");
  return root;
}

const roots = [];
try {
  const firstRoot = await fixture("provena-kernel-a-");
  const secondRoot = await fixture("provena-kernel-b-");
  roots.push(firstRoot, secondRoot);
  await put(
    secondRoot,
    "src/index.ts",
    'import { helper } from "./helper.js";\r\nexport function run() { return process.env.API_URL ? helper() : 0; }\r\n',
  );
  await put(firstRoot, "src/Zeta.ts", "export const zeta = true;\n");
  await put(secondRoot, "src/Zeta.ts", "export const zeta = true;\r\n");
  await put(firstRoot, "src/éclair.ts", "export const éclair = true;\n");
  await put(secondRoot, "src/éclair.ts", "export const éclair = true;\r\n");
  const first = await scanRepo(firstRoot);
  const second = await scanRepo(secondRoot);

  assert.deepEqual(first, second, "the same repo tree must produce a clone-portable map");
  assert.deepEqual(
    first.files.map((file) => file.path),
    [...first.files.map((file) => file.path)].sort((a, b) => a < b ? -1 : a > b ? 1 : 0),
    "persisted ordering must not depend on the host locale",
  );
  assert.equal(first.repository.name, "portable-brain-fixture");
  assert(!first.files.some((file) => file.path === ".env"));
  assert(first.files.some((file) => file.path === ".env.example"));
  assert(!first.files.some((file) => file.path.startsWith(".provena/")));
  assert(!first.files.some((file) => file.path === "ignored.txt"));
  assert(first.files.every((file) => !file.path.includes(firstRoot)));
  assert(first.symbols.some((symbol) => symbol.name === "run" && symbol.path === "src/index.ts"));
  assert(first.commands.some((command) => command.command === "npm run test"));
  assert(first.packages.some((pkg) => pkg.dependencies.includes("zod")));
  assert.deepEqual(
    first.environmentVariables.map((variable) => variable.name),
    ["API_URL", "SESSION_TTL"],
  );

  const refreshed = await refreshRepoBrain(firstRoot);
  assert(refreshed.written.includes(".provena/repo.brain.md"));
  assert(refreshed.written.includes(".provena/manifest.json"));
  const appId = refreshed.map.files.find((file) => file.path === "src/index.ts").id;
  const helperId = refreshed.map.files.find((file) => file.path === "src/helper.ts").id;
  assert(
    refreshed.graph.edges.some(
      (edge) => edge.from === appId && edge.to === helperId && edge.type === "imports",
    ),
    "TypeScript .js import specifiers must resolve to their .ts source nodes",
  );
  const apiEnvironment = refreshed.map.environmentVariables.find((item) => item.name === "API_URL");
  assert(apiEnvironment);
  assert(
    refreshed.graph.edges.some(
      (edge) => edge.from === appId && edge.to === apiEnvironment.id && edge.type === "uses",
    ),
    "environment references must be represented in the graph",
  );
  const expected = [
    ".provena/repo.brain.md",
    ".provena/repo.map.json",
    ".provena/graph.json",
    ".provena/maintenance.plan.json",
    ".provena/manifest.json",
    ".provena/memory/events.jsonl",
    ".provena/schema/memory-event.schema.json",
    ".provena/views/decisions.md",
    ".provena/views/workflows.md",
    ".provena/views/learnings.md",
  ];
  for (const path of expected) await readFile(join(firstRoot, ...path.split("/")), "utf8");

  const brain = await readFile(join(firstRoot, ".provena", "repo.brain.md"), "utf8");
  const mapJson = await readFile(join(firstRoot, ".provena", "repo.map.json"), "utf8");
  const manifestJson = await readFile(join(firstRoot, ".provena", "manifest.json"), "utf8");
  assert(!brain.includes(firstRoot));
  assert(!mapJson.includes(firstRoot));
  assert(!manifestJson.includes(firstRoot));
  assert(!/generatedAt/i.test(`${brain}${mapJson}${manifestJson}`));
  assert(brain.includes("Agent boot protocol"));

  const warnings = [];
  const bounded = await scanRepo(firstRoot, {
    maxFiles: 2,
    warn: (message) => warnings.push(message),
  });
  assert.equal(bounded.files.length, 2);
  assert(warnings.some((message) => message.includes("file cap")));

  const unchanged = await refreshRepoBrain(firstRoot);
  assert.deepEqual(unchanged.written, [], "an unchanged refresh must not churn tracked artifacts");

  const previousFingerprint = unchanged.map.sourceFingerprint;
  await put(firstRoot, "src/helper.ts", "export const helper = () => 43;\n");
  const changed = await refreshRepoBrain(firstRoot);
  assert.notEqual(changed.map.sourceFingerprint, previousFingerprint);
  assert(changed.written.includes(".provena/repo.map.json"));
  assert(changed.written.includes(".provena/manifest.json"));

  const remoteA = await mkdtemp(join(tmpdir(), "provena-remote-name-a-"));
  const remoteB = await mkdtemp(join(tmpdir(), "provena-remote-name-b-"));
  roots.push(remoteA, remoteB);
  for (const remoteRoot of [remoteA, remoteB]) {
    await put(remoteRoot, "source.ts", "export const value = 1;\n");
    execFileSync("git", ["init", "--quiet"], { cwd: remoteRoot });
    execFileSync("git", ["remote", "add", "origin", "git@github.com:example/stable-repo.git"], { cwd: remoteRoot });
  }
  assert.equal((await scanRepo(remoteA)).repository.name, "stable-repo");
  assert.equal((await scanRepo(remoteB)).repository.name, "stable-repo");

  const filteredRoot = await fixture("provena-configured-scan-");
  roots.push(filteredRoot);
  await put(filteredRoot, ".provena/config.json", `${JSON.stringify({
    version: 1,
    repository_id: "configured-scan-repository",
    backend: "sqlite",
    database: { path: ".provena/provena.db" },
    store_url: "http://127.0.0.1:18092",
    scope: { tenant_id: "test", project_id: "configured-scan" },
    index: { include: ["src/**"], exclude: ["src/helper.ts"] },
  }, null, 2)}\n`);
  const filtered = await refreshRepoBrain(filteredRoot);
  assert.deepEqual(
    filtered.map.files.map((file) => file.path),
    ["src/index.ts"],
    "repo-brain refresh must honor configured include and exclude patterns",
  );

  console.log("repo brain kernel tests passed");
} finally {
  await Promise.all(roots.map((root) => rm(root, { recursive: true, force: true })));
}
