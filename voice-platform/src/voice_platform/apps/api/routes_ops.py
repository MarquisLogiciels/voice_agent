"""Credentials, integrations, phone numbers, calls, conversations, analytics, webhooks."""

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, Header, Request, Response
from pydantic import BaseModel, Field

from ...core.audit import record_audit
from ...core.db import DbScope
from ...core.models import (
    AgentRow,
    Conversation,
    ConversationEvent,
    Credential,
    Integration,
    PhoneNumber,
    ToolExecution,
)
from ...core.rbac import Permission as P
from ...integrations.base import IntegrationError
from ...integrations.registry import KINDS, validate_integration_config
from ...observability.stats import summarize
from ...shared.blobstore import LocalBlobStore
from ...shared.errors import (
    ConfigurationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from ...shared.logging import get_logger
from ...shared.phone import normalize_e164
from ...telephony.outbound import (
    CallingWindow,
    DispatchMetadata,
    OutboundPolicy,
    OutboundRequest,
    check_outbound,
    dispatch_outbound,
)
from .deps import ServicesDep, TenantCtx, tenant_ctx

logger = get_logger(__name__)
router = APIRouter(prefix="/v1/tenants/{tenant_id}")
public = APIRouter(prefix="/v1")


# --- credentials (write-only) -----------------------------------------------------------------
class CredentialIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{0,99}$")
    kind: str
    payload: dict[str, Any]
    meta: dict[str, Any] = Field(default_factory=dict)


def _cred(c: Credential) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "name": c.name,
        "kind": c.kind,
        "meta": c.meta,
        "key_id": c.key_id,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    }


@router.post("/credentials", status_code=201)
async def create_credential(
    body: CredentialIn, ctx: TenantCtx = Depends(tenant_ctx(P.CREDENTIALS_WRITE))
) -> dict[str, Any]:
    cred = await ctx.services.vault.create(
        ctx.session,
        tenant_id=ctx.tenant_id,
        name=body.name,
        kind=body.kind,
        payload=body.payload,
        meta=body.meta,
        created_by=ctx.principal.user_id,
    )
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="credential.create",
        resource_type="credential",
        resource_id=cred.id,
        details={"name": body.name, "kind": body.kind},
    )
    return _cred(cred)


@router.get("/credentials")
async def list_credentials(
    ctx: TenantCtx = Depends(tenant_ctx(P.CREDENTIALS_READ)),
) -> list[dict[str, Any]]:
    rows = (
        await ctx.session.scalars(
            sa.select(Credential)
            .where(Credential.tenant_id == ctx.tenant_id)
            .order_by(Credential.name)
        )
    ).all()
    return [_cred(c) for c in rows]


class CredentialRotate(BaseModel):
    payload: dict[str, Any]


@router.put("/credentials/{credential_id}")
async def rotate_credential(
    credential_id: uuid.UUID,
    body: CredentialRotate,
    ctx: TenantCtx = Depends(tenant_ctx(P.CREDENTIALS_WRITE)),
) -> dict[str, Any]:
    cred = await ctx.services.vault.update(
        ctx.session, tenant_id=ctx.tenant_id, credential_id=credential_id, payload=body.payload
    )
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="credential.rotate",
        resource_type="credential",
        resource_id=credential_id,
    )
    return _cred(cred)


@router.delete("/credentials/{credential_id}", status_code=204)
async def delete_credential(
    credential_id: uuid.UUID, ctx: TenantCtx = Depends(tenant_ctx(P.CREDENTIALS_WRITE))
) -> None:
    cred = await ctx.session.scalar(
        sa.select(Credential).where(
            Credential.id == credential_id, Credential.tenant_id == ctx.tenant_id
        )
    )
    if cred is None:
        raise NotFoundError("credential not found")
    await ctx.session.delete(cred)
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="credential.delete",
        resource_type="credential",
        resource_id=credential_id,
    )


# --- integrations -----------------------------------------------------------------------------
class IntegrationIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{0,99}$")
    kind: str
    config: dict[str, Any] = Field(default_factory=dict)
    credential_id: uuid.UUID | None = None
    enabled: bool = True


def _integ(i: Integration) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "name": i.name,
        "kind": i.kind,
        "config": i.config,
        "credential_id": str(i.credential_id) if i.credential_id else None,
        "enabled": i.enabled,
        "capabilities": sorted(KINDS[i.kind].capabilities) if i.kind in KINDS else [],
    }


