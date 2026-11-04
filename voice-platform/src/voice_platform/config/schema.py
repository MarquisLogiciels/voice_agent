"""AgentConfig — the complete, versioned description of an agent's behaviour.

The schema is execution-environment independent: it names providers, integrations, knowledge
bases and transfer destinations; the runtime resolves those names per tenant at call time.
Secrets never appear here — only references (credential names or env var names).
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION: Literal[1] = 1

_NAME = r"^[a-z][a-z0-9_\-]{0,62}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------------------------


class ConnectionPolicy(_Model):
    """Maps onto LiveKit ``APIConnectOptions`` for each provider instance."""

    timeout_s: float = Field(default=10.0, gt=0, le=120)
    max_retries: int = Field(default=1, ge=0, le=5)
    retry_interval_s: float = Field(default=1.0, ge=0, le=30)


class ProviderRef(_Model):
    provider: str = Field(pattern=r"^[a-z][a-z0-9_\-]{0,40}$")
    model: str | None = None
    language: str | None = None
    voice: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    options: dict[str, Any] = Field(default_factory=dict)
    credential: str | None = Field(
        default=None,
        description="Tenant credential name (kind provider_api_key) to use instead of the "
        "platform-level environment key (bring-your-own-key).",
    )
    connection: ConnectionPolicy = Field(default_factory=ConnectionPolicy)


class ProviderSpec(ProviderRef):
    fallbacks: list[ProviderRef] = Field(default_factory=list, max_length=4)

    def chain(self) -> list[ProviderRef]:
        primary = ProviderRef.model_validate(self.model_dump(exclude={"fallbacks"}))
        return [primary, *self.fallbacks]


class TurnDetectionMode(str, Enum):
    MODEL = "model"  # LiveKit turn-detector model (EnglishModel / MultilingualModel)
    VAD = "vad"  # VAD silence endpointing
    STT = "stt"  # STT provider end-of-speech
    REALTIME = "realtime_llm"  # server-side detection inside a realtime model
    MANUAL = "manual"


class TurnDetectionSpec(_Model):
    mode: TurnDetectionMode = TurnDetectionMode.MODEL
    provider: str | None = Field(default="livekit", description="for mode=model")
    model: str | None = Field(default="multilingual", description="english | multilingual")
    options: dict[str, Any] = Field(default_factory=dict)


class PipelineMode(str, Enum):
    CASCADED = "cascaded"
    REALTIME = "realtime"


class PipelineConfig(_Model):
    mode: PipelineMode = PipelineMode.CASCADED
    stt: ProviderSpec | None = None
    llm: ProviderSpec | None = None
    tts: ProviderSpec | None = None
    vad: ProviderRef | None = Field(default_factory=lambda: ProviderRef(provider="silero"))
    turn_detection: TurnDetectionSpec = Field(default_factory=TurnDetectionSpec)
    realtime: ProviderSpec | None = None
    # Optional TTS for realtime models that emit text only (half-cascade).
    realtime_tts: ProviderSpec | None = None

    @model_validator(mode="after")
    def _check_mode(self) -> PipelineConfig:
        if self.mode is PipelineMode.CASCADED:
            missing = [n for n in ("stt", "llm", "tts") if getattr(self, n) is None]
            if missing:
                raise ValueError(f"cascaded pipeline requires {', '.join(missing)}")
            if self.realtime is not None:
                raise ValueError("'realtime' is only valid when mode=realtime")
            if self.turn_detection.mode is TurnDetectionMode.REALTIME:
                raise ValueError("turn_detection realtime_llm requires mode=realtime")
        else:
            if self.realtime is None:
                raise ValueError("realtime pipeline requires 'realtime'")
            if self.stt is not None or self.llm is not None:
                raise ValueError(
                    "realtime pipeline must not define stt/llm (the realtime model replaces "
                    "them); use mode=cascaded for interchangeable components"
                )
        if self.turn_detection.mode is TurnDetectionMode.VAD and self.vad is None:
            raise ValueError("turn_detection mode=vad requires a vad provider")
        return self


# --------------------------------------------------------------------------------------------
# Conversation behaviour
# --------------------------------------------------------------------------------------------


class EndpointingConfig(_Model):
    mode: Literal["fixed", "dynamic"] = "fixed"
    min_delay_s: float = Field(default=0.5, ge=0.0, le=5.0)
    max_delay_s: float = Field(default=3.0, ge=0.2, le=10.0)

    @model_validator(mode="after")
    def _order(self) -> EndpointingConfig:
        if self.max_delay_s < self.min_delay_s:
            raise ValueError("max_delay_s must be >= min_delay_s")
        return self


class InterruptionConfig(_Model):
    enabled: bool = True
    mode: Literal["vad", "adaptive"] = Field(
        default="vad", description="'adaptive' uses LiveKit's hosted interruption model"
    )
    min_duration_s: float = Field(default=0.5, ge=0.0, le=5.0)
    min_words: int = Field(default=0, ge=0, le=10)
    false_interruption_timeout_s: float | None = Field(default=2.0, ge=0.1, le=10.0)
    resume_false_interruption: bool = True


class TurnHandlingConfig(_Model):
    endpointing: EndpointingConfig = Field(default_factory=EndpointingConfig)
    interruption: InterruptionConfig = Field(default_factory=InterruptionConfig)
    preemptive_generation: bool = True


class SilenceConfig(_Model):
    user_away_timeout_s: float = Field(default=12.0, ge=3.0, le=120.0)
    reprompt_messages: list[str] = Field(
        default_factory=lambda: ["Are you still there?", "I'm sorry, I can't hear you."],
        max_length=5,
    )
    max_reprompts: int = Field(default=2, ge=0, le=5)
    goodbye_message: str = "It seems we got disconnected. Feel free to call back anytime. Goodbye!"


class ContextConfig(_Model):
    """Bounds conversation context so long calls don't grow prompts without limit."""

    max_items: int = Field(default=60, ge=10, le=500)
    summarize_when_items_exceed: int = Field(default=40, ge=8, le=400)
    keep_last_turns: int = Field(default=6, ge=1, le=50)

    @model_validator(mode="after")
    def _order(self) -> ContextConfig:
        if self.summarize_when_items_exceed > self.max_items:
            raise ValueError("summarize_when_items_exceed must be <= max_items")
        return self


