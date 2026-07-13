import assert from "node:assert/strict";
import { appendFile, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  appendMemoryEvent,
  readMemoryLedgerSnapshot,
} from "../dist/brain/index.js";
import { ProvenaClient } from "../dist/client.js";
import { runSyncCommand } from "../dist/commands/sync.js";
import { createDefaultConfig, writeConfig } from "../dist/config.js";
import { canonicalJson } from "../dist/brain/utils.js";

const root = await mkdtemp(join(tmpdir(), "provena-repo-sync-"));
const apiKeyEnvironment = "PROVENA_API_KEY";
const previousApiKey = process.env[apiKeyEnvironment];
delete process.env[apiKeyEnvironment];
try {
  await writeFile(join(root, "package.json"), '{"name":"repo-sync"}\n', "utf8");
  const config = createDefaultConfig({ cwd: root, gitRoot: root });
  config.repository_id = "repository-sync-test";
  config.scope.tenant_id = "tenant-sync";
  config.scope.project_id = "project-sync";
  config.store_api_key_env = apiKeyEnvironment;
  writeConfig(root, config);

  await appendMemoryEvent(root, {
    id: "fact-sync-001",
    kind: "fact",
    subjectType: "file",
    title: "Canonical sync fact",
    body: "The exact repository ledger is the replication boundary.",
    structuredData: { contract: "exact-bytes" },
    sources: [{ path: "package.json", startLine: 1, endLine: 1 }],
    provenance: { actor: "sync-test", method: "observed" },
    authority: "tool",
    tags: ["replication"],
  }, { now: () => new Date("2026-07-13T10:00:00.000Z") });
  const ledgerPath = join(root, ".provena", "memory", "events.jsonl");
  await appendFile(ledgerPath, "\n", "utf8");
  const snapshot = await readMemoryLedgerSnapshot(root);

  let requestUrl = "";
  let requestBody;
  let requestHeaders;
  const response = {
    schema_version: 1,
    repository_id: config.repository_id,
    ledger_fingerprint: snapshot.memoryFingerprint,
    events_fingerprint: "a".repeat(64),
    received_events: 1,
    created_memories: 1,
    unchanged_memories: 0,
    repaired_memories: 0,
    suppressed_events: 0,
    status_updates: 0,
    created_relations: 0,
    repaired_relations: 0,
    unchanged_relations: 0,
    suppressed_relations: 0,
    checkpoint_updated: true,
    no_op: false,
    duration_ms: 1.25,
    timings_ms: { validate: 0.25, write: 0.75, relations: 0.1, total: 1.25 },
  };
  const fetchImpl = async (url, init) => {
    requestUrl = String(url);
    requestBody = JSON.parse(String(init?.body));
    requestHeaders = init?.headers;
    return new Response(JSON.stringify(response), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  const logs = [];
  const originalLog = console.log;
  console.log = (...values) => logs.push(values.join(" "));
  try {
    assert.equal(await runSyncCommand(["store", "--json"], root, fetchImpl), 0);
  } finally {
    console.log = originalLog;
  }

  assert.match(requestUrl, /\/v1\/repositories\/repository-sync-test\/memory-events\/sync$/);
  assert.equal(requestBody.schema_version, 1);
  assert.deepEqual(requestBody.scope, {
    tenant_id: "tenant-sync",
    project_id: "project-sync",
  });
  assert.equal(requestBody.ledger_path, ".provena/memory/events.jsonl");
  assert.equal(requestBody.memory_fingerprint, snapshot.memoryFingerprint);
  assert.equal(requestBody.ledger_bytes, snapshot.bytes);
  assert.equal(requestBody.ledger, snapshot.rawLedger, "the hashed snapshot must be sent without a second read");
  assert.equal(requestBody.ledger.endsWith("\n\n"), true, "blank ledger lines remain part of raw attestation");
  assert.equal(requestHeaders["X-Provena-Tenant-Id"], "tenant-sync");
  assert.equal(requestHeaders["X-Provena-Principal-Id"], "provena-cli");
  assert.equal(requestHeaders["X-Provena-Role"], "editor");
  assert.equal(requestHeaders.Authorization, undefined, "local direct mode does not invent a bearer token");
  assert.deepEqual(JSON.parse(logs.join("\n")), response);

  const apiKey = "sync-test-secret-never-print";
  process.env[apiKeyEnvironment] = apiKey;
  let authenticatedHeaders;
  const authenticatedLogs = [];
  console.log = (...values) => authenticatedLogs.push(values.join(" "));
  try {
    assert.equal(
      await runSyncCommand(
        ["store", "--json"],
        root,
        async (_url, init) => {
          authenticatedHeaders = init?.headers;
          return new Response(
            JSON.stringify({
              ...response,
              reflected_authorization: `Bearer ${apiKey}`,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        },
      ),
      0,
    );
  } finally {
    console.log = originalLog;
  }
  assert.equal(authenticatedHeaders.Authorization, `Bearer ${apiKey}`);
  assert.deepEqual(
    JSON.parse(authenticatedLogs.join("\n")),
    response,
    "JSON output contains only the attested response schema",
  );
  assert.doesNotMatch(authenticatedLogs.join("\n"), new RegExp(apiKey));
  assert.doesNotMatch(await readFile(join(root, ".provena", "config.json"), "utf8"), new RegExp(apiKey));
  await assert.rejects(
    runSyncCommand(
      ["store", "--json"],
      root,
      async () => new Response(`gateway rejected ${apiKey}`, { status: 401 }),
    ),
    (error) => {
      assert.match(error.message, /gateway rejected \[REDACTED\]/);
      assert.doesNotMatch(error.message, new RegExp(apiKey));
      return true;
    },
  );
  await assert.rejects(
    runSyncCommand(["store"], root, async () => {
      throw new TypeError(`fetch failed for ${apiKey}`);
    }),
    (error) => {
      assert.match(error.message, /fetch failed for \[REDACTED\]/);
      assert.doesNotMatch(error.message, new RegExp(apiKey));
      return true;
    },
  );
  delete process.env[apiKeyEnvironment];

  const mismatchedResponses = [
    [
      { repository_id: "repository-sync-other" },
      /repository_id does not match the request/,
    ],
    [
      { ledger_fingerprint: "b".repeat(64) },
      /ledger_fingerprint does not match the exact local ledger/,
    ],
    [
      { received_events: 2, created_memories: 1, unchanged_memories: 1 },
      /received_events does not match the validated local event count/,
    ],
    [
      { created_memories: -1, unchanged_memories: 2 },
      /created_memories must be a non-negative safe integer/,
    ],
    [
      { unchanged_memories: 1 },
      /memory projection counters must equal received_events/,
    ],
    [
      { unchanged_relations: 1 },
      /relation projection counters must equal the validated local relation count/,
    ],
    [
      { duration_ms: 2 },
      /duration_ms must equal timings_ms.total/,
    ],
    [
      {
        no_op: true,
        checkpoint_updated: false,
        created_memories: 1,
        unchanged_memories: 0,
      },
      /a no-op response cannot report projection mutations/,
    ],
  ];
  for (const [overrides, expectedError] of mismatchedResponses) {
    await assert.rejects(
      runSyncCommand(
        ["store", "--json"],
        root,
        async () => new Response(JSON.stringify({ ...response, ...overrides }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
      expectedError,
    );
  }

  const suppressionResponse = {
    ...response,
    created_memories: 0,
    unchanged_memories: 0,
    suppressed_events: 1,
    checkpoint_updated: false,
    no_op: true,
  };
  const suppressionLogs = [];
  console.log = (...values) => suppressionLogs.push(values.join(" "));
  try {
    assert.equal(
      await runSyncCommand(
        ["store", "--json"],
        root,
        async () => new Response(JSON.stringify(suppressionResponse), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
      0,
    );
  } finally {
    console.log = originalLog;
  }
  assert.deepEqual(JSON.parse(suppressionLogs.join("\n")), suppressionResponse);

  const humanLogs = [];
  console.log = (...values) => humanLogs.push(values.join(" "));
  try {
    assert.equal(await runSyncCommand(["store"], root, fetchImpl), 0);
  } finally {
    console.log = originalLog;
  }
  assert.match(humanLogs[0], /~0 repaired, -0 suppressed/);
  assert.match(humanLogs[0], /relations, ~0 repaired, =0 unchanged, -0 suppressed/);

  let dryRunFetches = 0;
  const dryRunLogs = [];
  console.log = (...values) => dryRunLogs.push(values.join(" "));
  try {
    assert.equal(
      await runSyncCommand(["store", "--dry-run", "--json"], root, async () => {
        dryRunFetches += 1;
        throw new Error("dry run must not use the network");
      }),
      0,
    );
  } finally {
    console.log = originalLog;
  }
  const dryRun = JSON.parse(dryRunLogs.join("\n"));
  assert.equal(dryRun.dry_run, true);
  assert.equal(dryRun.ledger_fingerprint, snapshot.memoryFingerprint);
  assert.equal(dryRun.ledger_bytes, snapshot.bytes);
  assert.equal(dryRunFetches, 0);

  await assert.rejects(
    runSyncCommand(["store"], root, async () => new Response("invalid ledger", { status: 422 })),
    /failed \(422\): invalid ledger/,
  );
  await assert.rejects(
    runSyncCommand(["store"], root, async () => {
      throw new TypeError("fetch failed");
    }),
    /store not reachable at http:\/\/127\.0\.0\.1:18092; run `provena serve` or `provena doctor`/,
  );

  const validLedger = await readFile(ledgerPath);
  const invalidLedger = Buffer.from(validLedger);
  const bodyOffset = invalidLedger.indexOf(Buffer.from("The exact repository ledger", "utf8"));
  assert.notEqual(bodyOffset, -1, "fixture contains a body byte to corrupt");
  invalidLedger[bodyOffset] = 0xff;
  await writeFile(ledgerPath, invalidLedger);
  await assert.rejects(
    readMemoryLedgerSnapshot(root),
    /\.provena\/memory\/events\.jsonl must contain valid UTF-8/,
  );
  await writeFile(ledgerPath, validLedger);

  const secretRecord = JSON.parse(validLedger.toString("utf8").trim());
  secretRecord.body = `accidental credential ghp_${"A".repeat(24)}`;
  await writeFile(ledgerPath, `${JSON.stringify(secretRecord)}\n`, "utf8");
  await assert.rejects(
    readMemoryLedgerSnapshot(root),
    /memory appears to contain a credential or private key/,
  );
  await writeFile(ledgerPath, validLedger);

  const numericRecord = JSON.parse(validLedger.toString("utf8").trim());
  numericRecord.structured_data = { rate: 1e-7, 2: "two", 10: "ten" };
  const numericLedger = canonicalJson(numericRecord);
  assert.match(numericLedger, /"rate":1e-7/);
  assert.match(numericLedger, /"10":"ten","2":"two","rate":1e-7/);
  await writeFile(ledgerPath, numericLedger, "utf8");
  assert.equal((await readMemoryLedgerSnapshot(root)).events.length, 1);

  const duplicateKeyLedger = numericLedger.replace(
    `"body":${JSON.stringify(numericRecord.body)}`,
    `"body":"accidental credential ghp_${"A".repeat(24)}","body":${JSON.stringify(numericRecord.body)}`,
  );
  await writeFile(ledgerPath, duplicateKeyLedger, "utf8");
  await assert.rejects(
    readMemoryLedgerSnapshot(root),
    /canonical RFC 8785 JSON without duplicate keys/,
  );

  await writeFile(
    ledgerPath,
    numericLedger.replace('"rate":1e-7', '"rate":9007199254740992'),
    "utf8",
  );
  await assert.rejects(readMemoryLedgerSnapshot(root), /safe-integer range/);

  await writeFile(ledgerPath, numericLedger.replace('"rate":1e-7', '"rate":-0'), "utf8");
  await assert.rejects(readMemoryLedgerSnapshot(root), /negative-zero|canonical RFC 8785 JSON/);

  const unpairedSurrogate = numericLedger.replace(
    `"body":${JSON.stringify(numericRecord.body)}`,
    '"body":"\\ud800"',
  );
  await writeFile(ledgerPath, unpairedSurrogate, "utf8");
  await assert.rejects(readMemoryLedgerSnapshot(root), /unpaired UTF-16 surrogate/);

  const nulBody = numericLedger.replace(
    `"body":${JSON.stringify(numericRecord.body)}`,
    '"body":"\\u0000"',
  );
  await writeFile(ledgerPath, nulBody, "utf8");
  await assert.rejects(readMemoryLedgerSnapshot(root), /contains NUL text/);
  await writeFile(ledgerPath, validLedger);

  const restrictedRecord = JSON.parse(validLedger.toString("utf8").trim());
  restrictedRecord.sensitivity = "restricted";
  await writeFile(ledgerPath, `${JSON.stringify(restrictedRecord)}\n`, "utf8");
  await assert.rejects(
    runSyncCommand(["store", "--dry-run"], root),
    /restricted memory cannot be read from the Git-tracked repo ledger/,
  );
  await writeFile(ledgerPath, validLedger);

  delete config.repository_id;
  writeConfig(root, config);
  await assert.rejects(
    runSyncCommand(["store", "--dry-run"], root),
    /run `provena init` to repair config/,
  );

  const memoryWriteRequests = [];
  const client = new ProvenaClient({
    storeUrl: "http://127.0.0.1:18092",
    intelligenceUrl: "http://127.0.0.1:18081",
    apiKey: "pipeline-write-key",
    fetchImpl: async (url, init) => {
      memoryWriteRequests.push({ url: String(url), headers: init?.headers });
      return new Response(JSON.stringify({ created: true, memory: { memory_id: "memory-test" } }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  });
  const generatedMemory = {
    kind: "artifact",
    scope: { tenant_id: "tenant-sync", project_id: "project-sync" },
    content: "Generated source memory",
  };
  await client.createMemory({
    ...generatedMemory,
    metadata: { provena_generated_fingerprint: "not-a-sha256" },
  });
  await client.createMemory({
    ...generatedMemory,
    metadata: { provena_generated_fingerprint: "c".repeat(64) },
  });
  assert.deepEqual(memoryWriteRequests.map(({ url }) => url), [
    "http://127.0.0.1:18081/v1/pipeline/write",
    "http://127.0.0.1:18092/v1/memories",
  ]);
  assert.equal(memoryWriteRequests[0].headers["X-Provena-Tenant-Id"], "tenant-sync");
  assert.equal(memoryWriteRequests[0].headers["X-Provena-Principal-Id"], "provena-cli");
  assert.equal(memoryWriteRequests[0].headers["X-Provena-Role"], "editor");
  assert.equal(memoryWriteRequests[0].headers.Authorization, "Bearer pipeline-write-key");
  console.log("repo-memory-sync.test: ok");
} finally {
  if (previousApiKey === undefined) delete process.env[apiKeyEnvironment];
  else process.env[apiKeyEnvironment] = previousApiKey;
  await rm(root, { recursive: true, force: true });
}
