import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  assertDatabasePathUnderProvena,
  assertLoopbackStoreUrl,
  createDefaultConfig,
  validateConfig,
} from "../dist/config.js";
import { enumerateFiles, isDeniedSecretPath } from "../dist/indexer/discover.js";
import { assertNoSecretMaterial } from "../dist/security/memory.js";

function testSecretDenylistPatterns() {
  assert.equal(isDeniedSecretPath(".env"), true);
  assert.equal(isDeniedSecretPath(".env.local"), true);
  assert.equal(isDeniedSecretPath("secrets/server.pem"), true);
  assert.equal(isDeniedSecretPath("id_rsa.key"), true);
  assert.equal(isDeniedSecretPath("credentials.json"), true);
  assert.equal(isDeniedSecretPath(".npmrc"), true);
  assert.equal(isDeniedSecretPath(".aws/credentials"), true);
  assert.equal(isDeniedSecretPath("src/foo.ts"), false);
}

function testMemoryCredentialGuard() {
  const credentials = [
    ["sk", "proj", "abcdefghijklmnopqrstuv"].join("-"),
    ["sk", "ant", "api03", "abcdefghijklmnopqrstuv"].join("-"),
    ["ghp", "abcdefghijklmnopqrstuvwxyz123456"].join("_"),
    ["github", "pat", "abcdefghijklmnopqrstuvwxyz123456"].join("_"),
    ["xoxb", "1234567890", "abcdefghijklmnop"].join("-"),
    ["ASIA", "ABCDEFGHIJKLMNOP"].join(""),
    ["glpat", "abcdefghijklmnopqrstuv"].join("-"),
    ["sk", "live", "abcdefghijklmnopqrstuv"].join("_"),
    ["AI", "za", "abcdefghijklmnopqrstuvwxyz1234567890"].join(""),
    ["eyJabcdefghijk", "abcdefghijklmnop", "abcdefghijklmnop"].join("."),
    `${"postgresql"}://${"admin"}:${"correct-horse-battery"}@db.example.test/app`,
  ];
  for (const credential of credentials) {
    assert.throws(() => assertNoSecretMaterial(credential), /credential|private key/);
  }
  assert.doesNotThrow(() => assertNoSecretMaterial("Use the OPENAI_API_KEY environment variable."));
}

async function testSecretDenylistRegardlessOfGitignore() {
  const root = mkdtempSync(join(tmpdir(), "provena-security-denylist-"));
  let testError;
  try {
    mkdirSync(join(root, "src"), { recursive: true });
    writeFileSync(join(root, "src", "ok.ts"), "export const ok = 1;\n", "utf8");
    writeFileSync(join(root, "secret.pem"), "PRIVATE\n", "utf8");
    writeFileSync(join(root, ".env.production"), "API_KEY=abc\n", "utf8");
    writeFileSync(join(root, ".npmrc"), "//registry.npmjs.org/:_authToken=token\n", "utf8");

    const config = createDefaultConfig({ cwd: root, gitRoot: root });
    const files = await enumerateFiles(root, config);
    const paths = files.map((file) => file.path);

    assert.ok(paths.includes("src/ok.ts"));
    assert.ok(!paths.includes("secret.pem"));
    assert.ok(!paths.includes(".env.production"));
    assert.ok(!paths.includes(".npmrc"));
  } catch (error) {
    testError = error;
  }
  try {
    rmSync(root, {
      recursive: true,
      force: true,
      maxRetries: 10,
      retryDelay: 50,
    });
  } catch (cleanupError) {
    if (testError) {
      throw new AggregateError([testError, cleanupError], "security test and cleanup failed");
    }
    throw cleanupError;
  }
  if (testError) throw testError;
}

function testLoopbackStoreUrlValidation() {
  const base = createDefaultConfig({ cwd: "/tmp/app", gitRoot: "/tmp/repo" });

  assert.doesNotThrow(() => assertLoopbackStoreUrl("http://127.0.0.1:18092"));
  assert.doesNotThrow(() => assertLoopbackStoreUrl("http://localhost:18092"));
  assert.doesNotThrow(() => assertLoopbackStoreUrl("http://[::1]:18092"));
  assert.throws(
    () => assertLoopbackStoreUrl("http://user:secret@127.0.0.1:18092"),
    /must not include credentials/,
  );
  assert.throws(
    () => assertLoopbackStoreUrl("http://127.0.0.1:18092?token=secret"),
    /must not include credentials/,
  );

  assert.throws(
    () => assertLoopbackStoreUrl("http://192.168.1.10:18092"),
    /HTTPS/,
  );
  assert.throws(
    () => assertLoopbackStoreUrl("https://memory.example.test"),
    /loopback host/,
  );

  const prev = process.env.PROVENA_ALLOW_REMOTE_STORE;
  process.env.PROVENA_ALLOW_REMOTE_STORE = "1";
  try {
    assert.doesNotThrow(() => assertLoopbackStoreUrl("https://memory.example.test"));
    assert.throws(
      () => assertLoopbackStoreUrl("https://user:secret@memory.example.test"),
      /must not include credentials/,
    );
    const validated = validateConfig({
      ...base,
      store_url: "https://memory.example.test",
    });
    assert.equal(validated.store_url, "https://memory.example.test");
    assert.throws(
      () => assertLoopbackStoreUrl("http://memory.example.test"),
      /HTTPS/,
    );
  } finally {
    if (prev === undefined) {
      delete process.env.PROVENA_ALLOW_REMOTE_STORE;
    } else {
      process.env.PROVENA_ALLOW_REMOTE_STORE = prev;
    }
  }

  assert.throws(
    () =>
      validateConfig({
        ...base,
        store_url: "https://memory.example.test",
      }),
    /loopback host/,
  );
}

function testDatabasePathValidation() {
  const base = createDefaultConfig({ cwd: "/tmp/app", gitRoot: "/tmp/repo" });

  assert.doesNotThrow(() => assertDatabasePathUnderProvena(".provena/provena.db"));
  assert.doesNotThrow(() => assertDatabasePathUnderProvena(".provena/nested/db.sqlite"));

  assert.throws(
    () => assertDatabasePathUnderProvena("../outside.db"),
    /\.provena\//,
  );
  assert.throws(
    () => assertDatabasePathUnderProvena("/tmp/provena.db"),
    /\.provena\//,
  );
  assert.throws(
    () =>
      validateConfig({
        ...base,
        database: { path: "../secrets.db" },
      }),
    /\.provena\//,
  );
}

testSecretDenylistPatterns();
testMemoryCredentialGuard();
await testSecretDenylistRegardlessOfGitignore();
testLoopbackStoreUrlValidation();
testDatabasePathValidation();

console.log("security.test: ok");
