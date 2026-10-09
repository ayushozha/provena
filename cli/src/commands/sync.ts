import { performance } from "node:perf_hooks";
import {
  MEMORY_LEDGER_PATH,
  readMemoryLedgerSnapshot,
} from "../brain/index.js";
import {
  ProvenaClient,
  RepositoryMemorySyncTransportError,
  readStoreApiKey,
  scopeEnvelopeFromConfig,
  type RepositoryMemorySyncResponse,
} from "../client.js";
import { loadConfig } from "../config.js";

function hasFlag(args: string[], ...flags: string[]): boolean {
  return flags.some((flag) => args.includes(flag));
}

export function printSyncHelp(): void {
  console.log("Usage: provena sync store [options]");
  console.log("");
  console.log("Replicate the exact canonical repository ledger into the governed store.");
  console.log("");
  console.log("Options:");
  console.log("  --dry-run    Validate and fingerprint the local ledger without networking");
  console.log("  --json       Print a machine-readable result");
  console.log("  --help, -h   Show this help");
}

export async function runSyncCommand(
  args: string[],
  cwd = process.cwd(),
  fetchImpl: typeof fetch = fetch,
): Promise<number> {
  if (hasFlag(args, "--help", "-h")) {
    printSyncHelp();
    return 0;
  }
  if (args[0] !== "store") {
    printSyncHelp();
    return 1;
  }
  const unknown = args.slice(1).filter((value) => !["--dry-run", "--json"].includes(value));
  if (unknown.length > 0) {
    throw new Error(`unknown sync option: ${unknown[0]}`);
  }

  const { config, projectRoot } = loadConfig(cwd);
  if (!config.repository_id) {
    throw new Error("config.repository_id is required; run `provena init` to repair config");
  }
  const started = performance.now();
  const snapshot = await readMemoryLedgerSnapshot(projectRoot);
  const validationMs = Math.round((performance.now() - started) * 1000) / 1000;

  if (hasFlag(args, "--dry-run")) {
    const result = {
      schema_version: 1,
      repository_id: config.repository_id,
      ledger_path: MEMORY_LEDGER_PATH,
      ledger_fingerprint: snapshot.memoryFingerprint,
      ledger_bytes: snapshot.bytes,
      received_events: snapshot.events.length,
      dry_run: true,
      timings_ms: { validate: validationMs, total: validationMs },
    } as const;
    if (hasFlag(args, "--json")) console.log(JSON.stringify(result, null, 2));
    else {
      console.log(
        `Validated ${result.received_events} events (${result.ledger_bytes} bytes) in ${result.timings_ms.total.toFixed(3)} ms.`,
      );
      console.log(`ledger sha256:${result.ledger_fingerprint}`);
    }
    return 0;
  }

  const client = new ProvenaClient({
    storeUrl: config.store_url,
    apiKey: readStoreApiKey(config.store_api_key_env),
    fetchImpl,
  });
  let result: RepositoryMemorySyncResponse;
  try {
    result = await client.syncRepositoryMemoryEvents(
      config.repository_id,
      {
        schema_version: 1,
        scope: scopeEnvelopeFromConfig(config.scope),
        ledger_path: MEMORY_LEDGER_PATH,
        memory_fingerprint: snapshot.memoryFingerprint,
        ledger_bytes: snapshot.bytes,
        ledger: snapshot.rawLedger,
      },
      snapshot.events.length,
      snapshot.events.reduce((count, event) => count + event.supersedes.length, 0),
    );
  } catch (error) {
    if (error instanceof RepositoryMemorySyncTransportError) {
      throw new Error(
        `store not reachable at ${config.store_url}; run \`provena serve\` or \`provena doctor\` (${error.message})`,
        { cause: error },
      );
    }
    throw error;
  }
  printSyncResult(result, hasFlag(args, "--json"));
  return 0;
}

function printSyncResult(result: RepositoryMemorySyncResponse, json: boolean): void {
  if (json) {
    console.log(JSON.stringify(result, null, 2));
    return;
  }
  const state = result.no_op ? "already current" : "checkpoint updated";
  console.log(
    `Synced ${result.received_events} events: +${result.created_memories} memories, ` +
      `=${result.unchanged_memories} unchanged, ~${result.repaired_memories} repaired, ` +
      `-${result.suppressed_events} suppressed, ${result.status_updates} status updates; ` +
      `+${result.created_relations} relations, ~${result.repaired_relations} repaired, ` +
      `=${result.unchanged_relations} unchanged, ` +
      `-${result.suppressed_relations} suppressed ` +
      `(${state}) in ${result.duration_ms.toFixed(3)} ms.`,
  );
  console.log(`ledger sha256:${result.ledger_fingerprint}`);
}
