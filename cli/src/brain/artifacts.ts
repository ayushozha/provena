import { readFile, rm, stat } from "node:fs/promises";
import { basename, join, posix } from "node:path";
import {
  activeMemoryEvents,
  assertMemoryLedgerSnapshotAttestation,
  readMemoryLedgerSnapshot,
  MEMORY_LEDGER_PATH,
  type MemoryLedgerSnapshot,
} from "./events.js";
import {
  repoMapSourceFingerprint,
  scanRepo,
  type ScanRepoOptions,
} from "./detect.js";
import { MEMORY_EVENT_JSON_SCHEMA } from "./schema.js";
import type {
  MemoryEvent,
  MemoryKind,
  RepoBrainManifest,
  RepoMap,
} from "./types.js";
import { canonicalJson, compareText, sha256, writeFileAtomic } from "./utils.js";
import { withRepoMemoryLock } from "./lock.js";
import { buildRepoGraph } from "../graph/build.js";
import { degreeCentrality, pageRank } from "../graph/algorithms.js";
import type { RepoGraph } from "../graph/types.js";
import { repoGraphProjectionFingerprint } from "../graph/temporal.js";
import { configExists, readConfig } from "../config.js";
import { assertSafeRepoPath } from "../security/paths.js";
import { assertNoSecretMaterial } from "../security/memory.js";
import {
  reconcileRepoMapMemories,
  type RepoMemoryReconciliation,
} from "./reconcile.js";
import {
  assertMaintenancePlanAttestation,
  canonicalMaintenancePlan,
  compileMaintenancePlan,
  MAINTENANCE_PLAN_PATH,
  MAX_MAINTENANCE_PLAN_BYTES,
} from "../maintenance/plan.js";
import type { MaintenancePlan } from "../maintenance/types.js";

export const REPO_BRAIN_PATH = ".provena/repo.brain.md";
export const REPO_MAP_PATH = ".provena/repo.map.json";
export const REPO_GRAPH_PATH = ".provena/graph.json";
export const REPO_MANIFEST_PATH = ".provena/manifest.json";
export const MEMORY_EVENT_SCHEMA_PATH = ".provena/schema/memory-event.schema.json";

const VIEW_PATHS = {
  decision: ".provena/views/decisions.md",
  workflow: ".provena/views/workflows.md",
  learning: ".provena/views/learnings.md",
} as const;

/** Exact tracked artifacts committed by the manifest; the manifest commits itself last. */
export const REPO_BRAIN_MANAGED_ARTIFACT_PATHS = Object.freeze([
  REPO_BRAIN_PATH,
  REPO_MAP_PATH,
  REPO_GRAPH_PATH,
  MAINTENANCE_PLAN_PATH,
  MEMORY_EVENT_SCHEMA_PATH,
  VIEW_PATHS.decision,
  VIEW_PATHS.workflow,
  VIEW_PATHS.learning,
  MEMORY_LEDGER_PATH,
] as const);

/** Read-time ceilings for checked-in generations before any artifact is parsed. */
export const MAX_STORED_ARTIFACT_BYTES = 64 * 1024 * 1024;
export const MAX_STORED_GENERATION_BYTES = 128 * 1024 * 1024;
const STORED_GENERATION_ERROR =
  "stored repo brain artifacts do not describe one committed generation; run `provena refresh`";

export interface RefreshRepoBrainOptions extends ScanRepoOptions {
  map?: RepoMap;
  /** Observation clock for deterministic tests; production uses wall time. */
  now?: () => Date;
  /** Monotonic timing source for deterministic tests. */
  clock?: () => number;
}

export interface RefreshRepoBrainResult {
  map: RepoMap;
  graph: RepoGraph;
  maintenancePlan: MaintenancePlan;
  manifest: RepoBrainManifest;
  memory: MemoryLedgerSnapshot;
  reconciliation: RepoMemoryReconciliation;
  written: string[];
}

