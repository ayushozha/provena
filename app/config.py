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
    default_limit: int = 10
    max_limit: int = 50

    @property
    def resolved_db_path(self) -> Path:
        return Path(self.db_path).expanduser().resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
