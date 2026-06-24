import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  configPath,
  createDefaultConfig,
  ensureGitignore,
  readConfig,
  validateConfig,
  writeConfig,
} from "../dist/config.js";

function testCreateDefaultConfig() {
  const config = createDefaultConfig({
    cwd: "/repos/monorepo/packages/app",
    gitRoot: "/repos/monorepo",
  });

  assert.equal(config.version, 1);
  assert.equal(config.backend, "sqlite");
  assert.equal(config.database.path, ".provena/provena.db");
  assert.equal(config.store_url, "http://127.0.0.1:18092");
  assert.equal(config.scope.tenant_id, "app");
  assert.equal(config.scope.project_id, "monorepo");
  assert.deepEqual(config.index.include, ["**/*"]);
  assert.ok(config.index.exclude.includes("node_modules/**"));
}

function testValidateRejectsInvalid() {
  assert.throws(() => validateConfig(null), /object/);
  assert.throws(() => validateConfig({ version: 2 }), /version/);
  assert.throws(() => validateConfig({ version: 1, backend: "postgres" }), /backend/);
}

function testRoundTrip() {
  const root = mkdtempSync(join(tmpdir(), "provena-config-test-"));
  try {
    const original = createDefaultConfig({
      cwd: join(root, "my-repo"),
      gitRoot: root,
    });
    writeConfig(root, original);
    const onDisk = JSON.parse(readFileSync(configPath(root), "utf8"));
    const parsed = validateConfig(onDisk);
    const loaded = readConfig(root);

    assert.deepEqual(parsed, original);
    assert.deepEqual(loaded, original);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

function testEnsureGitignore() {
  const root = mkdtempSync(join(tmpdir(), "provena-gitignore-test-"));
  try {
    const added = ensureGitignore(root);
    assert.equal(added, true);
    const content = readFileSync(join(root, ".gitignore"), "utf8");
    assert.match(content, /\.provena\//);

    const addedAgain = ensureGitignore(root);
    assert.equal(addedAgain, false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

testCreateDefaultConfig();
testValidateRejectsInvalid();
testRoundTrip();
testEnsureGitignore();

console.log("config.test: ok");