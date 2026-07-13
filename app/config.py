from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from secrets import compare_digest
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


LOCAL_ENVIRONMENTS = frozenset({"development", "local", "test"})


class ServiceIdentity(BaseModel):
    token_sha256: str
    tenant_id: str
    principal_id: str
    role: Literal["viewer", "editor", "admin"] = "editor"

    @field_validator("token_sha256")
    @classmethod
    def validate_token_digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError("token_sha256 must be a lowercase SHA-256 hex digest")
        return normalized

    @field_validator("tenant_id", "principal_id")
    @classmethod
    def validate_identity_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("service identity tenant_id and principal_id are required")
        return normalized


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PROVENA_",
        extra="ignore",
    )

    service_name: str = "provena"
    environment: str = "development"
    # The store is a data-plane service. In production every /v1 request must
    # authenticate with this transport token, which maps to one configured,
    # tenant-bound service identity. Local development can explicitly retain
    # the legacy header/no-auth behavior with allow_unauthenticated_local.
    service_token: SecretStr | None = None
    gateway_service_token: SecretStr | None = None
    service_tenant_id: str = ""
    service_principal_id: str = "provena-service"
    service_role: Literal["viewer", "editor", "admin"] = "editor"
    # Optional multi-tenant registry. Configure as JSON using only SHA-256
    # digests of high-entropy bearer tokens; raw tokens never enter settings.
    service_identities: list[ServiceIdentity] = Field(default_factory=list)
    allow_unauthenticated_local: bool = True
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
    def local_auth_bypass_enabled(self) -> bool:
        return self.allow_unauthenticated_local and self.environment.strip().lower() in LOCAL_ENVIRONMENTS

    @model_validator(mode="after")
    def validate_service_auth(self) -> "Settings":
        token = self.service_token.get_secret_value().strip() if self.service_token else ""
        gateway_token = (
            self.gateway_service_token.get_secret_value().strip()
            if self.gateway_service_token
            else ""
        )
        self.service_tenant_id = self.service_tenant_id.strip()
        self.service_principal_id = self.service_principal_id.strip()
        digests = [identity.token_sha256 for identity in self.service_identities]
        if len(digests) != len(set(digests)):
            raise ValueError("PROVENA_SERVICE_IDENTITIES contains duplicate token_sha256 values")
        if gateway_token:
            gateway_digest = sha256(gateway_token.encode("utf-8")).hexdigest()
            if (token and compare_digest(gateway_token, token)) or gateway_digest in digests:
                raise ValueError(
                    "PROVENA_GATEWAY_SERVICE_TOKEN must differ from tenant service credentials"
                )
        if token and not self.service_tenant_id:
            raise ValueError(
                "PROVENA_SERVICE_TENANT_ID is required when PROVENA_SERVICE_TOKEN is set"
            )
        if token and not self.service_principal_id:
            raise ValueError(
                "PROVENA_SERVICE_PRINCIPAL_ID is required when PROVENA_SERVICE_TOKEN is set"
            )
        if (
            not self.local_auth_bypass_enabled
            and not token
            and not gateway_token
            and not self.service_identities
        ):
            raise ValueError(
                "a service or gateway credential is required outside explicit local development"
            )
        return self

    @property
    def resolved_db_path(self) -> Path:
        return Path(self.db_path).expanduser().resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
