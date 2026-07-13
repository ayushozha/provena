import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
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
import { runInit } from "../dist/commands/init.js";

function testCreateDefaultConfig() {
  const config = createDefaultConfig({
    cwd: "/repos/monorepo/packages/app",
    gitRoot: "/repos/monorepo",
  });

  assert.equal(config.version, 1);
  assert.match(config.repository_id, /^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$/);
  assert.equal(config.backend, "sqlite");
  assert.equal(config.database.path, ".provena/provena.db");
  assert.equal(config.store_url, "http://127.0.0.1:18092");
  assert.equal(config.store_api_key_env, "PROVENA_API_KEY");
  assert.equal(config.scope.tenant_id, "app");
  assert.equal(config.scope.project_id, "monorepo");
  assert.deepEqual(config.index.include, ["**/*"]);
  assert.ok(config.index.exclude.includes("node_modules/**"));
}

function testValidateRejectsInvalid() {
  assert.throws(() => validateConfig(null), /object/);
  assert.throws(() => validateConfig({ version: 2 }), /version/);
  assert.throws(() => validateConfig({ version: 1, backend: "postgres" }), /backend/);
  const valid = createDefaultConfig({ cwd: "/repos/app", gitRoot: "/repos/app" });
  for (const invalid of [" tenant", "tenant\nshadow", "tenant\0shadow", "\ud800"]) {
    assert.throws(
      () => validateConfig({ ...valid, scope: { ...valid.scope, tenant_id: invalid } }),
      /portable boundary-trimmed identifier/,
    );
  }
  assert.equal(
    validateConfig({ ...valid, scope: { ...valid.scope, tenant_id: "tenant-😀" } }).scope.tenant_id,
    "tenant-😀",
  );
  assert.equal(
    validateConfig({ ...valid, store_api_key_env: undefined }).store_api_key_env,
    "PROVENA_API_KEY",
    "older configs receive the non-secret default environment-variable name",
  );
  for (const invalid of ["", "9KEY", "KEY-NAME", "KEY NAME", "AWS_SECRET_ACCESS_KEY"]) {
    assert.throws(
      () => validateConfig({ ...valid, store_api_key_env: invalid }),
      /must be PROVENA_API_KEY/,
    );
  }
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
    assert.match(content, /^\.provena\/\*$/m);
    assert.match(content, /^!\.provena\/repo\.brain\.md$/m);
    assert.match(content, /^!\.provena\/maintenance\.plan\.json$/m);
    assert.doesNotMatch(content, /^\.provena\/$/m, "durable brain remains trackable");

    const addedAgain = ensureGitignore(root);
    assert.equal(addedAgain, false);

    writeFileSync(join(root, ".gitignore"), "dist/\n.provena/\n", "utf8");
    assert.equal(ensureGitignore(root), true, "adds a safe allowlist after a legacy ignore");
    const migrated = readFileSync(join(root, ".gitignore"), "utf8");
    assert.match(migrated, /^dist\/$/m, "preserves user patterns");
    assert.match(migrated, /^\.provena\/$/m, "preserves the user's legacy boundary");
    assert.match(migrated, /# >>> provena local state >>>/);

    mkdirSync(join(root, ".provena"), { recursive: true });
    writeFileSync(join(root, ".provena", "repo.brain.md"), "brain\n", "utf8");
    writeFileSync(join(root, ".provena", "maintenance.plan.json"), "{}\n", "utf8");
    writeFileSync(join(root, ".provena", "private.tmp"), "private\n", "utf8");
    spawnSync("git", ["init", "--quiet"], { cwd: root });
    assert.notEqual(
      spawnSync("git", ["check-ignore", "--quiet", ".provena/repo.brain.md"], { cwd: root }).status,
      0,
      "durable brain must remain trackable",
    );
    assert.notEqual(
      spawnSync("git", ["check-ignore", "--quiet", ".provena/maintenance.plan.json"], { cwd: root }).status,
      0,
      "durable maintenance plan must remain trackable",
    );
    assert.equal(
      spawnSync("git", ["check-ignore", "--quiet", ".provena/private.tmp"], { cwd: root }).status,
      0,
      "unknown legacy files must remain ignored",
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

testCreateDefaultConfig();
testValidateRejectsInvalid();
testRoundTrip();
testEnsureGitignore();

const invalidRoot = mkdtempSync(join(tmpdir(), "provena-invalid-init-"));
try {
  writeFileSync(join(invalidRoot, ".gitignore"), "node_modules/\n", "utf8");
  mkdirSync(join(invalidRoot, ".provena"), { recursive: true });
  writeFileSync(join(invalidRoot, ".provena", "config.json"), "{ invalid", "utf8");
  await assert.rejects(
    runInit({ cwd: invalidRoot, daemon: false }),
    /config\.json is invalid.*--force/,
  );
  assert.equal(existsSync(join(invalidRoot, "AGENTS.md")), false);
} finally {
  rmSync(invalidRoot, { recursive: true, force: true });
}

const forceRoot = mkdtempSync(join(tmpdir(), "provena-force-init-"));
try {
  const original = createDefaultConfig({ cwd: forceRoot, gitRoot: forceRoot });
  original.scope.project_id = "user-configured-project";
  writeConfig(forceRoot, original);
  await runInit({
    cwd: forceRoot,
    force: true,
    agents: false,
    hooks: false,
    mcp: false,
    runtime: false,
    daemon: false,
  });
  const afterForce = readConfig(forceRoot);
  assert.equal(afterForce.repository_id, original.repository_id);
  assert.equal(afterForce.scope.project_id, "user-configured-project");
} finally {
  rmSync(forceRoot, { recursive: true, force: true });
}

console.log("config.test: ok");
