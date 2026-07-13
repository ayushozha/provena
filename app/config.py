from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PROVENA_",
        extra="ignore",
    )

    service_name: str = "provena"
    environment: str = "development"
    db_path: str = "./data/provena.db"
    # PostgreSQL connection URL (e.g. postgresql://user:pass@host:5432/db?sslmode=require).
    # When set, takes precedence over db_path (SQLite). Mutually exclusive backends.
    database_url: str | None = None
    # Redis URL for distributed search hot cache (e.g. redis://localhost:6379/0).
    redis_url: str | None = None
    default_limit: int = 10
    max_limit: int = 50
    # Dimension of the sqlite-vec KNN index. Must match the embedding model in
    # use. The index is an optional fast path; if the
    # sqlite-vec extension can't load, the store falls back to a linear scan.
    vector_dimensions: int = 768

    @property
    def resolved_db_path(self) -> Path:
        return Path(self.db_path).expanduser().resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
