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
  readRepoBrainArtifacts,
  refreshRepoBrain,
  REPO_BRAIN_PATH,
  REPO_GRAPH_PATH,
  REPO_MANIFEST_PATH,
  REPO_MAP_PATH,
} from "../brain/index.js";
import { buildContextPacket, renderContextPacketMarkdown } from "../context/index.js";
import {
  induceRepoGraphAt,
  neighborhood,
  shortestPath,
  type RepoGraph,
  type RepoGraphNode,
} from "../graph/index.js";
import {
  compileMaintenanceTaskContext,
  maintenancePlanView,
} from "../maintenance/index.js";
import { assertNoSecretMaterial } from "../security/memory.js";
import {
  learnProcedure, recordProcedureOutcome, recallProcedures,
  learnProcedureInputSchema, procedureOutcomeInputSchema,
} from "../procedures/index.js";

const PACKAGE_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

async function packageVersion(): Promise<string> {
  const pkg = JSON.parse(await readFile(join(PACKAGE_ROOT, "package.json"), "utf8")) as {
    version: string;
  };
  return pkg.version;
}

function text(value: unknown) {
  return {
    content: [{ type: "text" as const, text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }],
  };
}

export interface RepoMcpServerOptions {
  sanitizeErrors?: boolean;
  onOperationStart?: () => void;
  onOperationEnd?: () => void;
}

async function boundedMcpOperation<T>(
  options: RepoMcpServerOptions,
  operation: () => Promise<T>,
): Promise<T> {
  options.onOperationStart?.();
  try {
    return await operation();
  } catch (error) {
    if (!options.sanitizeErrors) throw error;
    throw new Error("Repository memory request failed");
  } finally {
    options.onOperationEnd?.();
  }
}

function resolveNode(nodes: RepoGraphNode[], value: string): RepoGraphNode {
  const query = value.trim().toLowerCase();
  const tiers = [
    nodes.filter((node) => node.id.toLowerCase() === query),
    nodes.filter((node) =>
      node.type === "memory" && typeof node.metadata.eventId === "string" &&
      node.metadata.eventId.toLowerCase() === query,
    ),
    nodes.filter((node) => node.type !== "symbol" && node.path?.toLowerCase() === query),
    nodes.filter((node) => node.label.toLowerCase() === query),
  ];
  for (const matches of tiers) {
    if (matches.length === 1) return matches[0]!;
    if (matches.length > 1) throw new Error("graph node is ambiguous");
  }
  throw new Error("graph node not found");
}

function graphQueryMetadata(graph: RepoGraph, memoryAsOf?: string): Record<string, unknown> {
  return {
    sourceFingerprint: graph.sourceFingerprint,
    ...(graph.schemaVersion === 2
      ? {
          memoryFingerprint: graph.memoryFingerprint,
          projectionFingerprint: graph.projectionFingerprint,
          timeSemantics: graph.timeSemantics,
        }
      : {}),
    repositoryTopology: "current",
    ...(memoryAsOf ? { memoryAsOf } : {}),
  };
}

