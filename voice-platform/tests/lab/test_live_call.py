"""A complete voice conversation through a real LiveKit server, with real models.

Path under test (nothing mocked):

    caller (livekit.rtc, Opus/WebRTC) → LiveKit SFU → ``vp-worker start`` job process →
    Silero VAD + Vosk streaming STT → VAD end-of-turn → Qwen 2.5 3B via Ollama
    (OpenAI-compatible, tool calling) → sandbox calendar tool → SVOX Pico TTS →
    LiveKit SFU → caller

The caller's speech is synthesized; what the agent says is captured from the subscribed
track and re-transcribed with Vosk to check it is intelligible. Persistence (conversation,
transcript events, tool executions, latency metrics) is read back from Postgres.

Not covered here: PSTN/SIP ingress (see test_sip_lab.py), commercial providers.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from tests.lab.support import (
    LAB_MODEL,
    LIVEKIT_KEY,
    LIVEKIT_SECRET,
    LIVEKIT_URL,
    OLLAMA_URL,
    VOSK_EN,
    Caller,
    read_wav,
    synthesize,
    vosk_transcribe,
    word_error_rate,
    write_wav,
)

from voice_platform.core.db import DbScope

pytestmark = pytest.mark.lab

GREETING = "Hi, thanks for calling Maple Dental. How can I help you today?"
WEEKDAYS = {d: ["09:00-17:00"] for d in ("mon", "tue", "wed", "thu", "fri")}


def lab_config(**extra: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "name": "Lab front desk",
        "language": "en",
        # Tenant instructions written as a procedure: with the longer descriptive wording,
        # Qwen 2.5 3B narrated "let me check" instead of calling the tool (0/5 in probing);
        # this wording measured 5/5. The benchmark quantifies this on many scenarios.
        "instructions": (
            "When a caller asks about appointments or availability: 1) call "
            "check_availability immediately, without replying first; 2) offer only the times "
            "it returns. For other questions, answer from the business facts."
        ),
        "business": {
            "name": "Maple Dental",
            "timezone": "America/Toronto",
            "description": (
                "Family dental clinic offering cleanings, fillings, whitening, crowns, "
                "orthodontic consultations and emergency visits. Free parking behind the "
                "building. New patients welcome."
            ),
            "hours": {"schedule": WEEKDAYS},
            "appointments": {
                "default_duration_min": 30,
                "min_notice_min": 0,
                "hours": {"schedule": WEEKDAYS},
            },
        },
        "greeting": {"mode": "say", "text": GREETING},
        "pipeline": {
            "mode": "cascaded",
            "stt": {"provider": "vosk", "language": "en", "options": {"url": VOSK_EN}},
            "llm": {
                "provider": "openai-compatible",
                "model": LAB_MODEL,
                "temperature": 0.2,
                "options": {"base_url": OLLAMA_URL},
                "connection": {"timeout_s": 90, "max_retries": 0},
            },
            "tts": {"provider": "pico", "language": "en-US"},
            "vad": {"provider": "silero"},
            "turn_detection": {"mode": "vad"},
        },
        "turn_handling": {
            "preemptive_generation": False,
            "endpointing": {"min_delay_s": 0.6, "max_delay_s": 3.0},
            "interruption": {"min_duration_s": 0.4, "false_interruption_timeout_s": None},
        },
        "conversation": {"post_call_summary": False, "prewarm_llm_prompt": True},
        "tools": {"enabled": ["check_availability"]},
        "integrations": {"calendar": "lab_calendar"},
    }
    cfg.update(extra)
    return cfg


async def create_lab_agent(db: Any, config: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID]:
    from voice_platform.config.service import AgentService
    from voice_platform.core.models import Integration, Tenant

    async with db.session(DbScope.system()) as s:
        tenant = Tenant(slug=f"lab-{uuid.uuid4().hex[:8]}", name="Lab tenant")
        s.add(tenant)
        await s.flush()
        tenant_id = tenant.id
    async with db.session(DbScope.for_tenant(tenant_id)) as s:
        s.add(Integration(tenant_id=tenant_id, name="lab_calendar", kind="sandbox", config={}))
        svc = AgentService(s, tenant_id, None)
        agent, version = await svc.create_agent(
            slug="front-desk", name="Front desk", description="lab", config=config
        )
        await svc.publish(version.id)
        await svc.deploy(agent.id, "staging", version.id)
        return tenant_id, agent.id


async def dispatch(agent_name: str, room: str, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    from livekit import api

    from voice_platform.telephony.outbound import WebDispatch

    meta = WebDispatch(tenant_id=str(tenant_id), agent_id=str(agent_id), environment="staging")
    http_url = LIVEKIT_URL.replace("ws://", "http://").replace("wss://", "https://")
    async with api.LiveKitAPI(http_url, LIVEKIT_KEY, LIVEKIT_SECRET) as lk:
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=agent_name, room=room, metadata=meta.to_json()
            )
        )


async def conversation_rows(db: Any, tenant_id: uuid.UUID) -> dict[str, Any]:
    async with db.session(DbScope.for_tenant(tenant_id)) as s:
        conv = (await s.execute(sa.text("select * from conversations"))).mappings().one_or_none()
        if conv is None:
            return {}
        events = (
            (
                await s.execute(
                    sa.text(
                        "select type, payload from conversation_events where conversation_id=:c "
                        "order by seq"
                    ),
                    {"c": conv["id"]},
                )
            )
            .mappings()
            .all()
        )
        tools = (
            (
                await s.execute(
                    sa.text("select tool_name, status, request, response from tool_executions")
                )
            )
            .mappings()
            .all()
        )
        return {
            "conversation": dict(conv),
            "events": [dict(e) for e in events],
            "tools": [dict(t) for t in tools],
        }


async def wait_for_status(db: Any, tenant_id: uuid.UUID, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    rows: dict[str, Any] = {}
    while time.monotonic() < deadline:
        rows = await conversation_rows(db, tenant_id)
        conv = rows.get("conversation")
        if conv and conv["status"] not in ("initiated", "ringing", "in_progress", "active"):
            return rows
        await asyncio.sleep(0.5)
    return rows


async def test_live_voice_conversation(
    db: Any, worker: dict[str, Any], evidence_dir: Path, tmp_path: Path
) -> None:
    tenant_id, agent_id = await create_lab_agent(db, lab_config())
    room = f"lab-{uuid.uuid4().hex[:8]}"
    caller = Caller(room_name=room, identity="web-caller")
    await caller.connect()
    t0 = time.monotonic()
    await dispatch(worker["agent_name"], room, tenant_id, agent_id)
    report: dict[str, Any] = {"room": room, "model": LAB_MODEL, "turns": []}

    try:
        # 1) greeting: job start + session build + TTS, heard by the caller
        greet = await caller.wait_agent_turn(t0, timeout=90)
        report["dispatch_to_greeting_s"] = round(greet.start - t0, 2)
        heard = await vosk_transcribe(caller.agent_pcm(greet.start, greet.end or greet.start))
        report["greeting_heard"] = heard
        report["greeting_wer"] = word_error_rate(GREETING, heard)
        assert report["greeting_wer"] <= 0.35, heard
        await caller.wait_quiet(0.8)

        async def turn(text: str, *, timeout: float = 120) -> dict[str, Any]:
            pcm = read_wav(synthesize(text, tmp_path / f"u{len(report['turns'])}.wav"))
            _, said_end = await caller.say(pcm)
            reply = await caller.wait_agent_turn(said_end, timeout=timeout)
            # a caller told "one moment" waits; give follow-through time to act
            await caller.wait_quiet(float(os.environ.get("VP_LAB_TURN_GAP_S", "3.0")))
            last = caller.speech[-1]
            audio = caller.agent_pcm(reply.start, last.end or reply.end or reply.start)
            t = {
                "user": text,
                "response_latency_s": round(reply.start - said_end, 2),
                "agent_heard": await vosk_transcribe(audio),
                "agent_audio_s": round(len(audio) / 16000, 2),
            }
            report["turns"].append(t)
            return t

        # 2) factual question answered from the configured business facts
        t1 = await turn("What time do you open on Monday?")
        assert t1["agent_audio_s"] > 0.5
        assert any(w in t1["agent_heard"] for w in ("nine", "9", "monday", "open")), t1

        # 3) availability → sandbox calendar tool → spoken slots. On CPU-only inference the
        # model may first say "one moment" (follow-through then makes the tool call), so wait
        # for the utterance that carries the times.
        t2 = await turn("Do you have any openings next Monday?", timeout=180)
        if not any(w in t2["agent_heard"] for w in (" am", " pm", "nine", "ten", "eleven")):
            answer = await caller.wait_agent_turn(caller.speech[-1].end or 0, timeout=90)
            await caller.wait_quiet(1.5)
            heard = await vosk_transcribe(caller.agent_pcm(answer.start, answer.end or 0))
            t2["agent_heard_after_hold"] = heard
        heard_all = t2["agent_heard"] + " " + t2.get("agent_heard_after_hold", "")
        assert any(w in heard_all for w in (" am", " pm", "nine", "ten", "eleven")), t2

        # 4) barge-in: talk over a long answer and measure how fast the agent yields
        pcm_q = read_wav(
            synthesize(
                "Can you describe all of the services you offer, in detail?", tmp_path / "q.wav"
            )
        )
        _, q_end = await caller.say(pcm_q)
        long_reply = await caller.wait_agent_start(q_end, timeout=120)
        await asyncio.sleep(1.0)
        pcm_i = read_wav(synthesize("Sorry, stop, actually never mind.", tmp_path / "i.wav"))
        interrupt_at, _ = await caller.say(pcm_i)
        stopped = long_reply.end
        deadline = time.monotonic() + 10
        while stopped is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            stopped = long_reply.end
        report["interruption"] = {
            "agent_spoke_before_barge_in_s": round(interrupt_at - long_reply.start, 2),
            "agent_stopped_after_s": round((stopped or 99) - interrupt_at, 2),
        }
        assert stopped is not None and stopped - interrupt_at < 2.0, report["interruption"]
        await caller.wait_quiet(1.5, timeout=150)
    finally:
        write_wav(tmp_path / "agent.wav", caller.agent_recording())
        await caller.disconnect()

    # 5) caller hangs up → job finalizes the conversation record
    rows = await wait_for_status(db, tenant_id, timeout=60)
    conv = rows["conversation"]
    report["conversation"] = {
        k: conv[k] for k in ("status", "end_reason", "channel", "duration_s", "environment")
    }
    report["metrics"] = conv["metrics"]
    transcripts = [e["payload"] for e in rows["events"] if e["type"] == "transcript"]
    report["transcript"] = [
        {"role": t["role"], "text": t["text"], "interrupted": t.get("interrupted")}
        for t in transcripts
    ]
    tool_events = [e["payload"] for e in rows["events"] if e["type"] == "tool"]
    report["tool_calls"] = [
        {"tool": t["tool"], "ok": t["ok"], "args": t["args"], "error": t["error"]}
        for t in tool_events
    ]
    report["latency_events"] = [e["payload"] for e in rows["events"] if e["type"] == "turn_latency"]
    report["event_types"] = dict(Counter(e["type"] for e in rows["events"]))
    report["follow_through"] = [
        e["payload"] for e in rows["events"] if e["type"] == "follow_through"
    ]
    report["recorded_at"] = datetime.now(UTC).isoformat()
    report["worker_log_errors"] = [
        line[:300]
        for line in Path(worker["log"]).read_text().splitlines()
        if '"level": "error"' in line or "Traceback" in line
    ][:20]
    (evidence_dir / "live_call.json").write_text(json.dumps(report, indent=2, default=str))

    assert conv["status"] == "completed" and conv["channel"] == "web"
    users = [t["text"] for t in transcripts if t["role"] == "user"]
    assistants = [t for t in transcripts if t["role"] == "assistant"]
    assert any("monday" in u.lower() for u in users), users  # STT → persisted transcript
    assert len(assistants) >= 3
    assert any(t.get("interrupted") for t in assistants), "barge-in not recorded"
    assert any(t["tool"] == "check_availability" and t["ok"] for t in tool_events), report[
        "tool_calls"
    ]
    assert report["latency_events"], "no per-turn latency metrics persisted"
