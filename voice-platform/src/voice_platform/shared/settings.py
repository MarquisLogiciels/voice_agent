"""Process-wide settings loaded from environment variables (prefix ``VP_``).

Secrets are typed as :class:`pydantic.SecretStr` so they never appear in ``repr`` or logs.
Production-only invariants are validated at startup so a misconfigured deployment fails
fast instead of running insecurely.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    DEV = "dev"
    TEST = "test"
    STAGING = "staging"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VP_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    environment: Environment = Environment.DEV
    service_name: str = "voice-platform"
    log_level: str = "INFO"
    log_json: bool = True

    # --- database -------------------------------------------------------------------------
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://vp_app:vp_app@localhost:5432/voice_platform"
    )
    db_pool_size: int = Field(default=10, ge=1, le=200)
    db_max_overflow: int = Field(default=10, ge=0, le=200)
    db_pool_timeout_s: float = Field(default=10.0, gt=0)
    db_statement_timeout_ms: int = Field(default=15_000, ge=100)

    # --- redis (optional: rate limiting + transient state) -----------------------------------
    redis_url: SecretStr | None = None

    # --- auth -------------------------------------------------------------------------------
    jwt_secret: SecretStr = SecretStr("dev-insecure-jwt-secret-change-me-0123456789")
    jwt_issuer: str = "voice-platform"
    jwt_ttl_s: int = Field(default=3600, ge=60, le=86_400)
    # Comma separated Fernet keys. The first encrypts; all decrypt (rotation).
    encryption_keys: SecretStr = SecretStr("")
    api_rate_limit_per_minute: int = Field(default=600, ge=1)
    login_rate_limit_per_minute: int = Field(default=10, ge=1)
    cors_origins: list[str] = Field(default_factory=list)

    # --- object storage ---------------------------------------------------------------------
    blob_backend: Literal["local", "s3"] = "local"
    blob_local_path: str = "./.data/blobs"
    s3_bucket: str = ""
    s3_endpoint_url: str | None = None
    s3_region: str = "ca-central-1"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)

    # --- livekit ----------------------------------------------------------------------------
    livekit_url: str = ""
    livekit_api_key: SecretStr | None = None
    livekit_api_secret: SecretStr | None = None
    agent_dispatch_name: str = "voice-platform"
    livekit_sip_uri: str = ""  # e.g. sip:xxxx.sip.livekit.cloud — used for trunk provisioning

    # --- telephony safety -------------------------------------------------------------------
    # When true, outbound dialing and transfers are validated and logged but not executed.
    telephony_dry_run: bool = False
    # E.164 prefixes outbound calls may target (empty = all). Protects staging from dialing
    # real customers.
    outbound_allowed_prefixes: list[str] = Field(default_factory=list)

    # --- integrations -----------------------------------------------------------------------
    allow_insecure_http_integrations: bool = False  # http:// and private IPs; dev only
    integration_default_timeout_s: float = Field(default=8.0, gt=0, le=60)

    # --- knowledge --------------------------------------------------------------------------
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"

    # --- observability ----------------------------------------------------------------------
    otel_exporter_otlp_endpoint: str | None = None
    worker_metrics_port: int = 9464
    public_base_url: str = "http://localhost:8000"

    @model_validator(mode="after")
    def _production_invariants(self) -> Settings:
        if self.environment in (Environment.PROD, Environment.STAGING):
            problems: list[str] = []
            secret = self.jwt_secret.get_secret_value()
            if len(secret) < 32 or secret.startswith("dev-insecure"):
                problems.append("VP_JWT_SECRET must be a random value of at least 32 chars")
            if not self.encryption_keys.get_secret_value():
                problems.append("VP_ENCRYPTION_KEYS is required")
            if self.allow_insecure_http_integrations:
                problems.append("VP_ALLOW_INSECURE_HTTP_INTEGRATIONS must be false")
            if self.blob_backend == "local" and self.environment is Environment.PROD:
                problems.append("VP_BLOB_BACKEND must be s3 in prod")
            if problems:
                raise ValueError("; ".join(problems))
        return self

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PROD


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
