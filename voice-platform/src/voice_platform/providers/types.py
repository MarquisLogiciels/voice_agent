"""Provider registry types. Instances produced by factories are LiveKit objects."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel

from ..config.schema import ProviderRef


class ProviderKind(str, Enum):
    STT = "stt"
    LLM = "llm"
    TTS = "tts"
    VAD = "vad"
    TURN_DETECTION = "turn_detection"
    REALTIME = "realtime"


@dataclass(frozen=True)
class Capabilities:
    streaming: bool = True
    interim_results: bool = False
    emits_end_of_speech: bool = False
    """STT emits END_OF_SPEECH events, so ``turn_detection: stt`` works."""
    supports_tools: bool = True
    requires_voice: bool = False
    languages: tuple[str, ...] | None = None
    """BCP-47 primary tags supported; ``None`` means broad/unknown (not validated)."""
    notes: str = ""


@dataclass(frozen=True)
class Pricing:
    """Approximate list price used for cost *estimates*; see providers/pricing.yaml."""

    usd_per_audio_minute: float | None = None
    usd_per_1k_chars: float | None = None
    usd_per_1m_input_tokens: float | None = None
    usd_per_1m_output_tokens: float | None = None


@dataclass
class BuildContext:
    """Everything a factory may need besides the ProviderRef."""

    tenant_secrets: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """Resolved tenant credentials (name → payload) referenced by ``ProviderRef.credential``."""
    default_language: str = "en"
    vad: Any | None = None
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    trusted_endpoints: tuple[str, ...] = field(default_factory=lambda: _trusted_from_env())
    """Platform-operated endpoint prefixes (self-hosted LLM/STT). Only these may receive
    platform-level keys or use plain HTTP / private addresses."""

    def is_trusted(self, url: str) -> bool:
        return any(url.rstrip("/").startswith(p.rstrip("/")) for p in self.trusted_endpoints if p)

    def secret(self, ref: ProviderRef, env_names: tuple[str, ...]) -> str | None:
        if ref.credential:
            payload = self.tenant_secrets.get(ref.credential)
            if payload is None:
                raise MissingSecretError(f"credential '{ref.credential}' was not resolved")
            value = payload.get("api_key")
            if not value:
                raise MissingSecretError(f"credential '{ref.credential}' has no api_key")
            return str(value)
        for name in env_names:
            if self.env.get(name):
                return self.env[name]
        return None

    def base_url(self, ref: ProviderRef) -> str | None:
        if ref.credential:
            payload = self.tenant_secrets.get(ref.credential) or {}
            url = payload.get("base_url")
            return str(url) if url else None
        return None


def _trusted_from_env() -> tuple[str, ...]:
    import json

    raw = os.environ.get("VP_TRUSTED_PROVIDER_ENDPOINTS", "")
    if not raw:
        return ()
    try:
        values = json.loads(raw)
    except ValueError:
        values = raw.split(",")
    return tuple(str(v).strip() for v in values if str(v).strip())


class MissingSecretError(Exception):
    pass


Factory = Callable[[ProviderRef, BuildContext], Any]
HealthCheck = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class ProviderDescriptor:
    kind: ProviderKind
    name: str
    display_name: str
    factory: Factory
    package: str
    capabilities: Capabilities = field(default_factory=Capabilities)
    secret_env: tuple[str, ...] = ()
    """Environment variables holding the platform-level key (any of). Empty = keyless."""
    options_model: type[BaseModel] | None = None
    default_model: str | None = None
    known_models: tuple[str, ...] = ()
    pricing: Mapping[str, Pricing] = field(default_factory=dict)
    """model → pricing; key '*' is the provider default."""
    health_check: HealthCheck | None = None

    def price_for(self, model: str | None) -> Pricing | None:
        if model and model in self.pricing:
            return self.pricing[model]
        return self.pricing.get("*")

    def metadata(self) -> dict[str, Any]:
        caps = self.capabilities
        return {
            "kind": self.kind.value,
            "name": self.name,
            "display_name": self.display_name,
            "package": self.package,
            "default_model": self.default_model,
            "known_models": list(self.known_models),
            "requires_secret": bool(self.secret_env),
            "secret_env": list(self.secret_env),
            "capabilities": {
                "streaming": caps.streaming,
                "interim_results": caps.interim_results,
                "emits_end_of_speech": caps.emits_end_of_speech,
                "supports_tools": caps.supports_tools,
                "requires_voice": caps.requires_voice,
                "languages": list(caps.languages) if caps.languages else None,
                "notes": caps.notes,
            },
            "options_schema": self.options_model.model_json_schema()
            if self.options_model
            else None,
            "health_check": self.health_check is not None,
        }
