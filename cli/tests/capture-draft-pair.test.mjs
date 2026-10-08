import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fileSystem, { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { syncBuiltinESMExports } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { writeCaptureDraftPair } from "../dist/integrations/capture-hooks.js";

const sandbox = await mkdtemp(join(tmpdir(), "provena draft pair-"));
const originalRename = fileSystem.rename;
const ignore = ".provena/cache/\n";
async function fixture(name) {
  const root = join(sandbox, name);
  await mkdir(root);
  assert.equal(spawnSync("git", ["init", "-q"], { cwd: root }).status, 0);
  await writeFile(join(root, ".gitignore"), ignore);
  const directory = join(root, ".provena/cache/episodes/drafts");
  return { root, directory, wrapper: join(directory, `${"a".repeat(64)}.json`), candidate: join(directory, `${"a".repeat(64)}.candidate.json`) };
}
async function entries(directory) {
  try { return (await readdir(directory)).sort(); }
  catch (error) { if (error.code === "ENOENT") return []; throw error; }
}
function patchRename(callback) { fileSystem.rename = callback; syncBuiltinESMExports(); }

try {
  for (const rule of ["*.candidate.json", "*.candidate.json.capture-*.tmp"]) {
    const state = await fixture(`private-${rule.startsWith("*.candidate.json.capture") ? "temp" : "target"}`);
    await writeFile(join(state.root, ".gitignore"), ".provena/cache/*\n!.provena/cache/episodes/\n.provena/cache/episodes/*\n!.provena/cache/episodes/drafts/\n.provena/cache/episodes/drafts/*\n!.provena/cache/episodes/drafts/" + rule + "\n");
    await assert.rejects(writeCaptureDraftPair(state.root, state.wrapper, "wrapper", state.candidate, "candidate"), /ignored and untracked/);
    assert.deepEqual(await entries(state.directory), [], "both targets and nonces are private before either payload is persisted");
  }
  for (const existing of [false, true]) {
    const state = await fixture(existing ? "existing" : "new");
    const oldWrapper = Buffer.from([0xff, 0, 1]), oldCandidate = Buffer.from([0xfe, 2, 3]);
    if (existing) {
      await mkdir(state.directory, { recursive: true });
      await writeFile(state.wrapper, oldWrapper); await writeFile(state.candidate, oldCandidate);
    }
    patchRename(async (from, to) => {
      if (to === state.candidate) throw Object.assign(new Error("controlled second rename failure"), { code: "EACCES" });
      return originalRename(from, to);
    });
    try {
      await assert.rejects(writeCaptureDraftPair(state.root, state.wrapper, "new wrapper", state.candidate, "new candidate"), /previous files were preserved/);
      if (existing) {
        assert((await readFile(state.wrapper)).equals(oldWrapper));
        assert((await readFile(state.candidate)).equals(oldCandidate));
        assert.deepEqual(await entries(state.directory), [state.candidate, state.wrapper].map((path) => path.split(/[\\/]/).at(-1)).sort());
      } else assert.deepEqual(await entries(state.directory), []);
    } finally { patchRename(originalRename); }
  }
  const changingIgnore = await fixture("ignore-race");
  patchRename(async (from, to) => {
    await originalRename(from, to);
    if (to === changingIgnore.wrapper) {
      await writeFile(join(changingIgnore.root, ".gitignore"), ".provena/cache/*\n!.provena/cache/episodes/\n.provena/cache/episodes/*\n!.provena/cache/episodes/drafts/\n.provena/cache/episodes/drafts/*\n!.provena/cache/episodes/drafts/*.tmp\n");
    }
  });
  try {
    await assert.rejects(writeCaptureDraftPair(changingIgnore.root, changingIgnore.wrapper, "private wrapper", changingIgnore.candidate, "private candidate"));
    assert.deepEqual(await entries(changingIgnore.directory), [], "an owned staged payload is removed when ignore rules change before publication");
  } finally { patchRename(originalRename); }
  console.log("capture draft-pair privacy and handled-failure tests passed");
} finally {
  patchRename(originalRename);
  await rm(sandbox, { recursive: true, force: true });
}
