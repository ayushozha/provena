import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import * as z from "zod/v4";
import { getGitRoot } from "../config.js";
import {
  activeMemoryEvents,
  appendMemoryEvent,
  memoryEventToRecord,
  readMemoryLedgerSnapshot,
  readMemoryEvents,
  readRepoBrainArtifacts,
  refreshRepoBrain,
  REPO_BRAIN_PATH,
  REPO_GRAPH_PATH,
  REPO_MANIFEST_PATH,
  REPO_MAP_PATH,
} from "../brain/index.js";
import { buildContextPacket, renderContextPacketMarkdown } from "../context/index.js";
import { neighborhood, shortestPath, type RepoGraphNode } from "../graph/index.js";
import { assertNoSecretMaterial } from "../security/memory.js";
import { assertSafeRepoPath } from "../security/paths.js";

const PACKAGE_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

async function packageVersion(): Promise<string> {
  const pkg = JSON.parse(await readFile(join(PACKAGE_ROOT, "package.json"), "utf8")) as {
    version: string;
  };
  return pkg.version;
}

async function readRepoFile(repoRoot: string, relativePath: string): Promise<string> {
  const path = join(repoRoot, ...relativePath.split("/"));
  assertSafeRepoPath(repoRoot, path);
  return readFile(path, "utf8");
}

function text(value: unknown) {
  return {
    content: [{ type: "text" as const, text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }],
  };
}

function resolveNode(nodes: RepoGraphNode[], value: string): RepoGraphNode {
  const query = value.trim().toLowerCase();
  const matches = nodes.filter(
    (node) =>
      node.id.toLowerCase() === query ||
      node.path?.toLowerCase() === query ||
      node.label.toLowerCase() === query,
  );
  if (matches.length === 1) return matches[0]!;
  if (matches.length > 1) throw new Error(`ambiguous graph node: ${value}`);
  throw new Error(`graph node not found: ${value}`);
}

