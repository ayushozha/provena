import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chunkTypeScriptFile } from "../dist/indexer/chunkers/typescript.js";
import {
  chunkSymbolKey,
  emitMemories,
  emptyIndexState,
  memoryFingerprint,
} from "../dist/indexer/emit.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const fixturePath = join(root, "fixtures", "sample.ts");
const sample = readFileSync(fixturePath, "utf8");

const chunks = await chunkTypeScriptFile("fixtures/sample.ts", sample);

function namesOf(kind) {
  return chunks.filter((chunk) => chunk.kind === kind).map((chunk) => chunk.name);
}

assert.equal(chunks.length, 7, `unexpected chunk count: ${chunks.map((c) => c.kind + ":" + c.name).join(", ")}`);

const moduleChunk = chunks.find((chunk) => chunk.kind === "module");
assert.ok(moduleChunk, "expected module chunk");
assert.equal(moduleChunk.name, "sample");
assert.ok(Array.isArray(moduleChunk.imports));
assert.ok(moduleChunk.imports.some((spec) => spec.includes("createHash")));
assert.ok(moduleChunk.imports.some((spec) => spec.includes("User")));

const importChunk = chunks.find((chunk) => chunk.kind === "import");
assert.ok(importChunk, "expected import chunk");
assert.match(importChunk.content, /node:crypto/);
assert.match(importChunk.content, /from "\.\/types"/);

assert.deepEqual(namesOf("function"), ["authenticate"]);
assert.deepEqual(namesOf("class"), ["UserStore"]);
assert.deepEqual(namesOf("interface"), ["UserProfile"]);
assert.deepEqual(namesOf("type_alias"), ["AppConfig"]);
assert.deepEqual(namesOf("method"), ["findById"]);

const authenticate = chunks.find((chunk) => chunk.name === "authenticate");
assert.equal(authenticate.exported, true);
assert.ok(authenticate.docstring?.includes("Authenticate a user"));

const findById = chunks.find((chunk) => chunk.name === "findById");
assert.equal(findById.parentSymbol, "UserStore");
assert.ok(findById.docstring?.includes("Find a user"));

for (const chunk of chunks) {
  assert.ok(chunk.startLine >= 1);
  assert.ok(chunk.endLine >= chunk.startLine);
  assert.ok(chunk.content.length > 0, `${chunk.kind}:${chunk.name} missing content`);
}

const broken = await chunkTypeScriptFile("broken.ts", "export function {{{");
assert.equal(broken.length, 1);
assert.equal(broken[0].kind, "artifact");
assert.equal(broken[0].content, "export function {{{");

const writes = [];
const fallbackState = emptyIndexState();
await emitMemories(
  broken,
  { path: "broken.ts", absolutePath: join(root, "broken.ts"), sha256: "broken-sha" },
  { tenant_id: "chunk-test", project_id: "fixture" },
  {
    storeUrl: "http://127.0.0.1:0",
    projectRoot: root,
    indexState: fallbackState,
    persistIndexState: false,
    client: {
      async createMemory(payload) {
        writes.push(payload);
        return {
          created: true,
          memory: { memory_id: `memory-${writes.length}` },
        };
      },
    },
  },
);
assert.equal(writes.length, 2, "fallback should emit a file artifact and full-content fact");
assert.equal(writes[1].kind, "fact");
assert.equal(writes[1].content, "export function {{{");

const duplicateMethods = await chunkTypeScriptFile(
  "duplicates.ts",
  "class First { run() { return true; } }\nclass Second { run() { return true; } }\n",
);
const methods = duplicateMethods.filter((chunk) => chunk.kind === "method");
assert.equal(methods.length, 2);
assert.notEqual(
  chunkSymbolKey("duplicates.ts", methods[0]),
  chunkSymbolKey("duplicates.ts", methods[1]),
  "same-name methods need collision-free index keys",
);

const fingerprintScope = { tenant_id: "chunk-test", project_id: "fixture" };
const generatedIdentity = "a".repeat(64);
const generatedFingerprintInput = [
  JSON.stringify(fingerprintScope),
  "artifact",
  "generated title",
  "generated content",
  "provena-generated-v2",
  generatedIdentity,
].join("|");
assert.equal(
  memoryFingerprint(
    fingerprintScope,
    "artifact",
    " Generated Title ",
    " Generated Content ",
    generatedIdentity,
  ),
  createHash("sha256").update(generatedFingerprintInput, "utf8").digest("hex"),
  "local generated-memory dedupe must mirror the store v2 fingerprint",
);
assert.notEqual(
  memoryFingerprint(fingerprintScope, "artifact", "Title", "content-a", generatedIdentity),
  memoryFingerprint(fingerprintScope, "artifact", "Title", "content-b", generatedIdentity),
  "generated identity must not erase title/content from the dedupe boundary",
);
assert.equal(
  memoryFingerprint(fingerprintScope, "artifact", "Title", "content", "not-a-sha256"),
  memoryFingerprint(fingerprintScope, "artifact", "Title", "content"),
  "invalid generated identities must not enter the trusted dedupe formula",
);

console.log("chunk-typescript.test: ok");
