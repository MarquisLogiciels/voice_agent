"""Telephony through a real LiveKit SIP service (livekit/sip), with scripted SIP endpoints.

Topology (all on loopback; nothing leaves the machine and no real number can be dialed —
the worker's outbound allowlist is the lab prefix +1514555):

    "PSTN" phone (tests/lab/sipua.py, :5062)  ──INVITE──▶  LiveKit SIP (:5060)
          ▲   (REFER: dials the transfer target)               │ room + SIP participant
          │                                                    ▼
    "office" lines (:5070)  ◀──INVITE (outbound trunk)──  vp-worker (agent)

Each test provisions its own number with the platform's ``SipProvisioner`` (inbound trunk +
dispatch rule) and removes it afterwards.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import pytest_asyncio
import sqlalchemy as sa
from tests.lab.sipua import Call, LinePolicy, SipEndpoint, wait_for
from tests.lab.support import (
    LIVEKIT_KEY,
    LIVEKIT_SECRET,
    LIVEKIT_URL,
    read_wav,
    synthesize,
    vosk_transcribe,
    word_error_rate,
)
from tests.lab.test_live_call import GREETING, conversation_rows, create_lab_agent, lab_config

from voice_platform.core.db import DbScope

pytestmark = pytest.mark.lab

LIVEKIT_SIP = ("127.0.0.1", 5060)
CALLER = "+15145550199"
OFFICE_ANSWER = "+15145550177"  # answers and says who they are
OFFICE_BUSY = "+15145550178"
OFFICE_NO_ANSWER = "+15145550179"
OFFICE_HELLO = "+15145550180"  # answers with "Hello, who is calling please?"
SAM = "Hi, this is Sam at the front desk. How can I help?"
CALLEE_HELLO = "Hello, who is calling please?"


def http_url() -> str:
    return LIVEKIT_URL.replace("ws://", "http://").replace("wss://", "https://")


@pytest.fixture(scope="module")
def speech(tmp_path_factory: pytest.TempPathFactory) -> dict[str, np.ndarray]:
    d = tmp_path_factory.mktemp("speech")
    lines = {
        "sam": SAM,
        "hello": CALLEE_HELLO,
        "hours": "What time do you open on Monday?",
        "person": "Can I speak to a person, please?",
        "thanks": "Okay, thank you. Goodbye.",
    }
    return {k: read_wav(synthesize(v, d / f"{k}.wav")) for k, v in lines.items()}


@pytest_asyncio.fixture(scope="module")
async def phones(speech: dict[str, np.ndarray]) -> AsyncIterator[dict[str, SipEndpoint]]:
    office = await SipEndpoint(
        "office",
        5070,
        lines={
            OFFICE_ANSWER: LinePolicy("answer", ring_s=1.0, say=speech["sam"]),
            OFFICE_BUSY: LinePolicy("busy"),
            OFFICE_NO_ANSWER: LinePolicy("no_answer"),
            "*": LinePolicy("answer", ring_s=1.0, say=speech["hello"]),
        },
    ).start()
    # tel: Refer-To targets are routed to the office lines, like a carrier would
    pstn = await SipEndpoint(
        "pstn", 5062, carrier=("127.0.0.1", 5070), transfer_timeout_s=12
    ).start()
    yield {"office": office, "pstn": pstn}
    office.close()
    pstn.close()


@pytest_asyncio.fixture
async def lk() -> AsyncIterator[Any]:
    from livekit import api

    async with api.LiveKitAPI(http_url(), LIVEKIT_KEY, LIVEKIT_SECRET) as client:
        yield client


@pytest_asyncio.fixture
async def outbound_trunk(lk: Any) -> AsyncIterator[str]:
    from voice_platform.telephony.provisioning import SipProvisioner

    prov = SipProvisioner(lk, agent_name="unused")
    trunk_id = await prov.create_outbound_trunk(
        label="lab-office", address="127.0.0.1:5070", numbers=["+15145550100"],
        auth_username=None, auth_password=None,
    )  # fmt: skip
    yield trunk_id
    await prov._safe_delete_trunk(trunk_id)


async def provision(
    db: Any, lk: Any, worker: dict[str, Any], config: dict[str, Any], outbound_trunk: str
) -> dict[str, Any]:
    """Tenant + agent + phone number, provisioned on LiveKit SIP like production."""
    from voice_platform.core.models import PhoneNumber
    from voice_platform.telephony.provisioning import SipProvisioner

    tenant_id, agent_id = await create_lab_agent(db, config)
    e164 = f"+1514555{uuid.uuid4().int % 9000 + 1000:04d}"
    prov = SipProvisioner(lk, agent_name=worker["agent_name"])
    p = await prov.provision_inbound(e164=e164, label="lab", krisp=False)
    async with db.session(DbScope.for_tenant(tenant_id)) as s:
        s.add(
            PhoneNumber(
                tenant_id=tenant_id,
                e164=e164,
                agent_id=agent_id,
                environment="staging",
                provider="lab",
                livekit_inbound_trunk_id=p.inbound_trunk_id,
                livekit_dispatch_rule_id=p.dispatch_rule_id,
                livekit_outbound_trunk_id=outbound_trunk,
            )
        )
    return {"tenant_id": tenant_id, "agent_id": agent_id, "e164": e164, "prov": prov, "p": p}


async def deprovision(n: dict[str, Any]) -> None:
    await n["prov"].deprovision(
        inbound_trunk_id=n["p"].inbound_trunk_id, dispatch_rule_id=n["p"].dispatch_rule_id
    )


def transfer_config(destinations: list[dict[str, Any]], default: str) -> dict[str, Any]:
    return lab_config(
        transfer={"enabled": True, "destinations": destinations, "default_destination": default}
    )


async def next_utterance(call: Call, after: float, timeout: float) -> tuple[float, float]:
    """Wait for the far end's next utterance starting after ``after``; returns (start, end)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        segs = [s for s in call.rtp.speech_segments() if s[0] > after]
        if segs and time.monotonic() - segs[0][1] > 1.0:
            return segs[0][0], segs[0][1]
        await asyncio.sleep(0.1)
    raise TimeoutError("no utterance from the far end")


