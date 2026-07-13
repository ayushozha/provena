import {
  activeMemoryEventsAt,
  assertMemoryLedgerSnapshotAttestation,
  type MemoryLedgerSnapshot,
} from "../brain/events.js";
import type { RepoMap } from "../brain/types.js";
import {
  buildContextPacket,
  type ContextPacket,
  type ContextQuery,
} from "../context/packet.js";
import type { RepoGraph } from "../graph/types.js";
import { assertMaintenancePlanAttestation } from "./plan.js";
import type { MaintenancePlan, MaintenanceTask } from "./types.js";

const TASK_ERROR =
  "maintenance task is unavailable for the attested repository generation";
const TASK_ID_PATTERN = /^maintenance-task:[a-f0-9]{20}$/;

export interface MaintenanceContextOptions {
  maxItems?: number;
  maxCharacters?: number;
  maxTokens?: number;
  graphHops?: number;
  includeSensitive?: boolean;
  memoryAsOf?: string;
}

function findTask(plan: MaintenancePlan, taskId: string): MaintenanceTask {
  if (
    typeof taskId !== "string" ||
    !TASK_ID_PATTERN.test(taskId) ||
    !Array.isArray(plan.tasks)
  ) {
    throw new Error(TASK_ERROR);
  }
  const matches = plan.tasks.filter((task) => task?.id === taskId);
  if (matches.length !== 1) throw new Error(TASK_ERROR);
  return matches[0]!;
}

export function maintenanceTaskQuery(
  plan: MaintenancePlan,
  taskId: string,
  options: MaintenanceContextOptions = {},
): ContextQuery {
  const task = findTask(plan, taskId);
  return {
    query: `Review ${task.kind} maintenance proposal`,
    paths: [...task.paths],
    memoryIds: [...task.memoryIds],
    ...(options.maxItems === undefined ? {} : { maxItems: options.maxItems }),
    ...(options.maxCharacters === undefined
      ? {}
      : { maxCharacters: options.maxCharacters }),
    ...(options.maxTokens === undefined ? {} : { maxTokens: options.maxTokens }),
    ...(options.graphHops === undefined ? {} : { graphHops: options.graphHops }),
    ...(options.includeSensitive === undefined
      ? {}
      : { includeSensitive: options.includeSensitive }),
    ...(options.memoryAsOf === undefined ? {} : { memoryAsOf: options.memoryAsOf }),
  };
}

export function compileMaintenanceTaskContext(
  map: RepoMap,
  graph: RepoGraph,
  memory: MemoryLedgerSnapshot,
  plan: MaintenancePlan,
  taskId: string,
  options: MaintenanceContextOptions = {},
): ContextPacket {
  try {
    assertMemoryLedgerSnapshotAttestation(memory);
    assertMaintenancePlanAttestation(plan, map, memory);
    const query = maintenanceTaskQuery(plan, taskId, options);
    const available = new Set(
      activeMemoryEventsAt(memory.events, options.memoryAsOf)
        .filter((event) =>
          options.includeSensitive ||
          !["confidential", "restricted"].includes(event.sensitivity)
        )
        .map((event) => event.id),
    );
    if ((query.memoryIds ?? []).some((id) => !available.has(id))) {
      throw new Error();
    }
    const packet = buildContextPacket(map, graph, memory, query);
    const emittedMemoryIds = new Set(
      packet.items
        .filter((item) => item.type === "memory")
        .map((item) => item.id),
    );
    if ((query.memoryIds ?? []).some((id) => !emittedMemoryIds.has(id))) {
      throw new Error("context budget is too small for the selected maintenance memories");
    }
    return packet;
  } catch (error) {
    if (
      error instanceof Error &&
      (error.message.includes("maxItems") ||
        error.message.includes("maxCharacters") ||
        error.message.includes("maxTokens") ||
        error.message.includes("graphHops") ||
        error.message.includes("memoryAsOf") ||
        error.message.includes("context budget"))
    ) {
      throw error;
    }
    throw new Error(TASK_ERROR);
  }
}
