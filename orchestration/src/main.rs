use std::env;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::{
    extract::State,
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};

use provena_orchestration::{
    cold_start::{CachedOverview, ColdStartPrimer},
    context_budget::{self, BudgetAllocation, MemoryCandidate},
    prompt_assembly::{self, AssembledPrompt, CitationBlock, MemoryBlock},
    token_counter,
    trigger_index::{TriggerHit, TriggerIndex},
};

struct AppState {
    trigger_index: TriggerIndex,
    cold_start: ColdStartPrimer,
}

#[derive(Debug, Clone, Deserialize, Default)]
struct ScopeRef {
    tenant_id: Option<String>,
    project_id: Option<String>,
}

#[derive(Debug, Deserialize, Default)]
struct TriggerLookupReq {
    user_message: Option<String>,
    query: Option<String>,
    tenant_id: Option<String>,
    scope: Option<ScopeRef>,
}

#[derive(Serialize)]
struct TriggerLookupResp {
    hits: Vec<TriggerHit>,
}

#[derive(Debug, Deserialize)]
struct TriggerIndexReq {
    memory_id: String,
    phrases: Vec<String>,
    tenant_id: String,
}

#[derive(Debug, Deserialize)]
struct TriggerRemoveReq {
    memory_id: String,
}

#[derive(Debug, Deserialize, Default)]
struct BudgetReq {
    #[serde(default)]
    candidates: Vec<MemoryCandidate>,
    #[serde(default)]
    memories: Vec<CompatBudgetMemory>,
    total_budget: Option<u32>,
    max_tokens: Option<u32>,
    system_tokens: Option<u32>,
    user_tokens: Option<u32>,
}

#[derive(Debug, Clone, Deserialize, Default)]
struct CompatBudgetMemory {
    memory_id: String,
    content: Option<String>,
    score: Option<f64>,
    importance: Option<f64>,
    token_count: Option<u32>,
}

#[derive(Deserialize)]
struct AssembleReq {
    system_prompt: String,
    overview: String,
    memories: Vec<MemoryBlock>,
    user_message: String,
    citations: Vec<CitationBlock>,
}

#[derive(Deserialize)]
struct TokenCountReq {
    text: String,
}

#[derive(Serialize)]
struct TokenCountResp {
    tokens: u32,
    tokens_fast: u32,
}

#[derive(Deserialize)]
struct PreflightReq {
    tenant_id: String,
    project_id: String,
    user_message: String,
    system_prompt: String,
    candidates: Vec<MemoryCandidate>,
    memories: Vec<MemoryBlock>,
    citations: Vec<CitationBlock>,
    total_budget: u32,
}

#[derive(Serialize)]
struct PreflightResp {
    trigger_hits: Vec<TriggerHit>,
    overview: CachedOverview,
    budget: BudgetAllocation,
    prompt: AssembledPrompt,
}

#[derive(Deserialize)]
struct OverviewCacheReq {
    tenant_id: String,
    project_id: String,
    summary: String,
    #[serde(default)]
    key_entities: Vec<String>,
    #[serde(default)]
    recent_decisions: Vec<String>,
    active_memory_count: u64,
    generated_at_epoch: u64,
    ttl_seconds: Option<u64>,
}

#[derive(Deserialize, Default)]
struct OverviewRefreshReq {
    scope: Option<ScopeRef>,
    tenant_id: Option<String>,
    project_id: Option<String>,
}

async fn healthz() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok" }))
}

async fn handle_trigger_lookup(
    State(state): State<Arc<AppState>>,
    Json(req): Json<TriggerLookupReq>,
) -> Json<TriggerLookupResp> {
    let tenant_id = req
        .tenant_id
        .or_else(|| req.scope.and_then(|scope| scope.tenant_id))
        .unwrap_or_default();
    let user_message = req
        .user_message
        .or(req.query)
        .unwrap_or_default();
    let hits = state.trigger_index.lookup(&user_message, &tenant_id);
    Json(TriggerLookupResp { hits })
}

async fn handle_trigger_index(
    State(state): State<Arc<AppState>>,
    Json(req): Json<TriggerIndexReq>,
) -> StatusCode {
    state
        .trigger_index
        .index(&req.memory_id, &req.phrases, &req.tenant_id);
    StatusCode::NO_CONTENT
}

async fn handle_trigger_remove(
    State(state): State<Arc<AppState>>,
    Json(req): Json<TriggerRemoveReq>,
) -> StatusCode {
    state.trigger_index.remove(&req.memory_id);
    StatusCode::NO_CONTENT
}

