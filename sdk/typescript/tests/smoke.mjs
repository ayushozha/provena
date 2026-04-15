import { ProvenaClient } from "../dist/index.js";

async function main() {
  const baseUrl = process.env.PROVENA_BASE_URL;
  if (!baseUrl) {
    throw new Error("PROVENA_BASE_URL is required");
  }

  const client = new ProvenaClient(baseUrl);
  const health = await client.health();
  if (health.status !== "ok") {
    throw new Error("Provena health check failed");
  }

  const created = await client.createMemory({
    kind: "artifact",
    scope: {
      tenant_id: "tenant-e2e",
      workspace_id: "ws-e2e",
      user_id: "typescript-sdk",
    },
    title: "TypeScript SDK smoke memory",
    content: "TypeScript SDK can write and search Provena memories.",
    tags: ["sdk", "typescript"],
    entity_keys: ["smoke"],
  });

  const found = await client.searchMemories({
    query: "TypeScript SDK smoke",
    scope: {
      tenant_id: "tenant-e2e",
      workspace_id: "ws-e2e",
      user_id: "typescript-sdk",
    },
    limit: 3,
  });

  if (!found.results.some((result) => result.memory.memory_id === created.memory.memory_id)) {
    throw new Error("TypeScript SDK search did not return created memory");
  }

  console.log("typescript-sdk-smoke: ok");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
