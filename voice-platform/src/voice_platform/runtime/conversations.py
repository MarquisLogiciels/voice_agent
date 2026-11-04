"""Conversation lifecycle persistence and per-call configuration resolution."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from ..config.service import AgentService, ResolvedVersion
from ..core.credentials import CredentialVault
from ..core.db import Database, DbScope
from ..core.models import Conversation
from ..shared.errors import NotFoundError
from ..shared.phone import try_normalize_e164


@dataclass
class OpenedCall:
    conversation_id: uuid.UUID
    resolved: ResolvedVersion
    provider_secrets: dict[str, dict[str, Any]]


async def open_call(
    db: Database,
    vault: CredentialVault,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    environment: str,
    direction: str,
    room_name: str,
    job_id: str | None,
    caller_number: str | None,
    callee_number: str | None,
    conversation_id: uuid.UUID | None = None,
    channel: str = "phone",
) -> OpenedCall:
    """Create (or adopt, for outbound) the conversation row, pin the agent version for this
    call and resolve provider credentials referenced by the pinned config."""
    scope = DbScope.for_tenant(tenant_id)
    async with db.session(scope) as s:
        conv_id = conversation_id or uuid.uuid4()
        resolved = await AgentService(s, tenant_id, None).resolve_for_call(
            agent_id, environment, str(conv_id)
        )
        row = await s.get(Conversation, conv_id) if conversation_id else None
        if row is None:
            row = Conversation(
                id=conv_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                environment=environment,
                channel=channel,
                direction=direction,
            )
            s.add(row)
        row.agent_version_id = resolved.version_id
        row.status = "in_progress"
        row.room_name = room_name
        row.job_id = job_id
        row.caller_number = try_normalize_e164(caller_number) or caller_number
        row.callee_number = try_normalize_e164(callee_number) or callee_number
        row.answered_at = datetime.now(UTC)
        row.recording_status = "pending" if resolved.config.recording.enabled else "disabled"
        row.meta = {
            **(row.meta or {}),
            "agent_version": resolved.version,
            "canary": resolved.is_canary,
        }
        secrets: dict[str, dict[str, Any]] = {}
        p = resolved.config.pipeline
        refs = [
            r
            for spec in (p.stt, p.llm, p.tts, p.realtime, p.realtime_tts)
            if spec
            for r in spec.chain()
        ]
        for name in {r.credential for r in refs if r.credential}:
            try:
                _kind, payload = await vault.resolve_by_name(s, tenant_id=tenant_id, name=name)
            except NotFoundError:
                continue  # builder reports the missing credential precisely
            secrets[name] = payload
    return OpenedCall(conversation_id=conv_id, resolved=resolved, provider_secrets=secrets)


async def mark_call(
    db: Database, tenant_id: uuid.UUID, conversation_id: uuid.UUID, **fields: Any
) -> None:
    async with db.session(DbScope.for_tenant(tenant_id)) as s:
        await s.execute(
            sa.update(Conversation).where(Conversation.id == conversation_id).values(**fields)
        )