@public.get("/integration-kinds")
async def integration_kinds() -> list[dict[str, Any]]:
    return [
        {
            "kind": k.name,
            "description": k.description,
            "capabilities": sorted(k.capabilities),
            "credential_kinds": sorted(k.credential_kinds),
            "config_schema": k.config_model.model_json_schema(),
        }
        for k in KINDS.values()
    ]


async def _check_credential(ctx: TenantCtx, kind: str, credential_id: uuid.UUID | None) -> None:
    spec = KINDS[kind]
    if credential_id is None:
        if spec.credential_kinds and kind != "custom_rest":
            raise ValidationFailedError(f"{kind} requires a credential")
        return
    cred = await ctx.session.scalar(
        sa.select(Credential).where(
            Credential.id == credential_id, Credential.tenant_id == ctx.tenant_id
        )
    )
    if cred is None:
        raise NotFoundError("credential not found")
    if spec.credential_kinds and cred.kind not in spec.credential_kinds:
        raise ValidationFailedError(
            f"{kind} needs a credential of kind {sorted(spec.credential_kinds)}"
        )


@router.post("/integrations", status_code=201)
async def create_integration(
    body: IntegrationIn, ctx: TenantCtx = Depends(tenant_ctx(P.INTEGRATIONS_WRITE))
) -> dict[str, Any]:
    config = validate_integration_config(body.kind, body.config)
    await _check_credential(ctx, body.kind, body.credential_id)
    if await ctx.session.scalar(
        sa.select(Integration.id).where(
            Integration.tenant_id == ctx.tenant_id, Integration.name == body.name
        )
    ):
        raise ConflictError("integration name already used")
    integ = Integration(
        tenant_id=ctx.tenant_id,
        name=body.name,
        kind=body.kind,
        config=config,
        credential_id=body.credential_id,
        enabled=body.enabled,
    )
    ctx.session.add(integ)
    await ctx.session.flush()
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="integration.create",
        resource_type="integration",
        resource_id=integ.id,
        details={"kind": body.kind},
    )
    return _integ(integ)


@router.get("/integrations")
async def list_integrations(
    ctx: TenantCtx = Depends(tenant_ctx(P.INTEGRATIONS_READ)),
) -> list[dict[str, Any]]:
    rows = (
        await ctx.session.scalars(
            sa.select(Integration)
            .where(Integration.tenant_id == ctx.tenant_id)
            .order_by(Integration.name)
        )
    ).all()
    return [_integ(i) for i in rows]


@router.put("/integrations/{integration_id}")
async def update_integration(
    integration_id: uuid.UUID,
    body: IntegrationIn,
    ctx: TenantCtx = Depends(tenant_ctx(P.INTEGRATIONS_WRITE)),
) -> dict[str, Any]:
    integ = await ctx.session.scalar(
        sa.select(Integration).where(
            Integration.id == integration_id, Integration.tenant_id == ctx.tenant_id
        )
    )
    if integ is None:
        raise NotFoundError("integration not found")
    if body.kind != integ.kind:
        raise ValidationFailedError("integration kind cannot change")
    integ.config = validate_integration_config(body.kind, body.config)
    await _check_credential(ctx, body.kind, body.credential_id)
    integ.credential_id = body.credential_id
    integ.enabled = body.enabled
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="integration.update",
        resource_type="integration",
        resource_id=integ.id,
    )
    return _integ(integ)


@router.post("/integrations/{integration_id}/test")
async def test_integration(
    integration_id: uuid.UUID, ctx: TenantCtx = Depends(tenant_ctx(P.INTEGRATIONS_WRITE))
) -> dict[str, Any]:
    """Runs a read-only probe through the real adapter (never writes)."""
    integ = await ctx.session.scalar(
        sa.select(Integration).where(
            Integration.id == integration_id, Integration.tenant_id == ctx.tenant_id
        )
    )
    if integ is None:
        raise NotFoundError("integration not found")
    started = time.perf_counter()
    adapter = await ctx.services.resolver.build(ctx.session, ctx.tenant_id, integ.name)
    try:
        if integ.kind in ("hubspot", "salesforce"):
            await adapter.search_contacts("connectivity-check", limit=1)
        elif integ.kind == "google_calendar":
            now = datetime.now(UTC)
            await adapter.busy_intervals(now, now + timedelta(hours=1))
        elif integ.kind == "twilio":
            pass  # sending is the only operation; credential format was validated on save
        result: dict[str, Any] = {"ok": True}
    except IntegrationError as e:
        result = {"ok": False, "error": e.kind.value, "message": e.safe_message()}
    finally:
        close = getattr(adapter, "aclose", None)
        if close:
            await close()
    return {**result, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}