export interface StoredRepoBrainArtifacts {
  map: RepoMap;
  graph: RepoGraph;
  maintenancePlan: MaintenancePlan;
  manifest: RepoBrainManifest;
  memory: MemoryLedgerSnapshot;
  /** Exact verified bytes captured under the repository-memory read lock. */
  artifactContents: Readonly<Record<string, string>>;
}

function inline(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function markdownText(value: string): string {
  return inline(value).replace(/[\\`*_[\]<>#]/g, "\\$&");
}

function markdownCode(value: string): string {
  return `\`${inline(value).replaceAll("`", "'")}\``;
}

function markdownSources(event: MemoryEvent): string {
  if (event.sources.length === 0) return markdownCode(`memory:${event.id}`);
  return event.sources
    .map((source) => markdownCode(`${source.path}${source.startLine ? `:${source.startLine}` : ""}`))
    .join(", ");
}

function renderMemoryView(
  title: string,
  explanation: string,
  events: MemoryEvent[],
): string {
  const lines = [
    `# ${title}`,
    "",
    `> ${explanation}`,
    "> Derived from the append-only `.provena/memory/events.jsonl` ledger. Do not edit this view directly.",
    "",
  ];
  if (events.length === 0) {
    lines.push("_No active memories in this view._", "");
    return lines.join("\n");
  }
  for (const event of events) {
    const procedureRecord = Object.hasOwn(event.structuredData, "procedure") ||
      Object.hasOwn(event.structuredData, "procedureOutcome");
    if (procedureRecord) {
      // Static views cannot validate live source freshness or caller prerequisites.
      // Preserve an inspectable reference without turning a stored goal into advice.
      const payload = event.structuredData.procedure;
      const state = payload && typeof payload === "object" && "state" in payload &&
        ["candidate", "approved"].includes(String(payload.state)) ? String(payload.state) : "outcome or unvalidated record";
      const outcome = event.structuredData.procedureOutcome;
      const outcomeTarget = outcome && typeof outcome === "object" && "procedureId" in outcome
        ? outcome.procedureId : undefined;
      const inspectId = typeof outcomeTarget === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$/.test(outcomeTarget)
        ? outcomeTarget : event.id;
      lines.push(
        `## Procedure reference: ${markdownText(event.title)}`, "",
        "> Reference only. Readiness is not validated by this static view. Recorded steps do not authorize execution.", "",
        `- Recorded state: ${markdownCode(state)}`,
        `- Inspect current evidence: ${markdownCode(`provena procedure inspect ${inspectId} --json`)}`,
        `- Sources: ${markdownSources(event)}`,
        `- Memory ID: ${markdownCode(event.id)}`, "",
      );
      continue;
    }
    lines.push(
      `## ${markdownText(event.title)}`,
      "",
      `> ${markdownText(event.body)}`,
      "",
      `- Kind: \`${event.kind}\``,
      `- Authority: \`${event.authority}\` via \`${event.provenance.method}\``,
      `- Confidence / importance: ${event.confidence.toFixed(2)} / ${event.importance.toFixed(2)}`,
      `- Applies to: ${event.appliesTo.length ? event.appliesTo.map(markdownCode).join(", ") : "repository"}`,
      `- Sources: ${markdownSources(event)}`,
      `- Memory ID: ${markdownCode(event.id)}`,
      "",
    );
  }
  return lines.join("\n");
}

function byKinds(events: MemoryEvent[], kinds: MemoryKind[]): MemoryEvent[] {
  const allowed = new Set(kinds);
  return activeMemoryEvents(events)
    .filter((event) => !["confidential", "restricted"].includes(event.sensitivity))
    .filter((event) => allowed.has(event.kind))
    .sort(
      (a, b) =>
        b.importance - a.importance ||
        compareText(a.title, b.title) ||
        compareText(a.id, b.id),
    );
}

function renderViews(events: MemoryEvent[]): Record<string, string> {
  return {
    [VIEW_PATHS.decision]: renderMemoryView(
      "Decisions",
      "Explicit architectural and product decisions, including their cited rationale.",
      byKinds(events, ["decision"]),
    ),
    [VIEW_PATHS.workflow]: renderMemoryView(
      "Workflows",
      "Durable commands and operating procedures recorded by humans or agents.",
      byKinds(events, ["workflow"]),
    ),
    [VIEW_PATHS.learning]: renderMemoryView(
      "Learnings",
      "Mistakes, invariants, handoffs, preferences, and verified facts worth carrying forward.",
      byKinds(events, ["mistake", "invariant", "handoff", "preference", "fact"]),
    ),
  };
}

function keyFiles(map: RepoMap, graph: RepoGraph): string[] {
  const rank = pageRank(graph);
  const degree = degreeCentrality(graph);
  const kindBoost: Record<string, number> = {
    manifest: 0.25,
    configuration: 0.2,
    documentation: 0.1,
    source: 0.05,
    test: 0,
    migration: 0,
    asset: -1,
    other: -0.25,
  };
  return map.files
    .map((file) => ({
      path: file.path,
      score:
        (rank[file.id] ?? 0) * 10 +
        (degree[file.id]?.total ?? 0) * 4 +
        (kindBoost[file.kind] ?? 0) +
        (basename(file.path).toLowerCase().startsWith("readme") ? 0.3 : 0),
    }))
    .sort((a, b) => b.score - a.score || compareText(a.path, b.path))
    .slice(0, 18)
    .map((item) => item.path);
}

function renderBrain(map: RepoMap, graph: RepoGraph, events: MemoryEvent[]): string {
  const active = activeMemoryEvents(events).filter(
    (event) => !["confidential", "restricted"].includes(event.sensitivity),
  );
  const topDirectories = map.directories
    .filter((directory) => !directory.path.includes("/"))
    .sort((a, b) => b.fileCount - a.fileCount || compareText(a.path, b.path))
    .slice(0, 15);
  const lines = [
    `# ${markdownText(map.repository.name)} repo brain`,
    "",
    "> Compact bootloader for coding agents. Read this before broad repository scans.",
    "> Provena generates this file from repository facts and the explicit memory ledger.",
    "",
    "## Agent boot protocol",
    "",
    "1. Read this bootloader and the memory views linked below.",
    "2. Request a task-scoped packet with `provena context \"<task>\"` when available.",
    "3. Verify cited files before changing behavior; memories guide work but do not override source truth.",
    "4. Record durable decisions, workflows, mistakes, preferences, or handoffs explicitly; never infer intent.",
    "5. Refresh the brain after meaningful repository changes.",
    "6. Use `provena procedure recall \"<task>\" --json` before reusing tool steps. Check ready status and source evidence; human approval and caller-reported success never grant execution permission.",
    "",
    "## Repository",
    "",
    map.repository.description
      ? `Repository-declared description: ${markdownText(map.repository.description)}`
      : `${markdownText(map.repository.name)} repository`,
    "",
    `- Source fingerprint: \`${map.sourceFingerprint}\``,
    `- Files / symbols / graph: ${map.files.length} / ${map.symbols.length} / ${graph.nodes.length} nodes, ${graph.edges.length} edges`,
    `- Languages: ${map.languages.length ? map.languages.join(", ") : "none detected"}`,
    `- Active memories: ${active.length}`,
    "",
    "## Repository map",
    "",
  ];
  if (topDirectories.length) {
    for (const directory of topDirectories) {
      lines.push(`- ${markdownCode(`${directory.path}/`)} — ${directory.fileCount} files`);
    }
  } else {
    lines.push("- _Single-directory repository._");
  }
  lines.push("", "## Packages and services", "");
  if (map.packages.length) {
    for (const pkg of map.packages.slice(0, 20)) {
      lines.push(`- ${markdownCode(pkg.path)} — ${markdownText(pkg.name)} (${pkg.ecosystem}; ${pkg.dependencies.length} dependencies)`);
    }
  } else {
    lines.push("- _No supported package manifests detected._");
  }
  lines.push("", "## Declared commands", "");
  if (map.commands.length) {
    for (const command of map.commands.slice(0, 30)) {
      lines.push(`- ${markdownCode(command.command)} from ${markdownCode(command.cwd)} (declared by ${markdownCode(command.source)})`);
    }
  } else {
    lines.push("- _No explicit package scripts or Make targets detected._");
  }
  lines.push("", "## Environment contract", "");
  if (map.environmentVariables.length) {
    for (const variable of map.environmentVariables.slice(0, 30)) {
      lines.push(`- \`${variable.name}\` — ${variable.sources.map((path) => `\`${path}\``).join(", ")}`);
    }
    if (map.environmentVariables.length > 30) {
      lines.push(`- _${map.environmentVariables.length - 30} additional variables are in repo.map.json._`);
    }
  } else {
    lines.push("- _No declared environment-variable names detected._");
  }
  lines.push("", "## Key files", "");
  for (const path of keyFiles(map, graph)) lines.push(`- ${markdownCode(path)}`);
  lines.push(
    "",
    "## Durable memory",
    "",
    "- [Decisions](views/decisions.md)",
    "- [Workflows](views/workflows.md)",
    "- [Learnings](views/learnings.md)",
    "- Canonical append-only ledger: `memory/events.jsonl`",
    "- Machine-readable map: `repo.map.json`",
    "- Machine-readable graph: `graph.json`",
    "- [Maintenance proposals](maintenance.plan.json)",
    "",
  );
  return lines.join("\n");
}

async function readExisting(repoRoot: string, path: string): Promise<string | null> {
  const absolute = join(repoRoot, ...path.split("/"));
  assertSafeRepoPath(repoRoot, absolute);
  try {
    return await readFile(absolute, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
}

async function writeChanged(
  repoRoot: string,
  path: string,
  content: string,
  written: string[],
  writer: typeof writeFileAtomic,
): Promise<void> {
  if ((await readExisting(repoRoot, path)) === content) return;
  await writer(repoRoot, join(repoRoot, ...path.split("/")), content);
  written.push(path);
}

/**
 * Publish one locked repo-brain generation and restore every changed path if a
 * handled write fails. The manifest is supplied last by the refresh caller so
 * it remains the commit record. Multi-file crash atomicity is detected and
 * repaired by the next refresh; it requires a generation pointer to guarantee.
 *
 * Exported from this module (but not the package root) so rollback behavior can
 * be fault-injected without adding production failpoint configuration.
 */
export async function commitRepoBrainGeneration(
  repoRoot: string,
  generation: Readonly<Record<string, string>>,
  generationPaths: readonly string[],
  writer: typeof writeFileAtomic = writeFileAtomic,
): Promise<string[]> {
  if (new Set(generationPaths).size !== generationPaths.length) {
    throw new Error("repo brain generation contains duplicate paths");
  }
  for (const path of generationPaths) {
    if (!Object.prototype.hasOwnProperty.call(generation, path)) {
      throw new Error(`repo brain generation is missing ${path}`);
    }
  }
  const previous = new Map<string, string | null>();
  for (const path of generationPaths) {
    previous.set(path, await readExisting(repoRoot, path));
  }
  const written: string[] = [];
  try {
    for (const path of generationPaths) {
      await writeChanged(repoRoot, path, generation[path] ?? "", written, writer);
    }
  } catch (error) {
    // A custom/OS writer can replace the destination and then throw before
    // returning, so restore the complete snapshotted generation rather than
    // only paths whose writer reported success.
    for (const path of [...generationPaths].reverse()) {
      const prior = previous.get(path) ?? null;
      const absolute = join(repoRoot, ...path.split("/"));
      if (prior === null) {
        assertSafeRepoPath(repoRoot, absolute);
        await rm(absolute, { force: true });
      } else {
        await writeFileAtomic(repoRoot, absolute, prior);
      }
    }
    throw error;
  }
  return written;
}

async function refreshRepoBrainUnlocked(
  repoRoot: string,
  options: RefreshRepoBrainOptions = {},
): Promise<RefreshRepoBrainResult> {
  let scanOptions: ScanRepoOptions = options;
  if (
    !options.map &&
    options.includePatterns === undefined &&
    options.excludePatterns === undefined &&
    configExists(repoRoot)
  ) {
    const config = readConfig(repoRoot);
    scanOptions = {
      ...options,
      includePatterns: config.index.include,
      excludePatterns: config.index.exclude,
    };
  }
  const map = options.map ?? (await scanRepo(repoRoot, scanOptions));
  // The map contains more than reconciled candidates (paths, descriptions,
  // imports, symbols, and warnings), so screen the complete tracked artifact.
  assertNoSecretMaterial(canonicalJson(map));
  const currentMemory = await readMemoryLedgerSnapshot(repoRoot);
  // A caller-supplied map is useful for pure graph tests but is not attested to
  // the repository on disk, so it must never create automatic durable memory.
  const reconciled = options.map
    ? {
        memory: currentMemory,
        appended: [],
        reconciliation: {
          candidates: 0,
          added: 0,
          noops: 0,
          superseded: 0,
          retracted: 0,
          deferred: 0,
          conflicts: 0,
          durationMs: 0,
        } satisfies RepoMemoryReconciliation,
      }
    : await reconcileRepoMapMemories(repoRoot, map, currentMemory, {
        ...(options.now ? { now: options.now } : {}),
        ...(options.clock ? { clock: options.clock } : {}),
      });
  const memory = reconciled.memory;
  const events = memory.events;
  // Graph v2 is derived from the exact post-reconciliation snapshot so a
  // transition appended by this refresh is visible in the same generation.
  const graph = buildRepoGraph(map, memory);
  if (
    graph.schemaVersion !== 2 ||
    graph.sourceFingerprint !== map.sourceFingerprint ||
    graph.memoryFingerprint !== memory.memoryFingerprint
  ) {
    throw new Error("repo graph attestation does not match the refresh generation");
  }
  const maintenancePlan = compileMaintenancePlan(map, memory);
  const artifacts: Record<string, string> = {
    [REPO_BRAIN_PATH]: renderBrain(map, graph, events),
    [REPO_MAP_PATH]: canonicalJson(map, true),
    [REPO_GRAPH_PATH]: canonicalJson(graph, true),
    [MAINTENANCE_PLAN_PATH]: canonicalMaintenancePlan(maintenancePlan),
    [MEMORY_EVENT_SCHEMA_PATH]: canonicalJson(MEMORY_EVENT_JSON_SCHEMA, true),
    ...renderViews(events),
  };
  const committedArtifacts: Record<string, string> = {
    ...artifacts,
    [MEMORY_LEDGER_PATH]: memory.rawLedger,
  };
  const manifest: RepoBrainManifest = {
    schemaVersion: 1,
    sourceFingerprint: map.sourceFingerprint,
    memoryFingerprint: memory.memoryFingerprint,
    artifacts: REPO_BRAIN_MANAGED_ARTIFACT_PATHS.map((path) => {
      const content = committedArtifacts[path];
      return {
        path,
        sha256: sha256(content),
        bytes: Buffer.byteLength(content, "utf8"),
      };
    }),
  };
  const generation: Record<string, string> = {
    [MEMORY_LEDGER_PATH]: memory.rawLedger,
    ...artifacts,
    [REPO_MANIFEST_PATH]: canonicalJson(manifest, true),
  };
  const generationPaths = [
    MEMORY_LEDGER_PATH,
    ...Object.keys(artifacts).sort(compareText),
    REPO_MANIFEST_PATH,
  ];
  const written = await commitRepoBrainGeneration(
    repoRoot,
    generation,
    generationPaths,
  );
  return {
    map,
    graph,
    maintenancePlan,
    manifest,
    memory,
    reconciliation: reconciled.reconciliation,
    written,
  };
}

export async function refreshRepoBrain(
  repoRoot: string,
  options: RefreshRepoBrainOptions = {},
): Promise<RefreshRepoBrainResult> {
  return withRepoMemoryLock(repoRoot, () =>
    refreshRepoBrainUnlocked(repoRoot, options),
  );
}

export async function readRepoBrainArtifacts(
  repoRoot: string,
): Promise<StoredRepoBrainArtifacts> {
  return withRepoMemoryLock(repoRoot, async () => {
    const generationPaths = [
      ...REPO_BRAIN_MANAGED_ARTIFACT_PATHS,
      REPO_MANIFEST_PATH,
    ] as const;
    let generationBytes = 0;
    for (const path of generationPaths) {
      const absolute = join(repoRoot, ...path.split("/"));
      assertSafeRepoPath(repoRoot, absolute);
      let info;
      try {
        info = await stat(absolute);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") {
          throw new Error(`missing ${path}; run \`provena refresh\` first`);
        }
        throw error;
      }
      const maxBytes = path === MAINTENANCE_PLAN_PATH
        ? MAX_MAINTENANCE_PLAN_BYTES
        : MAX_STORED_ARTIFACT_BYTES;
      if (!info.isFile() || info.size > maxBytes) {
        throw new Error(`invalid ${path}; run \`provena refresh\` to repair it`);
      }
      generationBytes += info.size;
      if (
        !Number.isSafeInteger(generationBytes) ||
        generationBytes > MAX_STORED_GENERATION_BYTES
      ) {
        throw new Error(
          "stored repo brain generation exceeds the safety cap; run `provena refresh` to repair it",
        );
      }
    }

    const readCapped = async (path: string): Promise<string> => {
      const bytes = await readFile(join(repoRoot, ...path.split("/")));
      const maxBytes = path === MAINTENANCE_PLAN_PATH
        ? MAX_MAINTENANCE_PLAN_BYTES
        : MAX_STORED_ARTIFACT_BYTES;
      if (bytes.byteLength > maxBytes) {
        throw new Error(`invalid ${path}; run \`provena refresh\` to repair it`);
      }
      return bytes.toString("utf8");
    };
    const [artifactPairs, memory] = await Promise.all([
      Promise.all(generationPaths.map(async (path) => [path, await readCapped(path)] as const)),
      readMemoryLedgerSnapshot(repoRoot),
    ]);
    const artifactContents: Record<string, string> = Object.fromEntries(artifactPairs);
    const content = (path: string): string => {
      const value = artifactContents[path];
      if (value === undefined) throw new Error(STORED_GENERATION_ERROR);
      return value;
    };
    const mapText = content(REPO_MAP_PATH);
    const graphText = content(REPO_GRAPH_PATH);
    const maintenancePlanText = content(MAINTENANCE_PLAN_PATH);
    const manifestText = content(REPO_MANIFEST_PATH);
    const parse = <T>(text: string, path: string): T => {
      try {
        return JSON.parse(text) as T;
      } catch {
        throw new Error(`invalid ${path}; run \`provena refresh\` to repair it`);
      }
    };
    const map = parse<RepoMap>(mapText, REPO_MAP_PATH);
    const graph = parse<RepoGraph>(graphText, REPO_GRAPH_PATH);
    const maintenancePlan = parse<MaintenancePlan>(
      maintenancePlanText,
      MAINTENANCE_PLAN_PATH,
    );
    const manifest = parse<RepoBrainManifest>(manifestText, REPO_MANIFEST_PATH);
    if (
      !map || typeof map !== "object" ||
      map.schemaVersion !== 1 ||
      !Array.isArray(map.files) ||
      typeof map.sourceFingerprint !== "string" ||
      !graph || typeof graph !== "object" ||
      ![1, 2].includes(graph.schemaVersion) ||
      !Array.isArray(graph.nodes) ||
      !Array.isArray(graph.edges) ||
      typeof graph.sourceFingerprint !== "string" ||
      (graph.schemaVersion === 2 && (
        typeof graph.memoryFingerprint !== "string" ||
        typeof graph.projectionFingerprint !== "string" ||
        graph.timeSemantics !== "event-effective-time"
      )) ||
      !manifest || typeof manifest !== "object" ||
      manifest.schemaVersion !== 1 ||
      typeof manifest.sourceFingerprint !== "string" ||
      typeof manifest.memoryFingerprint !== "string" ||
      !Array.isArray(manifest.artifacts)
    ) {
      throw new Error("stored repo brain artifacts have an unsupported schema; run `provena refresh`");
    }
    if (manifest.artifacts.some((artifact) =>
      !artifact || typeof artifact !== "object" ||
      typeof artifact.path !== "string" ||
      typeof artifact.sha256 !== "string" ||
      !Number.isSafeInteger(artifact.bytes) || artifact.bytes < 0
    )) {
      throw new Error(STORED_GENERATION_ERROR);
    }
    if (
      manifest.artifacts.reduce((total, artifact) => total + artifact.bytes, 0) >
      MAX_STORED_GENERATION_BYTES
    ) {
      throw new Error(STORED_GENERATION_ERROR);
    }
    assertMemoryLedgerSnapshotAttestation(memory);
    try {
      assertMaintenancePlanAttestation(maintenancePlan, map, memory);
      if (maintenancePlanText !== canonicalMaintenancePlan(maintenancePlan)) {
        throw new Error();
      }
    } catch {
      throw new Error(STORED_GENERATION_ERROR);
    }
    const entries = new Map(manifest.artifacts.map((artifact) => [artifact.path, artifact]));
    const expectedPaths = [...REPO_BRAIN_MANAGED_ARTIFACT_PATHS].sort(compareText);
    const actualPaths = manifest.artifacts.map((artifact) => artifact.path).sort(compareText);
    const expectedArtifacts: Record<string, string> = {
      [REPO_BRAIN_PATH]: renderBrain(map, graph, memory.events),
      [REPO_MAP_PATH]: canonicalJson(map, true),
      [REPO_GRAPH_PATH]: canonicalJson(graph, true),
      [MAINTENANCE_PLAN_PATH]: canonicalMaintenancePlan(maintenancePlan),
      [MEMORY_EVENT_SCHEMA_PATH]: canonicalJson(MEMORY_EVENT_JSON_SCHEMA, true),
      ...renderViews(memory.events),
      [MEMORY_LEDGER_PATH]: memory.rawLedger,
    };
    const graphAttested = graph.schemaVersion === 1 || (
      graph.memoryFingerprint === memory.memoryFingerprint &&
      graph.projectionFingerprint === repoGraphProjectionFingerprint(
        graph.sourceFingerprint,
        graph.memoryFingerprint,
      ) &&
      graph.timeSemantics === "event-effective-time"
    );
    let graphCanonical = false;
    try {
      if (graph.schemaVersion === 2) {
        graphCanonical = canonicalJson(graph) === canonicalJson(buildRepoGraph(map, memory));
      } else {
        const expected = buildRepoGraph(map, []);
        graphCanonical = canonicalJson(graph.nodes) === canonicalJson(expected.nodes) &&
          canonicalJson(graph.edges) === canonicalJson(expected.edges);
      }
    } catch {
      graphCanonical = false;
    }
    if (
      canonicalJson(actualPaths) !== canonicalJson(expectedPaths) ||
      entries.size !== manifest.artifacts.length ||
      map.sourceFingerprint !== repoMapSourceFingerprint(map) ||
      map.sourceFingerprint !== graph.sourceFingerprint ||
      map.sourceFingerprint !== manifest.sourceFingerprint ||
      manifest.memoryFingerprint !== memory.memoryFingerprint ||
      !graphAttested ||
      !graphCanonical ||
      manifestText !== canonicalJson(manifest, true) ||
      REPO_BRAIN_MANAGED_ARTIFACT_PATHS.some((path) => {
        const stored = content(path);
        if (stored !== expectedArtifacts[path]) return true;
        const entry = entries.get(path);
        return !entry || entry.sha256 !== sha256(stored) ||
          entry.bytes !== Buffer.byteLength(stored, "utf8");
      })
    ) {
      throw new Error(STORED_GENERATION_ERROR);
    }
    return {
      map,
      graph,
      maintenancePlan,
      manifest,
      memory,
      artifactContents: Object.freeze({ ...artifactContents }),
    };
  });
}
