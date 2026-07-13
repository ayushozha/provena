import type { MemoryEvent, RepoMap } from "../brain/types.js";
import { repoMapSourceFingerprint } from "../brain/detect.js";
import {
  activeMemoryEventsAt,
  assertMemoryLedgerSnapshotAttestation,
  canonicalMemoryAsOf,
  memoryEventToRecord,
  type MemoryLedgerSnapshot,
} from "../brain/events.js";
import { canonicalJson, compareText, sha256 } from "../brain/utils.js";
import {
  REPO_MAP_MEMORY_DATA_KEY,
  REPO_MAP_MEMORY_GENERATOR,
  REPO_MAP_MEMORY_GENERATOR_VERSION,
  REPO_MAP_MEMORY_TAG,
} from "../brain/reconcile.js";
import {
  degreeCentrality,
  induceRepoGraphAt,
  neighborhood,
  pageRank,
  repoGraphProjectionFingerprint,
} from "../graph/index.js";
import type { RepoGraph } from "../graph/types.js";

export interface ContextQuery {
  query?: string;
  paths?: string[];
  symbols?: string[];
  commands?: string[];
  memoryIds?: string[];
  maxItems?: number;
  maxCharacters?: number;
  maxTokens?: number;
  graphHops?: number;
  includeSensitive?: boolean;
  memoryAsOf?: string;
}

export interface ContextCitation {
  path?: string;
  symbol?: string;
  startLine?: number;
  endLine?: number;
  memoryId?: string;
}

function duplicatesCurrentCommand(memory: MemoryEvent, map: RepoMap): boolean {
  if (memory.kind !== "workflow" || !memory.tags.includes(REPO_MAP_MEMORY_TAG)) {
    return false;
  }
  const value = memory.structuredData[REPO_MAP_MEMORY_DATA_KEY];
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const managed = value as Record<string, unknown>;
  const candidate = managed.candidate;
  if (
    managed.generator !== REPO_MAP_MEMORY_GENERATOR ||
    managed.version !== REPO_MAP_MEMORY_GENERATOR_VERSION ||
    !candidate ||
    typeof candidate !== "object" ||
    Array.isArray(candidate)
  ) {
    return false;
  }
  const command = candidate as Record<string, unknown>;
  if (
    command.type !== "command" ||
    typeof command.manifestPath !== "string" ||
    typeof command.cwd !== "string" ||
    typeof command.name !== "string" ||
    typeof command.command !== "string"
  ) {
    return false;
  }
  const normalize = (text: string) => text.normalize("NFC").trim();
  return map.commands.some(
    (current) =>
      current.source === command.manifestPath &&
      current.cwd === command.cwd &&
      normalize(current.name) === command.name &&
      normalize(current.command) === command.command,
  );
}

export interface ContextItem {
  id: string;
  type: "file" | "symbol" | "command" | "environment" | "memory";
  title: string;
  summary: string;
  score: number;
  citations: ContextCitation[];
}

export interface ContextPacket {
  schemaVersion: 1;
  query: string;
  sourceFingerprint: string;
  memoryFingerprint: string;
  repositoryTopology: "current";
  memoryAsOf?: string;
  budget: {
    maxCharacters: number;
    maxTokens: number;
    usedCharacters: number;
    estimatedTokens: number;
    truncated: boolean;
  };
  items: ContextItem[];
}

const DEFAULT_MAX_ITEMS = 32;
const DEFAULT_MAX_CHARACTERS = 12_000;
const CHARS_PER_TOKEN = 4;
export const MAX_CONTEXT_MEMORY_IDS = 32;
const MEMORY_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$/;
const MEMORY_IDS_ERROR =
  `memoryIds must contain at most ${MAX_CONTEXT_MEMORY_IDS} valid memory IDs`;

