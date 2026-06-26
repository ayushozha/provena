"""Provena Intelligence layer configuration via Pydantic Settings."""

import logging
from urllib.parse import urlparse

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


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
    embedding_api_key: SecretStr | None = Field(default=None, repr=False)

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
    llm_api_key: SecretStr | None = Field(default=None, repr=False)
    llm_model: str = ""
    llm_providers: str = ""

    store_db_path: str = "./data/provena.db"
    listen_port: int = 8081


settings = IntelligenceSettings()


def _is_local_provider(url: str) -> bool:
    try:
        # urlparse requires a scheme to detect hostname correctly
        if "://" not in url and not url.startswith("//"):
            url = "//" + url
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if not hostname:
            return False
        return (
            hostname in ("localhost", "127.0.0.1", "::1") or
            hostname.endswith(".local") or
            "." not in hostname  # Docker Compose service names (no TLD)
        )
    except Exception:
        return False


# Patch None to empty string so downstream code doesn't have to handle None.
# For non-local providers, log a warning — the missing key will fail at the
# point of use (clearer error) instead of crashing at import time.
if settings.embedding_api_key is None:
    if not _is_local_provider(settings.embedding_base_url):
        logger.warning(
            "PROVENA_INTEL_EMBEDDING_API_KEY is required for non-local embedding providers. "
            "Embedding calls will fail until the key is configured."
        )
    settings.embedding_api_key = SecretStr("")

llm_enabled = bool(settings.llm_model or settings.llm_providers)
if settings.llm_api_key is None:
    if llm_enabled and not _is_local_provider(settings.llm_base_url):
        logger.warning(
            "PROVENA_INTEL_LLM_API_KEY is required for non-local LLM providers. "
            "LLM calls will fail until the key is configured."
        )
    settings.llm_api_key = SecretStr("")
