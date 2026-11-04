"""Built-in provider descriptors.

Each factory imports its LiveKit plugin lazily, so an uninstalled plugin only breaks agents
that use it. Option models whitelist the provider-specific constructor arguments that tenants
may set; anything else is rejected at validation time (no arbitrary kwargs into SDKs).

To add a provider: write an options model + factory and call ``registry.register``.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config.schema import ProviderRef
from . import health
from .pricing import load_pricing
from .registry import ProviderRegistry
from .types import BuildContext, Capabilities, ProviderDescriptor, ProviderKind


class _Opts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def kwargs(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


def _opts(model: type[_Opts], ref: ProviderRef) -> dict[str, Any]:
    return model.model_validate(ref.options).kwargs()


def _key(ref: ProviderRef, ctx: BuildContext, *env: str) -> Any:
    """API key for a plugin, or LiveKit's NOT_GIVEN so the plugin applies its own defaults."""
    from livekit.agents import NOT_GIVEN

    value = ctx.secret(ref, env)
    return value if value else NOT_GIVEN


def _voice(ref: ProviderRef) -> str:
    if not ref.voice:
        raise ValueError(f"{ref.provider} requires a voice")
    return ref.voice


def _lang(ref: ProviderRef, ctx: BuildContext) -> str:
    return ref.language or ctx.default_language


# ============================================================================================
# STT
# ============================================================================================


class DeepgramSTTOptions(_Opts):
    interim_results: bool | None = None
    punctuate: bool | None = None
    smart_format: bool | None = None
    endpointing_ms: int | None = Field(default=None, ge=0, le=5000)
    filler_words: bool | None = None
    keyterm: list[str] | None = Field(default=None, max_length=100)
    numerals: bool | None = None
    profanity_filter: bool | None = None
    no_delay: bool | None = None
    detect_language: bool | None = None


