"""Provena Intelligence layer configuration via Pydantic Settings."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class IntelligenceSettings(BaseSettings):
    """Configuration for the Provena intelligence service.

    All settings can be overridden via environment variables prefixed
    with ``PROVENA_INTEL_``.
    """

    model_config = {"env_prefix": "PROVENA_INTEL_"}

    # Embeddings are produced by any OpenAI-compatible endpoint, selected
    # entirely via env (prefix PROVENA_INTEL_). Defaults point at a local
    # Ollama /v1 shim; for OpenRouter set, e.g.:
    #   PROVENA_INTEL_EMBEDDING_BASE_URL=https://openrouter.ai/api/v1
    #   PROVENA_INTEL_EMBEDDING_MODEL=<provider/model>
    #   PROVENA_INTEL_EMBEDDING_API_KEY=sk-or-...
    #   PROVENA_INTEL_EMBEDDING_DIMENSIONS=<model dims>
    embedding_provider: str = "openai"
    embedding_model: str = "nomic-embed-text"
    embedding_dimensions: int = 768
    embedding_base_url: str = "http://localhost:11434/v1"
    embedding_api_key: SecretStr = Field(default=SecretStr(""), repr=False)

    model_router_default_tier: str = "balanced"

    pipeline_url: str = "http://localhost:8000"
    orchestration_url: str = "http://localhost:50051"

    # LLM-backed stages (fact extraction, rerank, contradiction detection,
    # compaction) call any OpenAI-compatible /chat/completions endpoint, selected
    # via env. Two ways to configure, both honest about what is actually served:
    #
    #  * Single provider (shorthand): set llm_model to a REAL served model name,
    #    plus llm_base_url / llm_api_key. That model serves every task and tier.
    #    Without llm_model the LLM stages stay DISABLED (deterministic heuristics)
    #    rather than firing requests with a placeholder model name.
    #  * Multiple providers: set llm_providers to a JSON array of
    #    {name, base_url, api_key, models:[{model, tier, tasks, cost_per_1k_input}]}.
    #    The router picks the cheapest configured model for the task+tier and calls
    #    that provider's endpoint with its own key. Takes precedence over the
    #    shorthand. OpenRouter is one such provider that itself fans out to many
    #    models behind a single key.
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    llm_model: str = ""
    llm_providers: str = ""

    store_db_path: str = "./data/provena.db"
    listen_port: int = 8081


settings = IntelligenceSettings()