@router.delete("/integrations/{integration_id}", status_code=204)
async def delete_integration(
    integration_id: uuid.UUID, ctx: TenantCtx = Depends(tenant_ctx(P.INTEGRATIONS_WRITE))
) -> None:
    integ = await ctx.session.scalar(
        sa.select(Integration).where(
            Integration.id == integration_id, Integration.tenant_id == ctx.tenant_id
        )
    )
    if integ is None:
        raise NotFoundError("integration not found")
    await ctx.session.delete(integ)
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="integration.delete",
        resource_type="integration",
        resource_id=integration_id,
    )


# --- phone numbers ----------------------------------------------------------------------------
class PhoneNumberIn(BaseModel):
    e164: str
    agent_id: uuid.UUID | None = None
    environment: str = "prod"
    provider: str = "twilio"
    provider_ref: str | None = None
    provision_livekit: bool = False
    allowed_addresses: list[str] = Field(default_factory=list)
    livekit_outbound_trunk_id: str | None = None
    capabilities: dict[str, bool] = Field(default_factory=lambda: {"voice": True, "sms": False})


def _number(n: PhoneNumber) -> dict[str, Any]:
    return {
        "id": str(n.id),
        "e164": n.e164,
        "agent_id": str(n.agent_id) if n.agent_id else None,
        "environment": n.environment,
        "provider": n.provider,
        "status": n.status,
        "livekit_inbound_trunk_id": n.livekit_inbound_trunk_id,
        "livekit_dispatch_rule_id": n.livekit_dispatch_rule_id,
        "livekit_outbound_trunk_id": n.livekit_outbound_trunk_id,
        "capabilities": n.capabilities,
    }


@router.post("/phone-numbers", status_code=201)
async def add_number(
    body: PhoneNumberIn, ctx: TenantCtx = Depends(tenant_ctx(P.PHONE_NUMBERS_WRITE))
) -> dict[str, Any]:
    e164 = normalize_e164(body.e164)
    if (
        body.agent_id is not None
        and await ctx.session.scalar(
            sa.select(AgentRow.id).where(
                AgentRow.id == body.agent_id, AgentRow.tenant_id == ctx.tenant_id
            )
        )
        is None
    ):
        raise NotFoundError("agent not found")
    # numbers are globally unique: check across tenants without exposing the owner
    async with ctx.services.db.session(DbScope()) as s:
        taken = (
            await s.execute(sa.text("select * from vp_route_inbound_number(:n)"), {"n": e164})
        ).first()
    if taken is not None:
        raise ConflictError("number already assigned")
    number = PhoneNumber(
        tenant_id=ctx.tenant_id,
        e164=e164,
        agent_id=body.agent_id,
        environment=body.environment,
        provider=body.provider,
        provider_ref=body.provider_ref,
        capabilities=body.capabilities,
        livekit_outbound_trunk_id=body.livekit_outbound_trunk_id,
    )
    if body.provision_livekit:
        from ...telephony.provisioning import SipProvisioner
        from ...telephony.sip import livekit_api

        lk = livekit_api(ctx.services.settings)
        try:
            prov = await SipProvisioner(
                lk, agent_name=ctx.services.settings.agent_dispatch_name
            ).provision_inbound(
                e164=e164,
                label=str(ctx.tenant_id)[:8],
                allowed_addresses=body.allowed_addresses or None,
            )
        finally:
            await lk.aclose()
        number.livekit_inbound_trunk_id = prov.inbound_trunk_id
        number.livekit_dispatch_rule_id = prov.dispatch_rule_id
    ctx.session.add(number)
    await ctx.session.flush()
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="phone_number.add",
        resource_type="phone_number",
        resource_id=number.id,
        details={"provisioned": body.provision_livekit},
    )
    return _number(number)


