"""Self-hosted speech providers implemented on LiveKit's STT/TTS interfaces.

* ``VoskSTT`` — streaming recognition against a Vosk server (WebSocket protocol of
  alphacep/vosk-server). Real ASR that runs inside your own network; useful for on-prem
  deployments, the validation lab and offline CI. Accuracy is below commercial cloud STT.
* ``LocalTTS`` — SVOX Pico (``pico2wave``) or eSpeak NG synthesis via subprocess. Intelligible
  but robotic; intended for labs, CI and fallbacks, not customer-facing production voices.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import tempfile
import uuid
import wave
from pathlib import Path
from typing import Any, Literal

import aiohttp
import numpy as np
from livekit import rtc
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectionError,
    APIConnectOptions,
    NotGivenOr,
    stt,
    tts,
    utils,
)
from livekit.agents.language import LanguageCode

VOSK_SAMPLE_RATE = 16000


def _lang(language: NotGivenOr[str] | str | None, default: str) -> str:
    return str(language) if isinstance(language, str) and language else default


class VoskSTT(stt.STT[Any]):
    def __init__(
        self,
        *,
        url: str,
        language: str = "en",
        sample_rate: int = VOSK_SAMPLE_RATE,
        connect_timeout_s: float = 5.0,
        noise_gate_dbfs: float | None = -60.0,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True, interim_results=True, offline_recognize=True, manual_flush=True
            )
        )
        self._url = url
        self._language = language
        self._sample_rate = sample_rate
        self._connect_timeout = connect_timeout_s
        # int16 RMS below which a frame is replaced with zeros
        self._gate_rms = None if noise_gate_dbfs is None else 32768 * 10 ** (noise_gate_dbfs / 20)

    @property
    def model(self) -> str:
        return f"vosk-{self._language}"

    @property
    def provider(self) -> str:
        return "vosk"

    async def _connect(self, session: aiohttp.ClientSession) -> aiohttp.ClientWebSocketResponse:
        try:
            ws = await asyncio.wait_for(
                session.ws_connect(self._url, heartbeat=20), self._connect_timeout
            )
        except (aiohttp.ClientError, OSError, TimeoutError) as e:
            raise APIConnectionError(f"vosk unreachable at {self._url}: {e}") from e
        await ws.send_str(json.dumps({"config": {"sample_rate": self._sample_rate, "words": 1}}))
        return ws

    async def _recognize_impl(
        self, buffer: Any, *, language: NotGivenOr[str] = NOT_GIVEN, conn_options: APIConnectOptions
    ) -> stt.SpeechEvent:
        frame = rtc.combine_audio_frames(buffer)
        if frame.sample_rate != self._sample_rate or frame.num_channels != 1:
            resampler = rtc.AudioResampler(
                frame.sample_rate, self._sample_rate, num_channels=frame.num_channels
            )
            frames = [*resampler.push(frame), *resampler.flush()]
            frame = rtc.combine_audio_frames(frames)
        texts: list[str] = []
        async with aiohttp.ClientSession() as session:
            ws = await self._connect(session)
            pcm = frame.data.tobytes()
            for i in range(0, len(pcm), 6400):
                await ws.send_bytes(pcm[i : i + 6400])
                await ws.receive()  # vosk answers every chunk with a partial/result
            await ws.send_str('{"eof" : 1}')
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = json.loads(msg.data)
                if data.get("text"):
                    texts.append(data["text"])
            await ws.close()
        lang = _lang(language, self._language)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[
                stt.SpeechData(language=LanguageCode(lang), text=" ".join(texts), confidence=1.0)
            ],
        )

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> VoskStream:
        return VoskStream(self, language=_lang(language, self._language), conn_options=conn_options)


def _gate(pcm: bytes, threshold: float | None) -> bytes:
    if threshold is None or not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if float(np.sqrt(np.mean(samples * samples))) < threshold:
        return bytes(len(pcm))
    return pcm


class VoskStream(stt.RecognizeStream):
    """Streams audio to vosk-server; supports LiveKit's manual flush.

    vosk-server only finalizes on its own endpointer (adds ~1.5–2 s after the caller stops)
    or on ``eof``, which also ends the connection. When LiveKit flushes at end of turn we send
    ``eof``, emit the final transcript (empty if nothing was recognized, so the turn is never
    left waiting) and continue the stream on a fresh connection.
    """

    def __init__(self, stt_: VoskSTT, *, language: str, conn_options: APIConnectOptions) -> None:
        super().__init__(stt=stt_, conn_options=conn_options, sample_rate=stt_._sample_rate)
        self._vosk = stt_
        self._language = LanguageCode(language)
        self._speaking = False

    async def _run(self) -> None:
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    first = await self._input_ch.recv()
                except utils.aio.ChanClosed:
                    return
                if isinstance(first, self._FlushSentinel):
                    self._final("", [])  # flush with no new audio: nothing to wait for
                    continue
                # connect lazily, only once there is audio to recognize
                ws = await self._vosk._connect(session)
                try:
                    more = await self._segment(ws, first)
                finally:
                    with contextlib.suppress(Exception):
                        await ws.close()
                if not more:
                    return

    async def _segment(self, ws: aiohttp.ClientWebSocketResponse, first: rtc.AudioFrame) -> bool:
        """One vosk connection. Returns True if it ended on a flush (more audio may follow)."""
        state: dict[str, Any] = {"flushed": False, "audio_s": 0.0}
        sender = asyncio.create_task(self._send(ws, first, state))
        try:
            await self._recv(ws, state)
        finally:
            if not sender.done():
                sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
        if state["audio_s"]:
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.RECOGNITION_USAGE,
                    recognition_usage=stt.RecognitionUsage(audio_duration=state["audio_s"]),
                )
            )
        return bool(state["flushed"])

    async def _send(
        self, ws: aiohttp.ClientWebSocketResponse, first: rtc.AudioFrame, state: dict[str, Any]
    ) -> None:
        bstream = utils.audio.AudioByteStream(
            sample_rate=self._vosk._sample_rate,
            num_channels=1,
            samples_per_channel=self._vosk._sample_rate // 10,
        )
        gate = self._vosk._gate_rms

        async def send(frames: list[rtc.AudioFrame]) -> None:
            for f in frames:
                state["audio_s"] += f.duration
                await ws.send_bytes(_gate(f.data.tobytes(), gate))

        await send(bstream.write(first.data.tobytes()))
        async for data in self._input_ch:
            if isinstance(data, self._FlushSentinel):
                state["flushed"] = True
                break
            await send(bstream.write(data.data.tobytes()))
        await send(bstream.flush())
        state["eof_sent"] = True
        await ws.send_str('{"eof" : 1}')

    def _final(self, text: str, words: list[dict[str, Any]]) -> None:
        conf = (sum(w.get("conf", 0.0) for w in words) / len(words)) if words else 0.0
        if text and not self._speaking:
            self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH))
            self._speaking = True
        self._event_ch.send_nowait(
            stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[
                    stt.SpeechData(
                        language=self._language,
                        text=text,
                        confidence=conf,
                        start_time=float(words[0]["start"]) if words else 0.0,
                        end_time=float(words[-1]["end"]) if words else 0.0,
                    )
                ],
            )
        )
        if self._speaking:
            self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH))
            self._speaking = False

    async def _recv(self, ws: aiohttp.ClientWebSocketResponse, state: dict[str, Any]) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            data = json.loads(msg.data)
            partial = data.get("partial")
            if partial:
                if not self._speaking:
                    self._speaking = True
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH)
                    )
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                        alternatives=[stt.SpeechData(language=self._language, text=partial)],
                    )
                )
            elif "text" in data:
                text = data.get("text") or ""
                if text:
                    self._final(text, data.get("result") or [])
                    if state.get("eof_sent"):
                        state["final_after_eof"] = True
        # vosk closes the connection right after the final result for ``eof``; make sure a
        # flushed turn always gets a FINAL_TRANSCRIPT (possibly empty) so it never stalls.
        if state.get("eof_sent") and not state.get("final_after_eof"):
            self._final("", [])


_PICO_LANGS = {
    "en": "en-US",
    "en-US": "en-US",
    "en-GB": "en-GB",
    "fr": "fr-FR",
    "fr-CA": "fr-FR",
    "fr-FR": "fr-FR",
    "es": "es-ES",
    "de": "de-DE",
    "it": "it-IT",
}


class LocalTTS(tts.TTS[Any]):
    def __init__(
        self,
        *,
        engine: Literal["pico", "espeak"] = "pico",
        language: str = "en",
        voice: str | None = None,
        rate_wpm: int | None = None,
    ) -> None:
        binary = "pico2wave" if engine == "pico" else "espeak-ng"
        if shutil.which(binary) is None:
            raise ValueError(f"{binary} is not installed")
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=16000 if engine == "pico" else 22050,
            num_channels=1,
        )
        self._engine = engine
        self._language = language
        self._voice = voice
        self._rate = rate_wpm

    @property
    def model(self) -> str:
        return self._engine

    @property
    def provider(self) -> str:
        return "local"

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> LocalChunkedStream:
        return LocalChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def command(self, text: str, out_path: str) -> list[str]:
        if self._engine == "pico":
            return [
                "pico2wave",
                "-l",
                _PICO_LANGS.get(self._language, "en-US"),
                "-w",
                out_path,
                text,
            ]
        voice = self._voice or self._language.split("-")[0]
        cmd = ["espeak-ng", "-v", voice, "-w", out_path]
        if self._rate:
            cmd += ["-s", str(self._rate)]
        return [*cmd, text]


class LocalChunkedStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        local: LocalTTS = self._tts  # type: ignore[assignment]
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            proc = await asyncio.create_subprocess_exec(
                *local.command(self.input_text[:2000], path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(
                    proc.communicate(), timeout=self._conn_options.timeout
                )
            except TimeoutError as e:
                proc.kill()
                raise APIConnectionError("local TTS timed out") from e
            if proc.returncode != 0:
                raise APIConnectionError(f"local TTS failed: {err.decode()[:200]}")
            raw = await asyncio.to_thread(Path(path).read_bytes)
            with wave.open(io.BytesIO(raw)) as w:
                pcm = w.readframes(w.getnframes())
                sr = w.getframerate()
            output_emitter.initialize(
                request_id=uuid.uuid4().hex, sample_rate=sr, num_channels=1, mime_type="audio/pcm"
            )
            output_emitter.push(pcm)
            output_emitter.flush()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)
