"""Failure modes and worker call-handling paths that don't need a live LiveKit server."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import sample_config
from voice_platform.config.schema import ConversationConfig
from voice_platform.config.service import AgentService
from voice_platform.core.db import Database, DbScope
from voice_platform.core.models import Conversation, PhoneNumber, ToolExecution
from voice_platform.integrations.registry import IntegrationSet
from voice_platform.integrations.sandbox import SandboxSuite
from voice_platform.runtime.conversations import open_call
from voice_platform.runtime.policies import DurationPolicy, SilencePolicy
from voice_platform.shared.errors import DependencyUnavailableError
from voice_platform.shared.ratelimit import RateLimiter
from voice_platform.telephony.outbound import DispatchMetadata
from voice_platform.telephony.routing import route_inbound
from voice_platform.telephony.sip import SipCallError, map_sip_status
from voice_platform.tools.executor import ToolExecutor
from voice_platform.tools.interfaces import ToolEnv
from voice_platform.tools.ledger import PostgresLedger


async def _deployed_agent(db: Any, tenant_id: uuid.UUID) -> uuid.UUID:
    async with db.session(DbScope.for_tenant(tenant_id)) as s:
        svc = AgentService(s, tenant_id, None)
        agent, v = await svc.create_agent(
            slug=f"a-{uuid.uuid4().hex[:6]}", name="A", description="", config=sample_config()
        )
        await svc.publish(v.id)
        await svc.deploy(agent.id, "prod", v.id)
        return agent.id


async def test_inbound_routing_and_version_pinning(db: Any, tenant: Any) -> None:
    agent_id = await _deployed_agent(db, tenant.id)
    number = f"+1438555{uuid.uuid4().int % 10000:04d}"
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        s.add(PhoneNumber(tenant_id=tenant.id, e164=number, agent_id=agent_id))
    route = await route_inbound(db, number.replace("+1", "1-"))  # formatting tolerated
    assert route and route.tenant_id == tenant.id and route.agent_id == agent_id
    assert await route_inbound(db, "+15550000000") is None
    from voice_platform.core.credentials import CredentialVault
    from voice_platform.shared.crypto import CredentialCipher, generate_fernet_key

    vault = CredentialVault(CredentialCipher([generate_fernet_key()]))
    opened = await open_call(
        db,
        vault,
        tenant_id=tenant.id,
        agent_id=agent_id,
        environment="prod",
        direction="inbound",
        room_name="call-x",
        job_id="j1",
        caller_number="(514) 555-0199",
        callee_number=number,
    )
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        conv = await s.get(Conversation, opened.conversation_id)
        assert conv and conv.status == "in_progress" and conv.caller_number == "+15145550199"
        assert conv.agent_version_id == opened.resolved.version_id
        assert conv.meta["agent_version"] == 1


async def test_database_unavailable_raises_dependency_error() -> None:
    dead = Database(create_async_engine("postgresql+asyncpg://x:y@127.0.0.1:1/none"))
    with pytest.raises(DependencyUnavailableError):
        async with dead.session(DbScope.system()) as s:
            await s.execute(sa.text("select 1"))
    assert await dead.ping() is False
    await dead.dispose()


async def test_rate_limiter_falls_back_when_redis_is_down() -> None:
    class BrokenRedis:
        def pipeline(self) -> Any:
            raise ConnectionError("redis down")

    rl = RateLimiter(BrokenRedis())
    decisions = [await rl.hit("k", limit=2) for _ in range(3)]
    assert [d.allowed for d in decisions] == [True, True, False]


async def test_rate_limiter_uses_redis_when_available() -> None:
    import fakeredis.aioredis

    rl = RateLimiter(fakeredis.aioredis.FakeRedis())
    assert all([(await rl.hit("r", limit=3)).allowed for _ in range(3)])
    assert not (await rl.hit("r", limit=3)).allowed


async def test_tool_idempotency_survives_worker_restart(db: Any, tenant: Any) -> None:
    """Two executor instances (e.g. a worker that crashed and a replacement) share the
    Postgres ledger: the second identical booking replays instead of booking again."""
    from voice_platform.config.schema import AgentConfig

    cfg = AgentConfig.model_validate(
        sample_config(
            tools={"enabled": ["create_lead"]},
            integrations={"crm": "sb"},
        )
    )
    suite = SandboxSuite()
    conv_id = uuid.uuid4()
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        s.add(Conversation(id=conv_id, tenant_id=tenant.id))

    def executor() -> ToolExecutor:
        return ToolExecutor.for_agent(
            ToolEnv(
                tenant_id=tenant.id,
                agent_id=None,
                conversation_id=str(conv_id),
                config=cfg,
                integrations=IntegrationSet(crm=suite),
                ledger=PostgresLedger(
                    db, tenant_id=tenant.id, conversation_id=conv_id, agent_id=None
                ),
                caller_number="+15145550199",
            )
        )

    args = {"title": "Kitchen renovation quote", "caller_confirmed": True}
    first = await executor().execute("create_lead", args)
    second = await executor().execute("create_lead", args)
    assert first.ok and second.ok and second.replayed
    assert len(suite.state.leads) == 1
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        rows = (
            await s.scalars(sa.select(ToolExecution).where(ToolExecution.tenant_id == tenant.id))
        ).all()
        assert len(rows) == 1 and rows[0].status == "succeeded"


async def test_concurrent_identical_calls_execute_once(db: Any, tenant: Any) -> None:
    from voice_platform.config.schema import AgentConfig

    cfg = AgentConfig.model_validate(
        sample_config(tools={"enabled": ["create_lead"]}, integrations={"crm": "sb"})
    )
    suite = SandboxSuite({"faults": {"create_lead": {"delay_s": 0.3}}})
    conv_id = uuid.uuid4()
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        s.add(Conversation(id=conv_id, tenant_id=tenant.id))
    ex = ToolExecutor.for_agent(
        ToolEnv(
            tenant_id=tenant.id,
            agent_id=None,
            conversation_id=str(conv_id),
            config=cfg,
            integrations=IntegrationSet(crm=suite),
            ledger=PostgresLedger(db, tenant_id=tenant.id, conversation_id=conv_id, agent_id=None),
        )
    )
    args = {"title": "Same request twice", "caller_confirmed": True}
    a, b = await asyncio.gather(ex.execute("create_lead", args), ex.execute("create_lead", args))
    assert sorted([a.error or "ok", b.error or "ok"]) == ["in_progress", "ok"]
    assert len(suite.state.leads) == 1


# --- worker call handler with a fake JobContext -----------------------------------------------
class FakeCtx:
    def __init__(self, metadata: str = "", participant: Any = None) -> None:
        self.job = SimpleNamespace(metadata=metadata, id="job-1")
        self.room = SimpleNamespace(name="call-room")
        self.api = SimpleNamespace(sip=SimpleNamespace(create_sip_participant=self._dial))
        self._participant = participant
        self.shutdown_reasons: list[str] = []
        self.deleted = False
        self.callbacks: list[Any] = []
        self.dial_error: Exception | None = None

    async def connect(self) -> None:
        return None

    def add_shutdown_callback(self, cb: Any) -> None:
        self.callbacks.append(cb)

    async def wait_for_participant(self, **kw: Any) -> Any:
        if self._participant is None:
            await asyncio.sleep(3600)
        return self._participant

    async def delete_room(self) -> None:
        self.deleted = True

    def shutdown(self, reason: str = "") -> None:
        self.shutdown_reasons.append(reason)

    async def _dial(self, req: Any) -> Any:
        if self.dial_error:
            raise self.dial_error
        return SimpleNamespace(participant_identity=req.participant_identity, sip_call_id="SCL1")


async def test_worker_hangs_up_unrouted_inbound_calls(db: Any, tmp_path: Any) -> None:
    from voice_platform.apps.services import Services
    from voice_platform.apps.worker.call import CallHandler
    from voice_platform.shared.settings import Settings

    services = Services.from_settings(Settings(blob_local_path=str(tmp_path)), db=db)
    participant = SimpleNamespace(
        identity="sip_1",
        attributes={"sip.trunkPhoneNumber": "+15550001111", "sip.phoneNumber": "+15145550199"},
    )
    ctx = FakeCtx(participant=participant)
    await CallHandler(ctx, services).run()  # type: ignore[arg-type]
    assert ctx.deleted and ctx.shutdown_reasons == ["unrouted"]


async def test_worker_outbound_dial_failure_updates_conversation(
    db: Any, tenant: Any, tmp_path: Any
) -> None:
    from livekit import api

    from voice_platform.apps.services import Services
    from voice_platform.apps.worker.call import CallHandler
    from voice_platform.shared.settings import Settings

    agent_id = await _deployed_agent(db, tenant.id)
    conv_id = uuid.uuid4()
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        s.add(
            Conversation(id=conv_id, tenant_id=tenant.id, agent_id=agent_id, direction="outbound")
        )
    meta = DispatchMetadata(
        tenant_id=str(tenant.id),
        agent_id=str(agent_id),
        conversation_id=str(conv_id),
        environment="prod",
        to="+15145550123",
        from_number="+15145550000",
        trunk_id="ST_x",
        purpose="reminder",
        context={},
    )
    ctx = FakeCtx(metadata=meta.to_json())
    ctx.dial_error = api.TwirpError(
        "unavailable", "busy here", status=503, metadata={"sip_status_code": "486"}
    )
    services = Services.from_settings(Settings(blob_local_path=str(tmp_path)), db=db)
    await CallHandler(ctx, services).run()  # type: ignore[arg-type]
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        conv = await s.get(Conversation, conv_id)
        assert conv and conv.status == "busy" and conv.end_reason == "dial_busy"
    assert ctx.shutdown_reasons and "busy" in ctx.shutdown_reasons[0]


def test_sip_status_mapping() -> None:
    assert map_sip_status(486) == "busy"
    assert map_sip_status(480) == "no_answer"
    assert map_sip_status(603) == "rejected"
    assert map_sip_status(503) == "unavailable"
    assert map_sip_status(None) == "failed"
    assert SipCallError("busy", "x").status == "busy"


# --- conversation policies ---------------------------------------------------------------------
class FakeSession:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.handlers: dict[str, Any] = {}
        self.interrupted = False

    def on(self, event: str, cb: Any) -> None:
        self.handlers[event] = cb

    def say(self, text: str, **kw: Any) -> Any:
        self.said.append(text)

        async def done() -> None:
            return None

        return SimpleNamespace(wait_for_playout=done)

    def interrupt(self) -> None:
        self.interrupted = True


async def test_silence_policy_reprompts_then_ends_call() -> None:
    session = FakeSession()
    ended: list[str] = []

    async def end(reason: str) -> None:
        ended.append(reason)

    cfg = ConversationConfig.model_validate({"silence": {"max_reprompts": 2}})
    policy = SilencePolicy(session, cfg, end)
    policy.attach()
    away = SimpleNamespace(new_state="away")
    for _ in range(3):
        session.handlers["user_state_changed"](away)
        await asyncio.sleep(0.01)
    assert session.said[:2] == cfg.silence.reprompt_messages[:2]
    assert session.said[2] == cfg.silence.goodbye_message and ended == ["silence_timeout"]
    # speaking resets the reprompt budget
    policy2 = SilencePolicy(FakeSession(), cfg, end)
    policy2.reprompts = 2
    policy2._on_state(SimpleNamespace(new_state="speaking"))
    assert policy2.reprompts == 0


async def test_duration_policy_wraps_up() -> None:
    session = FakeSession()
    ended: list[str] = []

    async def end(reason: str) -> None:
        ended.append(reason)

    cfg = ConversationConfig.model_validate({"max_duration_s": 60})
    # model_copy skips validation: an immediate deadline for the test only
    policy = DurationPolicy(session, cfg.model_copy(update={"max_duration_s": 0}), end)
    policy.start()
    await asyncio.sleep(0.05)
    assert policy.fired and session.interrupted and ended == ["max_duration"]
    assert session.said == [cfg.wrap_up_message]


async def test_retention_sweep_deletes_expired_data(db: Any, tenant: Any, tmp_path: Any) -> None:
    from datetime import UTC, datetime, timedelta

    from voice_platform.core.models import ConversationEvent, Tenant
    from voice_platform.core.retention import sweep
    from voice_platform.shared.blobstore import LocalBlobStore

    blobs = LocalBlobStore(tmp_path, signing_secret="s" * 32, public_base_url="http://x")
    async with db.session(DbScope.system()) as s:
        t = await s.get(Tenant, tenant.id)
        assert t is not None
        t.retention_days = 30
    now = datetime.now(UTC)
    old_id, recent_id, rec_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = f"tenants/{tenant.id}/recordings/{rec_id}.ogg"
    await blobs.put(key, b"audio", "audio/ogg")
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        s.add_all(
            [
                Conversation(id=old_id, tenant_id=tenant.id, started_at=now - timedelta(days=45)),
                Conversation(id=recent_id, tenant_id=tenant.id, started_at=now - timedelta(days=2)),
                Conversation(
                    id=rec_id,
                    tenant_id=tenant.id,
                    started_at=now - timedelta(days=10),
                    recording_uri=key,
                    meta={"recording_retention_days": 7},
                ),
            ]
        )
        await s.flush()
        s.add(
            ConversationEvent(
                tenant_id=tenant.id,
                conversation_id=old_id,
                seq=1,
                type="transcript",
                payload={"text": "old"},
            )
        )
    report = await sweep(db, blobs)
    assert report["conversations_deleted"] >= 1 and report["recordings_deleted"] >= 1
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        assert await s.get(Conversation, old_id) is None
        assert await s.get(Conversation, recent_id) is not None
        rec = await s.get(Conversation, rec_id)
        assert rec is not None and rec.recording_uri is None and rec.recording_status == "deleted"
        n = await s.scalar(
            sa.select(sa.func.count())
            .select_from(ConversationEvent)
            .where(ConversationEvent.conversation_id == old_id)
        )
        assert n == 0
    assert not (tmp_path / key).exists()
