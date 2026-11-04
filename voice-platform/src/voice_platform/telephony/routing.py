"""Inbound call routing: dialed number → (tenant, agent, environment)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa

from ..core.db import Database, DbScope
from ..shared.phone import try_normalize_e164

# LiveKit SIP participant attributes (https://docs.livekit.io/sip/)
ATTR_TRUNK_NUMBER = "sip.trunkPhoneNumber"
ATTR_CALLER_NUMBER = "sip.phoneNumber"
ATTR_CALL_ID = "sip.callID"
ATTR_CALL_STATUS = "sip.callStatus"


@dataclass(frozen=True)
class InboundRoute:
    tenant_id: uuid.UUID
    agent_id: uuid.UUID | None
    environment: str
    phone_number_id: uuid.UUID
    dialed_number: str


async def route_inbound(db: Database, dialed_number: str) -> InboundRoute | None:
    e164 = try_normalize_e164(dialed_number)
    if not e164:
        return None
    async with db.session(DbScope()) as s:
        row = (
            await s.execute(sa.text("select * from vp_route_inbound_number(:n)"), {"n": e164})
        ).first()
    if row is None:
        return None
    return InboundRoute(
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        environment=row.environment,
        phone_number_id=row.phone_number_id,
        dialed_number=e164,
    )
