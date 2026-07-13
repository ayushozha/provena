import assert from "node:assert/strict";
import {
  DEFAULT_SEARCH_LIMIT,
  ProvenaClient,
  parseSearchArgs,
  formatLocation,
  formatSearchJson,
  formatSearchTable,
  spanToLineDisplay,
  truncateExcerpt,
} from "../dist/index.js";

function testParseSearchArgs() {
  assert.deepEqual(parseSearchArgs(["auth", "middleware"]), {
    query: "auth middleware",
    limit: DEFAULT_SEARCH_LIMIT,
    json: false,
    explain: false,
  });

  assert.deepEqual(parseSearchArgs(["vector", "index", "--json", "--limit", "3"]), {
    query: "vector index",
    limit: 3,
    json: true,
    explain: false,
  });

  assert.equal(parseSearchArgs(["--help"]), "help");
  assert.equal(parseSearchArgs([]), "error");
  assert.equal(parseSearchArgs(["--limit", "0"]), "error");
}

function testSpanAndLocation() {
  assert.equal(spanToLineDisplay(12, 18), "12-18");
  assert.equal(spanToLineDisplay(7, 7), "7");

  const result = {
    memory: {
      memory_id: "mem_1",
      fingerprint: "fp",
      kind: "fact",
      title: "src/auth.ts::authenticate",
      content: "export function authenticate() {}",
      entity_keys: [],
      tags: [],
      source_references: [
        {
          source_type: "file",
          source_id: "src/auth.ts",
          span_start: 42,
          span_end: 58,
          excerpt: "export function authenticate() {}",
        },
      ],
    },
    score: 0.91,
    reasons: ["fts"],
  };

  assert.equal(formatLocation(result), "src/auth.ts:42-58");
  assert.match(formatSearchTable([result]), /src\/auth\.ts:42-58/);
  assert.match(formatSearchTable([result]), /0\.910/);
}

function testTruncateAndJson() {
  const long = "alpha beta gamma delta epsilon zeta";
  assert.equal(truncateExcerpt(long, 20), "alpha beta gamma de…");

  const json = formatSearchJson({
    results: [
      {
        memory: {
          memory_id: "mem_1",
          fingerprint: "fp",
          kind: "fact",
          title: null,
          content: "hello",
          entity_keys: [],
          tags: [],
          source_references: [],
        },
        score: 0.5,
        reasons: [],
      },
    ],
  });
  assert.match(json, /"results"/);
  assert.match(json, /mem_1/);
}

async function testClientSearchRouting() {
  const calls = [];

  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), method: init?.method, body: init?.body, headers: init?.headers });
    const path = String(url);
    if (path.endsWith("/healthz")) {
      return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
    }
    if (path.endsWith("/v1/pipeline/search")) {
      return new Response(
        JSON.stringify({
          results: [
            {
              memory: {
                memory_id: "mem_pipe",
                fingerprint: "fp",
                kind: "fact",
                title: "pipe hit",
                content: "pipeline result",
                entity_keys: [],
                tags: [],
                source_references: [],
              },
              score: 0.77,
              reasons: ["pipeline"],
            },
          ],
        }),
        { status: 200 },
      );
    }
    if (path.endsWith("/v1/memories/search")) {
      return new Response(
        JSON.stringify({
          results: [
            {
              memory: {
                memory_id: "mem_store",
                fingerprint: "fp",
                kind: "fact",
                title: "store hit",
                content: "store result",
                entity_keys: [],
                tags: [],
                source_references: [],
              },
              score: 0.66,
              reasons: ["fts"],
            },
          ],
        }),
        { status: 200 },
      );
    }
    if (path.endsWith("/v1/admin/memories/search/explain")) {
      return new Response(null, { status: 404 });
    }
    return new Response("not found", { status: 404 });
  };

  const pipelineClient = new ProvenaClient({
    storeUrl: "http://127.0.0.1:18092",
    intelligenceUrl: "http://127.0.0.1:18081",
    apiKey: "pipeline-test-key",
    fetchImpl,
  });

  const pipelineResponse = await pipelineClient.search({
    query: "auth",
    scope: { tenant_id: "t", project_id: "p" },
    limit: 2,
  });
  assert.equal(pipelineResponse.results[0].memory.memory_id, "mem_pipe");
  assert.ok(
    calls.some((call) => call.url.endsWith("/v1/pipeline/search")),
    "pipeline search should be used when intelligence_url is set",
  );
  const pipelineCall = calls.find((call) => call.url.endsWith("/v1/pipeline/search"));
  assert.equal(pipelineCall.headers["X-Provena-Tenant-Id"], "t");
  assert.equal(pipelineCall.headers["X-Provena-Principal-Id"], "provena-cli");
  assert.equal(pipelineCall.headers["X-Provena-Role"], "editor");
  assert.equal(pipelineCall.headers.Authorization, "Bearer pipeline-test-key");

  const storeClient = new ProvenaClient({
    storeUrl: "http://127.0.0.1:18092",
    fetchImpl,
  });

  const storeResponse = await storeClient.search({
    query: "auth",
    scope: { tenant_id: "t", project_id: "p" },
    limit: 2,
  });
  assert.equal(storeResponse.results[0].memory.memory_id, "mem_store");
  assert.ok(
    calls.some((call) => call.url.endsWith("/v1/memories/search")),
    "store search should be used without intelligence_url",
  );

  const explain = await storeClient.explainSearch({
    query: "auth",
    scope: { tenant_id: "t", project_id: "p" },
  });
  assert.equal(explain, null);
}

testParseSearchArgs();
testSpanAndLocation();
testTruncateAndJson();
await testClientSearchRouting();

console.log("search.test: ok");
