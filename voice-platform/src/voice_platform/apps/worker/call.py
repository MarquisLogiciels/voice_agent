"""CallHandler: everything that happens inside one LiveKit job (one phone call)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from typing import Any

from livekit import rtc
from livekit.agents import JobContext
from livekit.agents.voice import room_io

from ..services import Services
from ...config.schema import TransferDestination
from ...knowledge.service import TenantKnowledge
from ...observability import metrics as m
from ...providers.types import BuildContext
from ...shared.logging import bind_log_context, get_logger, mask_phone
from ...telephony.outbound import DispatchMetadata
from ...telephony.recording import RecordingService
from ...telephony.routing import ATTR_CALLER_NUMBER, ATTR_TRUNK_NUMBER, route_inbound
from ...telephony.sip import LiveKitTransport, SipCallError
from ...telephony.transfer import TransferManager
from ...tools.ledger import PostgresLedger
from ...runtime.conversations import mark_call, open_call
from ...runtime.recorder import PostgresSink
from ...runtime.session import CallInfo, RuntimeDeps, RuntimeSession, build_runtime_session

logger = get_logger(__name__)

SIP_KIND = rtc.ParticipantKind.PARTICIPANT_KIND_SIP


class CallHandler:
    def __init__(self, ctx: JobContext, services: Services, *, vad: Any | None = None) -> None:
        self.ctx = ctx
        self.services = services
        self.vad = vad
        self.rs: RuntimeSession | None = None
        self.tenant_id: uuid.UUID | None = None
        self.conversation_id: uuid.UUID | None = None
        self.egress_id: str | None = None
        self.close_reason: str | None = None
        self._finalized = False

    async def run(self) -> None:
        started = time.monotonic()
        ctx = self.ctx
        meta = DispatchMetadata.parse(ctx.job.metadata)
        await ctx.connect()
        m.ACTIVE_CALLS.inc()
        ctx.add_shutdown_callback(self._finalize)
        if meta is not None:
            await self._outbound(meta)
        else:
            await self._inbound()
        if self.rs is not None:
            m.CALL_SETUP.observe(time.monotonic() - started)

    # --- inbound -----------------------------------------------------------------------------
    async def _inbound(self) -> None:
        ctx = self.ctx
        try:
            participant = await asyncio.wait_for(ctx.wait_for_participant(kind=SIP_KIND), 30)
        except TimeoutError:
            logger.warning("no SIP participant joined; ending job")
            ctx.shutdown("no participant")
            return
        dialed = participant.attributes.get(ATTR_TRUNK_NUMBER, "")
        caller = participant.attributes.get(ATTR_CALLER_NUMBER)
        route = await route_inbound(self.services.db, dialed)
        if route is None or route.agent_id is None:
            logger.warning("no route for dialed number", extra={"dialed": mask_phone(dialed)})
            await self._hangup("unrouted")
            return
        self.tenant_id = route.tenant_id
        await self._start(tenant_id=route.tenant_id, agent_id=route.agent_id,
                          environment=route.environment, direction="inbound",
                          caller=caller, business=route.dialed_number,
                          caller_identity=participant.identity, conversation_id=None,
                          caller_context=None)

    # --- outbound ----------------------------------------------------------------------------
    async def _outbound(self, meta: DispatchMetadata) -> None:
        tenant_id = uuid.UUID(meta.tenant_id)
        conv_id = uuid.UUID(meta.conversation_id)
        self.tenant_id, self.conversation_id = tenant_id, conv_id
        transport = LiveKitTransport(self.ctx.api)
        identity = f"callee-{conv_id.hex[:12]}"
        settings = self.services.settings
        if settings.telephony_dry_run:
            logger.info("dry-run: outbound dial skipped", extra={"to": mask_phone(meta.to)})
            await mark_call(self.services.db, tenant_id, conv_id, status="failed",
                            end_reason="dry_run")
            m.OUTBOUND_DIALS.labels("dry_run").inc()
            self.ctx.shutdown("dry run")
            return
        await mark_call(self.services.db, tenant_id, conv_id, status="ringing",
                        room_name=self.ctx.room.name)
        try:
            await transport.dial(room_name=self.ctx.room.name, to_number=meta.to,
                                 trunk_id=meta.trunk_id, from_number=meta.from_number,
                                 participant_identity=identity, ringing_timeout_s=30)
        except SipCallError as e:
            status = {"busy": "busy", "no_answer": "no_answer"}.get(e.status, "failed")
            await mark_call(self.services.db, tenant_id, conv_id, status=status,
                            end_reason=f"dial_{e.status}")
            m.OUTBOUND_DIALS.labels(e.status).inc()
            self.ctx.shutdown(f"dial failed: {e.status}")
            return
        m.OUTBOUND_DIALS.labels("answered").inc()
        context = meta.purpose + (f" Details: {meta.context}" if meta.context else "")
        await self._start(tenant_id=tenant_id, agent_id=uuid.UUID(meta.agent_id),
                          environment=meta.environment, direction="outbound", caller=meta.to,
                          business=meta.from_number, caller_identity=identity,
                          conversation_id=conv_id, caller_context=context or None)

    # --- shared start ------------------------------------------------------------------------
    async def _start(self, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, environment: str,
                     direction: str, caller: str | None, business: str | None,
                     caller_identity: str, conversation_id: uuid.UUID | None,
                     caller_context: str | None) -> None:
        svc = self.services
        ctx = self.ctx
        opened = await open_call(
            svc.db, svc.vault, tenant_id=tenant_id, agent_id=agent_id, environment=environment,
            direction=direction, room_name=ctx.room.name, job_id=ctx.job.id,
            caller_number=caller, callee_number=business, conversation_id=conversation_id)
        self.conversation_id = opened.conversation_id
        cfg = opened.resolved.config
        bind_log_context(tenant_id=str(tenant_id), agent_id=str(agent_id),
                         conversation_id=str(opened.conversation_id),
                         agent_version=opened.resolved.version, room=ctx.room.name)
        async with svc.db.session(_scope(tenant_id)) as s:
            integrations = await svc.resolver.resolve_set(
                s, tenant_id, crm=cfg.integrations.crm, calendar=cfg.integrations.calendar,
                messaging=cfg.integrations.messaging, custom=cfg.integrations.custom,
                timezone=cfg.business.timezone)
        knowledge = None
        if cfg.knowledge.knowledge_base_ids:
            knowledge = TenantKnowledge(svc.knowledge, tenant_id,
                                        [uuid.UUID(k) for k in cfg.knowledge.knowledge_base_ids],
                                        mode=cfg.knowledge.retrieval)
        transport = LiveKitTransport(ctx.api)
        rs_ref: dict[str, RuntimeSession] = {}

        async def announce(text: str) -> None:
            handle = rs_ref["rs"].session.say(text, allow_interruptions=False)
            await handle.wait_for_playout()

        async def warm(dest: TransferDestination, summary: str) -> None:
            from livekit.agents.beta.workflows import WarmTransferTask
            from livekit.agents.beta.workflows.utils import WorkflowInstructions

            trunk = cfg.transfer.outbound_trunk_id or os.environ.get("LIVEKIT_SIP_OUTBOUND_TRUNK")
            task = WarmTransferTask(
                sip_call_to=dest.phone_number or dest.sip_uri or "", sip_trunk_id=trunk,
                sip_number=cfg.transfer.caller_id_number or business or "",
                chat_ctx=rs_ref["rs"].agent.chat_ctx, ringing_timeout=dest.ringing_timeout_s,
                instructions=WorkflowInstructions(extra=f"Handoff summary for the staff "
                                                  f"member: {summary}"))
            await task

        async def transfer_event(ev: dict[str, Any]) -> None:
            rs_ref["rs"].recorder.add(ev)

        def transfer_factory(session: Any, env: Any) -> TransferManager:
            return TransferManager(
                config=cfg.transfer, timezone=cfg.business.timezone, transport=transport,
                room_name=ctx.room.name, caller_identity=caller_identity, announce=announce,
                warm_runner=warm, dry_run=svc.settings.telephony_dry_run,
                allowed_prefixes=svc.settings.outbound_allowed_prefixes, on_event=transfer_event)

        def end_call_tool() -> Any:
            from livekit.agents.beta.tools import EndCallTool

            return EndCallTool(delete_room=True, ignore_on_enter=True,
                               end_instructions="Say a brief, polite goodbye.")

        async def end_call(reason: str) -> None:
            self.close_reason = self.close_reason or reason
            await self._hangup(reason)

        deps = RuntimeDeps(
            builder=svc.builder, integrations=integrations,
            build_ctx=BuildContext(tenant_secrets=opened.provider_secrets,
                                   default_language=cfg.language, vad=self.vad),
            ledger=PostgresLedger(svc.db, tenant_id=tenant_id,
                                  conversation_id=opened.conversation_id, agent_id=agent_id),
            sink=PostgresSink(svc.db, tenant_id, opened.conversation_id),
            knowledge=knowledge, transfer_factory=transfer_factory, end_call=end_call,
            end_call_tool_factory=end_call_tool,
        )
        try:
            rs = build_runtime_session(opened.resolved, CallInfo(
                conversation_id=str(opened.conversation_id), direction=direction,
                caller_number=caller, business_number=business, room_name=ctx.room.name,
                caller_identity=caller_identity, caller_context=caller_context), deps)
        except Exception:
            await integrations.aclose()
            logger.exception("failed to build agent session")
            await mark_call(svc.db, tenant_id, opened.conversation_id, status="failed",
                            end_reason="session_build_failed")
            await self._hangup("session_build_failed")
            return
        rs_ref["rs"] = rs
        self.rs = rs
        rs.session.on("close", self._on_close)

        if cfg.recording.enabled and cfg.recording.consent_required:
            # announced before recording starts; the greeting follows
            rs.session.say(cfg.recording.consent_announcement, allow_interruptions=False)
        await rs.session.start(
            agent=rs.agent, room=ctx.room,
            room_options=room_io.RoomOptions(participant_identity=caller_identity,
                                             close_on_disconnect=True))
        rs.start_policies()
        if cfg.recording.enabled:
            await self._start_recording(tenant_id, opened.conversation_id)

    async def _start_recording(self, tenant_id: uuid.UUID, conv_id: uuid.UUID) -> None:
        try:
            rec = RecordingService(self.ctx.api, self.services.settings)
            self.egress_id = await rec.start(room_name=self.ctx.room.name, tenant_id=tenant_id,
                                             conversation_id=conv_id)
            await mark_call(self.services.db, tenant_id, conv_id,
                            recording_status="recording" if self.egress_id else "unavailable",
                            recording_uri=rec.object_key(tenant_id, conv_id)
                            if self.egress_id else None,
                            recording_consent=True)
        except Exception:
            logger.exception("failed to start recording")
            await mark_call(self.services.db, tenant_id, conv_id, recording_status="failed")

    def _on_close(self, ev: Any) -> None:
        self.close_reason = self.close_reason or getattr(ev.reason, "value", str(ev.reason))

    async def _hangup(self, reason: str) -> None:
        self.close_reason = self.close_reason or reason
        with contextlib.suppress(Exception):
            await self.ctx.delete_room()
        self.ctx.shutdown(reason)

    async def _finalize(self, *_: Any) -> None:
        if self._finalized:
            return
        self._finalized = True
        m.ACTIVE_CALLS.dec()
        rs = self.rs
        if rs is None or self.tenant_id is None:
            return
        transferred = bool(rs.env.state.transfer_result and rs.env.state.transfer_result.ok)
        status = "transferred" if transferred else "completed"
        rs.end_reason = rs.end_reason or self.close_reason or "participant_disconnected"
        extra: dict[str, Any] = {}
        if rs.agent.config.conversation.post_call_summary:
            extra["summary"] = await _post_call_summary(rs)
        if self.egress_id:
            await RecordingService(self.ctx.api, self.services.settings).stop(self.egress_id)
            extra["recording_status"] = "completed"
        fields = await rs.close(status=status, extra=extra)
        m.CALLS_TOTAL.labels("call", rs.end_reason).inc()
        m.CALL_DURATION.observe(fields.get("duration_s") or 0)


async def _post_call_summary(rs: RuntimeSession) -> str | None:
    lines = [f"{t['role']}: {t['text']}" for t in rs.recorder.transcript if t["text"]]
    if not lines:
        return None
    from livekit.agents import llm as lk_llm

    model = rs.pipeline.llm
    if not isinstance(model, lk_llm.LLM):
        return None
    ctx = lk_llm.ChatContext()
    ctx.add_message(role="system", content="Summarize this phone call in 2-3 sentences for a "
                    "business owner: who called, what they wanted, and the outcome.")
    ctx.add_message(role="user", content="\n".join(lines)[-12_000:])
    try:
        async def run() -> str:
            out: list[str] = []
            async with model.chat(chat_ctx=ctx) as stream:
                async for chunk in stream:
                    if chunk.delta and chunk.delta.content:
                        out.append(chunk.delta.content)
            return "".join(out).strip()

        return await asyncio.wait_for(run(), timeout=8)
    except Exception:
        logger.warning("post-call summary failed", exc_info=True)
        return None


def _scope(tenant_id: uuid.UUID) -> Any:
    from ...core.db import DbScope

    return DbScope.for_tenant(tenant_id)