export async function createRepoMcpServer(
  cwd = process.cwd(),
  options: RepoMcpServerOptions = {},
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
      async (uri) => boundedMcpOperation(options, async () => {
        const stored = await readRepoBrainArtifacts(repoRoot);
        const resourceText = stored.artifactContents[resource.path];
        if (resourceText === undefined) throw new Error("repository memory resource is unavailable");
        return {
          contents: [
            { uri: uri.href, mimeType: resource.mimeType, text: resourceText },
          ],
        };
      }),
    );
  }

  server.registerResource(
    "repo-memories",
    "provena://repo/memories",
    { description: "Active public and internal repo memories", mimeType: "application/json" },
    async (uri) => boundedMcpOperation(options, async () => {
      const stored = await readRepoBrainArtifacts(repoRoot);
      const events = activeMemoryEvents(stored.memory.events).filter(
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
    }),
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
        memoryAsOf: z.string().optional(),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async (input) => boundedMcpOperation(options, async () => {
      const { map, graph, memory } = await readRepoBrainArtifacts(repoRoot);
      const packet = buildContextPacket(
        map,
        graph,
        memory,
        input,
      );
      return {
        content: [
          { type: "text" as const, text: renderContextPacketMarkdown(packet) },
          {
            type: "text" as const,
            text: JSON.stringify({
              sourceFingerprint: packet.sourceFingerprint,
              memoryFingerprint: packet.memoryFingerprint,
              repositoryTopology: packet.repositoryTopology,
              ...(packet.memoryAsOf ? { memoryAsOf: packet.memoryAsOf } : {}),
            }, null, 2),
          },
        ],
      };
    }),
  );

  server.registerTool(
    "provena_maintenance_plan",
    {
      description: "List a bounded view of deterministic repository-memory review proposals",
      inputSchema: {
        limit: z.number().int().min(1).max(256).default(32),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async ({ limit }) => boundedMcpOperation(options, async () => {
      const { maintenancePlan } = await readRepoBrainArtifacts(repoRoot);
      return text(maintenancePlanView(maintenancePlan, limit));
    }),
  );

  server.registerTool(
    "provena_maintenance_context",
    {
      description: "Compile a cited, bounded context packet for one maintenance proposal",
      inputSchema: {
        taskId: z.string().regex(/^maintenance-task:[a-f0-9]{20}$/),
        maxTokens: z.number().int().min(64).max(100_000).default(1_500),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async ({ taskId, maxTokens }) => boundedMcpOperation(options, async () => {
      const { map, graph, memory, maintenancePlan } = await readRepoBrainArtifacts(repoRoot);
      const packet = compileMaintenanceTaskContext(
        map,
        graph,
        memory,
        maintenancePlan,
        taskId,
        { maxTokens },
      );
      return {
        content: [
          { type: "text" as const, text: renderContextPacketMarkdown(packet) },
          {
            type: "text" as const,
            text: JSON.stringify({
              taskId,
              planFingerprint: maintenancePlan.planFingerprint,
              sourceFingerprint: packet.sourceFingerprint,
              memoryFingerprint: packet.memoryFingerprint,
              repositoryTopology: packet.repositoryTopology,
            }, null, 2),
          },
        ],
      };
    }),
  );

  server.registerTool(
    "provena_refresh",
    {
      description: "Refresh deterministic repo maps, graph, bootloader, and memory views",
      inputSchema: {},
      annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: false },
    },
    async () => boundedMcpOperation(options, async () => {
      const result = await refreshRepoBrain(repoRoot);
      return text({
        sourceFingerprint: result.map.sourceFingerprint,
        files: result.map.files.length,
        symbols: result.map.symbols.length,
        nodes: result.graph.nodes.length,
        edges: result.graph.edges.length,
        reconciliation: {
          candidates: result.reconciliation.candidates,
          added: result.reconciliation.added,
          noops: result.reconciliation.noops,
          superseded: result.reconciliation.superseded,
          retracted: result.reconciliation.retracted,
          deferred: result.reconciliation.deferred,
          conflicts: result.reconciliation.conflicts,
          duration_ms: result.reconciliation.durationMs,
        },
        written: result.written,
      });
    }),
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
    async (input) => boundedMcpOperation(options, async () => {
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
    }),
  );

  server.registerTool("provena_procedure_learn", {
    description: "Capture a candidate procedure from structured tool steps and caller-reported evidence. Human review is required before reuse.",
    inputSchema: learnProcedureInputSchema.omit({ actor: true }).shape,
    annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: false },
  }, async (input) => boundedMcpOperation(options, async () => {
    const result = await learnProcedure(repoRoot, { ...input, actor: "mcp-client" });
    await refreshRepoBrain(repoRoot);
    return text({ duplicate: result.duplicate, event: memoryEventToRecord(result.event) });
  }));

  server.registerTool("provena_procedure_outcome", {
    description: "Record a caller-reported procedure outcome with goal-verification receipts. Tool exit status alone is not goal verification.",
    inputSchema: procedureOutcomeInputSchema.omit({ actor: true }).shape,
    annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: false },
  }, async (input) => boundedMcpOperation(options, async () => {
    const result = await recordProcedureOutcome(repoRoot, { ...input, actor: "mcp-client" });
    await refreshRepoBrain(repoRoot);
    return text({ duplicate: result.duplicate, event: memoryEventToRecord(result.event) });
  }));

  server.registerTool("provena_procedure_recall", {
    description: "Recall relevant approved procedures with exact version citations, source freshness and outcome evidence. Stored steps do not authorize tool execution.",
    inputSchema: {
      query: z.string().max(2_048).default(""),
      paths: z.array(z.string()).max(32).default([]),
      procedureIds: z.array(z.string()).max(32).default([]),
      availableTools: z.array(z.string()).max(128).optional(),
      maxTokens: z.number().int().min(128).max(25_000).default(1_500),
      maxItems: z.number().int().min(1).max(32).default(8),
      includeReview: z.boolean().default(false),
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  }, async ({ query, ...input }) => boundedMcpOperation(options, async () => {
    const { map, graph, memory } = await readRepoBrainArtifacts(repoRoot);
    return text(JSON.stringify(await recallProcedures(repoRoot, query, { ...input, snapshot: memory, map, graph })));
  }));

  server.registerTool(
    "provena_graph_neighbors",
    {
      description: "Return the typed neighborhood around a repo graph node, path, or label",
      inputSchema: {
        node: z.string(),
        depth: z.number().int().min(0).max(8).default(1),
        memoryAsOf: z.string().optional(),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async ({ node: value, depth, memoryAsOf }) => boundedMcpOperation(options, async () => {
      const stored = await readRepoBrainArtifacts(repoRoot);
      const graph = induceRepoGraphAt(stored.graph, memoryAsOf);
      const root = resolveNode(graph.nodes, value);
      const found = neighborhood(graph, root.id, depth);
      return text({
        ...graphQueryMetadata(graph, memoryAsOf),
        root,
        nodes: graph.nodes.filter((node) => found.nodeIds.includes(node.id)),
        edges: graph.edges.filter((edge) => found.edgeIds.includes(edge.id)),
      });
    }),
  );

  server.registerTool(
    "provena_graph_path",
    {
      description: "Find the shortest typed relationship path between two repo nodes",
      inputSchema: {
        from: z.string(),
        to: z.string(),
        memoryAsOf: z.string().optional(),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async ({ from: fromValue, to: toValue, memoryAsOf }) => boundedMcpOperation(options, async () => {
      const stored = await readRepoBrainArtifacts(repoRoot);
      const graph = induceRepoGraphAt(stored.graph, memoryAsOf);
      const from = resolveNode(graph.nodes, fromValue);
      const to = resolveNode(graph.nodes, toValue);
      const ids = shortestPath(graph, from.id, to.id);
      return text({
        ...graphQueryMetadata(graph, memoryAsOf),
        from,
        to,
        path: ids?.map((id) => graph.nodes.find((node) => node.id === id)) ?? null,
      });
    }),
  );
  return server;
}

export async function runRepoMcpServer(cwd = process.cwd()): Promise<void> {
  let activeOperations = 0;
  let operationEnded: (() => void) | undefined;
  const server = await createRepoMcpServer(cwd, {
    onOperationStart: () => { activeOperations += 1; },
    onOperationEnd: () => {
      activeOperations -= 1;
      operationEnded?.();
    },
  });
  let finish!: () => void;
  let fail!: (error: unknown) => void;
  const finished = new Promise<void>((resolve, reject) => {
    finish = resolve;
    fail = reject;
  });
  let closing = false;
  let streamError: Error | undefined;
  let signalReceived = false;
  let stopFlushing: (() => void) | undefined;
  const shutdown = () => {
    if (closing) return;
    closing = true;
    process.stdin.pause();
    void (async () => {
      // The SDK dispatches already-parsed requests through promise microtasks.
      await new Promise<void>((resolve) => setImmediate(resolve));
      while (activeOperations > 0) {
        await new Promise<void>((resolve) => { operationEnded = resolve; });
      }
      // Let completed handlers construct their responses, then flush the pipe.
      await new Promise<void>((resolve) => setImmediate(resolve));
      if (!process.stdout.destroyed && !process.stdout.errored && !signalReceived) {
        await new Promise<void>((resolve, reject) => {
          let settled = false;
          const complete = (error?: Error | null) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            stopFlushing = undefined;
            if (error) reject(error);
            else resolve();
          };
          // A disconnected client may keep stdout open without consuming it.
          const timer = setTimeout(complete, 2_000);
          stopFlushing = () => complete();
          process.stdout.write("", complete);
        });
      }
      await server.close();
      if (streamError) throw streamError;
    })().then(finish, fail);
  };
  const onStreamError = (error: Error) => {
    streamError ??= error;
    shutdown();
  };
  const onSignal = () => {
    signalReceived = true;
    stopFlushing?.();
    shutdown();
  };
  server.server.onclose = shutdown;
  process.stdin.once("end", shutdown);
  process.stdin.once("close", shutdown);
  process.stdin.once("error", onStreamError);
  process.stdout.on("error", onStreamError);
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);
  try {
    await server.connect(new StdioServerTransport());
    if (process.stdin.readableEnded || process.stdin.destroyed) shutdown();
    await finished;
  } finally {
    process.stdin.off("end", shutdown);
    process.stdin.off("close", shutdown);
    process.stdin.off("error", onStreamError);
    process.off("SIGINT", onSignal);
    process.off("SIGTERM", onSignal);
    await server.close();
    if (process.stdout.errored && !process.stdout.closed) {
      const outputClosed = new Promise<void>((resolve) => process.stdout.once("close", resolve));
      process.stdout.destroy();
      await outputClosed;
    }
    process.stdout.off("error", onStreamError);
  }
}
