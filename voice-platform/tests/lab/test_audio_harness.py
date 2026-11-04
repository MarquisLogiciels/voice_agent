"""Audio-level simulation: recognition accuracy and turn boundaries under controlled conditions.

Runs the platform's own speech components through LiveKit's stream interfaces — the Vosk
provider (``VoskSTT.stream()``, the same code the worker uses, including the noise gate and
manual flush) and Silero VAD (``silero.VAD.stream()``) — on synthesized callers:

* voices/accents: SVOX Pico (en-US, fr-FR) and eSpeak NG (en-US, Scottish, Caribbean English;
  France, Belgian French). **No Québec French synthesizer is available offline**, so French
  is measured with Québec vocabulary ("c'est correct", "changé d'idée") spoken by European
  French voices — real Québec accents are not covered.
* speaking rate (×0.8, ×1.3), pink background noise at 20/10/5 dB SNR, and a telephone channel
  (8 kHz G.711 µ-law round trip, as on a SIP call);
* silence and noise-only input (false-trigger rate with and without the noise gate);
* turn boundaries: true end of speech (from the waveform) → VAD end-of-speech, and flush →
  final transcript in real time.

Synthetic speech is far cleaner and more regular than real callers: treat these numbers as a
regression baseline and a relative comparison between conditions, not as production accuracy.
Writes ``docs/evidence/audio_harness.json``.
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from tests.lab.sipua import resample, ulaw_decode, ulaw_encode
from tests.lab.support import SAMPLE_RATE, VOSK_EN, VOSK_FR, port_open, read_wav, synthesize

pytestmark = pytest.mark.lab

EN = [
    "Hi, I would like to book a cleaning for next Tuesday morning.",
    "Do you have anything available on Friday afternoon?",
    "My name is Ana Roy and I am an existing patient.",
    "Can I speak to a person, please?",
    "What are your opening hours on Saturday?",
    "I need to cancel my appointment for tomorrow.",
    "My phone number is five one four, five five five, zero one nine nine.",
    "Actually, no, I changed my mind. Please don't book anything.",
    "Is there parking near the clinic?",
    "Yes, that's correct, please go ahead and book it.",
]
FR = [
    "Bonjour, j'aimerais prendre un rendez-vous pour un nettoyage mardi prochain.",
    "Est-ce que vous avez de la disponibilité vendredi après-midi?",
    "Je voudrais parler à quelqu'un, s'il vous plaît.",
    "Quelles sont vos heures d'ouverture le samedi?",
    "Je dois annuler mon rendez-vous de demain.",
    "Mon numéro de téléphone est le cinq un quatre, cinq cinq cinq, zéro un neuf neuf.",
    "Non, finalement, j'ai changé d'idée.",
    "Oui, c'est correct, vous pouvez le réserver.",
]


def norm_words(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[-'’]", " ", s)
    return re.sub(r"[^\w\s]", " ", s).split()


def wer(ref: str, hyp: str) -> float:
    r, h = norm_words(ref), norm_words(hyp)
    d = np.zeros((len(r) + 1, len(h) + 1), dtype=int)
    d[:, 0], d[0, :] = range(len(r) + 1), range(len(h) + 1)
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i, j] = min(
                d[i - 1, j] + 1, d[i, j - 1] + 1, d[i - 1, j - 1] + (r[i - 1] != h[j - 1])
            )
    return float(d[-1, -1] / max(1, len(r)))


# --- conditions ------------------------------------------------------------------------------
def espeak(text: str, voice: str, path: Path) -> np.ndarray:
    raw = path.with_suffix(".es.wav")
    subprocess.run(["espeak-ng", "-v", voice, "-s", "160", "-w", str(raw), text], check=True)
    subprocess.run(["sox", str(raw), "-r", str(SAMPLE_RATE), "-c", "1", "-b", "16", str(path)],
                   check=True)  # fmt: skip
    return read_wav(path)


def with_noise(pcm: np.ndarray, snr_db: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    white = rng.normal(0, 1, len(pcm) + 64)
    # pink-ish noise: 1/f via cumulative sum of white noise, high-passed by differencing a mean
    pink = np.convolve(white, np.ones(32) / 32, mode="same")[: len(pcm)]
    speech = pcm.astype(np.float64)
    p_speech = np.mean(speech[np.abs(speech) > 200] ** 2) if np.any(np.abs(speech) > 200) else 1.0
    pink *= np.sqrt(p_speech / (10 ** (snr_db / 10)) / np.mean(pink**2))
    return np.clip(speech + pink, -32768, 32767).astype(np.int16)


def telephone(pcm: np.ndarray) -> np.ndarray:
    narrow = resample(pcm, SAMPLE_RATE, 8000)
    return resample(ulaw_decode(ulaw_encode(narrow)), 8000, SAMPLE_RATE)


def pad(pcm: np.ndarray, before_s: float = 0.5, after_s: float = 1.5) -> np.ndarray:
    z = lambda s: np.zeros(int(SAMPLE_RATE * s), dtype=np.int16)  # noqa: E731
    return np.concatenate([z(before_s), pcm, z(after_s)])


def speech_bounds(pcm: np.ndarray, threshold: int = 300) -> tuple[float, float]:
    idx = np.flatnonzero(np.abs(pcm.astype(np.int32)) > threshold)
    return float(idx[0] / SAMPLE_RATE), float(idx[-1] / SAMPLE_RATE)


# --- engines ---------------------------------------------------------------------------------
def frames(pcm: np.ndarray, ms: int = 20) -> list[Any]:
    from livekit import rtc

    n = SAMPLE_RATE * ms // 1000
    out = []
    for i in range(0, len(pcm), n):
        chunk = pcm[i : i + n]
        if len(chunk) < n:
            chunk = np.pad(chunk, (0, n - len(chunk)))
        out.append(rtc.AudioFrame(chunk.tobytes(), SAMPLE_RATE, 1, n))
    return out


async def recognize(
    url: str, pcm: np.ndarray, *, gate: float | None = -60.0, drop: tuple[str, ...] = ("the",)
) -> str:
    """Whole utterance through VoskStream (fast, not real time)."""
    from livekit.agents import stt

    from voice_platform.providers.local import VoskSTT

    stream = VoskSTT(url=url, noise_gate_dbfs=gate, drop_lone_words=drop).stream()
    for f in frames(pcm):
        stream.push_frame(f)
    stream.end_input()
    texts = [
        ev.alternatives[0].text
        async for ev in stream
        if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT and ev.alternatives[0].text
    ]
    return " ".join(texts)


async def flush_latency(url: str, pcm: np.ndarray) -> float:
    """Real-time push; time from flush (end of turn) to the final transcript."""
    from livekit.agents import stt

    from voice_platform.providers.local import VoskSTT

    stream = VoskSTT(url=url).stream()
    got: asyncio.Future[float] = asyncio.get_running_loop().create_future()

    async def read() -> None:
        async for ev in stream:
            if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT and not got.done():
                got.set_result(time.monotonic())

    reader = asyncio.create_task(read())
    for f in frames(pcm):
        stream.push_frame(f)
        await asyncio.sleep(0.02)
    t = time.monotonic()
    stream.flush()
    done = await asyncio.wait_for(got, 15)
    stream.end_input()
    await reader
    return done - t


async def vad_events(vad: Any, pcm: np.ndarray) -> list[tuple[str, float]]:
    from livekit.agents import vad as lk_vad

    stream = vad.stream()
    for f in frames(pcm, 10):
        stream.push_frame(f)
    stream.end_input()
    out = []
    async for ev in stream:
        if ev.type in (lk_vad.VADEventType.START_OF_SPEECH, lk_vad.VADEventType.END_OF_SPEECH):
            out.append((ev.type.value, ev.samples_index / SAMPLE_RATE))
    return out


def summarize(xs: list[float]) -> dict[str, float]:
    xs = sorted(xs)
    return {
        "n": len(xs),
        "mean": round(statistics.fmean(xs), 3),
        "p50": round(xs[len(xs) // 2], 3),
        "p90": round(xs[min(len(xs) - 1, int(len(xs) * 0.9))], 3),
        "max": round(xs[-1], 3),
    }


# --------------------------------------------------------------------------------------------
async def test_audio_harness(tmp_path: Path, evidence_dir: Path) -> None:
    if not port_open("127.0.0.1", int(VOSK_FR.rsplit(":", 1)[1])):
        pytest.skip("French Vosk server not running")
    from livekit.plugins import silero

    report: dict[str, Any] = {"conditions": {}, "utterances": []}

    def add(cond: str, lang: str, ref: str, hyp: str) -> None:
        w = wer(ref, hyp)
        report["utterances"].append({"cond": cond, "lang": lang, "ref": ref, "hyp": hyp, "wer": w})
        report["conditions"].setdefault(cond, []).append(w)

    # 1) recognition accuracy by condition
    for i, text in enumerate(EN):
        base = read_wav(synthesize(text, tmp_path / f"en{i}.wav"))
        variants = {
            "en/pico": base,
            "en/pico x0.8": read_wav(synthesize(text, tmp_path / f"en{i}s.wav", speed=0.8)),
            "en/pico x1.3": read_wav(synthesize(text, tmp_path / f"en{i}f.wav", speed=1.3)),
            "en/pico snr20": with_noise(base, 20, i),
            "en/pico snr10": with_noise(base, 10, i),
            "en/pico snr5": with_noise(base, 5, i),
            "en/pico telephone": telephone(base),
            "en/espeak en-us": espeak(text, "en-us", tmp_path / f"en{i}a.wav"),
            "en/espeak scottish": espeak(text, "en-gb-scotland", tmp_path / f"en{i}b.wav"),
            "en/espeak caribbean": espeak(text, "en-029", tmp_path / f"en{i}c.wav"),
        }
        for cond, pcm in variants.items():
            add(cond, "en", text, await recognize(VOSK_EN, pad(pcm)))
    for i, text in enumerate(FR):
        base = read_wav(synthesize(text, tmp_path / f"fr{i}.wav", language="fr-FR"))
        variants = {
            "fr/pico": base,
            "fr/pico snr10": with_noise(base, 10, i),
            "fr/pico telephone": telephone(base),
            "fr/espeak fr-fr": espeak(text, "fr-fr", tmp_path / f"fr{i}a.wav"),
            "fr/espeak fr-be": espeak(text, "fr-be", tmp_path / f"fr{i}b.wav"),
        }
        for cond, pcm in variants.items():
            add(cond, "fr", text, await recognize(VOSK_FR, pad(pcm)))
    report["wer_by_condition"] = {
        c: {"mean": round(statistics.fmean(v), 3), "n": len(v)}
        for c, v in report["conditions"].items()
    }
    del report["conditions"]

    # 2) turn boundaries (Silero VAD, LiveKit default parameters) on clean and noisy speech
    vad = silero.VAD.load()
    endpoint, onset, misses = [], [], 0
    for i, text in enumerate(EN):
        for noisy in (False, True):
            pcm = pad(read_wav(synthesize(text, tmp_path / f"v{i}.wav")))
            start, end = speech_bounds(pcm)
            if noisy:
                pcm = with_noise(pcm, 10, i)
            evs = await vad_events(vad, pcm)
            sos = [t for k, t in evs if k == "start_of_speech"]
            eos = [t for k, t in evs if k == "end_of_speech"]
            if not (sos and eos):
                misses += 1
                continue
            onset.append(sos[0] - start)
            endpoint.append(eos[-1] - end)
    report["vad"] = {
        "speech_start_detection_s": summarize(onset),
        "end_of_speech_detection_s": summarize(endpoint),
        "utterances_without_both_events": misses,
        "note": "audio time from the waveform's true boundary to the VAD event; includes "
        "Silero's min_silence_duration",
    }

    # 3) flush → final transcript, pushing audio in real time (what a live turn waits for)
    lat = [
        await flush_latency(VOSK_EN, pad(read_wav(synthesize(t, tmp_path / f"l{i}.wav")), 0.3, 0.6))
        for i, t in enumerate(EN[:5])
    ]
    report["stt_flush_to_final_s"] = summarize(lat)

    # 4) false triggers on non-speech input, with and without the noise gate
    rng = np.random.default_rng(7)
    junk = {
        "silence 10s": np.zeros(SAMPLE_RATE * 10, dtype=np.int16),
        "noise -50dBFS 10s": (rng.normal(0, 32768 * 10 ** (-50 / 20), SAMPLE_RATE * 10)).astype(
            np.int16
        ),
        "noise -35dBFS 10s": (rng.normal(0, 32768 * 10 ** (-35 / 20), SAMPLE_RATE * 10)).astype(
            np.int16
        ),
    }
    report["false_triggers"] = {}
    for name, pcm in junk.items():
        report["false_triggers"][name] = {
            "platform_defaults": await recognize(VOSK_EN, pcm),
            "no_gate_no_filter": await recognize(VOSK_EN, pcm, gate=None, drop=()),
            "gate_only": await recognize(VOSK_EN, pcm, drop=()),
            "vad_segments": len(
                [1 for k, _ in await vad_events(vad, pcm) if k == "start_of_speech"]
            ),
        }

    (evidence_dir / "audio_harness.json").write_text(json.dumps(report, indent=2))

    w = report["wer_by_condition"]
    # regression guards, not accuracy claims (see module docstring)
    assert w["en/pico"]["mean"] <= 0.15, w
    assert w["fr/pico"]["mean"] <= 0.35, w
    # the wideband English model degrades on 8 kHz phone audio (measured ~0.17-0.20)
    assert w["en/pico telephone"]["mean"] <= 0.3, w
    assert all(v["platform_defaults"] == "" for v in report["false_triggers"].values())
    assert report["vad"]["utterances_without_both_events"] == 0
