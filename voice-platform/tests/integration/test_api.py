"""Management API end-to-end against a real database (ASGI in-process)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest_asyncio

from tests.conftest import sample_config
from tests.support.models import receptionist_model
from voice_platform.apps.api.main import create_app
from voice_platform.apps.jobs.runner import JobRunner
from voice_platform.apps.services import Services
from voice_platform.core.db import DbScope
from voice_platform.core.models import User
from voice_platform.shared.crypto import hash_password
from voice_platform.shared.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "correct-horse-battery-staple"


@pytest_asyncio.fixture
async def api(db: Any, tmp_path: Path) -> Any:
    settings = Settings(
        environment="test",
        blob_local_path=str(tmp_path / "blobs"),
        login_rate_limit_per_minute=5,
        telephony_dry_run=True,
        scenario_library_path=str(ROOT / "scenarios"),
        judge_llm="test:judge",
        simulator_llm="test:caller",
    )
    services = Services.from_settings(settings, db=db)
    app = create_app(services=services)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.services = services  # type: ignore[attr-defined]
        yield client


async def _admin_token(api: Any, db: Any) -> str:
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    async with db.session(DbScope.system()) as s:
        s.add(User(email=email, password_hash=hash_password(PASSWORD), is_platform_admin=True))
    r = await api.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return str(r.json()["access_token"])


def H(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _tenant_with_owner(api: Any, admin: str) -> tuple[str, str]:
    slug = f"t-{uuid.uuid4().hex[:8]}"
    email = f"owner-{slug}@example.com"
    r = await api.post(
        "/v1/tenants",
        headers=H(admin),
        json={"slug": slug, "name": slug, "owner_email": email, "owner_password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    tok = (await api.post("/v1/auth/login", json={"email": email, "password": PASSWORD})).json()[
        "access_token"
    ]
    return r.json()["id"], tok


async def test_auth_rbac_and_tenant_isolation(api: Any, db: Any) -> None:
    assert (await api.get("/v1/me")).status_code == 401
    assert (await api.get("/v1/me", headers=H("garbage"))).status_code == 401
    admin = await _admin_token(api, db)
    t1, owner1 = await _tenant_with_owner(api, admin)
    t2, _owner2 = await _tenant_with_owner(api, admin)
    me = (await api.get("/v1/me", headers=H(owner1))).json()
    assert [t["id"] for t in me["tenants"]] == [t1] and me["tenants"][0]["role"] == "owner"
    # owner of tenant 1 cannot touch tenant 2 (and cannot list tenants)
    assert (await api.get(f"/v1/tenants/{t2}/agents", headers=H(owner1))).status_code == 403
    assert (await api.get("/v1/tenants", headers=H(owner1))).status_code == 403
    # viewer can read but not write
    r = await api.post(
        f"/v1/tenants/{t1}/members",
        headers=H(owner1),
        json={
            "email": f"viewer-{uuid.uuid4().hex[:6]}@example.com",
            "role": "viewer",
            "password": PASSWORD,
        },
    )
    assert r.status_code == 201
    viewer = (
        await api.post("/v1/auth/login", json={"email": r.json()["email"], "password": PASSWORD})
    ).json()["access_token"]
    assert (await api.get(f"/v1/tenants/{t1}/agents", headers=H(viewer))).status_code == 200
    r = await api.post(
        f"/v1/tenants/{t1}/agents",
        headers=H(viewer),
        json={"slug": "x", "name": "x", "config": sample_config()},
    )
    assert r.status_code == 403
    # API keys: scoped to one tenant, cannot manage members/keys
    r = await api.post(
        f"/v1/tenants/{t1}/api-keys", headers=H(owner1), json={"name": "ci", "role": "editor"}
    )
    key = r.json()["key"]
    assert key.startswith("vpk_")
    assert (await api.get(f"/v1/tenants/{t1}/agents", headers=H(key))).status_code == 200
    assert (await api.get(f"/v1/tenants/{t2}/agents", headers=H(key))).status_code == 403
    assert (await api.get(f"/v1/tenants/{t1}/api-keys", headers=H(key))).status_code == 403
    keys = (await api.get(f"/v1/tenants/{t1}/api-keys", headers=H(owner1))).json()
    assert "key" not in keys[0] and keys[0]["prefix"]
    await api.delete(f"/v1/tenants/{t1}/api-keys/{keys[0]['id']}", headers=H(owner1))
    assert (await api.get(f"/v1/tenants/{t1}/agents", headers=H(key))).status_code == 401
    audit = (await api.get(f"/v1/tenants/{t1}/audit", headers=H(owner1))).json()
    assert {"api_key.create", "api_key.revoke", "member.add"} <= {a["action"] for a in audit}


async def test_login_rate_limit(api: Any) -> None:
    codes = [
        (
            await api.post("/v1/auth/login", json={"email": "nobody@example.com", "password": "x"})
        ).status_code
        for _ in range(7)
    ]
    assert codes[:5] == [401] * 5 and codes[-1] == 429


async def test_agent_lifecycle_credentials_integrations(api: Any, db: Any) -> None:
    admin = await _admin_token(api, db)
    tid, owner = await _tenant_with_owner(api, admin)
    base = f"/v1/tenants/{tid}"
    bad = sample_config()
    bad["pipeline"]["stt"]["options"] = {"nonsense": 1}
    v = (await api.post(f"{base}/agents/validate", headers=H(owner), json={"config": bad})).json()
    assert not v["valid"] and v["problems"][0]["path"].startswith("pipeline.stt.options")
    r = await api.post(
        f"{base}/agents",
        headers=H(owner),
        json={"slug": "front-desk", "name": "Front desk", "config": sample_config()},
    )
    assert r.status_code == 201, r.text
    agent_id, draft_id = r.json()["id"], r.json()["draft"]["id"]
    assert (
        await api.put(
            f"{base}/agents/{agent_id}/deployments/prod",
            headers=H(owner),
            json={"version_id": draft_id},
        )
    ).status_code == 409
    await api.post(f"{base}/versions/{draft_id}/publish", headers=H(owner))
    dep = (
        await api.put(
            f"{base}/agents/{agent_id}/deployments/prod",
            headers=H(owner),
            json={"version_id": draft_id},
        )
    ).json()
    assert dep["version_id"] == draft_id
    v2 = (
        await api.post(
            f"{base}/agents/{agent_id}/versions",
            headers=H(owner),
            json={
                "config": sample_config(instructions="Version two."),
                "parent_version_id": draft_id,
            },
        )
    ).json()
    diff = (await api.get(f"{base}/versions/{draft_id}/diff/{v2['id']}", headers=H(owner))).json()
    assert [d["path"] for d in diff] == ["instructions"]
    # credentials are write-only
    r = await api.post(
        f"{base}/credentials",
        headers=H(owner),
        json={"name": "hs", "kind": "hubspot", "payload": {"access_token": "pat-na1-secret-value"}},
    )
    assert r.status_code == 201 and "payload" not in r.json()
    listed = (await api.get(f"{base}/credentials", headers=H(owner))).text
    assert "secret-value" not in listed
    r = await api.post(
        f"{base}/integrations",
        headers=H(owner),
        json={"name": "crm", "kind": "hubspot", "credential_id": None},
    )
    assert r.status_code == 422  # hubspot needs a credential
    r = await api.post(
        f"{base}/integrations",
        headers=H(owner),
        json={"name": "sandbox", "kind": "sandbox", "config": {}},
    )
    assert r.status_code == 201
    t = (await api.post(f"{base}/integrations/{r.json()['id']}/test", headers=H(owner))).json()
    assert t["ok"] is True
    kinds = (await api.get("/v1/integration-kinds")).json()
    assert {"hubspot", "salesforce", "google_calendar", "custom_rest"} <= {k["kind"] for k in kinds}
    providers = (await api.get("/v1/providers", headers=H(owner))).json()
    assert {"deepgram", "assemblyai", "openai", "anthropic", "elevenlabs", "cartesia"} <= {
        p["name"] for p in providers
    }


async def test_phone_numbers_and_outbound_guardrails(api: Any, db: Any) -> None:
    admin = await _admin_token(api, db)
    tid, owner = await _tenant_with_owner(api, admin)
    base = f"/v1/tenants/{tid}"
    agent = (
        await api.post(
            f"{base}/agents",
            headers=H(owner),
            json={"slug": "out", "name": "Outbound", "config": sample_config()},
        )
    ).json()
    number = f"+1514555{uuid.uuid4().int % 10000:04d}"
    r = await api.post(
        f"{base}/phone-numbers",
        headers=H(owner),
        json={"e164": number, "agent_id": agent["id"], "livekit_outbound_trunk_id": "ST_test"},
    )
    assert r.status_code == 201, r.text
    assert (
        await api.post(f"{base}/phone-numbers", headers=H(owner), json={"e164": number})
    ).status_code == 409
    await api.patch(
        f"/v1/tenants/{tid}",
        headers=H(owner),
        json={
            "settings": {
                "do_not_call": ["+15145550111"],
                "outbound_calling_window": {
                    "weekday": ["00:00", "23:59"],
                    "weekend": ["00:00", "23:59"],
                },
            }
        },
    )
    r = await api.post(
        f"{base}/calls/outbound",
        headers=H(owner),
        json={"agent_id": agent["id"], "to": "+15145550111"},
    )
    assert r.status_code == 403  # do-not-call list
    r = await api.post(
        f"{base}/calls/outbound",
        headers=H(owner),
        json={"agent_id": agent["id"], "to": "+15145550112", "purpose": "appointment reminder"},
    )
    assert r.status_code == 202 and r.json()["dry_run"] is True
    convs = (await api.get(f"{base}/conversations", headers=H(owner))).json()
    assert convs[0]["direction"] == "outbound" and convs[0]["end_reason"] == "dry_run"
    overview = (await api.get(f"{base}/analytics/overview", headers=H(owner))).json()
    assert overview["calls"] == 1


async def test_knowledge_evaluation_and_optimization_via_jobs(api: Any, db: Any) -> None:
    admin = await _admin_token(api, db)
    tid, owner = await _tenant_with_owner(api, admin)
    base = f"/v1/tenants/{tid}"
    services: Services = api.services
    kb = (
        await api.post(
            f"{base}/knowledge-bases",
            headers=H(owner),
            json={"name": "faq", "embedding_provider": "hashing", "embedding_model": "hashing-v1"},
        )
    ).json()
    r = await api.post(
        f"{base}/knowledge-bases/{kb['id']}/documents",
        headers=H(owner),
        files={"file": ("faq.md", b"## Prices\nA cleaning costs 120 dollars.", "text/markdown")},
    )
    assert r.status_code == 202 and r.json()["status"] == "pending"

    def resolver(spec: str, secrets: dict[str, Any]) -> Any:
        return receptionist_model()  # instruction-sensitive test double

    runner = JobRunner(services, llm_resolver=resolver)
    assert await runner.run_once()
    docs = (await api.get(f"{base}/knowledge-bases/{kb['id']}/documents", headers=H(owner))).json()
    assert docs[0]["status"] == "ready" and docs[0]["chunk_count"] == 1
    hits = (
        await api.post(
            f"{base}/knowledge/search",
            headers=H(owner),
            json={"query": "how much is a cleaning", "knowledge_base_ids": [kb["id"]]},
        )
    ).json()
    assert "120 dollars" in hits[0]["content"]

    from tests.evaluations.test_evaluation_and_optimization import eval_config

    cfg = eval_config().model_dump(mode="json")
    agent = (
        await api.post(
            f"{base}/agents",
            headers=H(owner),
            json={"slug": "rx", "name": "Receptionist", "config": cfg},
        )
    ).json()
    vid = agent["draft"]["id"]
    await api.post(f"{base}/versions/{vid}/publish", headers=H(owner))
    await api.put(
        f"{base}/agents/{agent['id']}/deployments/staging",
        headers=H(owner),
        json={"version_id": vid},
    )
    slugs = ["book-simple", "wants-human", "book-change-mind"]
    run = (
        await api.post(
            f"{base}/evaluations",
            headers=H(owner),
            json={
                "agent_id": agent["id"],
                "version_id": vid,
                "scenario_slugs": slugs,
                "use_judge": False,
            },
        )
    ).json()
    assert await runner.run_once()
    detail = (await api.get(f"{base}/evaluations/{run['id']}", headers=H(owner))).json()
    assert detail["status"] == "succeeded", detail
    assert len(detail["results"]) == 3 and detail["summary"]["pass_rate"] < 1.0

    opt = (
        await api.post(
            f"{base}/optimizations",
            headers=H(owner),
            json={
                "agent_id": agent["id"],
                "baseline_version_id": vid,
                "scenario_slugs": slugs,
                "max_iterations": 4,
                "min_pass_rate": 1.0,
            },
        )
    ).json()
    assert await runner.run_once()
    done = (await api.get(f"{base}/optimizations/{opt['id']}", headers=H(owner))).json()
    assert done["status"] == "succeeded" and done["decision"] == "eligible", done
    cand = done["best_candidate_version_id"]
    cand_v = (await api.get(f"{base}/versions/{cand}", headers=H(owner))).json()
    assert cand_v["status"] == "candidate" and cand_v["source"] == "optimizer"
    # nothing reached production without approval
    agent_detail = (await api.get(f"{base}/agents/{agent['id']}", headers=H(owner))).json()
    assert all(
        d["version_id"] == vid and d["canary_version_id"] is None
        for d in agent_detail["deployments"]
    )
    appr = (
        await api.post(
            f"{base}/optimizations/{opt['id']}/approve",
            headers=H(owner),
            json={"environment": "staging", "canary_percent": 10},
        )
    ).json()
    assert appr["deployment"]["canary_version_id"] == cand
    assert appr["deployment"]["canary_percent"] == 10
    assert (
        await api.post(f"{base}/optimizations/{opt['id']}/approve", headers=H(owner), json={})
    ).status_code == 409


async def test_twilio_webhook_rejects_bad_signature(api: Any, db: Any) -> None:
    admin = await _admin_token(api, db)
    tid, owner = await _tenant_with_owner(api, admin)
    cred = (
        await api.post(
            f"/v1/tenants/{tid}/credentials",
            headers=H(owner),
            json={
                "name": "tw",
                "kind": "twilio",
                "payload": {"account_sid": "AC" + "0" * 32, "auth_token": "t" * 32},
            },
        )
    ).json()
    await api.post(
        f"/v1/tenants/{tid}/integrations",
        headers=H(owner),
        json={"name": "sms", "kind": "twilio", "credential_id": cred["id"]},
    )
    r = await api.post(
        f"/v1/webhooks/twilio/{tid}/sms/sms-status",
        data={"MessageSid": "SM1", "MessageStatus": "delivered"},
        headers={"X-Twilio-Signature": "forged"},
    )
    assert r.status_code == 403


async def test_health_and_metrics(api: Any) -> None:
    assert (await api.get("/healthz")).json() == {"status": "ok"}
    ready = await api.get("/readyz")
    assert ready.status_code == 200 and ready.json()["checks"]["database"] is True
    assert "vp_api_requests_total" in (await api.get("/metrics")).text


async def test_scenario_generation_and_data_subject_requests(api: Any, db: Any) -> None:
    import json as _json

    from voice_platform.core.models import Conversation, ConversationEvent
    from voice_platform.evaluation.scripted import Reply, Rule, ScriptedLLM

    admin = await _admin_token(api, db)
    tid, owner = await _tenant_with_owner(api, admin)
    base = f"/v1/tenants/{tid}"
    agent = (
        await api.post(
            f"{base}/agents",
            headers=H(owner),
            json={
                "slug": "gen",
                "name": "Gen",
                "config": sample_config(
                    tools={"enabled": ["lookup_customer"]}, integrations={"crm": "sandbox"}
                ),
            },
        )
    ).json()
    generated = {
        "scenarios": [
            {
                "id": "Angry Caller",
                "name": "Angry caller",
                "persona": {"mood": "angry"},
                "script": [{"say": "This is ridiculous, let me talk to someone!"}],
                "expectations": {"tools": [{"name": "lookup_customer"}]},
                "sandbox": {"faults": {"x": {"error": "auth"}}},
            },  # must be stripped
            {
                "id": "bad",
                "name": "Uses disabled tool",
                "script": [{"say": "hi"}],
                "expectations": {"tools": [{"name": "book_appointment"}]},
            },  # rejected
            {"id": "no-driver", "name": "Invalid"},  # rejected
        ]
    }
    api.services.scenario_llm = ScriptedLLM([Rule(lambda t: Reply(text=_json.dumps(generated)))])
    r = (
        await api.post(
            f"{base}/scenarios/generate",
            headers=H(owner),
            json={"version_id": agent["draft"]["id"], "count": 3},
        )
    ).json()
    assert r["created"] == ["gen-angry-caller"] and len(r["rejected"]) == 2
    listed = (await api.get(f"{base}/scenarios", headers=H(owner))).json()
    assert any(s["slug"] == "gen-angry-caller" and s["source"] == "generated" for s in listed)

    conv_id = uuid.uuid4()
    async with db.session(DbScope.for_tenant(uuid.UUID(tid))) as s:
        s.add(Conversation(id=conv_id, tenant_id=uuid.UUID(tid), caller_number="+15145550142"))
        await s.flush()
        s.add(
            ConversationEvent(
                tenant_id=uuid.UUID(tid),
                conversation_id=conv_id,
                seq=1,
                type="transcript",
                payload={"role": "user", "text": "hi"},
            )
        )
    export = (
        await api.get(f"{base}/data-subjects/export?phone=5145550142", headers=H(owner))
    ).json()
    assert len(export["conversations"]) == 1
    assert export["conversations"][0]["events"][0]["text"] == "hi"
    erased = (
        await api.delete(f"{base}/data-subjects?phone=%2B15145550142", headers=H(owner))
    ).json()
    assert erased["conversations_deleted"] == 1
    again = (
        await api.get(f"{base}/data-subjects/export?phone=5145550142", headers=H(owner))
    ).json()
    assert again["conversations"] == []
