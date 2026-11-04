from __future__ import annotations

import asyncio
from typing import Any

import pytest

from voice_platform.config.schema import PipelineConfig, ProviderRef
from voice_platform.providers.builder import PipelineBuilder
from voice_platform.providers.health import ProviderHealthTracker
from voice_platform.providers.registry import ProviderError, ProviderRegistry, default_registry
from voice_platform.providers.types import BuildContext, ProviderDescriptor, ProviderKind

KEYS = {k: "k" * 32 for k in ("DEEPGRAM_API_KEY", "ASSEMBLYAI_API_KEY", "OPENAI_API_KEY",
                              "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "CARTESIA_API_KEY",
                              "ELEVEN_API_KEY", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")}


def _ctx(**env: str) -> BuildContext:
    return BuildContext(env={**KEYS, **env})


def _pipeline(stt: dict[str, Any], llm: dict[str, Any], tts: dict[str, Any],
              **extra: Any) -> PipelineConfig:
    return PipelineConfig.model_validate({
        "mode": "cascaded", "stt": stt, "llm": llm, "tts": tts,
        "vad": {"provider": "livekit"}, "turn_detection": {"mode": "vad"}, **extra,
    })


def _cls(obj: Any) -> str:
    return f"{type(obj).__module__.split('.')[-2]}.{type(obj).__name__}"


def test_swapping_providers_requires_only_config() -> None:
    builder = PipelineBuilder(default_registry(), tracker=ProviderHealthTracker())
    a = builder.build(_pipeline(
        {"provider": "deepgram", "model": "nova-3", "language": "fr-CA"},
        {"provider": "openai", "model": "gpt-4.1", "temperature": 0.3},
        {"provider": "elevenlabs", "model": "eleven_turbo_v2_5", "voice": "custom_voice_id"},
    ), _ctx())
    b = builder.build(_pipeline(
        {"provider": "assemblyai", "language": "en"},
        {"provider": "anthropic", "model": "claude-haiku-4-5"},
        {"provider": "cartesia", "model": "sonic-2", "voice": "abc"},
    ), _ctx())
    assert (_cls(a.stt), _cls(a.llm), _cls(a.tts)) == (
        "deepgram.STT", "openai.LLM", "elevenlabs.TTS")
    assert (_cls(b.stt), _cls(b.llm), _cls(b.tts)) == (
        "assemblyai.STT", "anthropic.LLM", "cartesia.TTS")
    assert a.stt._opts.language == "fr-CA"  # type: ignore[union-attr]
    assert a.providers["llm"] == ["llm:openai:gpt-4.1"]


def test_fallback_chain_builds_livekit_adapters_in_order() -> None:
    from livekit.agents import llm, stt, tts

    builder = PipelineBuilder(default_registry(), tracker=ProviderHealthTracker())
    built = builder.build(_pipeline(
        {"provider": "deepgram", "fallbacks": [{"provider": "assemblyai"}]},
        {"provider": "openai", "fallbacks": [{"provider": "anthropic"}, {"provider": "google"}]},
        {"provider": "cartesia", "voice": "v", "fallbacks": [{"provider": "openai"}]},
    ), _ctx())
    assert isinstance(built.stt, stt.FallbackAdapter)
    assert isinstance(built.llm, llm.FallbackAdapter)
    assert isinstance(built.tts, tts.FallbackAdapter)
    assert [k.split(":")[1] for k in built.providers["llm"]] == ["openai", "anthropic", "google"]


def test_circuit_breaker_reorders_chain_across_sessions() -> None:
    tracker = ProviderHealthTracker(failure_threshold=2, recovery_timeout_s=60)
    builder = PipelineBuilder(default_registry(), tracker=tracker)
    cfg = _pipeline(
        {"provider": "deepgram"},
        {"provider": "openai", "model": "gpt-4.1-mini", "fallbacks": [{"provider": "anthropic"}]},
        {"provider": "cartesia", "voice": "v"},
    )
    for _ in range(2):
        tracker.record_failure("llm:openai:gpt-4.1-mini")
    built = builder.build(cfg, _ctx())
    assert built.providers["llm"][0].startswith("llm:anthropic")
    tracker.record_success("llm:openai:gpt-4.1-mini")
    assert builder.build(cfg, _ctx()).providers["llm"][0].startswith("llm:openai")


def test_missing_primary_secret_is_fatal_but_fallback_is_skipped() -> None:
    builder = PipelineBuilder(default_registry(), tracker=ProviderHealthTracker())
    env = dict(KEYS)
    env.pop("ANTHROPIC_API_KEY")
    cfg = _pipeline(
        {"provider": "deepgram"},
        {"provider": "openai", "fallbacks": [{"provider": "anthropic"}]},
        {"provider": "cartesia", "voice": "v"},
    )
    built = builder.build(cfg, BuildContext(env=env))
    assert built.providers["llm"] == ["llm:openai:-"]
    assert any("skipped" in w.message or "no API key" in w.message for w in built.warnings)
    env.pop("OPENAI_API_KEY")
    with pytest.raises(ProviderError, match="no API key"):
        builder.build(cfg, BuildContext(env=env))


def test_tenant_credentials_override_env_keys() -> None:
    builder = PipelineBuilder(default_registry(), tracker=ProviderHealthTracker())
    cfg = _pipeline(
        {"provider": "deepgram", "credential": "dg"},
        {"provider": "openai"},
        {"provider": "cartesia", "voice": "v"},
    )
    with pytest.raises(ProviderError, match="credential 'dg'"):
        builder.build(cfg, _ctx())
    ctx = BuildContext(env=KEYS, tenant_secrets={"dg": {"api_key": "tenant-own-key-123456"}})
    built = builder.build(cfg, ctx)
    assert built.stt._api_key == "tenant-own-key-123456"  # type: ignore[union-attr]


def test_option_validation_rejects_unknown_and_out_of_range() -> None:
    builder = PipelineBuilder(default_registry())
    bad = _pipeline(
        {"provider": "deepgram", "options": {"endpointing_ms": 99999, "evil": True}},
        {"provider": "openai"},
        {"provider": "elevenlabs"},
    )
    problems = builder.validate(bad, _ctx())
    paths = {p.path for p in problems if p.severity == "error"}
    assert "pipeline.stt.options.endpointing_ms" in paths
    assert "pipeline.stt.options.evil" in paths
    assert "pipeline.tts.voice" in paths  # elevenlabs requires a voice


def test_capability_checks() -> None:
    builder = PipelineBuilder(default_registry())
    no_vad = PipelineConfig.model_validate({
        "mode": "cascaded", "stt": {"provider": "openai"}, "llm": {"provider": "openai"},
        "tts": {"provider": "openai"}, "vad": None, "turn_detection": {"mode": "stt"},
    })
    msgs = " ".join(p.message for p in builder.validate(no_vad, _ctx()))
    assert "needs a VAD" in msgs
    assert "does not emit end-of-speech" in msgs
    model_td = _pipeline({"provider": "deepgram"}, {"provider": "openai"},
                         {"provider": "openai"},
                         turn_detection={"mode": "model", "provider": "nope"})
    assert any("unknown turn_detection provider" in p.message
               for p in builder.validate(model_td, _ctx()))


def test_non_streaming_stt_is_wrapped_with_stream_adapter() -> None:
    from livekit.agents import stt

    builder = PipelineBuilder(default_registry(), tracker=ProviderHealthTracker())
    built = builder.build(_pipeline({"provider": "openai"}, {"provider": "openai"},
                                    {"provider": "openai"}), _ctx())
    assert isinstance(built.stt, stt.StreamAdapter)


def test_realtime_mode() -> None:
    from livekit.agents import llm

    builder = PipelineBuilder(default_registry(), tracker=ProviderHealthTracker())
    cfg = PipelineConfig.model_validate({
        "mode": "realtime",
        "realtime": {"provider": "openai-realtime", "voice": "marin",
                     "fallbacks": [{"provider": "google-realtime"}]},
        "vad": None, "turn_detection": {"mode": "realtime_llm"},
    })
    built = builder.build(cfg, _ctx())
    assert isinstance(built.llm, llm.RealtimeModelFallbackAdapter)
    assert built.stt is None and built.turn_detection == "realtime_llm"


def test_registry_rejects_duplicates_and_reports_metadata() -> None:
    reg = ProviderRegistry()
    desc = ProviderDescriptor(kind=ProviderKind.LLM, name="x", display_name="X",
                              factory=lambda r, c: object(), package="pkg")
    reg.register(desc)
    with pytest.raises(ProviderError):
        reg.register(desc)
    meta = default_registry().get(ProviderKind.STT, "deepgram").metadata()
    assert meta["capabilities"]["streaming"] is True and meta["options_schema"]
    with pytest.raises(ProviderError, match="available"):
        reg.get(ProviderKind.STT, "nope")


def test_every_builtin_provider_has_pricing_or_is_keyless() -> None:
    for d in default_registry().list():
        if d.kind in (ProviderKind.STT, ProviderKind.LLM, ProviderKind.TTS):
            assert d.price_for(None) is not None or d.name == "openai-compatible", d.name


# --- functional fallback with fake LLMs (no network) -----------------------------------------
async def test_livekit_llm_fallback_switches_and_tracker_records() -> None:
    from livekit.agents import APIConnectionError, llm
    from livekit.agents.llm import ChatChunk, ChatContext, ChoiceDelta

    class FailingStream(llm.LLMStream):
        async def _run(self) -> None:
            raise APIConnectionError("primary down", retryable=False)

    class OkStream(llm.LLMStream):
        async def _run(self) -> None:
            self._event_ch.send_nowait(ChatChunk(
                id="1", delta=ChoiceDelta(role="assistant", content="hello from backup")))

    class FakeLLM(llm.LLM):
        def __init__(self, ok: bool) -> None:
            super().__init__()
            self._ok = ok

        @property
        def model(self) -> str:
            return "fake-ok" if self._ok else "fake-bad"

        def chat(self, *, chat_ctx: ChatContext, tools: Any = None, conn_options: Any = None,
                 **_: Any) -> llm.LLMStream:
            from livekit.agents import DEFAULT_API_CONNECT_OPTIONS

            cls = OkStream if self._ok else FailingStream
            return cls(self, chat_ctx=chat_ctx, tools=tools or [],
                       conn_options=conn_options or DEFAULT_API_CONNECT_OPTIONS)

    reg = ProviderRegistry()
    reg.register(ProviderDescriptor(kind=ProviderKind.LLM, name="bad", display_name="bad",
                                    factory=lambda r, c: FakeLLM(False), package="t"))
    reg.register(ProviderDescriptor(kind=ProviderKind.LLM, name="good", display_name="good",
                                    factory=lambda r, c: FakeLLM(True), package="t"))
    from voice_platform.config.schema import ProviderSpec

    tracker = ProviderHealthTracker(failure_threshold=1)
    builder = PipelineBuilder(reg, tracker=tracker)
    spec = ProviderSpec(provider="bad", fallbacks=[ProviderRef(provider="good")])
    from voice_platform.providers.builder import BuiltPipeline

    out = BuiltPipeline(mode=PipelineConfig.model_fields["mode"].default)
    adapter = builder._build_chain(ProviderKind.LLM, spec, BuildContext(env={}), out)
    ctx = ChatContext()
    ctx.add_message(role="user", content="hi")
    text = ""
    async with adapter.chat(chat_ctx=ctx) as stream:
        async for chunk in stream:
            if chunk.delta and chunk.delta.content:
                text += chunk.delta.content
    assert text == "hello from backup"
    await asyncio.sleep(0)
    assert not tracker.is_available("llm:bad:-")
