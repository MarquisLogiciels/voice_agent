"""Lab harness: a scripted caller in a real LiveKit room and helpers for local speech.

The caller publishes real audio frames (WebRTC, Opus) and records what the agent publishes,
so tests observe exactly what a human on the call would hear.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np

LIVEKIT_URL = os.environ.get("VP_LAB_LIVEKIT_URL", "ws://127.0.0.1:7880")
LIVEKIT_KEY = os.environ.get("VP_LAB_LIVEKIT_KEY", "devkey")
LIVEKIT_SECRET = os.environ.get("VP_LAB_LIVEKIT_SECRET", "secret")
OLLAMA_URL = os.environ.get("VP_LAB_OLLAMA_URL", "http://127.0.0.1:11434/v1")
VOSK_EN = os.environ.get("VP_LAB_VOSK_EN", "ws://127.0.0.1:2700")
VOSK_FR = os.environ.get("VP_LAB_VOSK_FR", "ws://127.0.0.1:2701")
LAB_MODEL = os.environ.get("VP_LAB_MODEL", "qwen2.5-3b")
SAMPLE_RATE = 16000


def port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def lab_available() -> list[str]:
    missing = []
    for name, (h, p) in {
        "livekit": ("127.0.0.1", 7880),
        "ollama": ("127.0.0.1", 11434),
        "vosk-en": ("127.0.0.1", 2700),
    }.items():
        if not port_open(h, p):
            missing.append(name)
    if shutil.which("pico2wave") is None:
        missing.append("pico2wave")
    return missing


def synthesize(
    text: str,
    path: Path,
    *,
    language: str = "en-US",
    speed: float = 1.0,
    noise_db: float | None = None,
) -> Path:
    """Text → 16 kHz mono WAV via SVOX Pico, optional tempo change and background noise."""
    raw = path.with_suffix(".raw.wav")
    subprocess.run(["pico2wave", "-l", language, "-w", str(raw), text], check=True)
    cmd = ["sox", str(raw), "-r", str(SAMPLE_RATE), "-c", "1", "-b", "16", str(path)]
    if speed != 1.0:
        cmd += ["tempo", str(speed)]
    subprocess.run(cmd, check=True)
    if noise_db is not None:
        noisy = path.with_suffix(".noisy.wav")
        dur = float(
            subprocess.run(
                ["soxi", "-D", str(path)], check=True, capture_output=True, text=True
            ).stdout
        )
        noise = path.with_suffix(".noise.wav")
        subprocess.run(
            [
                "sox",
                "-n",
                "-r",
                str(SAMPLE_RATE),
                "-c",
                "1",
                "-b",
                "16",
                str(noise),
                "synth",
                str(dur),
                "pinknoise",
                "vol",
                str(10 ** (noise_db / 20)),
            ],
            check=True,
        )
        subprocess.run(["sox", "-m", str(path), str(noise), str(noisy)], check=True)
        noisy.replace(path)
    raw.unlink(missing_ok=True)
    return path


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == SAMPLE_RATE and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def write_wav(path: Path, pcm: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.astype(np.int16).tobytes())


async def vosk_transcribe(pcm: np.ndarray, url: str = VOSK_EN) -> str:
    async with aiohttp.ClientSession() as s, s.ws_connect(url) as ws:
        await ws.send_str(json.dumps({"config": {"sample_rate": SAMPLE_RATE}}))
        data = pcm.astype(np.int16).tobytes()
        for i in range(0, len(data), 8000):
            await ws.send_bytes(data[i : i + 8000])
            await ws.receive()
        await ws.send_str('{"eof" : 1}')
        texts = []
        async for msg in ws:
            d = json.loads(msg.data)
            if d.get("text"):
                texts.append(d["text"])
        return " ".join(texts)


def word_error_rate(reference: str, hypothesis: str) -> float:
    import re

    def norm(s: str) -> list[str]:
        return re.sub(r"[^\w\s'-]", " ", s.lower()).split()

    r, h = norm(reference), norm(hypothesis)
    d = np.zeros((len(r) + 1, len(h) + 1), dtype=int)
    d[:, 0] = range(len(r) + 1)
    d[0, :] = range(len(h) + 1)
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i, j] = min(
                d[i - 1, j] + 1, d[i, j - 1] + 1, d[i - 1, j - 1] + (r[i - 1] != h[j - 1])
            )
    return float(d[len(r), len(h)] / max(1, len(r)))


@dataclass
class AgentSpeech:
    start: float
    end: float | None = None


FRAME = SAMPLE_RATE // 100  # 10 ms


@dataclass
class Caller:
    """A participant that behaves like a phone line: it streams audio continuously (speech
    when talking, digital silence otherwise) and records everything the agent publishes."""

    room_name: str
    identity: str = "caller"
    agent_audio: list[tuple[float, np.ndarray]] = field(default_factory=list)
    caller_audio: list[np.ndarray] = field(default_factory=list)
    transcriptions: list[dict[str, Any]] = field(default_factory=list)
    speech: list[AgentSpeech] = field(default_factory=list)
    room: Any = None
    _source: Any = None
    _pending: list[tuple[np.ndarray, asyncio.Future[tuple[float, float]]]] = field(
        default_factory=list
    )
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    _last_loud: float = 0.0

    async def connect(self) -> None:
        from livekit import api, rtc

        token = (
            api.AccessToken(LIVEKIT_KEY, LIVEKIT_SECRET)
            .with_identity(self.identity)
            .with_grants(api.VideoGrants(room_join=True, room=self.room_name))
            .to_jwt()
        )
        self.room = rtc.Room()

        @self.room.on("track_subscribed")
        def _on_track(track: Any, pub: Any, participant: Any) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                self._tasks.append(asyncio.create_task(self._record(track)))

        def _on_text(reader: Any, participant_identity: str) -> None:
            async def read() -> None:
                text = await reader.read_all()
                attrs = dict(reader.info.attributes or {})
                self.transcriptions.append(
                    {
                        "from": participant_identity,
                        "text": text,
                        "final": attrs.get("lk.transcription_final"),
                        "at": time.monotonic(),
                    }
                )

            self._tasks.append(asyncio.create_task(read()))

        self.room.register_text_stream_handler("lk.transcription", _on_text)
        await self.room.connect(LIVEKIT_URL, token)
        # small queue so capture timestamps match what the far end receives
        self._source = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=60)
        track = rtc.LocalAudioTrack.create_audio_track("mic", self._source)
        await self.room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        self._tasks.append(asyncio.create_task(self._pump()))

    async def _pump(self) -> None:
        from livekit import rtc

        silence = np.zeros(FRAME, dtype=np.int16)
        while True:
            if self._pending:
                pcm, fut = self._pending.pop(0)
                start = time.monotonic()
                for i in range(0, len(pcm), FRAME):
                    chunk = pcm[i : i + FRAME]
                    if len(chunk) < FRAME:
                        chunk = np.pad(chunk, (0, FRAME - len(chunk)))
                    self.caller_audio.append(chunk)
                    await self._source.capture_frame(
                        rtc.AudioFrame(chunk.tobytes(), SAMPLE_RATE, 1, FRAME)
                    )
                fut.set_result((start, time.monotonic()))
            else:
                self.caller_audio.append(silence)
                await self._source.capture_frame(
                    rtc.AudioFrame(silence.tobytes(), SAMPLE_RATE, 1, FRAME)
                )

    async def _record(self, track: Any) -> None:
        from livekit import rtc

        stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1)
        async for ev in stream:
            now = time.monotonic()
            pcm = np.frombuffer(ev.frame.data, dtype=np.int16).copy()
            self.agent_audio.append((now, pcm))
            if float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) > 300:
                if not self.speech or self.speech[-1].end is not None:
                    self.speech.append(AgentSpeech(start=now))
                self._last_loud = now
            elif self.speech and self.speech[-1].end is None and now - self._last_loud > 0.8:
                self.speech[-1].end = self._last_loud

    def say_nowait(self, pcm: np.ndarray) -> asyncio.Future[tuple[float, float]]:
        fut: asyncio.Future[tuple[float, float]] = asyncio.get_running_loop().create_future()
        self._pending.append((pcm, fut))
        return fut

    async def say(self, pcm: np.ndarray) -> tuple[float, float]:
        """Speak in real time. Returns (start, end) monotonic times of the utterance."""
        return await self.say_nowait(pcm)

    async def wait_agent_start(self, after: float, timeout: float) -> AgentSpeech:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for sp in self.speech:
                if sp.start > after:
                    return sp
            await asyncio.sleep(0.05)
        raise TimeoutError(f"agent did not start speaking within {timeout}s")

    async def wait_agent_turn(self, after: float, timeout: float) -> AgentSpeech:
        """Wait for the first agent utterance that starts after ``after`` to finish."""
        sp = await self.wait_agent_start(after, timeout)
        deadline = time.monotonic() + timeout
        while sp.end is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if sp.end is None:
            raise TimeoutError("agent did not stop speaking")
        return sp

    async def wait_quiet(self, seconds: float, timeout: float = 60) -> None:
        """Wait until the agent has been silent for ``seconds``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            busy = self.speech and self.speech[-1].end is None
            if not busy and time.monotonic() - self._last_loud > seconds:
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("agent never went quiet")

    def agent_pcm(self, start: float, end: float) -> np.ndarray:
        parts = [p for t, p in self.agent_audio if start - 0.05 <= t <= end + 0.05]
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)

    def agent_recording(self) -> np.ndarray:
        return (
            np.concatenate([p for _, p in self.agent_audio])
            if self.agent_audio
            else np.zeros(0, dtype=np.int16)
        )

    async def disconnect(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.room is not None:
            await self.room.disconnect()
