"""Process-wide service container shared by the API, worker and job runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.credentials import CredentialVault
from ..core.db import Database
from ..integrations.registry import IntegrationResolver
from ..knowledge.service import KnowledgeService
from ..providers.builder import PipelineBuilder
from ..providers.registry import ProviderRegistry, default_registry
from ..shared.blobstore import BlobStore, build_blob_store
from ..shared.crypto import CredentialCipher
from ..shared.logging import get_logger
from ..shared.ratelimit import RateLimiter
from ..shared.settings import Environment, Settings, get_settings

logger = get_logger(__name__)


@dataclass
class Services:
    settings: Settings
    db: Database
    vault: CredentialVault
    resolver: IntegrationResolver
    registry: ProviderRegistry
    builder: PipelineBuilder
    blobs: BlobStore
    knowledge: KnowledgeService
    rate_limiter: RateLimiter
    redis: Any | None = None

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, db: Database | None = None
    ) -> Services:
        settings = settings or get_settings()
        keys = settings.encryption_keys.get_secret_value()
        if not keys and settings.environment in (Environment.DEV, Environment.TEST):
            # deterministic dev-only key so local stacks work out of the box; refused in prod
            keys = "ZGV2LW9ubHktZW5jcnlwdGlvbi1rZXktMDEyMzQ1Njc="
            logger.warning("using the built-in development encryption key")
        cipher = CredentialCipher.from_setting(keys)
        vault = CredentialVault(cipher)
        database = db or Database.from_settings(settings)
        registry = default_registry()
        blobs = build_blob_store(settings)
        redis = None
        if settings.redis_url is not None:
            import redis.asyncio as aioredis

            redis = aioredis.from_url(
                settings.redis_url.get_secret_value(),
                socket_timeout=0.5,
                socket_connect_timeout=0.5,
            )
        return cls(
            settings=settings,
            db=database,
            vault=vault,
            resolver=IntegrationResolver(
                vault,
                timeout_s=settings.integration_default_timeout_s,
                allow_insecure=settings.allow_insecure_http_integrations,
            ),
            registry=registry,
            builder=PipelineBuilder(registry),
            blobs=blobs,
            knowledge=KnowledgeService(database, blobs),
            rate_limiter=RateLimiter(redis),
            redis=redis,
        )

    async def aclose(self) -> None:
        await self.db.dispose()
        if self.redis is not None:
            await self.redis.aclose()