async fn handle_budget(Json(req): Json<BudgetReq>) -> Json<BudgetAllocation> {
    let candidates = if req.candidates.is_empty() {
        req.memories
            .into_iter()
            .map(|memory| MemoryCandidate {
                memory_id: memory.memory_id,
                token_count: memory
                    .token_count
                    .unwrap_or_else(|| (memory.content.unwrap_or_default().len() / 4).max(1) as u32),
                score: memory.score.unwrap_or(0.0),
                importance: memory.importance.unwrap_or(0.5),
            })
            .collect::<Vec<_>>()
    } else {
        req.candidates
    };
    let total_budget = req.total_budget.or(req.max_tokens).unwrap_or(0);
    let system_tokens = req.system_tokens.unwrap_or(0);
    let user_tokens = req.user_tokens.unwrap_or(0);
    Json(context_budget::allocate(
        &candidates,
        total_budget,
        system_tokens,
        user_tokens,
    ))
}

async fn handle_assemble(Json(req): Json<AssembleReq>) -> Json<AssembledPrompt> {
    Json(prompt_assembly::assemble(
        &req.system_prompt,
        &req.overview,
        &req.memories,
        &req.user_message,
        &req.citations,
    ))
}

async fn handle_token_count(Json(req): Json<TokenCountReq>) -> Json<TokenCountResp> {
    Json(TokenCountResp {
        tokens: token_counter::count_tokens(&req.text),
        tokens_fast: token_counter::count_tokens_fast(&req.text),
    })
}

async fn handle_preflight(
    State(state): State<Arc<AppState>>,
    Json(req): Json<PreflightReq>,
) -> Json<PreflightResp> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();

    let trigger_hits = state
        .trigger_index
        .lookup(&req.user_message, &req.tenant_id);
    let overview = state
        .cold_start
        .get_or_default(&req.tenant_id, &req.project_id, now);

    let system_tokens = token_counter::count_tokens(&req.system_prompt);
    let user_tokens = token_counter::count_tokens(&req.user_message);
    let budget = context_budget::allocate(
        &req.candidates,
        req.total_budget,
        system_tokens,
        user_tokens,
    );

    let selected_memories: Vec<MemoryBlock> = req
        .memories
        .into_iter()
        .filter(|memory| budget.selected_memory_ids.contains(&memory.memory_id))
        .collect();

    let prompt = prompt_assembly::assemble(
        &req.system_prompt,
        &overview.summary,
        &selected_memories,
        &req.user_message,
        &req.citations,
    );

    Json(PreflightResp {
        trigger_hits,
        overview,
        budget,
        prompt,
    })
}

async fn handle_overview_cache(
    State(state): State<Arc<AppState>>,
    Json(req): Json<OverviewCacheReq>,
) -> StatusCode {
    state.cold_start.set(
        &req.tenant_id,
        &req.project_id,
        CachedOverview {
            summary: req.summary,
            key_entities: req.key_entities,
            recent_decisions: req.recent_decisions,
            active_memory_count: req.active_memory_count,
            generated_at_epoch: req.generated_at_epoch,
            ttl_seconds: req.ttl_seconds.unwrap_or(300),
        },
    );
    StatusCode::NO_CONTENT
}

async fn handle_overview_refresh(
    State(state): State<Arc<AppState>>,
    Json(req): Json<OverviewRefreshReq>,
) -> StatusCode {
    let tenant_id = req
        .tenant_id
        .or_else(|| req.scope.as_ref().and_then(|scope| scope.tenant_id.clone()));
    let project_id = req
        .project_id
        .or_else(|| req.scope.as_ref().and_then(|scope| scope.project_id.clone()));
    if let (Some(tenant_id), Some(project_id)) = (tenant_id, project_id) {
        state.cold_start.invalidate(&tenant_id, &project_id);
    }
    StatusCode::NO_CONTENT
}

#[tokio::main]
async fn main() {
    let shared = Arc::new(AppState {
        trigger_index: TriggerIndex::new(),
        cold_start: ColdStartPrimer::new(),
    });

    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/preflight", post(handle_preflight))
        .route("/trigger/lookup", post(handle_trigger_lookup))
        .route("/trigger/index", post(handle_trigger_index))
        .route("/trigger/remove", post(handle_trigger_remove))
        .route("/budget", post(handle_budget))
        .route("/prompt/assemble", post(handle_assemble))
        .route("/tokens/count", post(handle_token_count))
        .route("/overview/cache", post(handle_overview_cache))
        .route("/overview/refresh", post(handle_overview_refresh))
        .with_state(shared);

    let listen_addr = env::var("PROVENA_ORCHESTRATION_LISTEN_ADDR").unwrap_or_else(|_| "0.0.0.0:50051".into());
    let listener = tokio::net::TcpListener::bind(&listen_addr)
        .await
        .unwrap_or_else(|_| panic!("failed to bind {}", listen_addr));

    println!("provena-orchestration listening on {}", listen_addr);

    axum::serve(listener, app).await.expect("server error");
}
