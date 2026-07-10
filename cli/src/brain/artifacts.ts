import { readFile, rm } from "node:fs/promises";
import { basename, join, posix } from "node:path";
import { activeMemoryEvents, readMemoryEvents, MEMORY_LEDGER_PATH } from "./events.js";
import { scanRepo, type ScanRepoOptions } from "./detect.js";
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
import { configExists, readConfig } from "../config.js";
import { assertSafeRepoPath } from "../security/paths.js";

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

export interface RefreshRepoBrainOptions extends ScanRepoOptions {
  map?: RepoMap;
}

export interface RefreshRepoBrainResult {
  map: RepoMap;
  graph: RepoGraph;
  manifest: RepoBrainManifest;
  written: string[];
}

export interface StoredRepoBrainArtifacts {
  map: RepoMap;
  graph: RepoGraph;
  manifest: RepoBrainManifest;
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
): Promise<void> {
  if ((await readExisting(repoRoot, path)) === content) return;
  await writeFileAtomic(repoRoot, join(repoRoot, ...path.split("/")), content);
  written.push(path);
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
  const graph = buildRepoGraph(map);
  const written: string[] = [];
  if ((await readExisting(repoRoot, MEMORY_LEDGER_PATH)) === null) {
    await writeFileAtomic(repoRoot, join(repoRoot, ...MEMORY_LEDGER_PATH.split("/")), "");
    written.push(MEMORY_LEDGER_PATH);
  }
  const events = await readMemoryEvents(repoRoot);
  const artifacts: Record<string, string> = {
    [REPO_BRAIN_PATH]: renderBrain(map, graph, events),
    [REPO_MAP_PATH]: canonicalJson(map, true),
    [REPO_GRAPH_PATH]: canonicalJson(graph, true),
    [MEMORY_EVENT_SCHEMA_PATH]: canonicalJson(MEMORY_EVENT_JSON_SCHEMA, true),
    ...renderViews(events),
  };
  const ledger = (await readExisting(repoRoot, MEMORY_LEDGER_PATH)) ?? "";
  const manifest: RepoBrainManifest = {
    schemaVersion: 1,
    sourceFingerprint: map.sourceFingerprint,
    memoryFingerprint: sha256(ledger),
    artifacts: [
      ...Object.entries(artifacts).map(([path, content]) => ({
        path,
        sha256: sha256(content),
        bytes: Buffer.byteLength(content, "utf8"),
      })),
      {
        path: MEMORY_LEDGER_PATH,
        sha256: sha256(ledger),
        bytes: Buffer.byteLength(ledger, "utf8"),
      },
    ].sort((a, b) => compareText(a.path, b.path)),
  };
  const generation: Record<string, string> = {
    ...artifacts,
    [REPO_MANIFEST_PATH]: canonicalJson(manifest, true),
  };
  const generationPaths = [
    ...Object.keys(artifacts).sort(compareText),
    REPO_MANIFEST_PATH,
  ];
  const previous = new Map<string, string | null>();
  for (const path of generationPaths) {
    previous.set(path, await readExisting(repoRoot, path));
  }
  try {
    for (const path of generationPaths) {
      await writeChanged(repoRoot, path, generation[path] ?? "", written);
    }
  } catch (error) {
    for (const path of [...written].reverse()) {
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
  return { map, graph, manifest, written };
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
  const readJson = async <T>(path: string): Promise<T> => {
    const text = await readExisting(repoRoot, path);
    if (text === null) {
      throw new Error(`missing ${path}; run \`provena refresh\` first`);
    }
    try {
      return JSON.parse(text) as T;
    } catch {
      throw new Error(`invalid ${path}; run \`provena refresh\` to repair it`);
    }
  };
  const map = await readJson<RepoMap>(REPO_MAP_PATH);
  const graph = await readJson<RepoGraph>(REPO_GRAPH_PATH);
  const manifest = await readJson<RepoBrainManifest>(REPO_MANIFEST_PATH);
  if (
    map.schemaVersion !== 1 ||
    !Array.isArray(map.files) ||
    graph.schemaVersion !== 1 ||
    !Array.isArray(graph.nodes) ||
    !Array.isArray(graph.edges) ||
    manifest.schemaVersion !== 1
  ) {
    throw new Error("stored repo brain artifacts have an unsupported schema; run `provena refresh`");
  }
  return { map, graph, manifest };
}