class GreetingConfig(_Model):
    mode: Literal["say", "generate", "none"] = "say"
    text: str = ""
    allow_interruptions: bool = True

    @model_validator(mode="after")
    def _text(self) -> GreetingConfig:
        if self.mode == "say" and not self.text.strip():
            raise ValueError("greeting.text is required when mode=say")
        return self


class ConversationConfig(_Model):
    max_duration_s: int = Field(default=1800, ge=60, le=4 * 3600)
    wrap_up_message: str = (
        "I'm sorry, we've reached the maximum call length. Please call back if you need "
        "anything else. Goodbye!"
    )
    max_tool_steps: int = Field(default=4, ge=1, le=10)
    silence: SilenceConfig = Field(default_factory=SilenceConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    end_call_enabled: bool = True
    post_call_summary: bool = True
    ivr_detection_outbound: bool = Field(
        default=True, description="Detect IVR menus/voicemail on outbound calls (LiveKit)"
    )
    prewarm_llm_prompt: bool = Field(
        default=False,
        description="Send the system prompt to the LLM while the greeting plays so a "
        "prefix-caching server (Ollama, vLLM, llama.cpp) has it cached before the caller's "
        "first turn. One extra request per call; useful for self-hosted models",
    )
    follow_through: bool = Field(
        default=True,
        description="If the agent says it will check something but called no tool that turn, "
        "immediately run one follow-up generation asking it to call the tool",
    )


# --------------------------------------------------------------------------------------------
# Tools, integrations, knowledge, transfers
# --------------------------------------------------------------------------------------------

STANDARD_TOOLS = (
    "lookup_customer",
    "search_contacts",
    "create_contact",
    "update_contact",
    "create_lead",
    "update_lead",
    "add_note",
    "get_appointments",
    "check_availability",
    "book_appointment",
    "cancel_appointment",
    "search_knowledge_base",
    "transfer_to_human",
    "send_sms",
    "end_call",
)

CRM_TOOLS = {
    "lookup_customer",
    "search_contacts",
    "create_contact",
    "update_contact",
    "create_lead",
    "update_lead",
    "add_note",
}
CALENDAR_TOOLS = {
    "get_appointments",
    "check_availability",
    "book_appointment",
    "cancel_appointment",
}


class ToolsConfig(_Model):
    enabled: list[str] = Field(default_factory=list)
    description_overrides: dict[str, str] = Field(default_factory=dict)
    require_confirmation: list[str] = Field(
        default_factory=lambda: [
            "book_appointment",
            "cancel_appointment",
            "create_lead",
            "update_contact",
            "send_sms",
        ]
    )
    timeout_s: float = Field(default=8.0, gt=0, le=30)
    max_calls_per_conversation: int = Field(default=30, ge=1, le=200)

    @field_validator("enabled")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        for name in v:
            if name not in STANDARD_TOOLS and not name.startswith("custom."):
                raise ValueError(
                    f"unknown tool '{name}' (custom REST actions are named 'custom.<integration>"
                    ".<action>')"
                )
        if len(set(v)) != len(v):
            raise ValueError("duplicate tool names")
        return v

    @field_validator("description_overrides")
    @classmethod
    def _desc_len(cls, v: dict[str, str]) -> dict[str, str]:
        for k, text in v.items():
            if len(text) > 1500:
                raise ValueError(f"description override for {k} is too long")
        return v


class IntegrationBindings(_Model):
    """Names of tenant integration records bound to each capability."""

    crm: str | None = None
    calendar: str | None = None
    messaging: str | None = None
    custom: list[str] = Field(default_factory=list)


class KnowledgeConfig(_Model):
    knowledge_base_ids: list[str] = Field(default_factory=list)
    mode: Literal["tool", "auto"] = "tool"
    top_k: int = Field(default=4, ge=1, le=20)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    retrieval: Literal["hybrid", "vector", "keyword"] = "hybrid"
    auto_timeout_s: float = Field(default=0.35, gt=0, le=3.0)


class TransferMode(str, Enum):
    BLIND = "blind"
    WARM = "warm"


class BusinessHours(_Model):
    """Weekly schedule in the business timezone. Keys mon..sun → list of 'HH:MM-HH:MM'."""

    schedule: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("schedule")
    @classmethod
    def _valid(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        rx = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-4]):[0-5]\d$")
        for day, ranges in v.items():
            if day not in days:
                raise ValueError(f"unknown day '{day}'")
            for r in ranges:
                if not rx.match(r) or r.split("-")[0] >= r.split("-")[1]:
                    raise ValueError(f"invalid range '{r}' for {day}")
        return v


