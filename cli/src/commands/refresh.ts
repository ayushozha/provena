import { getGitRoot, loadConfig } from "../config.js";
import { refreshRepoBrain } from "../brain/index.js";

function reconciliationRecord(result: Awaited<ReturnType<typeof refreshRepoBrain>>) {
  const value = result.reconciliation;
  return {
    candidates: value.candidates,
    added: value.added,
    noops: value.noops,
    superseded: value.superseded,
    retracted: value.retracted,
    deferred: value.deferred,
    conflicts: value.conflicts,
    duration_ms: value.durationMs,
  };
}

export function printRefreshHelp(): void {
  console.log("Usage: provena refresh [--json] [--quiet]");
  console.log("");
  console.log("Rebuild the deterministic repo brain, map, graph, and memory views.");
}

export async function runRefreshCommand(
  args: string[],
  cwd = process.cwd(),
): Promise<number> {
  if (args.includes("--help") || args.includes("-h")) {
    printRefreshHelp();
    return 0;
  }
  const repoRoot = getGitRoot(cwd);
  loadConfig(repoRoot);
  const result = await refreshRepoBrain(repoRoot, {
    warn: (message) => console.error(`provena refresh: ${message}`),
  });
  if (args.includes("--json")) {
    console.log(
      JSON.stringify(
        {
          sourceFingerprint: result.map.sourceFingerprint,
          files: result.map.files.length,
          symbols: result.map.symbols.length,
          nodes: result.graph.nodes.length,
          edges: result.graph.edges.length,
          reconciliation: reconciliationRecord(result),
          written: result.written,
        },
        null,
        2,
      ),
    );
  } else if (!args.includes("--quiet")) {
    console.log(
      `Provena refreshed ${result.map.files.length} files, ${result.map.symbols.length} symbols, ` +
        `${result.graph.nodes.length} graph nodes.`,
    );
    console.log(
      result.written.length
        ? `  updated: ${result.written.join(", ")}`
        : "  brain already current",
    );
    const memory = reconciliationRecord(result);
    console.log(
      `  memory: ${memory.candidates} candidates; ${memory.added} added, ` +
        `${memory.noops} no-op, ${memory.superseded} superseded, ` +
        `${memory.retracted} retracted, ${memory.deferred} deferred, ` +
        `${memory.conflicts} conflicts (${memory.duration_ms.toFixed(3)} ms)`,
    );
  }
  return 0;
}
