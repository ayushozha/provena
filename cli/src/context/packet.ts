import type { MemoryEvent, RepoMap } from "../brain/types.js";
import { activeMemoryEvents } from "../brain/events.js";
import { canonicalJson, compareText, sha256 } from "../brain/utils.js";
import { degreeCentrality, neighborhood, pageRank } from "../graph/algorithms.js";
import type { RepoGraph } from "../graph/types.js";

export interface ContextQuery {
  query?: string;
  paths?: string[];
  symbols?: string[];
  commands?: string[];
  maxItems?: number;
  maxCharacters?: number;
  maxTokens?: number;
  graphHops?: number;
  includeSensitive?: boolean;
}

export interface ContextCitation {
  path?: string;
  symbol?: string;
  startLine?: number;
  endLine?: number;
  memoryId?: string;
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
  budget: {
    maxCharacters: number;
    maxTokens: number;
    usedCharacters: number;
    estimatedTokens: number;
    truncated: boolean;
  };
  items: ContextItem[];
}

const DEFAULT_MAX_ITEMS = 30;
const DEFAULT_MAX_CHARACTERS = 12_000;
const CHARS_PER_TOKEN = 4;

function normalized(values: string[] | undefined): Set<string> {
  return new Set(
    (values ?? [])
      .map((value) => value.trim().replaceAll("\\", "/").replace(/^\.\//, "").toLowerCase())
      .filter(Boolean),
  );
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

export function buildContextPacket(
  map: RepoMap,
  graph: RepoGraph,
  events: MemoryEvent[],
  input: ContextQuery = {},
): ContextPacket {
  const query = inline(input.query ?? "");
  const queryTokens = tokens(query);
  const exactPaths = normalized(input.paths);
  const exactSymbols = normalized(input.symbols);
  const exactCommands = normalized(input.commands);
  const graphScores = scoreGraph(graph);
  const candidates: ContextItem[] = [];
  const seedIds = new Set<string>();
  const exactTiers = new Map<string, number>();

  for (const file of map.files) {
    const path = file.path.toLowerCase();
    let score = graphScores[file.id] ?? 0;
    if (exactPaths.has(path)) {
      score += 1_000;
      seedIds.add(file.id);
      exactTiers.set(file.id, 4);
    }
    if (query && query.toLowerCase() === path) {
      score += 900;
      seedIds.add(file.id);
      exactTiers.set(file.id, 4);
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
      exactTiers.set(symbol.id, 3);
    }
    if (query && query.toLowerCase() === name) {
      score += 850;
      seedIds.add(symbol.id);
      exactTiers.set(symbol.id, 3);
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
      exactTiers.set(command.id, 2);
    }
    if (query && keys.includes(query.toLowerCase())) {
      score += 825;
      seedIds.add(command.id);
      exactTiers.set(command.id, 2);
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

  for (const memory of activeMemoryEvents(events)) {
    if (!input.includeSensitive && ["confidential", "restricted"].includes(memory.sensitivity)) continue;
    let score = memory.importance * 30 + memory.confidence * 15;
    if (memory.appliesTo.some((value) => exactPaths.has(value.toLowerCase()))) {
      score += 800;
      exactTiers.set(memory.id, 1);
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
    for (const id of neighborhood(graph, seed, graphHops).nodeIds) graphNeighbors.add(id);
  }
  for (const candidate of candidates) {
    if (graphNeighbors.has(candidate.id) && !seedIds.has(candidate.id)) candidate.score += 20;
  }

  candidates.sort(
    (a, b) =>
      (exactTiers.get(b.id) ?? 0) - (exactTiers.get(a.id) ?? 0) ||
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
      memoryFingerprint: sha256(canonicalJson(events)),
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