class TransferDestination(_Model):
    name: str = Field(pattern=_NAME)
    description: str = Field(default="", max_length=500)
    department: str | None = None
    phone_number: str | None = None
    sip_uri: str | None = Field(default=None, pattern=r"^sips?:[^\s]+@[^\s]+$")
    mode: TransferMode = TransferMode.BLIND
    hours: BusinessHours | None = None
    fallback: list[str] = Field(default_factory=list, max_length=5)
    ringing_timeout_s: float = Field(default=25.0, ge=5, le=90)

    @model_validator(mode="after")
    def _target(self) -> TransferDestination:
        if bool(self.phone_number) == bool(self.sip_uri):
            raise ValueError(f"destination '{self.name}' needs exactly one of phone_number/sip_uri")
        if self.name in self.fallback:
            raise ValueError(f"destination '{self.name}' cannot fall back to itself")
        return self


class TransferConfig(_Model):
    enabled: bool = False
    destinations: list[TransferDestination] = Field(default_factory=list)
    default_destination: str | None = None
    handoff_summary: bool = True
    transfer_on_request: bool = True
    transfer_on_failure: bool = True
    max_failed_tool_calls_before_transfer: int = Field(default=3, ge=1, le=20)
    announce_message: str = "Please hold while I transfer you to a member of our team."
    failure_message: str = (
        "I'm sorry, nobody is available to take your call right now. I can take a message "
        "and have someone call you back."
    )
    outbound_trunk_id: str | None = None
    caller_id_number: str | None = None

    @model_validator(mode="after")
    def _refs(self) -> TransferConfig:
        names = [d.name for d in self.destinations]
        if len(set(names)) != len(names):
            raise ValueError("duplicate transfer destination names")
        known = set(names)
        if self.default_destination and self.default_destination not in known:
            raise ValueError(f"default_destination '{self.default_destination}' is not defined")
        for d in self.destinations:
            for f in d.fallback:
                if f not in known:
                    raise ValueError(f"destination '{d.name}' falls back to unknown '{f}'")
        if self.enabled and not self.destinations:
            raise ValueError("transfer.enabled requires at least one destination")
        _detect_fallback_cycles(self.destinations)
        return self


def _detect_fallback_cycles(destinations: list[TransferDestination]) -> None:
    graph = {d.name: d.fallback for d in destinations}
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(n: str) -> None:
        if n in done:
            return
        if n in visiting:
            raise ValueError(f"transfer fallback cycle involving '{n}'")
        visiting.add(n)
        for m in graph.get(n, []):
            visit(m)
        visiting.discard(n)
        done.add(n)

    for name in graph:
        visit(name)


class RecordingConfig(_Model):
    enabled: bool = False
    consent_required: bool = True
    consent_announcement: str = "This call may be recorded for quality and training purposes."
    retention_days: int = Field(default=90, ge=1, le=3650)
    redact_transcripts: bool = False


class SmsConfig(_Model):
    enabled: bool = False
    from_number: str | None = None
    allowed_templates: dict[str, str] = Field(default_factory=dict)
    allow_free_text: bool = False
    max_per_conversation: int = Field(default=2, ge=0, le=10)