@router.get("/phone-numbers")
async def list_numbers(
    ctx: TenantCtx = Depends(tenant_ctx(P.PHONE_NUMBERS_READ)),
) -> list[dict[str, Any]]:
    rows = (
        await ctx.session.scalars(
            sa.select(PhoneNumber)
            .where(PhoneNumber.tenant_id == ctx.tenant_id)
            .order_by(PhoneNumber.e164)
        )
    ).all()
    return [_number(n) for n in rows]


class NumberPatch(BaseModel):
    agent_id: uuid.UUID | None = None
    environment: str | None = None
    status: str | None = Field(default=None, pattern=r"^(active|disabled)$")


@router.patch("/phone-numbers/{number_id}")
async def update_number(
    number_id: uuid.UUID,
    body: NumberPatch,
    ctx: TenantCtx = Depends(tenant_ctx(P.PHONE_NUMBERS_WRITE)),
) -> dict[str, Any]:
    n = await ctx.session.scalar(
        sa.select(PhoneNumber).where(
            PhoneNumber.id == number_id, PhoneNumber.tenant_id == ctx.tenant_id
        )
    )
    if n is None:
        raise NotFoundError("phone number not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(n, k, v)
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="phone_number.update",
        resource_type="phone_number",
        resource_id=n.id,
        details=body.model_dump(exclude_unset=True, mode="json"),
    )
    return _number(n)


@router.delete("/phone-numbers/{number_id}", status_code=204)
async def release_number(
    number_id: uuid.UUID, ctx: TenantCtx = Depends(tenant_ctx(P.PHONE_NUMBERS_WRITE))
) -> None:
    n = await ctx.session.scalar(
        sa.select(PhoneNumber).where(
            PhoneNumber.id == number_id, PhoneNumber.tenant_id == ctx.tenant_id
        )
    )
    if n is None:
        raise NotFoundError("phone number not found")
    if n.livekit_inbound_trunk_id or n.livekit_dispatch_rule_id:
        from ...telephony.provisioning import SipProvisioner
        from ...telephony.sip import livekit_api

        lk = livekit_api(ctx.services.settings)
        try:
            await SipProvisioner(
                lk, agent_name=ctx.services.settings.agent_dispatch_name
            ).deprovision(
                inbound_trunk_id=n.livekit_inbound_trunk_id,
                dispatch_rule_id=n.livekit_dispatch_rule_id,
            )
        finally:
            await lk.aclose()
    await ctx.session.delete(n)
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="phone_number.release",
        resource_type="phone_number",
        resource_id=number_id,
    )


# --- outbound calls ---------------------------------------------------------------------------
@router.post("/calls/outbound", status_code=202)
async def outbound_call(
    body: OutboundRequest, ctx: TenantCtx = Depends(tenant_ctx(P.CALLS_OUTBOUND))
) -> dict[str, Any]:
    settings = ctx.services.settings
    tenant_settings = (
        await ctx.session.execute(
            sa.text("select settings from tenants where id = :t"), {"t": ctx.tenant_id}
        )
    ).scalar() or {}
    window = tenant_settings.get("outbound_calling_window")
    policy = OutboundPolicy(
        allowed_prefixes=settings.outbound_allowed_prefixes,
        do_not_call=set(tenant_settings.get("do_not_call", [])),
        window=CallingWindow.model_validate(window) if window is not None else CallingWindow(),
        dry_run=settings.telephony_dry_run,
    )
    to = check_outbound(body, policy, datetime.now(UTC))
    if (
        await ctx.session.scalar(
            sa.select(AgentRow.id).where(
                AgentRow.id == body.agent_id, AgentRow.tenant_id == ctx.tenant_id
            )
        )
        is None
    ):
        raise NotFoundError("agent not found")
    q = sa.select(PhoneNumber).where(
        PhoneNumber.tenant_id == ctx.tenant_id, PhoneNumber.livekit_outbound_trunk_id.is_not(None)
    )
    if body.from_number:
        q = q.where(PhoneNumber.e164 == normalize_e164(body.from_number))
    number = (await ctx.session.scalars(q.limit(1))).first()
    if number is None or not number.livekit_outbound_trunk_id:
        raise ConfigurationError("no phone number with an outbound trunk is configured")
    conv = Conversation(
        tenant_id=ctx.tenant_id,
        agent_id=body.agent_id,
        environment=body.environment,
        direction="outbound",
        status="initiated",
        caller_number=to,
        callee_number=number.e164,
        meta={"purpose": body.purpose, "requested_by": ctx.principal.actor_label},
    )
    ctx.session.add(conv)
    await ctx.session.flush()
    meta = DispatchMetadata(
        tenant_id=str(ctx.tenant_id),
        agent_id=str(body.agent_id),
        conversation_id=str(conv.id),
        environment=body.environment,
        to=to,
        from_number=number.e164,
        trunk_id=number.livekit_outbound_trunk_id,
        purpose=body.purpose,
        context=body.context,
    )
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="call.outbound",
        resource_type="conversation",
        resource_id=conv.id,
        details={"to_last4": to[-4:], "dry_run": settings.telephony_dry_run},
    )
    if settings.telephony_dry_run:
        conv.status = "failed"
        conv.end_reason = "dry_run"
        return {"conversation_id": str(conv.id), "dispatched": False, "dry_run": True}
    from ...telephony.sip import livekit_api

    lk = livekit_api(settings)
    try:
        room = await dispatch_outbound(lk, agent_name=settings.agent_dispatch_name, meta=meta)
    finally:
        await lk.aclose()
    conv.room_name = room
    return {"conversation_id": str(conv.id), "dispatched": True, "room": room}


