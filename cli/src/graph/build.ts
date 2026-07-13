import { dirname, extname, posix } from "node:path";
import {
  assertMemoryLedgerSnapshotAttestation,
  memoryEventToRecord,
  type MemoryLedgerSnapshot,
} from "../brain/events.js";
import type { MemoryEvent, RepoMap, RepoPackage } from "../brain/types.js";
import { canonicalJson, compareText, sha256, stableId } from "../brain/utils.js";
import type {
  RepoGraphEdge,
  RepoGraphEdgeType,
  RepoGraphNode,
  RepoGraphV2,
  TemporalGraphDiagnostics,
} from "./types.js";
import {
  deriveMemoryTemporalRecords,
  memoryGraphNodeId,
  repoGraphProjectionFingerprint,
} from "./temporal.js";

function edge(
  from: string,
  to: string,
  type: RepoGraphEdgeType,
  weight = 1,
  effectiveAt?: string,
): RepoGraphEdge {
  return {
    id: stableId("edge", from, type, to),
    from,
    to,
    type,
    weight,
    ...(effectiveAt ? { effectiveAt } : {}),
  };
}

function sameStructure(left: unknown, right: unknown): boolean {
  return canonicalJson(left) === canonicalJson(right);
}

interface GraphMemoryInput {
  events: readonly MemoryEvent[];
  memoryFingerprint: string;
}

function graphMemoryInput(
  memory?: MemoryLedgerSnapshot | MemoryEvent[],
): GraphMemoryInput {
  let snapshot: MemoryLedgerSnapshot;
  if (memory === undefined || Array.isArray(memory)) {
    const events = memory ?? [];
    const rawLedger = events
      .map((event) => canonicalJson(memoryEventToRecord(event)))
      .join("");
    snapshot = {
      events,
      rawLedger,
      bytes: Buffer.byteLength(rawLedger, "utf8"),
      memoryFingerprint: sha256(rawLedger),
    };
  } else {
    snapshot = memory;
  }
  assertMemoryLedgerSnapshotAttestation(snapshot);
  return { events: snapshot.events, memoryFingerprint: snapshot.memoryFingerprint };
}

function parentDirectory(path: string): string | null {
  const parent = posix.dirname(path);
  return parent === "." ? null : parent;
}

