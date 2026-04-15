use provena_sdk::{extract_json_string, ProvenaClient};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let base_url = std::env::var("PROVENA_BASE_URL")?;
    let client = ProvenaClient::new(&base_url)?;

    let health = client.health()?;
    if !health.contains("\"status\":\"ok\"") && !health.contains("\"status\": \"ok\"") {
        return Err("health check failed".into());
    }

    let created = client.create_memory(
        r#"{
            "kind": "artifact",
            "scope": {
                "tenant_id": "tenant-e2e",
                "workspace_id": "ws-e2e",
                "user_id": "rust-sdk"
            },
            "title": "Rust SDK smoke memory",
            "content": "Rust SDK can write and search Provena memories.",
            "tags": ["sdk", "rust"],
            "entity_keys": ["smoke"]
        }"#,
    )?;

    let memory_id = extract_json_string(&created, "memory_id").ok_or("missing memory id")?;

    let results = client.search_memories(
        r#"{
            "query": "Rust SDK smoke",
            "scope": {
                "tenant_id": "tenant-e2e",
                "workspace_id": "ws-e2e",
                "user_id": "rust-sdk"
            },
            "limit": 3
        }"#,
    )?;

    if !results.contains(&memory_id) {
        return Err("search did not return created memory".into());
    }

    println!("rust-sdk-smoke: ok");
    Ok(())
}