# --- conversations ----------------------------------------------------------------------------
def _conv(c: Conversation) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "agent_id": str(c.agent_id) if c.agent_id else None,
        "agent_version_id": str(c.agent_version_id) if c.agent_version_id else None,
        "environment": c.environment,
        "direction": c.direction,
        "channel": c.channel,
        "status": c.status,
        "caller_number": c.caller_number,
        "callee_number": c.callee_number,
        "started_at": c.started_at.isoformat(),
        "ended_at": c.ended_at.isoformat() if c.ended_at else None,
        "duration_s": c.duration_s,
        "end_reason": c.end_reason,
        "outcome": c.outcome,
        "summary": c.summary,
        "recording_status": c.recording_status,
        "metrics": c.metrics,
        "cost": c.cost,
        "transfer": c.transfer,
        "meta": c.meta,
    }


@router.get("/conversations")
async def list_conversations(
    agent_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    before: datetime | None = None,
    ctx: TenantCtx = Depends(tenant_ctx(P.CONVERSATIONS_READ)),
) -> list[dict[str, Any]]:
    q = sa.select(Conversation).where(Conversation.tenant_id == ctx.tenant_id)
    if agent_id:
        q = q.where(Conversation.agent_id == agent_id)
    if status:
        q = q.where(Conversation.status == status)
    if before:
        q = q.where(Conversation.started_at < before)
    rows = (
        await ctx.session.scalars(q.order_by(Conversation.started_at.desc()).limit(min(limit, 200)))
    ).all()
    return [_conv(c) for c in rows]


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID, ctx: TenantCtx = Depends(tenant_ctx(P.CONVERSATIONS_READ))
) -> dict[str, Any]:
    c = await ctx.session.scalar(
        sa.select(Conversation).where(
            Conversation.id == conversation_id, Conversation.tenant_id == ctx.tenant_id
        )
    )
    if c is None:
        raise NotFoundError("conversation not found")
    events = (
        await ctx.session.scalars(
            sa.select(ConversationEvent)
            .where(ConversationEvent.conversation_id == conversation_id)
            .order_by(ConversationEvent.seq)
            .limit(5000)
        )
    ).all()
    tools = (
        await ctx.session.scalars(
            sa.select(ToolExecution)
            .where(ToolExecution.conversation_id == conversation_id)
            .order_by(ToolExecution.started_at)
        )
    ).all()
    return {
        **_conv(c),
        "events": [{"seq": e.seq, "type": e.type, **e.payload} for e in events],
        "tool_executions": [
            {
                "tool": t.tool_name,
                "status": t.status,
                "error_kind": t.error_kind,
                "duration_ms": t.duration_ms,
                "attempts": t.attempts,
            }
            for t in tools
        ],
    }