async def settle(call: Call, timeout: float = 150, quiet_s: float = 20) -> str:
    """Wait until the transfer outcome is final: transferred, call ended, or every REFER so
    far failed and the agent sent no further REFER (fallback) for ``quiet_s``."""
    deadline = time.monotonic() + timeout
    failed_at: float | None = None
    seen = 0
    while time.monotonic() < deadline:
        if call.ended.is_set():
            return "ended"
        tc = call.transfer_call
        if tc is not None and tc.established.is_set():
            return "transferred"
        outcomes = list(call.transfer_outcomes)
        if outcomes and len(outcomes) != seen:
            seen, failed_at = len(outcomes), time.monotonic()
        if failed_at is not None and time.monotonic() - failed_at > quiet_s:
            return "failed"
        await asyncio.sleep(0.2)
    return "timeout"


async def heard(call: Call, seg: tuple[float, float]) -> str:
    return await vosk_transcribe(call.rtp.recording(seg[0] - 0.1, seg[1] + 0.1))


async def wait_final(db: Any, tenant_id: uuid.UUID, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    rows: dict[str, Any] = {}
    while time.monotonic() < deadline:
        rows = await conversation_rows(db, tenant_id)
        c = rows.get("conversation")
        if c and c["status"] not in ("initiated", "ringing", "in_progress", "active"):
            return rows
        await asyncio.sleep(0.5)
    return rows


def evidence(evidence_dir: Path, name: str, data: dict[str, Any]) -> None:
    path = evidence_dir / "sip_lab.json"
    all_ = json.loads(path.read_text()) if path.exists() else {}
    all_[name] = data
    path.write_text(json.dumps(all_, indent=2, default=str))


def sip_lines(ep: SipEndpoint, call_ids: set[str]) -> list[str]:
    return [f"{t['dir']} {t['line']}" for t in ep.trace if t.get("call_id") in call_ids]


# ------------------------------------------------------------------------------------------
async def test_inbound_pstn_call(
    db: Any, lk: Any, worker: dict[str, Any], phones: dict[str, SipEndpoint],
    speech: dict[str, np.ndarray], outbound_trunk: str, evidence_dir: Path,
) -> None:  # fmt: skip
    n = await provision(db, lk, worker, lab_config(), outbound_trunk)
    pstn = phones["pstn"]
    try:
        t0 = time.monotonic()
        call = await pstn.call(f"sip:{n['e164']}@127.0.0.1:5060", from_user=CALLER, via=LIVEKIT_SIP)
        await asyncio.wait_for(call.established.wait(), 60)
        answered_s = time.monotonic() - t0
        greet = await next_utterance(call, t0, 30)
        greeting = await heard(call, greet)
        _start, end = await call.rtp.say(speech["hours"])
        answer = await next_utterance(call, end, 120)
        answer_text = await heard(call, answer)
        pstn.hangup(call)
        rows = await wait_final(db, n["tenant_id"])
    finally:
        await deprovision(n)
    conv = rows["conversation"]
    users = [e["payload"]["text"] for e in rows["events"]
             if e["type"] == "transcript" and e["payload"]["role"] == "user"]  # fmt: skip
    data = {
        "invite_to_200ok_s": round(answered_s, 2),
        "greeting_heard": greeting,
        "greeting_wer": word_error_rate(GREETING, greeting),
        "answer_heard": answer_text,
        "response_latency_s": round(answer[0] - end, 2),
        "user_transcripts": users,
        "conversation": {
            k: conv[k]
            for k in (
                "status",
                "direction",
                "channel",
                "caller_number",
                "callee_number",
                "end_reason",
                "duration_s",
            )
        },
        "sip": sip_lines(pstn, {call.call_id}),
    }
    evidence(evidence_dir, "inbound", data)
    assert data["greeting_wer"] <= 0.4, data  # 8 kHz G.711 is harder for Vosk than wideband
    assert any(w in answer_text for w in ("nine", "monday", "open")), data
    assert conv["status"] == "completed" and conv["direction"] == "inbound"
    assert conv["channel"] == "phone"
    assert conv["caller_number"] == CALLER and conv["callee_number"] == n["e164"]
    assert any("monday" in u for u in users)


async def _transfer_call(
    db: Any, lk: Any, worker: dict[str, Any], phones: dict[str, SipEndpoint],
    speech: dict[str, np.ndarray], outbound_trunk: str, config: dict[str, Any],
    *, hang_up_after_refer: bool = False,
) -> dict[str, Any]:  # fmt: skip
    """Caller asks for a person; returns what happened on every leg."""
    n = await provision(db, lk, worker, config, outbound_trunk)
    pstn = phones["pstn"]
    out: dict[str, Any] = {}
    try:
        t0 = time.monotonic()
        call = await pstn.call(f"sip:{n['e164']}@127.0.0.1:5060", from_user=CALLER, via=LIVEKIT_SIP)
        await asyncio.wait_for(call.established.wait(), 60)
        await next_utterance(call, t0, 30)  # greeting
        _, asked = await call.rtp.say(speech["person"])
        out["asked_at"] = asked
        await wait_for(lambda: call.refer_to is not None or call.ended.is_set(), 60)
        out["refer_to"] = call.refer_to
        if hang_up_after_refer and call.refer_to:
            await wait_for(lambda: call.transfer_call is not None, 10)
            await asyncio.sleep(1.0)
            if call.transfer_call is not None:
                pstn.hangup(call.transfer_call)  # caller gives up while it rings
            pstn.hangup(call)
        else:
            await settle(call)
            tc = call.transfer_call
            if tc is not None and tc.established.is_set():
                seg = await next_utterance(tc, tc.answered_at or 0, 15)
                out["caller_heard_after_transfer"] = await heard(tc, seg)
                await asyncio.sleep(1)
                pstn.hangup(tc)
            elif not call.ended.is_set():
                # transfer failed: the agent should have told the caller; then we hang up
                await asyncio.sleep(6)
                out["agent_after_failure"] = await vosk_transcribe(
                    call.rtp.recording(out["asked_at"])
                )
                rx = [t for t, _ in call.rtp.received if t > out["asked_at"]]
                out["media_after_request"] = {
                    "frames": len(rx),
                    "first_s": round(rx[0] - out["asked_at"], 2) if rx else None,
                    "last_s": round(rx[-1] - out["asked_at"], 2) if rx else None,
                    "speech_segments_s": [
                        [round(a - out["asked_at"], 1), round(b - out["asked_at"], 1)]
                        for a, b in call.rtp.speech_segments()
                        if a > out["asked_at"]
                    ],
                    "now_s": round(time.monotonic() - out["asked_at"], 1),
                }
                pstn.hangup(call)
        out["refers"] = [t["refer_to"] for t in pstn.trace
                         if t["method"] == "REFER" and t["dir"] == "in"
                         and t["call_id"] == call.call_id]  # fmt: skip
        out["notifies"] = [t["body"] for t in pstn.trace
                           if t["method"] == "NOTIFY" and t["dir"] == "out"
                           and t["call_id"] == call.call_id]  # fmt: skip
        out["original_leg_end"] = call.end_reason
        out["transfer_outcomes"] = call.transfer_outcomes
        rows = await wait_final(db, n["tenant_id"])
        out["rows"] = rows
        out["sip"] = sip_lines(pstn, {call.call_id})
    finally:
        await deprovision(n)
    return out


def _summary(out: dict[str, Any]) -> dict[str, Any]:
    conv = out["rows"]["conversation"]
    return {
        **{k: v for k, v in out.items() if k != "rows"},
        "conversation": {k: conv[k] for k in ("status", "end_reason", "transfer", "duration_s")},
        "tool_events": [
            {k: e["payload"].get(k) for k in ("tool", "ok", "error", "args")}
            for e in out["rows"]["events"]
            if e["type"] == "tool"
        ],
        "transfer_events": [
            e["payload"] for e in out["rows"]["events"] if e["type"].startswith("transfer")
        ],
    }


async def test_blind_transfer_completes(
    db: Any, lk: Any, worker: dict[str, Any], phones: dict[str, SipEndpoint],
    speech: dict[str, np.ndarray], outbound_trunk: str, evidence_dir: Path,
) -> None:  # fmt: skip
    cfg = transfer_config(
        [{"name": "front_desk", "phone_number": OFFICE_ANSWER, "ringing_timeout_s": 15}],
        "front_desk",
    )
    out = await _transfer_call(db, lk, worker, phones, speech, outbound_trunk, cfg)
    data = _summary(out)
    evidence(evidence_dir, "blind_transfer", data)
    assert out["refer_to"] and OFFICE_ANSWER in out["refer_to"], data
    assert "SIP/2.0 200 OK" in " ".join(str(b) for b in out["notifies"]), data
    assert word_error_rate(SAM, out.get("caller_heard_after_transfer", "")) <= 0.5, data
    assert out["original_leg_end"] == "remote_bye", data  # LiveKit released its leg
    assert data["conversation"]["status"] == "transferred", data


async def test_blind_transfer_busy_falls_back(
    db: Any, lk: Any, worker: dict[str, Any], phones: dict[str, SipEndpoint],
    speech: dict[str, np.ndarray], outbound_trunk: str, evidence_dir: Path,
) -> None:  # fmt: skip
    cfg = transfer_config(
        [
            {"name": "front_desk", "phone_number": OFFICE_BUSY, "fallback": ["backup"],
             "ringing_timeout_s": 15},
            {"name": "backup", "phone_number": OFFICE_ANSWER, "ringing_timeout_s": 15},
        ],
        "front_desk",
    )  # fmt: skip
    out = await _transfer_call(db, lk, worker, phones, speech, outbound_trunk, cfg)
    data = _summary(out)
    evidence(evidence_dir, "busy_then_fallback", data)
    assert len(out["refers"]) == 2, data
    assert OFFICE_BUSY in out["refers"][0] and OFFICE_ANSWER in out["refers"][1], data
    assert "486" in str(out["notifies"]), data
    assert data["conversation"]["status"] == "transferred", data


async def test_blind_transfer_no_answer_returns_to_agent(
    db: Any, lk: Any, worker: dict[str, Any], phones: dict[str, SipEndpoint],
    speech: dict[str, np.ndarray], outbound_trunk: str, evidence_dir: Path,
) -> None:  # fmt: skip
    cfg = transfer_config(
        [{"name": "front_desk", "phone_number": OFFICE_NO_ANSWER, "ringing_timeout_s": 10}],
        "front_desk",
    )
    out = await _transfer_call(db, lk, worker, phones, speech, outbound_trunk, cfg)
    data = _summary(out)
    evidence(evidence_dir, "no_answer", data)
    assert out["refer_to"] and OFFICE_NO_ANSWER in out["refer_to"], data
    # the agent tells the caller nobody is available (failure_message) instead of going silent
    said = out.get("agent_after_failure", "")
    assert any(w in said for w in ("sorry", "available", "message")), data
    assert data["conversation"]["status"] == "completed", data


async def test_caller_hangs_up_during_transfer(
    db: Any, lk: Any, worker: dict[str, Any], phones: dict[str, SipEndpoint],
    speech: dict[str, np.ndarray], outbound_trunk: str, evidence_dir: Path,
) -> None:  # fmt: skip
    cfg = transfer_config(
        [{"name": "front_desk", "phone_number": OFFICE_NO_ANSWER, "ringing_timeout_s": 20}],
        "front_desk",
    )
    out = await _transfer_call(
        db, lk, worker, phones, speech, outbound_trunk, cfg, hang_up_after_refer=True
    )
    data = _summary(out)
    evidence(evidence_dir, "caller_hangs_up_during_transfer", data)
    assert out["refer_to"], data
    conv = data["conversation"]
    assert conv["status"] in ("completed", "transferred") and conv["duration_s"], data
    rooms = (await lk.room.list_rooms(__import__("livekit").api.ListRoomsRequest())).rooms
    assert not [r for r in rooms if CALLER in r.name and r.num_participants > 1], rooms


# ------------------------------------------------------------------------------------------
@pytest_asyncio.fixture
async def api_client(db: Any, worker: dict[str, Any], tmp_path: Path) -> AsyncIterator[Any]:
    from voice_platform.apps.api.main import create_app
    from voice_platform.apps.services import Services
    from voice_platform.shared.settings import Settings

    settings = Settings(
        environment="test",
        blob_local_path=str(tmp_path / "blobs"),
        telephony_dry_run=False,
        outbound_allowed_prefixes=["+1514555"],
        livekit_url=LIVEKIT_URL,
        livekit_api_key=LIVEKIT_KEY,
        livekit_api_secret=LIVEKIT_SECRET,
        agent_dispatch_name=worker["agent_name"],
    )
    app = create_app(services=Services.from_settings(settings, db=db))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def _owner_token(api: Any, db: Any, tenant_id: uuid.UUID) -> str:
    from voice_platform.core.models import Membership, User
    from voice_platform.shared.crypto import hash_password

    email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
    async with db.session(DbScope.system()) as s:
        u = User(email=email, password_hash=hash_password("lab-password-123"))
        s.add(u)
        await s.flush()
        s.add(Membership(user_id=u.id, tenant_id=tenant_id, role="owner"))  # nosec - lab user
    r = await api.post("/v1/auth/login", json={"email": email, "password": "lab-password-123"})
    assert r.status_code == 200, r.text
    return str(r.json()["access_token"])


@pytest.mark.parametrize(
    ("callee", "expected_status"),
    [(OFFICE_HELLO, "completed"), (OFFICE_BUSY, "busy")],
)
async def test_outbound_call_via_api(
    db: Any, lk: Any, worker: dict[str, Any], phones: dict[str, SipEndpoint],
    outbound_trunk: str, api_client: Any, evidence_dir: Path, callee: str,
    expected_status: str,
) -> None:  # fmt: skip
    n = await provision(db, lk, worker, lab_config(), outbound_trunk)
    office = phones["office"]
    try:
        async with db.session(DbScope.system()) as s:
            await s.execute(
                sa.text("update tenants set settings = :s where id = :t"),
                {"t": n["tenant_id"], "s": json.dumps({"outbound_calling_window": {
                    "weekday": ["00:00", "23:59"], "weekend": ["00:00", "23:59"]}})},
            )  # fmt: skip
        token = await _owner_token(api_client, db, n["tenant_id"])
        before = set(office.calls)
        r = await api_client.post(
            f"/v1/tenants/{n['tenant_id']}/calls/outbound",
            headers={"Authorization": f"Bearer {token}"},
            json={"agent_id": str(n["agent_id"]), "to": callee, "from_number": n["e164"],
                  "environment": "staging", "purpose": "lab call"},
        )  # fmt: skip
        assert r.status_code == 202, r.text
        assert r.json()["dispatched"] is True
        await wait_for(lambda: set(office.calls) - before, 30)
        new = [office.calls[c] for c in set(office.calls) - before]
        data: dict[str, Any] = {"callee": callee}
        if new and expected_status == "completed":
            c = new[0]
            await asyncio.wait_for(c.established.wait(), 20)
            # callee says hello at pickup (talking over the greeting, as people do); listen
            await asyncio.sleep(25)
            data["callee_heard"] = await vosk_transcribe(c.rtp.recording(c.answered_at or 0))
            office.hangup(c)
        rows = await wait_final(db, n["tenant_id"], timeout=90)
    finally:
        await deprovision(n)
    conv = rows["conversation"]
    data["conversation"] = {
        k: conv[k] for k in ("status", "end_reason", "direction", "caller_number", "callee_number")
    }
    data["sip"] = sip_lines(office, {c.call_id for c in new})
    data["transcript"] = [
        (e["payload"]["role"], e["payload"]["text"], e["payload"].get("interrupted"))
        for e in rows["events"]
        if e["type"] == "transcript"
    ]
    evidence(evidence_dir, f"outbound_{expected_status}", data)
    assert new, "LiveKit SIP never sent the INVITE"
    assert conv["direction"] == "outbound" and conv["status"] == expected_status, data
    if expected_status == "completed":
        assert "maple" in data["callee_heard"] or "dental" in data["callee_heard"], data
