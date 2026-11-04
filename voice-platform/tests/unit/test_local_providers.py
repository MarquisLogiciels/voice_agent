"""Self-hosted providers (Vosk STT, local TTS) and endpoint-trust rules."""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

import pytest
from aiohttp import WSMsgType, web

from voice_platform.config.schema import ProviderRef
from voice_platform.providers.registry import ProviderError, default_registry
from voice_platform.providers.types import BuildContext, ProviderKind


async def _fake_vosk(results: list[dict[str, Any]]) -> tuple[web.AppRunner, str, list[Any]]:
    """Minimal server speaking the vosk-server WebSocket protocol."""
    received: list[Any] = []

    async def handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        queue = list(results)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                received.append(data)
                if "eof" in data:
                    while queue:
                        await ws.send_str(json.dumps(queue.pop(0)))
                    await ws.send_str(json.dumps({"text": ""}))
                    break
            elif msg.type == WSMsgType.BINARY:
                received.append(len(msg.data))
                await ws.send_str(json.dumps(queue.pop(0) if queue else {"partial": ""}))
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"ws://127.0.0.1:{port}", received


async def test_vosk_stream_maps_protocol_to_livekit_events() -> None:
    from livekit import rtc
    from livekit.agents import stt

    runner, url, received = await _fake_vosk(
        [
            {"partial": "hello"},
            {"partial": "hello there"},
            {
                "text": "hello there",
                "result": [
                    {"word": "hello", "start": 0.1, "end": 0.4, "conf": 0.9},
                    {"word": "there", "start": 0.5, "end": 0.8, "conf": 0.7},
                ],
            },
        ]
    )
    ctx = BuildContext(env={}, trusted_endpoints=(url,))
    engine = default_registry().create(
        ProviderKind.STT, ProviderRef(provider="vosk", language="en", options={"url": url}), ctx
    )
    stream = engine.stream()
    frame = rtc.AudioFrame(
        b"\x00\x00" * 4800, sample_rate=48000, num_channels=1, samples_per_channel=4800
    )
    for _ in range(5):
        stream.push_frame(frame)
    stream.end_input()
    events = [ev async for ev in stream]
    kinds = [ev.type for ev in events]
    assert kinds[0] == stt.SpeechEventType.START_OF_SPEECH
    assert stt.SpeechEventType.INTERIM_TRANSCRIPT in kinds
    final = next(ev for ev in events if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT)
    assert final.alternatives[0].text == "hello there"
    assert final.alternatives[0].confidence == pytest.approx(0.8)
    assert stt.SpeechEventType.END_OF_SPEECH in kinds
    assert received[0] == {"config": {"sample_rate": 16000, "words": 1}}
    assert any(isinstance(r, int) for r in received)  # audio was streamed (resampled 48k→16k)
    assert received[-1] == {"eof": 1}
    await runner.cleanup()


async def test_vosk_unreachable_raises_connection_error() -> None:
    from livekit.agents import APIConnectionError

    from voice_platform.providers.local import VoskSTT

    engine = VoskSTT(url="ws://127.0.0.1:9/", connect_timeout_s=1)
    with pytest.raises(APIConnectionError):
        await engine._connect(__import__("aiohttp").ClientSession())


def test_untrusted_endpoints_are_refused() -> None:
    reg = default_registry()
    with pytest.raises(ProviderError, match=r"(?i)trusted"):
        reg.create(
            ProviderKind.STT,
            ProviderRef(provider="vosk", options={"url": "ws://10.0.0.5:2700"}),
            BuildContext(env={}),
        )
    llm = ProviderRef(
        provider="openai-compatible", model="m", options={"base_url": "http://169.254.169.254/v1"}
    )
    with pytest.raises(ProviderError, match="tenant credential"):
        reg.create(ProviderKind.LLM, llm, BuildContext(env={"OPENAI_COMPATIBLE_API_KEY": "x" * 20}))
    with_cred = llm.model_copy(update={"credential": "byok"})
    with pytest.raises(ProviderError):  # private/metadata address refused even with BYO key
        reg.create(
            ProviderKind.LLM,
            with_cred,
            BuildContext(env={}, tenant_secrets={"byok": {"api_key": "tenant-key-123456"}}),
        )
    trusted = BuildContext(env={}, trusted_endpoints=("http://127.0.0.1:11434/v1",))
    ok = reg.create(
        ProviderKind.LLM,
        ProviderRef(
            provider="openai-compatible",
            model="qwen2.5-3b",
            options={"base_url": "http://127.0.0.1:11434/v1"},
        ),
        trusted,
    )
    assert type(ok).__name__ == "LLM"


@pytest.mark.skipif(shutil.which("pico2wave") is None, reason="pico2wave not installed")
async def test_local_tts_produces_audio() -> None:
    engine = default_registry().create(
        ProviderKind.TTS, ProviderRef(provider="pico", language="fr-CA"), BuildContext(env={})
    )
    frames = [ev.frame async for ev in engine.synthesize("Bonjour, comment puis-je vous aider?")]
    duration = sum(f.duration for f in frames)
    assert 1.0 < duration < 6.0 and frames[0].sample_rate == 16000
    pcm = b"".join(f.data.tobytes() for f in frames)
    assert (
        max(
            abs(int.from_bytes(pcm[i : i + 2], "little", signed=True))
            for i in range(0, len(pcm), 200)
        )
        > 1000
    )  # not silence
    await asyncio.sleep(0)


async def test_vosk_manual_flush_finalizes_and_reconnects() -> None:
    from livekit import rtc
    from livekit.agents import stt

    from voice_platform.providers.local import VoskSTT

    runner, url, received = await _fake_vosk([])
    engine = VoskSTT(url=url)
    assert engine.capabilities.manual_flush
    stream = engine.stream()
    frame = rtc.AudioFrame(
        b"\x00\x00" * 1600, sample_rate=16000, num_channels=1, samples_per_channel=1600
    )
    stream.push_frame(frame)
    stream.flush()  # end of turn: must yield a final even though nothing was recognized
    stream.push_frame(frame)
    stream.end_input()
    events = [ev async for ev in stream]
    finals = [ev for ev in events if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT]
    assert len(finals) == 2  # one per connection (flush, then end of input)
    configs = [r for r in received if isinstance(r, dict) and "config" in r]
    eofs = [r for r in received if r == {"eof": 1}]
    assert len(configs) == 2 and len(eofs) == 2  # reconnected after the flush
    usage = [ev for ev in events if ev.type == stt.SpeechEventType.RECOGNITION_USAGE]
    assert sum(u.recognition_usage.audio_duration for u in usage) == pytest.approx(0.2)
    await runner.cleanup()


def test_vosk_noise_gate_zeroes_near_silence_only() -> None:
    import numpy as np

    from voice_platform.providers.local import _gate

    threshold = 32768 * 10 ** (-60 / 20)
    faint = np.random.default_rng(0).normal(0, 3, 1600).astype(np.int16).tobytes()
    speech = (np.sin(np.arange(1600) / 5) * 3000).astype(np.int16).tobytes()
    assert _gate(faint, threshold) == bytes(len(faint))  # vosk would hear "the"
    assert _gate(speech, threshold) == speech
    assert _gate(faint, None) == faint  # gate disabled