export async function createRepoMcpServer(
  cwd = process.cwd(),
): Promise<McpServer> {
  const repoRoot = getGitRoot(cwd);
  const server = new McpServer({ name: "provena-repo-memory", version: await packageVersion() });

  for (const resource of [
    { name: "repo-brain", uri: "provena://repo/brain", path: REPO_BRAIN_PATH, mimeType: "text/markdown" },
    { name: "repo-map", uri: "provena://repo/map", path: REPO_MAP_PATH, mimeType: "application/json" },
    { name: "repo-graph", uri: "provena://repo/graph", path: REPO_GRAPH_PATH, mimeType: "application/json" },
    { name: "repo-manifest", uri: "provena://repo/manifest", path: REPO_MANIFEST_PATH, mimeType: "application/json" },
  ]) {
    server.registerResource(
      resource.name,
      resource.uri,
      { description: `Current Provena ${resource.name}`, mimeType: resource.mimeType },
      async (uri) => {
        return {
          contents: [
            { uri: uri.href, mimeType: resource.mimeType, text: await readRepoFile(repoRoot, resource.path) },
          ],
        };
      },
    );
  }

  server.registerResource(
    "repo-memories",
    "provena://repo/memories",
    { description: "Active public and internal repo memories", mimeType: "application/json" },
    async (uri) => {
      const events = activeMemoryEvents(await readMemoryEvents(repoRoot)).filter(
        (event) => !["confidential", "restricted"].includes(event.sensitivity),
      );
      return {
        contents: [
          {
            uri: uri.href,
            mimeType: "application/json",
            text: JSON.stringify(events.map(memoryEventToRecord), null, 2),
          },
        ],
      };
    },
  );

  server.registerTool(
    "provena_context",
    {
      description: "Build a compact, cited context packet for a repository task",
      inputSchema: {
        query: z.string().max(50_000).default(""),
        paths: z.array(z.string()).default([]),
        symbols: z.array(z.string()).default([]),
        commands: z.array(z.string()).default([]),
        maxTokens: z.number().int().min(64).max(100_000).default(2_500),
        graphHops: z.number().int().min(0).max(5).default(1),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async (input) => {
      const { map, graph } = await readRepoBrainArtifacts(repoRoot);
      const packet = buildContextPacket(
        map,
        graph,
        await readMemoryLedgerSnapshot(repoRoot),
        input,
      );
      return text(renderContextPacketMarkdown(packet));
    },
  );

  server.registerTool(
    "provena_refresh",
    {
      description: "Refresh deterministic repo maps, graph, bootloader, and memory views",
      inputSchema: {},
      annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: false },
    },
    async () => {
      const result = await refreshRepoBrain(repoRoot);
      return text({
        sourceFingerprint: result.map.sourceFingerprint,
        files: result.map.files.length,
        symbols: result.map.symbols.length,
        nodes: result.graph.nodes.length,
        edges: result.graph.edges.length,
        written: result.written,
      });
    },
  );

  server.registerTool(
    "provena_remember",
    {
      description: "Append an explicit durable repo memory with provenance and citations",
      inputSchema: {
        kind: z.enum(["fact", "decision", "workflow", "mistake", "preference", "handoff", "invariant"]),
        subjectType: z.enum(["repo", "file", "symbol", "command", "test", "api", "architecture", "task"]).default("repo"),
        title: z.string().min(1).max(240),
        body: z.string().min(1).max(50_000),
        appliesTo: z.array(z.string()).default([]),
        sources: z.array(z.object({ path: z.string(), symbol: z.string().optional(), startLine: z.number().int().positive().optional(), endLine: z.number().int().positive().optional() })).default([]),
        agent: z.string().optional(),
        confidence: z.number().min(0).max(1).default(1),
        importance: z.number().min(0).max(1).default(0.5),
        sensitivity: z.enum(["public", "internal"]).default("internal"),
        supersedes: z.array(z.string()).default([]),
        tags: z.array(z.string()).default([]),
        triggers: z.array(z.string()).default([]),
      },
      annotations: { readOnlyHint: false, idempotentHint: false, openWorldHint: false },
    },
    async (input) => {
      assertNoSecretMaterial(`${input.title}\n${input.body}`);
      const event = await appendMemoryEvent(repoRoot, {
        ...input,
        provenance: {
          actor: "mcp-client",
          method: "explicit",
          ...(input.agent ? { agent: input.agent } : {}),
          command: "MCP provena_remember",
        },
        authority: "agent",
      });
      await refreshRepoBrain(repoRoot);
      return text(memoryEventToRecord(event));
    },
  );

  server.registerTool(
    "provena_graph_neighbors",
    {
      description: "Return the typed neighborhood around a repo graph node, path, or label",
      inputSchema: { node: z.string(), depth: z.number().int().min(0).max(8).default(1) },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async ({ node: value, depth }) => {
      const { graph } = await readRepoBrainArtifacts(repoRoot);
      const root = resolveNode(graph.nodes, value);
      const found = neighborhood(graph, root.id, depth);
      return text({
        root,
        nodes: graph.nodes.filter((node) => found.nodeIds.includes(node.id)),
        edges: graph.edges.filter((edge) => found.edgeIds.includes(edge.id)),
      });
    },
  );

  server.registerTool(
    "provena_graph_path",
    {
      description: "Find the shortest typed relationship path between two repo nodes",
      inputSchema: { from: z.string(), to: z.string() },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async ({ from: fromValue, to: toValue }) => {
      const { graph } = await readRepoBrainArtifacts(repoRoot);
      const from = resolveNode(graph.nodes, fromValue);
      const to = resolveNode(graph.nodes, toValue);
      const ids = shortestPath(graph, from.id, to.id);
      return text({
        from,
        to,
        path: ids?.map((id) => graph.nodes.find((node) => node.id === id)) ?? null,
      });
    },
  );
  return server;
}

export async function runRepoMcpServer(cwd = process.cwd()): Promise<void> {
  const server = await createRepoMcpServer(cwd);
  await server.connect(new StdioServerTransport());
}
