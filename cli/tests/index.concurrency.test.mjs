import assert from "node:assert/strict";
import {
  mkdirSync,
  mkdtempSync,
  rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chunkTypeScriptFile } from "../dist/indexer/chunkers/typescript.js";
import {
  emptyIndexState,
  emitMemories,
  loadIndexState,
  saveIndexState,
} from "../dist/indexer/emit.js";

const fixtureSources = {
  "src/a.ts": `export function alpha(): string { return "a"; }\n`,
  "src/b.ts": `export function beta(): string { return "b"; }\n`,
  "src/c.ts": `export function gamma(): string { return "c"; }\n`,
};

class MockProvenaClient {
  #counter = 0;

  async createMemory(payload) {
    this.#counter += 1;
    const memoryId = `mem-${this.#counter}-${(payload.title ?? "untitled").replace(/[^\w.-]+/g, "_")}`;
    return { created: true, memory: { memory_id: memoryId } };
  }

  async healthz() {
    return true;
  }
}

async function indexFixtureFile(client, projectRoot, relativePath, source, options) {
  const chunks = await chunkTypeScriptFile(relativePath, source);
  return emitMemories(
    chunks,
    { path: relativePath, absolutePath: join(projectRoot, relativePath), sha256: `sha-${relativePath}` },
    { tenant_id: "concurrency-test", project_id: "fixture" },
    {
      storeUrl: "http://127.0.0.1:0",
      projectRoot,
      client,
      ...options,
    },
  );
}

async function testParallelSharedStatePreservesAllFiles() {
  const projectRoot = mkdtempSync(join(tmpdir(), "provena-index-concurrency-"));
  mkdirSync(join(projectRoot, ".provena"), { recursive: true });

  try {
    const client = new MockProvenaClient();
    const sharedState = emptyIndexState();
    const entries = Object.entries(fixtureSources);

    await Promise.all(
      entries.map(([relativePath, source]) =>
        indexFixtureFile(client, projectRoot, relativePath, source, {
          indexState: sharedState,
          persistIndexState: false,
        }),
      ),
    );

    saveIndexState(projectRoot, sharedState);
    const persisted = loadIndexState(projectRoot);

    assert.equal(
      Object.keys(persisted.files).length,
      entries.length,
      "shared in-memory state should retain every indexed file",
    );
    for (const [relativePath] of entries) {
      assert.ok(persisted.files[relativePath], `missing file entry for ${relativePath}`);
      assert.ok(
        persisted.files[relativePath].memoryIds.length > 0,
        `expected memory ids for ${relativePath}`,
      );
    }
  } finally {
    rmSync(projectRoot, { recursive: true, force: true });
  }
}

async function testParallelIndependentLoadSaveLosesFiles() {
  const projectRoot = mkdtempSync(join(tmpdir(), "provena-index-concurrency-"));
  mkdirSync(join(projectRoot, ".provena"), { recursive: true });

  try {
    const client = new MockProvenaClient();
    const entries = Object.entries(fixtureSources);

    await Promise.all(
      entries.map(([relativePath, source]) =>
        indexFixtureFile(client, projectRoot, relativePath, source),
      ),
    );

    const persisted = loadIndexState(projectRoot);
    assert.ok(
      Object.keys(persisted.files).length < entries.length,
      "concurrent load/save should drop file entries (last writer wins)",
    );
  } finally {
    rmSync(projectRoot, { recursive: true, force: true });
  }
}

await testParallelSharedStatePreservesAllFiles();
await testParallelIndependentLoadSaveLosesFiles();
console.log("index.concurrency.test: ok");