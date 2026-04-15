"""Provena Intelligence layer configuration via Pydantic Settings."""

from pydantic_settings import BaseSettings


class IntelligenceSettings(BaseSettings):
    """Configuration for the Provena intelligence service.

    All settings can be overridden via environment variables prefixed
    with ``PROVENA_INTEL_``.
    """

    model_config = {"env_prefix": "PROVENA_INTEL_"}

    embedding_provider: str = "local"
    embedding_model: str = "local-minilm"
    embedding_dimensions: int = 384

    model_router_default_tier: str = "balanced"

    pipeline_url: str = "http://localhost:8000"
    orchestration_url: str = "http://localhost:50051"

    llm_provider: str = "anthropic"
    llm_api_key: str = ""
    llm_model: str = "claude-sonnet-4-20250514"

    store_db_path: str = "./data/provena.db"
    listen_port: int = 8081


settings = IntelligenceSettings()
