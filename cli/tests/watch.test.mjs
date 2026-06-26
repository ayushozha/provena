import assert from "node:assert/strict";
import {
  DebouncedIndexFlush,
  formatWatchLog,
  parseIntervalMs,
  parseWatchArgs,
} from "../dist/commands/watch.js";

assert.equal(parseIntervalMs("30s"), 30_000);
assert.equal(parseIntervalMs("30"), 30_000);
assert.equal(parseIntervalMs("500ms"), 500);
assert.equal(parseIntervalMs("2m"), 120_000);
assert.equal(parseIntervalMs("nope"), null);

assert.equal(parseWatchArgs(["--help"]), "help");
assert.equal(parseWatchArgs(["--interval", "nope"]), "error");
assert.deepEqual(parseWatchArgs([]), { debounceMs: 500, intervalMs: undefined });
assert.deepEqual(parseWatchArgs(["--interval", "15s"]), {
  debounceMs: 500,
  intervalMs: 15_000,
});

assert.match(formatWatchLog("ready"), /^\d{4}-\d{2}-\d{2}T.* watch: ready$/);

const flushed = [];
const debouncer = new DebouncedIndexFlush(50, async (paths) => {
  flushed.push(paths);
});

debouncer.noteChange("src/a.ts");
debouncer.noteChange("src/b.ts");
await new Promise((r) => setTimeout(r, 120));
assert.equal(flushed.length, 1);
assert.deepEqual(new Set(flushed[0]), new Set(["src/a.ts", "src/b.ts"]));

debouncer.dispose();
console.log("watch.test: ok");