function normalized(values: string[] | undefined): Set<string> {
  return new Set(
    (values ?? [])
      .map((value) => value.trim().replaceAll("\\", "/").replace(/^\.\//, "").toLowerCase())
      .filter(Boolean),
  );
}

function normalizedMemoryIds(values: string[] | undefined): Set<string> {
  if (values === undefined) return new Set();
  if (
    !Array.isArray(values) ||
    values.length > MAX_CONTEXT_MEMORY_IDS ||
    values.some((value) => typeof value !== "string" || !MEMORY_ID_PATTERN.test(value))
  ) {
    throw new Error(MEMORY_IDS_ERROR);
  }
  return new Set(values);
}

function tokens(value: string): Set<string> {
  return new Set(
    value
      .toLowerCase()
      .split(/[^a-z0-9_./:@-]+/)
      .map((token) => token.trim())
      .filter((token) => token.length > 1),
  );
}

function overlap(needles: Set<string>, haystack: string): number {
  const available = tokens(haystack);
  let count = 0;
  for (const needle of needles) if (available.has(needle)) count += 1;
  return count;
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

function measureRenderedPacket(packet: ContextPacket): ContextPacket {
  let measured = packet;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const usedCharacters = Buffer.byteLength(
      renderContextPacketMarkdown(measured),
      "utf8",
    );
    const estimatedTokens = Math.ceil(usedCharacters / CHARS_PER_TOKEN);
    if (
      usedCharacters === measured.budget.usedCharacters &&
      estimatedTokens === measured.budget.estimatedTokens
    ) {
      return measured;
    }
    measured = {
      ...measured,
      budget: { ...measured.budget, usedCharacters, estimatedTokens },
    };
  }
  return measured;
}

function scoreGraph(graph: RepoGraph): Record<string, number> {
  const rank = pageRank(graph);
  const degree = degreeCentrality(graph);
  return Object.fromEntries(
    graph.nodes.map((node) => [
      node.id,
      (rank[node.id] ?? 0) * 25 + (degree[node.id]?.total ?? 0) * 10,
    ]),
  );
}

function contextMemorySnapshot(
  memory: MemoryEvent[] | MemoryLedgerSnapshot,
): MemoryLedgerSnapshot {
  if (!Array.isArray(memory)) return memory;
  // Preserve the pre-release array API using the canonical JSONL form. Repo
  // callers pass a raw-byte snapshot so blank lines and line endings stay exact.
  const ledger = memory.map((event) => canonicalJson(memoryEventToRecord(event))).join("");
  return {
    events: memory,
    memoryFingerprint: sha256(ledger),
    bytes: Buffer.byteLength(ledger, "utf8"),
    rawLedger: ledger,
  };
}

const CONTEXT_ATTESTATION_ERROR =
  "context inputs do not describe one attested repo generation";

function assertContextAttestation(
  map: RepoMap,
  graph: RepoGraph,
  snapshot: MemoryLedgerSnapshot,
): void {
  try {
    assertMemoryLedgerSnapshotAttestation(snapshot);
    if (
      map.sourceFingerprint !== repoMapSourceFingerprint(map) ||
      map.sourceFingerprint !== graph.sourceFingerprint ||
      (graph.schemaVersion === 2 && (
        snapshot.memoryFingerprint !== graph.memoryFingerprint ||
        graph.timeSemantics !== "event-effective-time" ||
        graph.projectionFingerprint !== repoGraphProjectionFingerprint(
          graph.sourceFingerprint,
          graph.memoryFingerprint,
        )
      ))
    ) {
      throw new Error();
    }
  } catch {
    throw new Error(CONTEXT_ATTESTATION_ERROR);
  }
}

export function buildContextPacket(
  map: RepoMap,
  graph: RepoGraph,
  memory: MemoryEvent[] | MemoryLedgerSnapshot,
  input: ContextQuery = {},
): ContextPacket {
  const snapshot = contextMemorySnapshot(memory);
  assertContextAttestation(map, graph, snapshot);
  const events = snapshot.events;
  const memoryAsOf = canonicalMemoryAsOf(input.memoryAsOf);
  const contextGraph = induceRepoGraphAt(graph, memoryAsOf);
  const query = inline(input.query ?? "");
  const queryTokens = tokens(query);
  const exactPaths = normalized(input.paths);
  const exactSymbols = normalized(input.symbols);
  const exactCommands = normalized(input.commands);
  const exactMemoryIds = normalizedMemoryIds(input.memoryIds);
  const graphScores = scoreGraph(contextGraph);
  const memoryGraphIds = new Map(
    contextGraph.nodes.flatMap((node) =>
      node.type === "memory" && typeof node.metadata.eventId === "string"
        ? [[node.metadata.eventId, node.id] as const]
        : [],
    ),
  );
  const candidates: ContextItem[] = [];
  const seedIds = new Set<string>();
  const exactTiers = new Map<string, number>();
  const tierKey = (type: ContextItem["type"], id: string): string =>
    `${type}\u0000${id}`;

  for (const file of map.files) {
    const path = file.path.toLowerCase();
    let score = graphScores[file.id] ?? 0;
    if (exactPaths.has(path)) {
      score += 1_000;
      seedIds.add(file.id);
      exactTiers.set(tierKey("file", file.id), 4);
    }
    if (query && query.toLowerCase() === path) {
      score += 900;
      seedIds.add(file.id);
      exactTiers.set(tierKey("file", file.id), 4);
    }
    score += overlap(queryTokens, `${file.path} ${file.kind} ${file.language ?? ""}`) * 45;
    if (score <= 0 && queryTokens.size > 0) continue;
    candidates.push({
      id: file.id,
      type: "file",
      title: file.path,
      summary: `${file.kind}${file.language ? ` ${file.language}` : ""}; ${file.sizeBytes} bytes${file.imports.length ? `; imports ${file.imports.slice(0, 8).join(", ")}` : ""}`,
      score,
      citations: [{ path: file.path }],
    });
  }

  for (const symbol of map.symbols) {
    const name = symbol.name.toLowerCase();
    let score = graphScores[symbol.id] ?? 0;
    if (exactSymbols.has(name) || exactSymbols.has(`${symbol.path}:${name}`)) {
      score += 950;
      seedIds.add(symbol.id);
      exactTiers.set(tierKey("symbol", symbol.id), 3);
    }
    if (query && query.toLowerCase() === name) {
      score += 850;
      seedIds.add(symbol.id);
      exactTiers.set(tierKey("symbol", symbol.id), 3);
    }
    score += overlap(queryTokens, `${symbol.name} ${symbol.kind} ${symbol.path}`) * 55;
    if (score <= 0 && queryTokens.size > 0) continue;
    candidates.push({
      id: symbol.id,
      type: "symbol",
      title: `${symbol.name} (${symbol.kind})`,
      summary: `${symbol.exported ? "exported" : "local"} symbol in ${symbol.path}:${symbol.line}`,
      score,
      citations: [{ path: symbol.path, symbol: symbol.name, startLine: symbol.line, endLine: symbol.line }],
    });
  }

  for (const command of map.commands) {
    const keys = [command.name, command.command].map((value) => value.toLowerCase());
    let score = graphScores[command.id] ?? 0;
    if (keys.some((value) => exactCommands.has(value))) {
      score += 900;
      seedIds.add(command.id);
      exactTiers.set(tierKey("command", command.id), 2);
    }
    if (query && keys.includes(query.toLowerCase())) {
      score += 825;
      seedIds.add(command.id);
      exactTiers.set(tierKey("command", command.id), 2);
    }
    score += overlap(queryTokens, `${command.name} ${command.command} ${command.cwd}`) * 50;
    if (score <= 0 && queryTokens.size > 0) continue;
    candidates.push({
      id: command.id,
      type: "command",
      title: command.name,
      summary: `Run ${command.command} from ${command.cwd}`,
      score,
      citations: [{ path: command.source }],
    });
  }

  for (const variable of map.environmentVariables ?? []) {
    let score = graphScores[variable.id] ?? 0;
    if (query && query.toLowerCase() === variable.name.toLowerCase()) {
      score += 825;
      seedIds.add(variable.id);
    }
    score += overlap(
      queryTokens,
      `${variable.name} ${variable.sources.join(" ")}`,
    ) * 50;
    if (score <= 0 && queryTokens.size > 0) continue;
    candidates.push({
      id: variable.id,
      type: "environment",
      title: variable.name,
      summary: `Environment variable referenced by ${variable.sources.join(", ")}`,
      score,
      citations: variable.sources.map((path) => ({ path })),
    });
  }

  for (const memory of activeMemoryEventsAt(events, memoryAsOf)) {
    if (!input.includeSensitive && ["confidential", "restricted"].includes(memory.sensitivity)) continue;
    // The current repo-map command already carries the same invocation and
    // source citation. Keep the managed event in the durable ledger/history,
    // but do not spend compact packet budget on a duplicate live item.
    if (duplicatesCurrentCommand(memory, map) && !exactMemoryIds.has(memory.id)) continue;
    const graphId = memoryGraphIds.get(memory.id);
    let score = (graphId ? graphScores[graphId] ?? 0 : 0) +
      memory.importance * 30 + memory.confidence * 15;
    if (exactMemoryIds.has(memory.id)) {
      score += 1_100;
      exactTiers.set(tierKey("memory", memory.id), 5);
      if (graphId) seedIds.add(graphId);
    }
    if (memory.appliesTo.some((value) => exactPaths.has(value.toLowerCase()))) {
      score += 800;
      const key = tierKey("memory", memory.id);
      exactTiers.set(key, Math.max(exactTiers.get(key) ?? 0, 1));
      if (graphId) seedIds.add(graphId);
    }
    score += overlap(
      queryTokens,
      `${memory.title} ${memory.body} ${memory.kind} ${memory.tags.join(" ")} ${memory.triggers.join(" ")} ${memory.appliesTo.join(" ")}`,
    ) * 60;
    if (score <= 0 && queryTokens.size > 0) continue;
    candidates.push({
      id: memory.id,
      type: "memory",
      title: `[${memory.kind}] ${memory.title}`,
      summary: inline(memory.body),
      score,
      citations: memory.sources.length
        ? memory.sources.map((source) => ({
            path: source.path,
            ...(source.symbol ? { symbol: source.symbol } : {}),
            ...(source.startLine ? { startLine: source.startLine } : {}),
            ...(source.endLine ? { endLine: source.endLine } : {}),
            memoryId: memory.id,
          }))
        : [{ memoryId: memory.id }],
    });
  }

  const graphHops = input.graphHops ?? 1;
  if (!Number.isInteger(graphHops) || graphHops < 0 || graphHops > 5) {
    throw new Error("graphHops must be an integer between 0 and 5");
  }
  const graphNeighbors = new Set<string>();
  for (const seed of seedIds) {
    for (const id of neighborhood(contextGraph, seed, graphHops).nodeIds) graphNeighbors.add(id);
  }
  for (const candidate of candidates) {
    const graphId = candidate.type === "memory"
      ? memoryGraphIds.get(candidate.id) ?? candidate.id
      : candidate.id;
    if (graphNeighbors.has(graphId) && !seedIds.has(graphId)) candidate.score += 20;
  }

  candidates.sort(
    (a, b) =>
      (exactTiers.get(tierKey(b.type, b.id)) ?? 0) -
        (exactTiers.get(tierKey(a.type, a.id)) ?? 0) ||
      b.score - a.score ||
      compareText(a.type, b.type) ||
      compareText(a.id, b.id),
  );
  const maxItems = input.maxItems ?? DEFAULT_MAX_ITEMS;
  if (!Number.isInteger(maxItems) || maxItems < 1) throw new Error("maxItems must be a positive integer");
  const requestedCharacters = input.maxCharacters ?? DEFAULT_MAX_CHARACTERS;
  const requestedTokens = input.maxTokens ?? Math.ceil(requestedCharacters / CHARS_PER_TOKEN);
  if (!Number.isInteger(requestedCharacters) || requestedCharacters < 256) {
    throw new Error("maxCharacters must be an integer of at least 256");
  }
  if (!Number.isInteger(requestedTokens) || requestedTokens < 64) {
    throw new Error("maxTokens must be an integer of at least 64");
  }
  const maxCharacters = Math.min(requestedCharacters, requestedTokens * CHARS_PER_TOKEN);
  const items: ContextItem[] = [];
  const createPacket = (
    selected: ContextItem[],
    truncated: boolean,
    packetQuery: string,
  ): ContextPacket =>
    measureRenderedPacket({
      schemaVersion: 1,
      query: packetQuery,
      sourceFingerprint: map.sourceFingerprint,
      memoryFingerprint: snapshot.memoryFingerprint,
      repositoryTopology: "current",
      ...(memoryAsOf ? { memoryAsOf } : {}),
      budget: {
        maxCharacters,
        maxTokens: requestedTokens,
        usedCharacters: 0,
        estimatedTokens: 0,
        truncated,
      },
      items: selected,
    });
  if (createPacket([], true, "").budget.usedCharacters > maxCharacters) {
    throw new Error("context budget is too small for the packet metadata");
  }
  let renderedQuery = query;
  let queryTruncated = false;
  if (createPacket([], true, renderedQuery).budget.usedCharacters > maxCharacters) {
    const characters = [...query];
    let low = 0;
    let high = characters.length;
    let best = "";
    while (low <= high) {
      const middle = Math.floor((low + high) / 2);
      const candidate = middle < characters.length
        ? `${characters.slice(0, middle).join("")}…`
        : query;
      if (createPacket([], true, candidate).budget.usedCharacters <= maxCharacters) {
        best = candidate;
        low = middle + 1;
      } else {
        high = middle - 1;
      }
    }
    renderedQuery = best;
    queryTruncated = true;
  }
  const packetWith = (selected: ContextItem[], truncated: boolean): ContextPacket =>
    createPacket(selected, truncated || queryTruncated, renderedQuery);
  for (const candidate of candidates) {
    if (items.length >= maxItems) break;
    if (packetWith([...items, candidate], true).budget.usedCharacters <= maxCharacters) {
      items.push(candidate);
      continue;
    }
    const characters = [...candidate.summary];
    let low = 0;
    let high = characters.length;
    let clipped: ContextItem | null = null;
    while (low <= high) {
      const middle = Math.floor((low + high) / 2);
      const next: ContextItem = {
        ...candidate,
        summary:
          middle < characters.length
            ? `${characters.slice(0, middle).join("")}…`
            : candidate.summary,
      };
      if (packetWith([...items, next], true).budget.usedCharacters <= maxCharacters) {
        clipped = next;
        low = middle + 1;
      } else {
        high = middle - 1;
      }
    }
    if (clipped) items.push(clipped);
    break;
  }

  return packetWith(items, items.length < candidates.length);
}

export function renderContextPacketMarkdown(packet: ContextPacket): string {
  const lines = [
    "# Provena context packet",
    "",
    `Query: ${markdownText(packet.query || "(repository overview)")}`,
    `Memory: ${packet.memoryAsOf ? `historical as of ${markdownCode(packet.memoryAsOf)}` : "current"}; repository topology: current`,
    `Budget: ${packet.budget.usedCharacters}/${packet.budget.maxCharacters} characters (~${packet.budget.estimatedTokens} tokens)${packet.budget.truncated ? "; truncated" : ""}`,
    "",
  ];
  for (const item of packet.items) {
    lines.push(`## ${markdownText(item.title)}`, "", `> ${markdownText(item.summary)}`, "");
    for (const citation of item.citations) {
      const location = citation.path
        ? `${citation.path}${citation.startLine ? `:${citation.startLine}` : ""}`
        : `memory:${citation.memoryId ?? item.id}`;
      lines.push(`- Source: ${markdownCode(location)}`);
    }
    lines.push("");
  }
  return `${lines.join("\n").trimEnd()}\n`;
}