@router.get("/conversations/{conversation_id}/recording")
async def recording_url(
    conversation_id: uuid.UUID, ctx: TenantCtx = Depends(tenant_ctx(P.RECORDINGS_READ))
) -> dict[str, Any]:
    c = await ctx.session.scalar(
        sa.select(Conversation).where(
            Conversation.id == conversation_id, Conversation.tenant_id == ctx.tenant_id
        )
    )
    if c is None or not c.recording_uri:
        raise NotFoundError("no recording for this conversation")
    if not c.recording_uri.startswith(f"tenants/{ctx.tenant_id}/"):
        raise PermissionDeniedError("recording outside tenant prefix")
    url = await ctx.services.blobs.signed_url(c.recording_uri, ttl_s=300)
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="recording.access",
        resource_type="conversation",
        resource_id=conversation_id,
    )
    return {"url": url, "expires_in_s": 300}


# --- data subject requests (access / erasure by phone number) ---------------------------------
@router.get("/data-subjects/export")
async def export_subject(
    phone: str, ctx: TenantCtx = Depends(tenant_ctx(P.RECORDINGS_READ))
) -> dict[str, Any]:
    """Everything stored about a caller (by phone) in this tenant: access requests."""
    e164 = normalize_e164(phone)
    convs = (
        await ctx.session.scalars(
            sa.select(Conversation).where(
                Conversation.tenant_id == ctx.tenant_id,
                sa.or_(Conversation.caller_number == e164, Conversation.callee_number == e164),
            )
        )
    ).all()
    out = []
    for c in convs:
        events = (
            await ctx.session.scalars(
                sa.select(ConversationEvent)
                .where(ConversationEvent.conversation_id == c.id)
                .order_by(ConversationEvent.seq)
            )
        ).all()
        tools = (
            await ctx.session.scalars(
                sa.select(ToolExecution).where(ToolExecution.conversation_id == c.id)
            )
        ).all()
        out.append(
            {
                **_conv(c),
                "events": [{"type": e.type, **e.payload} for e in events],
                "tool_executions": [
                    {"tool": t.tool_name, "request": t.request, "response": t.response}
                    for t in tools
                ],
            }
        )
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="data_subject.export",
        resource_type="data_subject",
        resource_id=e164[-4:],
        details={"conversations": len(out)},
    )
    return {
        "phone": e164,
        "conversations": out,
        "note": "CRM/calendar records live in the tenant's own systems and are not included.",
    }


@router.delete("/data-subjects")
async def erase_subject(
    phone: str, ctx: TenantCtx = Depends(tenant_ctx(P.TENANT_UPDATE))
) -> dict[str, Any]:
    """Erase a caller's conversations, transcripts, tool payloads and recordings."""
    e164 = normalize_e164(phone)
    convs = (
        await ctx.session.scalars(
            sa.select(Conversation).where(
                Conversation.tenant_id == ctx.tenant_id,
                sa.or_(Conversation.caller_number == e164, Conversation.callee_number == e164),
            )
        )
    ).all()
    ids = [c.id for c in convs]
    recordings = 0
    for c in convs:
        if c.recording_uri:
            await ctx.services.blobs.delete(c.recording_uri)
            recordings += 1
    if ids:
        await ctx.session.execute(
            sa.update(ToolExecution)
            .where(ToolExecution.conversation_id.in_(ids))
            .values(request={}, response={})
        )
        await ctx.session.execute(sa.delete(Conversation).where(Conversation.id.in_(ids)))
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="data_subject.erase",
        resource_type="data_subject",
        resource_id=e164[-4:],
        details={"conversations": len(ids), "recordings": recordings},
    )
    return {
        "conversations_deleted": len(ids),
        "recordings_deleted": recordings,
        "note": "Also process the request in connected CRM/calendar systems.",
    }


