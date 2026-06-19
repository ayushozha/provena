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

    # LLM-backed extraction uses any OpenAI-compatible /chat/completions
    # endpoint, selected via env (same pattern as embeddings). Point base_url
    # at a local server (Ollama/llama-server), OpenRouter, OpenAI, or an
    # Anthropic OpenAI-compat shim. llm_model="" => use the model the router
    # selects for the task; set it to pin a concrete served model.
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    llm_model: str = ""

    store_db_path: str = "./data/provena.db"
    listen_port: int = 8081


settings = IntelligenceSettings()