def _deepgram_stt(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import deepgram

    return deepgram.STT(
        model=ref.model or "nova-3",
        language=_lang(ref, ctx),
        api_key=_key(ref, ctx, "DEEPGRAM_API_KEY"),
        **_opts(DeepgramSTTOptions, ref),
    )


class AssemblyAISTTOptions(_Opts):
    end_of_turn_confidence_threshold: float | None = Field(default=None, ge=0, le=1)
    min_turn_silence: int | None = Field(default=None, ge=0, le=10_000)
    max_turn_silence: int | None = Field(default=None, ge=0, le=20_000)
    format_turns: bool | None = None
    keyterms_prompt: list[str] | None = Field(default=None, max_length=100)
    language_detection: bool | None = None


def _assemblyai_stt(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import assemblyai

    kwargs = _opts(AssemblyAISTTOptions, ref)
    lang = _lang(ref, ctx)
    model = ref.model or (
        "universal-streaming-english"
        if lang.startswith("en")
        else "universal-streaming-multilingual"
    )
    if not lang.startswith("en") and "language_detection" not in kwargs:
        kwargs["language_detection"] = True
    return assemblyai.STT(
        model=model,  # type: ignore[arg-type]  # model ids are validated by AssemblyAI
        api_key=_key(ref, ctx, "ASSEMBLYAI_API_KEY"),
        **kwargs,
    )


class OpenAISTTOptions(_Opts):
    prompt: str | None = Field(default=None, max_length=2000)
    detect_language: bool | None = None
    noise_reduction_type: Literal["near_field", "far_field"] | None = None


def _openai_stt(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import openai

    kwargs: dict[str, Any] = _opts(OpenAISTTOptions, ref)
    base_url = ctx.base_url(ref)
    if base_url:
        kwargs["base_url"] = base_url
    return openai.STT(
        model=ref.model or "gpt-4o-transcribe",
        language=_lang(ref, ctx),
        api_key=_key(ref, ctx, "OPENAI_API_KEY"),
        **kwargs,
    )


class GoogleSTTOptions(_Opts):
    location: str | None = None
    interim_results: bool | None = None
    punctuate: bool | None = None
    min_confidence_threshold: float | None = Field(default=None, ge=0, le=1)


def _google_credentials(ref: ProviderRef, ctx: BuildContext) -> dict[str, Any]:
    """Google uses service-account JSON rather than an API key."""
    if ref.credential:
        payload = ctx.tenant_secrets.get(ref.credential) or {}
        raw = payload.get("api_key")
        if raw:
            try:
                info = json.loads(str(raw))
            except json.JSONDecodeError:
                return {}
            return {"credentials_info": info}
    return {}


def _google_stt(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import google

    lang = _lang(ref, ctx)
    return google.STT(
        languages=lang,
        model=ref.model or "chirp_2",
        **_google_credentials(ref, ctx),
        **_opts(GoogleSTTOptions, ref),
    )


def _cartesia_stt(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import cartesia

    return cartesia.STT(
        model=ref.model or "ink-whisper",
        language=_lang(ref, ctx),
        api_key=_key(ref, ctx, "CARTESIA_API_KEY"),
    )


def _livekit_stt(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.agents import inference

    return inference.STT(model=ref.model or "deepgram/nova-3", language=_lang(ref, ctx))


class VoskOptions(_Opts):
    url: str = Field(pattern=r"^wss?://[^\s]+$", description="Vosk server WebSocket URL")
    noise_gate_dbfs: float | None = Field(
        default=-60.0,
        ge=-90,
        le=-20,
        description="100 ms frames quieter than this are sent as digital silence (vosk "
        "transcribes near-silent noise as 'the'); null disables the gate",
    )
    drop_lone_words: list[str] = Field(
        default_factory=lambda: ["the"],
        max_length=20,
        description="Discard a final transcript consisting only of one of these words "
        "(the English model's hallucination on steady line noise)",
    )


def _vosk_stt(ref: ProviderRef, ctx: BuildContext) -> Any:
    from .local import VoskSTT

    opts = VoskOptions.model_validate(ref.options)
    url = opts.url
    if not ctx.is_trusted(url):
        raise ValueError(
            "vosk url must be a platform-operated endpoint (VP_TRUSTED_PROVIDER_ENDPOINTS)"
        )
    return VoskSTT(
        url=url,
        language=_lang(ref, ctx),
        noise_gate_dbfs=opts.noise_gate_dbfs,
        drop_lone_words=tuple(opts.drop_lone_words),
    )


# ============================================================================================
# LLM
# ============================================================================================


class OpenAILLMOptions(_Opts):
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_completion_tokens: int | None = Field(default=None, ge=16, le=8192)
    parallel_tool_calls: bool | None = None
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    service_tier: Literal["auto", "default", "flex", "priority"] | None = None


def _openai_llm(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import openai

    kwargs: dict[str, Any] = _opts(OpenAILLMOptions, ref)
    if ref.temperature is not None:
        kwargs["temperature"] = ref.temperature
    base_url = ctx.base_url(ref)
    if base_url:
        kwargs["base_url"] = base_url
    return openai.LLM(
        model=ref.model or "gpt-4.1-mini", api_key=_key(ref, ctx, "OPENAI_API_KEY"), **kwargs
    )


class OpenAICompatibleOptions(OpenAILLMOptions):
    base_url: str = Field(pattern=r"^https?://[^\s]+$")


def check_untrusted_endpoint(url: str) -> None:
    """A tenant-supplied endpoint must be public HTTPS (no private/metadata addresses)."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    from ..shared.http import EgressBlockedError, EgressPolicy

    policy = EgressPolicy(allowed_ports=(443,))
    try:
        policy.check_url(url)
    except EgressBlockedError as e:
        raise ValueError(f"endpoint refused: {e.message}") from e
    host = urlsplit(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as e:
        raise ValueError(f"cannot resolve {host}") from e
    for info in infos:
        if not policy.ip_allowed(ipaddress.ip_address(info[4][0])):
            raise ValueError(f"{host} resolves to a non-public address")


def _openai_compatible_llm(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import openai

    opts = OpenAICompatibleOptions.model_validate(ref.options).kwargs()
    base_url = opts.pop("base_url")
    if ref.temperature is not None:
        opts["temperature"] = ref.temperature
    if not ref.model:
        raise ValueError("openai-compatible provider requires an explicit model")
    if ctx.is_trusted(base_url):
        # platform-operated endpoint (e.g. in-VPC vLLM/Ollama): platform key allowed
        api_key = _key(ref, ctx, "OPENAI_COMPATIBLE_API_KEY")
        from livekit.agents import NOT_GIVEN

        if api_key is NOT_GIVEN:
            api_key = "not-required"  # most self-hosted servers ignore the key
    else:
        # Tenant-supplied endpoint: never send a platform secret to it, and refuse private
        # networks (SSRF). The tenant must bring its own key as a credential.
        if not ref.credential:
            raise ValueError(
                "untrusted openai-compatible endpoints require a tenant credential "
                "(platform keys are only sent to VP_TRUSTED_PROVIDER_ENDPOINTS)"
            )
        check_untrusted_endpoint(base_url)
        api_key = _key(ref, ctx)
    return openai.LLM(model=ref.model, base_url=base_url, api_key=api_key, **opts)


class AnthropicLLMOptions(_Opts):
    max_tokens: int | None = Field(default=None, ge=16, le=8192)
    top_k: int | None = Field(default=None, ge=1, le=500)
    caching: Literal["ephemeral"] | None = None
    parallel_tool_calls: bool | None = None


def _anthropic_llm(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import anthropic

    kwargs: dict[str, Any] = _opts(AnthropicLLMOptions, ref)
    if ref.temperature is not None:
        kwargs["temperature"] = ref.temperature
    return anthropic.LLM(
        model=ref.model or "claude-haiku-4-5",
        api_key=_key(ref, ctx, "ANTHROPIC_API_KEY"),
        **kwargs,
    )


class GoogleLLMOptions(_Opts):
    max_output_tokens: int | None = Field(default=None, ge=16, le=8192)
    top_p: float | None = Field(default=None, ge=0, le=1)
    vertexai: bool | None = None
    project: str | None = None
    location: str | None = None


def _google_llm(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import google

    kwargs: dict[str, Any] = _opts(GoogleLLMOptions, ref)
    if ref.temperature is not None:
        kwargs["temperature"] = ref.temperature
    if not kwargs.get("vertexai"):
        kwargs["api_key"] = _key(ref, ctx, "GOOGLE_API_KEY", "GEMINI_API_KEY")
    return google.LLM(model=ref.model or "gemini-2.5-flash", **kwargs)


def _livekit_llm(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.agents import inference

    return inference.LLM(model=ref.model or "openai/gpt-4.1-mini")


# ============================================================================================
# TTS
# ============================================================================================


class ElevenLabsTTSOptions(_Opts):
    streaming_latency: int | None = Field(default=None, ge=0, le=4)
    auto_mode: bool | None = None
    apply_text_normalization: Literal["auto", "on", "off"] | None = None
    stability: float | None = Field(default=None, ge=0, le=1)
    similarity_boost: float | None = Field(default=None, ge=0, le=1)
    speed: float | None = Field(default=None, ge=0.7, le=1.2)


def _elevenlabs_tts(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import elevenlabs

    opts = _opts(ElevenLabsTTSOptions, ref)
    settings = {k: opts.pop(k) for k in ("stability", "similarity_boost", "speed") if k in opts}
    kwargs: dict[str, Any] = dict(opts)
    if settings:
        kwargs["voice_settings"] = elevenlabs.VoiceSettings(
            stability=settings.get("stability", 0.5),
            similarity_boost=settings.get("similarity_boost", 0.75),
            speed=settings.get("speed", 1.0),
        )
    if ref.language:
        kwargs["language"] = ref.language.split("-")[0]
    return elevenlabs.TTS(
        voice_id=_voice(ref),
        model=ref.model or "eleven_flash_v2_5",
        api_key=_key(ref, ctx, "ELEVEN_API_KEY", "ELEVENLABS_API_KEY"),
        **kwargs,
    )


class CartesiaTTSOptions(_Opts):
    speed: float | Literal["slowest", "slow", "normal", "fast", "fastest"] | None = None
    emotion: list[str] | None = None
    volume: float | None = Field(default=None, ge=0.5, le=2.0)


def _cartesia_tts(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import cartesia

    return cartesia.TTS(
        model=ref.model or "sonic-2",
        voice=_voice(ref),
        language=(ref.language or ctx.default_language).split("-")[0],
        api_key=_key(ref, ctx, "CARTESIA_API_KEY"),
        **_opts(CartesiaTTSOptions, ref),
    )


class DeepgramTTSOptions(_Opts):
    speed: float | None = Field(default=None, ge=0.7, le=1.5)


def _deepgram_tts(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import deepgram

    # Deepgram Aura encodes the voice in the model id (e.g. aura-2-thalia-en).
    return deepgram.TTS(
        model=ref.voice or ref.model or "aura-2-thalia-en",
        api_key=_key(ref, ctx, "DEEPGRAM_API_KEY"),
        **_opts(DeepgramTTSOptions, ref),
    )


class OpenAITTSOptions(_Opts):
    instructions: str | None = Field(default=None, max_length=2000)
    speed: float | None = Field(default=None, ge=0.25, le=4.0)


def _openai_tts(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import openai

    return openai.TTS(
        model=ref.model or "gpt-4o-mini-tts",
        voice=ref.voice or "ash",
        api_key=_key(ref, ctx, "OPENAI_API_KEY"),
        **_opts(OpenAITTSOptions, ref),
    )


class GoogleTTSOptions(_Opts):
    speaking_rate: float | None = Field(default=None, ge=0.25, le=4.0)
    pitch: float | None = Field(default=None, ge=-20, le=20)
    location: str | None = None


def _google_tts(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import google

    return google.TTS(
        language=_lang(ref, ctx),
        voice_name=ref.voice or "",
        **_google_credentials(ref, ctx),
        **_opts(GoogleTTSOptions, ref),
    )


def _livekit_tts(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.agents import inference

    kwargs: dict[str, Any] = {}
    if ref.voice:
        kwargs["voice"] = ref.voice
    if ref.language:
        kwargs["language"] = ref.language
    return inference.TTS(model=ref.model or "cartesia/sonic-3", **kwargs)


class LocalTTSOptions(_Opts):
    rate_wpm: int | None = Field(default=None, ge=80, le=400)


def _local_tts(engine: Literal["pico", "espeak"]) -> Any:
    def factory(ref: ProviderRef, ctx: BuildContext) -> Any:
        from .local import LocalTTS

        return LocalTTS(
            engine=engine,
            language=ref.language or ctx.default_language,  # type: ignore[arg-type]
            voice=ref.voice,
            rate_wpm=_opts(LocalTTSOptions, ref).get("rate_wpm"),
        )

    return factory


# ============================================================================================
# VAD / turn detection
# ============================================================================================


class SileroOptions(_Opts):
    min_speech_duration: float | None = Field(default=None, ge=0.01, le=2)
    min_silence_duration: float | None = Field(default=None, ge=0.05, le=3)
    prefix_padding_duration: float | None = Field(default=None, ge=0, le=2)
    activation_threshold: float | None = Field(default=None, ge=0.05, le=0.95)
    force_cpu: bool | None = None


def _silero_vad(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import silero

    return silero.VAD.load(**_opts(SileroOptions, ref))


class InferenceVADOptions(_Opts):
    min_speech_duration: float | None = Field(default=None, ge=0.01, le=2)
    min_silence_duration: float | None = Field(default=None, ge=0.05, le=3)
    activation_threshold: float | None = Field(default=None, ge=0.05, le=0.95)


def _livekit_vad(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.agents import inference

    kwargs = _opts(InferenceVADOptions, ref)
    if ref.model:
        kwargs["model"] = ref.model
    return inference.VAD(**kwargs)


class TurnDetectorOptions(_Opts):
    unlikely_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


def _livekit_turn_detector(ref: ProviderRef, ctx: BuildContext) -> Any:
    opts = _opts(TurnDetectorOptions, ref)
    if (ref.model or "multilingual") == "english":
        from livekit.plugins.turn_detector.english import EnglishModel

        return EnglishModel(**opts)
    from livekit.plugins.turn_detector.multilingual import MultilingualModel

    return MultilingualModel(**opts)


def _livekit_inference_turn_detector(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.agents import inference

    return inference.TurnDetector(**_opts(TurnDetectorOptions, ref))


# ============================================================================================
# Realtime (speech-to-speech)
# ============================================================================================


class OpenAIRealtimeOptions(_Opts):
    speed: float | None = Field(default=None, ge=0.25, le=1.5)


def _openai_realtime(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins import openai

    kwargs: dict[str, Any] = _opts(OpenAIRealtimeOptions, ref)
    if ref.temperature is not None:
        kwargs["temperature"] = ref.temperature
    return openai.realtime.RealtimeModel(
        model=ref.model or "gpt-realtime",
        voice=ref.voice or "marin",
        api_key=_key(ref, ctx, "OPENAI_API_KEY"),
        **kwargs,
    )


def _google_realtime(ref: ProviderRef, ctx: BuildContext) -> Any:
    from livekit.plugins.google import realtime

    kwargs: dict[str, Any] = {}
    if ref.temperature is not None:
        kwargs["temperature"] = ref.temperature
    if ref.voice:
        kwargs["voice"] = ref.voice
    if ref.language:
        kwargs["language"] = ref.language
    return realtime.RealtimeModel(
        model=ref.model or "gemini-2.5-flash-native-audio-preview-09-2025",
        api_key=_key(ref, ctx, "GOOGLE_API_KEY", "GEMINI_API_KEY"),
        **kwargs,
    )


# ============================================================================================


def register_builtin_providers(reg: ProviderRegistry) -> None:
    pricing = load_pricing()
    P = ProviderKind

    def add(
        kind: ProviderKind, name: str, display: str, factory: Any, package: str, **kw: Any
    ) -> None:
        reg.register(
            ProviderDescriptor(
                kind=kind,
                name=name,
                display_name=display,
                factory=factory,
                package=package,
                pricing=pricing.get(f"{kind.value}/{name}", {}),
                **kw,
            )
        )

    stream_stt = Capabilities(streaming=True, interim_results=True, emits_end_of_speech=True)
    add(
        P.STT,
        "deepgram",
        "Deepgram",
        _deepgram_stt,
        "livekit-plugins-deepgram",
        capabilities=stream_stt,
        secret_env=("DEEPGRAM_API_KEY",),
        options_model=DeepgramSTTOptions,
        default_model="nova-3",
        known_models=("nova-3", "nova-2", "nova-2-phonecall", "flux-general-en"),
        health_check=health.deepgram,
    )
    add(
        P.STT,
        "assemblyai",
        "AssemblyAI",
        _assemblyai_stt,
        "livekit-plugins-assemblyai",
        capabilities=stream_stt,
        secret_env=("ASSEMBLYAI_API_KEY",),
        options_model=AssemblyAISTTOptions,
        default_model="universal-streaming-english",
        health_check=health.assemblyai,
    )
    add(
        P.STT,
        "openai",
        "OpenAI",
        _openai_stt,
        "livekit-plugins-openai",
        capabilities=Capabilities(streaming=False, notes="batch transcription; needs VAD"),
        secret_env=("OPENAI_API_KEY",),
        options_model=OpenAISTTOptions,
        default_model="gpt-4o-transcribe",
        health_check=health.openai,
    )
    add(
        P.STT,
        "google",
        "Google Cloud Speech",
        _google_stt,
        "livekit-plugins-google",
        capabilities=stream_stt,
        options_model=GoogleSTTOptions,
        default_model="chirp_2",
    )
    add(
        P.STT,
        "cartesia",
        "Cartesia",
        _cartesia_stt,
        "livekit-plugins-cartesia",
        capabilities=stream_stt,
        secret_env=("CARTESIA_API_KEY",),
        default_model="ink-whisper",
        health_check=health.cartesia,
    )
    add(
        P.STT,
        "livekit",
        "LiveKit Inference",
        _livekit_stt,
        "livekit-agents",
        capabilities=stream_stt,
        secret_env=("LIVEKIT_API_KEY",),
        default_model="deepgram/nova-3",
    )

    add(
        P.STT,
        "vosk",
        "Vosk (self-hosted)",
        _vosk_stt,
        "voice-platform",
        capabilities=Capabilities(
            streaming=True,
            interim_results=True,
            emits_end_of_speech=True,
            notes="self-hosted; endpoint must be trusted",
        ),
        options_model=VoskOptions,
    )
    add(
        P.LLM,
        "openai",
        "OpenAI",
        _openai_llm,
        "livekit-plugins-openai",
        secret_env=("OPENAI_API_KEY",),
        options_model=OpenAILLMOptions,
        default_model="gpt-4.1-mini",
        known_models=("gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o", "gpt-4o-mini"),
        health_check=health.openai,
    )
    add(
        P.LLM,
        "openai-compatible",
        "OpenAI-compatible endpoint",
        _openai_compatible_llm,
        "livekit-plugins-openai",
        options_model=OpenAICompatibleOptions,
    )
    add(
        P.LLM,
        "anthropic",
        "Anthropic",
        _anthropic_llm,
        "livekit-plugins-anthropic",
        secret_env=("ANTHROPIC_API_KEY",),
        options_model=AnthropicLLMOptions,
        default_model="claude-haiku-4-5",
        health_check=health.anthropic,
    )
    add(
        P.LLM,
        "google",
        "Google Gemini",
        _google_llm,
        "livekit-plugins-google",
        secret_env=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        options_model=GoogleLLMOptions,
        default_model="gemini-2.5-flash",
        health_check=health.google_genai,
    )
    add(
        P.LLM,
        "livekit",
        "LiveKit Inference",
        _livekit_llm,
        "livekit-agents",
        secret_env=("LIVEKIT_API_KEY",),
        default_model="openai/gpt-4.1-mini",
    )

    add(
        P.TTS,
        "elevenlabs",
        "ElevenLabs",
        _elevenlabs_tts,
        "livekit-plugins-elevenlabs",
        capabilities=Capabilities(streaming=True, requires_voice=True),
        secret_env=("ELEVEN_API_KEY", "ELEVENLABS_API_KEY"),
        options_model=ElevenLabsTTSOptions,
        default_model="eleven_flash_v2_5",
        known_models=(
            "eleven_flash_v2_5",
            "eleven_turbo_v2_5",
            "eleven_multilingual_v2",
            "eleven_v3",
        ),
        health_check=health.elevenlabs,
    )
    add(
        P.TTS,
        "cartesia",
        "Cartesia",
        _cartesia_tts,
        "livekit-plugins-cartesia",
        capabilities=Capabilities(streaming=True, requires_voice=True),
        secret_env=("CARTESIA_API_KEY",),
        options_model=CartesiaTTSOptions,
        default_model="sonic-2",
        known_models=("sonic", "sonic-2", "sonic-3", "sonic-turbo"),
        health_check=health.cartesia,
    )
    add(
        P.TTS,
        "deepgram",
        "Deepgram Aura",
        _deepgram_tts,
        "livekit-plugins-deepgram",
        capabilities=Capabilities(streaming=True, languages=("en", "es")),
        secret_env=("DEEPGRAM_API_KEY",),
        options_model=DeepgramTTSOptions,
        default_model="aura-2-thalia-en",
        health_check=health.deepgram,
    )
    add(
        P.TTS,
        "openai",
        "OpenAI",
        _openai_tts,
        "livekit-plugins-openai",
        capabilities=Capabilities(streaming=True),
        secret_env=("OPENAI_API_KEY",),
        options_model=OpenAITTSOptions,
        default_model="gpt-4o-mini-tts",
        health_check=health.openai,
    )
    add(
        P.TTS,
        "google",
        "Google Cloud TTS",
        _google_tts,
        "livekit-plugins-google",
        capabilities=Capabilities(streaming=True, requires_voice=True),
        options_model=GoogleTTSOptions,
    )
    add(
        P.TTS,
        "livekit",
        "LiveKit Inference",
        _livekit_tts,
        "livekit-agents",
        secret_env=("LIVEKIT_API_KEY",),
        default_model="cartesia/sonic-3",
    )

    local_engines: tuple[tuple[Literal["pico", "espeak"], str], ...] = (
        ("pico", "SVOX Pico (local)"),
        ("espeak", "eSpeak NG (local)"),
    )
    for engine, label in local_engines:
        add(
            P.TTS,
            engine,
            label,
            _local_tts(engine),
            "voice-platform",
            capabilities=Capabilities(streaming=False, notes="lab/CI grade voice"),
            options_model=LocalTTSOptions,
        )
    add(
        P.VAD,
        "silero",
        "Silero VAD (local)",
        _silero_vad,
        "livekit-plugins-silero",
        options_model=SileroOptions,
    )
    add(
        P.VAD,
        "livekit",
        "LiveKit Inference VAD",
        _livekit_vad,
        "livekit-agents",
        options_model=InferenceVADOptions,
        secret_env=("LIVEKIT_API_KEY",),
    )

    add(
        P.TURN_DETECTION,
        "livekit",
        "LiveKit turn detector (local model)",
        _livekit_turn_detector,
        "livekit-plugins-turn-detector",
        options_model=TurnDetectorOptions,
        known_models=("english", "multilingual"),
    )
    add(
        P.TURN_DETECTION,
        "livekit-inference",
        "LiveKit Inference turn detector",
        _livekit_inference_turn_detector,
        "livekit-agents",
        options_model=TurnDetectorOptions,
        secret_env=("LIVEKIT_API_KEY",),
    )

    add(
        P.REALTIME,
        "openai-realtime",
        "OpenAI Realtime",
        _openai_realtime,
        "livekit-plugins-openai",
        secret_env=("OPENAI_API_KEY",),
        options_model=OpenAIRealtimeOptions,
        default_model="gpt-realtime",
        health_check=health.openai,
    )
    add(
        P.REALTIME,
        "google-realtime",
        "Gemini Live",
        _google_realtime,
        "livekit-plugins-google",
        secret_env=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        health_check=health.google_genai,
    )