# --- analytics --------------------------------------------------------------------------------
@router.get("/analytics/overview")
async def analytics(
    days: int = 30,
    agent_id: uuid.UUID | None = None,
    ctx: TenantCtx = Depends(tenant_ctx(P.CONVERSATIONS_READ)),
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=min(days, 365))
    q = sa.select(Conversation).where(
        Conversation.tenant_id == ctx.tenant_id,
        Conversation.started_at >= since,
        Conversation.direction != "simulation",
    )
    if agent_id:
        q = q.where(Conversation.agent_id == agent_id)
    convs = (await ctx.session.scalars(q.limit(20_000))).all()
    e2e = [
        v
        for c in convs
        for v in ((c.metrics or {}).get("latency", {}).get("e2e", {}),)
        if v.get("p50") is not None
    ]
    statuses: dict[str, int] = {}
    for c in convs:
        statuses[c.status] = statuses.get(c.status, 0) + 1
    tools = (
        await ctx.session.execute(
            sa.select(ToolExecution.tool_name, ToolExecution.status, sa.func.count())
            .where(ToolExecution.tenant_id == ctx.tenant_id, ToolExecution.started_at >= since)
            .group_by(ToolExecution.tool_name, ToolExecution.status)
        )
    ).all()
    tool_stats: dict[str, dict[str, int]] = {}
    for name, st, n in tools:
        tool_stats.setdefault(name, {})[st] = n
    durations = [c.duration_s for c in convs if c.duration_s]
    costs = [(c.cost or {}).get("total_usd", 0.0) for c in convs]
    return {
        "since": since.isoformat(),
        "calls": len(convs),
        "by_status": statuses,
        "transfer_rate": round(statuses.get("transferred", 0) / len(convs), 4) if convs else 0,
        "duration_s": summarize(durations),
        "response_latency_p50_s": summarize([v["p50"] for v in e2e]),
        "response_latency_p95_s": summarize([v["p95"] for v in e2e if v.get("p95")]),
        "estimated_cost_usd": {
            "total": round(sum(costs), 4),
            "per_call": round(sum(costs) / len(costs), 4) if costs else 0,
        },
        "tools": tool_stats,
    }


# --- webhooks ---------------------------------------------------------------------------------
@public.post("/webhooks/twilio/{tenant_id}/{integration_name}/sms-status")
async def twilio_sms_status(
    tenant_id: uuid.UUID,
    integration_name: str,
    request: Request,
    services: ServicesDep,
    x_twilio_signature: str = Header(default=""),
    MessageSid: str = Form(""),
    MessageStatus: str = Form(""),
) -> Response:
    from ...integrations.twilio import validate_twilio_signature

    form = {k: str(v) for k, v in (await request.form()).items()}
    async with services.db.session(DbScope.for_tenant(tenant_id)) as s:
        integ = await s.scalar(
            sa.select(Integration).where(
                Integration.tenant_id == tenant_id,
                Integration.name == integration_name,
                Integration.kind == "twilio",
            )
        )
        if integ is None or integ.credential_id is None:
            return Response(status_code=404)
        creds = await services.vault.resolve(
            s, tenant_id=tenant_id, credential_id=integ.credential_id
        )
        url = services.settings.public_base_url.rstrip("/") + request.url.path
        if not validate_twilio_signature(creds["auth_token"], url, form, x_twilio_signature):
            logger.warning("invalid Twilio signature", extra={"integration": integration_name})
            return Response(status_code=403)
        await record_audit(
            s,
            tenant_id=tenant_id,
            actor=None,
            action="sms.status",
            resource_type="sms",
            resource_id=MessageSid,
            details={"status": MessageStatus},
        )
    return Response(status_code=204)


@public.post("/webhooks/livekit")
async def livekit_webhook(
    request: Request, services: ServicesDep, authorization: str = Header(default="")
) -> Response:
    """Room/participant lifecycle from LiveKit (signed JWT). Marks calls whose worker died."""
    from livekit import api

    s = services.settings
    if not (s.livekit_api_key and s.livekit_api_secret):
        return Response(status_code=404)
    receiver = api.WebhookReceiver(
        api.TokenVerifier(
            s.livekit_api_key.get_secret_value(), s.livekit_api_secret.get_secret_value()
        )
    )
    try:
        event = receiver.receive((await request.body()).decode(), authorization)
    except Exception:
        return Response(status_code=401)
    if event.event == "room_finished" and event.room.name:
        async with services.db.session(DbScope.system()) as sess:
            await sess.execute(
                sa.update(Conversation)
                .where(
                    Conversation.room_name == event.room.name,
                    Conversation.status.in_(("initiated", "ringing", "in_progress")),
                )
                .values(status="completed", end_reason="room_finished", ended_at=datetime.now(UTC))
            )
    return Response(status_code=204)


# --- local blob downloads (dev blob backend only) ---------------------------------------------
@public.get("/blobs/{key:path}")
async def blob(key: str, expires: int, sig: str, services: ServicesDep) -> Response:
    store = services.blobs
    if not isinstance(store, LocalBlobStore) or not store.verify(key, expires, sig):
        return Response(status_code=403)
    data = await store.get(key)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, no-store"},
    )
