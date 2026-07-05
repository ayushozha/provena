import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  classifyRole,
  classifyFiles,
  detectRepoIntelligence,
  buildRepoMap,
  renderBrainMarkdown,
} from "../dist/index.js";

function file(path, language, sizeBytes = 100) {
  return { path, absolutePath: path, sha256: "0".repeat(64), sizeBytes, language };
}

// --- classifyRole -------------------------------------------------------
assert.equal(classifyRole("src/index.ts"), "entrypoint", "index.ts is an entrypoint");
assert.equal(classifyRole("cmd/gateway/main.go"), "entrypoint", "cmd/*/main.go is an entrypoint");
assert.equal(classifyRole("package.json"), "config", "package.json is config");
assert.equal(classifyRole("tests/foo.test.ts"), "test", "test dir is test");
assert.equal(classifyRole("README.md"), "doc", "markdown is doc");
assert.equal(classifyRole("dist/bundle.js"), "generated", "dist is generated");
assert.equal(classifyRole("node_modules/x/y.js"), "vendor", "node_modules is vendor");
assert.equal(classifyRole("src/util/math.ts"), "source", "plain module is source");

// --- detectRepoIntelligence on a real temp repo -------------------------
const root = mkdtempSync(join(tmpdir(), "provena-brain-"));
try {
  writeFileSync(
    join(root, "package.json"),
    JSON.stringify({
      name: "demo-app",
      description: "A demo app for the brain test",
      scripts: { test: "vitest", build: "tsc", dev: "vite" },
    }),
  );
  writeFileSync(join(root, ".env.example"), "API_URL=\nSECRET_TOKEN=\n# comment\n");
  writeFileSync(join(root, "Makefile"), "deploy:\n\techo deploy\n");
  mkdirSync(join(root, "src"), { recursive: true });
  writeFileSync(join(root, "src", "index.ts"), 'const k = process.env.RUNTIME_KEY;\n');

  const discovered = [
    file("package.json", null),
    file(".env.example", null),
    file("Makefile", null),
    file("src/index.ts", "typescript"),
    file("README.md", "markdown"),
  ];
  const classified = classifyFiles(discovered);
  const intel = detectRepoIntelligence(root, classified);

  assert.ok(intel.packageManagers.includes("npm"), "detects npm");
  assert.equal(intel.commands.test, "npm test", "test command from scripts");
  assert.equal(intel.commands.build, "npm run build", "build command from scripts");
  assert.equal(intel.commands.run, "npm run dev", "run command from dev script");
  assert.equal(intel.commands.deploy, "make deploy", "deploy command from Makefile");
  assert.ok(intel.entrypoints.includes("src/index.ts"), "src/index.ts is an entrypoint");
  assert.ok(intel.envVars.includes("API_URL"), "env var from .env.example");
  assert.ok(intel.envVars.includes("SECRET_TOKEN"), "second env var from .env.example");
  assert.ok(intel.envVars.includes("RUNTIME_KEY"), "env var scanned from source");

  // --- buildRepoMap + renderBrainMarkdown -------------------------------
  const map = buildRepoMap(root, classified, intel, "2026-01-01T00:00:00.000Z");
  assert.equal(map.version, 1);
  assert.equal(map.identity, "A demo app for the brain test", "identity from package.json description");
  assert.equal(map.fileCount, 5);

  const md = renderBrainMarkdown(map);
  for (const section of ["# Repo Brain", "## Identity", "## Repo map", "## Commands", "## Entrypoints", "## How to get more"]) {
    assert.ok(md.includes(section), `brain includes "${section}"`);
  }
  assert.ok(md.includes("npm test"), "brain shows test command");
  assert.ok(md.includes("src/index.ts"), "brain lists entrypoint");
  assert.ok(Buffer.byteLength(md, "utf8") < 6144, "brain stays under ~6 KB");
} finally {
  rmSync(root, { recursive: true, force: true });
}

console.log("brain.test.mjs: OK");