function resolveImport(
  importerPath: string,
  specifier: string,
  knownFiles: Set<string>,
  packages: RepoPackage[],
): string | null {
  if (extname(importerPath) === ".py") {
    const leadingDots = /^\.+/.exec(specifier)?.[0].length ?? 0;
    let baseDirectory = posix.dirname(importerPath);
    for (let level = 1; level < leadingDots; level += 1) {
      baseDirectory = posix.dirname(baseDirectory);
    }
    const module = specifier.slice(leadingDots).replaceAll(".", "/");
    const pythonBase = leadingDots > 0
      ? posix.join(baseDirectory, module)
      : module;
    const pythonCandidates = [
      `${pythonBase}.py`,
      `${pythonBase}/__init__.py`,
    ];
    const pythonMatch = pythonCandidates.find((candidate) => knownFiles.has(candidate));
    if (pythonMatch) return pythonMatch;
  }
  if (extname(importerPath) === ".go") {
    const module = packages
      .filter((item) => item.ecosystem === "go")
      .sort((left, right) => right.name.length - left.name.length)
      .find((item) => specifier === item.name || specifier.startsWith(`${item.name}/`));
    if (module) {
      const suffix = specifier.slice(module.name.length).replace(/^\//, "");
      const base = posix.join(module.path === "." ? "" : module.path, suffix);
      const directory = base || (module.path === "." ? "." : module.path);
      const match = [...knownFiles]
        .filter((path) => extname(path) === ".go" && posix.dirname(path) === directory)
        .sort(compareText)[0];
      if (match) return match;
    }
  }
  if (extname(importerPath) === ".rs") {
    const parts = specifier.split("::").filter(Boolean);
    let baseDirectory = posix.dirname(importerPath);
    if (parts[0] === "crate") {
      parts.shift();
      const crate = packages
        .filter((item) => item.ecosystem === "rust")
        .sort((left, right) => right.path.length - left.path.length)
        .find((item) =>
          item.path === "." || importerPath.startsWith(`${item.path}/`),
        );
      baseDirectory = posix.join(crate?.path === "." ? "" : crate?.path ?? "", "src");
    } else if (parts[0] === "self") {
      parts.shift();
    } else {
      while (parts[0] === "super") {
        parts.shift();
        baseDirectory = posix.dirname(baseDirectory);
      }
    }
    for (let length = parts.length; length > 0; length -= 1) {
      const base = posix.join(baseDirectory, ...parts.slice(0, length));
      const match = [`${base}.rs`, `${base}/mod.rs`].find((path) => knownFiles.has(path));
      if (match) return match;
    }
  }
  if (!specifier.startsWith(".")) return null;
  const base = posix.normalize(posix.join(posix.dirname(importerPath), specifier));
  const sourceBase = /\.(?:c|m)?jsx?$/.test(base) ? base.replace(/\.(?:c|m)?jsx?$/, "") : base;
  const candidates = [
    base,
    ...[".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs"].map(
      (extension) => `${base}${extension}`,
    ),
    ...["index.ts", "index.tsx", "index.js", "index.jsx", "__init__.py"].map(
      (name) => `${base}/${name}`,
    ),
    ...[".ts", ".tsx"].map((extension) => `${sourceBase}${extension}`),
  ];
  return candidates.find((candidate) => knownFiles.has(candidate)) ?? null;
}

export interface RepoGraphBuildResult {
  graph: RepoGraphV2;
  diagnostics: TemporalGraphDiagnostics;
}

export function buildRepoGraphWithDiagnostics(
  map: RepoMap,
  memory?: MemoryLedgerSnapshot | MemoryEvent[],
): RepoGraphBuildResult {
  const nodes = new Map<string, RepoGraphNode>();
  const edges = new Map<string, RepoGraphEdge>();
  const addNode = (node: RepoGraphNode): void => {
    if (nodes.has(node.id)) throw new Error("repo graph node id collision");
    nodes.set(node.id, node);
  };
  // Dependencies are deliberately shared by ecosystem/name across packages.
  const ensureDependencyNode = (node: RepoGraphNode): void => {
    const existing = nodes.get(node.id);
    if (existing && !sameStructure(existing, node)) {
      throw new Error("repo graph node id collision");
    }
    if (!existing) nodes.set(node.id, node);
  };
  const addEdge = (item: RepoGraphEdge): void => {
    if (edges.has(item.id)) throw new Error("repo graph edge id collision");
    edges.set(item.id, item);
  };

  const rootId = "repo:root";
  addNode({
    id: rootId,
    type: "repository",
    label: map.repository.name,
    path: ".",
    metadata: { description: map.repository.description },
  });

  const directoryIds = new Map<string, string>();
  for (const directory of map.directories) {
    const existing = directoryIds.get(directory.path);
    if (existing && existing !== directory.id) {
      throw new Error("repo graph directory path collision");
    }
    directoryIds.set(directory.path, directory.id);
  }
  for (const directory of map.directories) {
    addNode({
      id: directory.id,
      type: "directory",
      label: posix.basename(directory.path),
      path: directory.path,
      metadata: { fileCount: directory.fileCount },
    });
    const parent = parentDirectory(directory.path);
    const parentId = parent ? directoryIds.get(parent) : rootId;
    if (parentId) addEdge(edge(parentId, directory.id, "contains"));
  }

  const knownFiles = new Set(map.files.map((file) => file.path));
  const fileIds = new Map<string, string>();
  for (const file of map.files) {
    const existing = fileIds.get(file.path);
    if (existing && existing !== file.id) {
      throw new Error("repo graph file path collision");
    }
    fileIds.set(file.path, file.id);
  }
  for (const file of map.files) {
    addNode({
      id: file.id,
      type: "file",
      label: posix.basename(file.path),
      path: file.path,
      metadata: {
        kind: file.kind,
        language: file.language,
        extension: extname(file.path),
        sizeBytes: file.sizeBytes,
      },
    });
    const parent = posix.dirname(file.path);
    addEdge(edge(parent === "." ? rootId : directoryIds.get(parent) ?? rootId, file.id, "contains"));
    for (const specifier of file.imports) {
      const imported = resolveImport(file.path, specifier, knownFiles, map.packages);
      const importedId = imported ? fileIds.get(imported) : undefined;
      if (importedId) addEdge(edge(file.id, importedId, "imports"));
    }
  }

  for (const symbol of map.symbols) {
    addNode({
      id: symbol.id,
      type: "symbol",
      label: symbol.name,
      path: symbol.path,
      metadata: {
        kind: symbol.kind,
        line: symbol.line,
        exported: symbol.exported,
      },
    });
    const fileId = fileIds.get(symbol.path);
    if (fileId) addEdge(edge(fileId, symbol.id, "defines"));
  }

  const commands = new Map<string, (typeof map.commands)[number]>();
  for (const command of map.commands) {
    if (commands.has(command.id)) throw new Error("repo graph command id collision");
    commands.set(command.id, command);
  }
  for (const pkg of map.packages) {
    addNode({
      id: pkg.id,
      type: "package",
      label: pkg.name,
      path: pkg.path,
      metadata: { ecosystem: pkg.ecosystem, manifestPath: pkg.manifestPath },
    });
    const parentId = pkg.path === "." ? rootId : directoryIds.get(pkg.path) ?? rootId;
    addEdge(edge(parentId, pkg.id, "contains"));
    for (const commandId of pkg.commandIds) {
      const command = commands.get(commandId);
      if (!command) continue;
      addNode({
        id: command.id,
        type: "command",
        label: command.name,
        path: command.source,
        metadata: { command: command.command, cwd: command.cwd },
      });
      addEdge(edge(pkg.id, command.id, "declares"));
    }
    for (const dependency of pkg.dependencies) {
      const dependencyId = stableId("dependency", pkg.ecosystem, dependency);
      ensureDependencyNode({
        id: dependencyId,
        type: "dependency",
        label: dependency,
        metadata: { ecosystem: pkg.ecosystem },
      });
      addEdge(edge(pkg.id, dependencyId, "depends_on"));
    }
  }

  for (const variable of map.environmentVariables ?? []) {
    addNode({
      id: variable.id,
      type: "environment",
      label: variable.name,
      metadata: { sourceCount: variable.sources.length },
    });
    for (const source of variable.sources) {
      const fileId = fileIds.get(source);
      if (fileId) addEdge(edge(fileId, variable.id, "uses"));
    }
  }

  const memoryInput = graphMemoryInput(memory);
  const temporal = deriveMemoryTemporalRecords(memoryInput.events);
  const recordsById = new Map(temporal.records.map((record) => [record.eventId, record]));
  const symbolIdsByLocation = new Map<string, string[]>();
  for (const symbol of map.symbols) {
    const key = `${symbol.path}\u0000${symbol.name}`;
    const ids = symbolIdsByLocation.get(key) ?? [];
    if (!ids.includes(symbol.id)) ids.push(symbol.id);
    symbolIdsByLocation.set(key, ids.sort(compareText));
  }

  for (const record of temporal.records) {
    addNode({
      id: memoryGraphNodeId(record.eventId),
      type: "memory",
      label: record.title,
      metadata: {
        eventId: record.eventId,
        title: record.title,
        kind: record.kind,
        subjectType: record.subjectType,
        declaredStatus: record.declaredStatus,
        authority: record.authority,
        confidence: record.confidence,
        importance: record.importance,
        sensitivity: record.sensitivity,
        validFrom: record.validFrom,
        validTo: record.validTo,
      },
    });
  }
  for (const event of memoryInput.events) {
    const from = memoryGraphNodeId(event.id);
    const effectiveAt = recordsById.get(event.id)!.validFrom;
    for (const predecessorId of event.supersedes) {
      addEdge(edge(
        from,
        memoryGraphNodeId(predecessorId),
        "supersedes",
        1,
        effectiveAt,
      ));
    }
    const citationTargets = new Set<string>();
    for (const source of event.sources) {
      const fileId = fileIds.get(source.path);
      if (!fileId) continue;
      citationTargets.add(fileId);
      if (!source.symbol) continue;
      const symbolIds = symbolIdsByLocation.get(`${source.path}\u0000${source.symbol}`) ?? [];
      if (symbolIds.length === 1) citationTargets.add(symbolIds[0]!);
    }
    for (const target of [...citationTargets].sort(compareText)) {
      addEdge(edge(from, target, "cites", 1, effectiveAt));
    }
    const applicabilityTargets = new Set<string>();
    for (const appliesTo of event.appliesTo) {
      const target = appliesTo === "." ? rootId : fileIds.get(appliesTo);
      if (target) applicabilityTargets.add(target);
    }
    for (const target of [...applicabilityTargets].sort(compareText)) {
      addEdge(edge(from, target, "applies_to", 1, effectiveAt));
    }
  }

  const graph: RepoGraphV2 = {
    schemaVersion: 2,
    sourceFingerprint: map.sourceFingerprint,
    memoryFingerprint: memoryInput.memoryFingerprint,
    projectionFingerprint: repoGraphProjectionFingerprint(
      map.sourceFingerprint,
      memoryInput.memoryFingerprint,
    ),
    timeSemantics: "event-effective-time",
    nodes: [...nodes.values()].sort((a, b) => compareText(a.id, b.id)),
    edges: [...edges.values()].sort(
      (a, b) =>
        compareText(a.from, b.from) ||
        compareText(a.type, b.type) ||
        compareText(a.to, b.to),
    ),
  };
  return { graph, diagnostics: temporal.diagnostics };
}

export function buildRepoGraph(
  map: RepoMap,
  memory?: MemoryLedgerSnapshot | MemoryEvent[],
): RepoGraphV2 {
  return buildRepoGraphWithDiagnostics(map, memory).graph;
}