class AppointmentPolicy(_Model):
    default_duration_min: int = Field(default=30, ge=5, le=480)
    min_notice_min: int = Field(default=60, ge=0, le=60 * 24 * 30)
    max_days_ahead: int = Field(default=60, ge=1, le=365)
    hours: BusinessHours = Field(default_factory=BusinessHours)


class BusinessInfo(_Model):
    name: str = Field(min_length=1, max_length=200)
    timezone: str = "America/Toronto"
    description: str = Field(default="", max_length=4000)
    address: str | None = None
    website: str | None = None
    phone: str | None = None
    hours: BusinessHours = Field(default_factory=BusinessHours)
    facts: dict[str, str] = Field(default_factory=dict)
    appointments: AppointmentPolicy = Field(default_factory=AppointmentPolicy)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone '{v}'") from e
        return v


class Persona(_Model):
    name: str = "Alex"
    role: str = "receptionist"
    personality: str = "warm, concise and professional"
    speaking_style: str = (
        "Use short sentences suited to speech. Never use lists, markdown or emojis."
    )


# --------------------------------------------------------------------------------------------
# Root
# --------------------------------------------------------------------------------------------


class AgentConfig(_Model):
    schema_version: Literal[1] = SCHEMA_VERSION
    name: str = Field(min_length=1, max_length=200)
    language: str = Field(default="en", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    supported_languages: list[str] = Field(default_factory=list)
    persona: Persona = Field(default_factory=Persona)
    instructions: str = Field(min_length=1, max_length=20_000)
    business: BusinessInfo
    greeting: GreetingConfig = Field(
        default_factory=lambda: GreetingConfig(mode="generate", text="")
    )
    pipeline: PipelineConfig
    turn_handling: TurnHandlingConfig = Field(default_factory=TurnHandlingConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    integrations: IntegrationBindings = Field(default_factory=IntegrationBindings)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    transfer: TransferConfig = Field(default_factory=TransferConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    sms: SmsConfig = Field(default_factory=SmsConfig)
    metadata: dict[str, Any] = Field(default_factory=dict)
    environment_overrides: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Partial configs deep-merged over this one per environment, e.g. "
        "{'staging': {'integrations': {'crm': 'hubspot_sandbox'}}}",
    )

    @field_validator("environment_overrides")
    @classmethod
    def _override_envs(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        for env, override in v.items():
            if not re.match(r"^[a-z][a-z0-9_\-]{0,31}$", env):
                raise ValueError(f"invalid environment name '{env}'")
            if "environment_overrides" in override or "schema_version" in override:
                raise ValueError("overrides cannot nest environment_overrides/schema_version")
        return v

    def materialize(self, environment: str) -> AgentConfig:
        """Return the effective config for ``environment`` (validated)."""
        override = self.environment_overrides.get(environment)
        base = self.model_dump(mode="json")
        base["environment_overrides"] = {}
        if override:
            base = deep_merge(base, override)
        return AgentConfig.model_validate(base)

    @model_validator(mode="after")
    def _overrides_valid(self) -> AgentConfig:
        for env in self.environment_overrides:
            try:
                self.materialize(env)
            except ValueError as e:
                raise ValueError(f"environment override '{env}' is invalid: {e}") from e
        return self

    @model_validator(mode="after")
    def _cross_checks(self) -> AgentConfig:
        enabled = set(self.tools.enabled)
        if enabled & CRM_TOOLS and not self.integrations.crm:
            raise ValueError(
                f"tools {sorted(enabled & CRM_TOOLS)} require integrations.crm to be bound"
            )
        if enabled & CALENDAR_TOOLS and not self.integrations.calendar:
            raise ValueError(
                f"tools {sorted(enabled & CALENDAR_TOOLS)} require integrations.calendar"
            )
        if "transfer_to_human" in enabled and not self.transfer.enabled:
            raise ValueError("transfer_to_human requires transfer.enabled")
        if "send_sms" in enabled and not (self.sms.enabled and self.integrations.messaging):
            raise ValueError("send_sms requires sms.enabled and integrations.messaging")
        if "search_knowledge_base" in enabled and not self.knowledge.knowledge_base_ids:
            raise ValueError("search_knowledge_base requires knowledge.knowledge_base_ids")
        for name in enabled:
            if name.startswith("custom."):
                parts = name.split(".")
                if len(parts) != 3 or parts[1] not in self.integrations.custom:
                    raise ValueError(
                        f"custom tool '{name}' must reference a bound custom integration"
                    )
        for name in self.tools.description_overrides:
            if name not in enabled:
                raise ValueError(f"description override for disabled tool '{name}'")
        return self


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``. Lists are replaced, not merged."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def config_hash(config: AgentConfig | dict[str, Any]) -> str:
    data = config.model_dump(mode="json") if isinstance(config, AgentConfig) else config
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


AgentConfigDict = Annotated[dict[str, Any], "serialized AgentConfig"]
