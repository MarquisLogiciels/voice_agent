"""Outbound calls: guardrails + LiveKit agent dispatch.

Flow: API validates the request (allowlist, do-not-call list, calling window, dry-run) and creates
the conversation row → creates an explicit agent dispatch into a fresh room with metadata →
the worker's entrypoint dials the callee with ``create_sip_participant(wait_until_answered)``
and only then starts the agent session.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from ..shared.errors import PermissionDeniedError, ValidationFailedError
from ..shared.phone import is_allowed_destination, normalize_e164


class CallingWindow(BaseModel):
    """Default mirrors common Canadian telemarketing limits (CRTC Unsolicited Telecom Rules:
    9:00–21:30 weekdays, 10:00–18:00 weekends, callee local time). Not legal advice — tenants
    are responsible for their own compliance."""

    model_config = ConfigDict(extra="forbid")
    weekday: tuple[str, str] = ("09:00", "21:30")
    weekend: tuple[str, str] = ("10:00", "18:00")
    timezone: str = "America/Toronto"

    def allows(self, now: datetime) -> bool:
        local = now.astimezone(ZoneInfo(self.timezone))
        start, end = self.weekend if local.weekday() >= 5 else self.weekday
        return time.fromisoformat(start) <= local.time() <= time.fromisoformat(end)


class OutboundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID
    to: str
    from_number: str | None = None
    environment: str = "prod"
    purpose: str = Field(default="", max_length=500)
    context: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class OutboundPolicy:
    allowed_prefixes: list[str]
    do_not_call: set[str]
    window: CallingWindow | None
    dry_run: bool


def check_outbound(req: OutboundRequest, policy: OutboundPolicy, now: datetime) -> str:
    to = normalize_e164(req.to)
    if to in policy.do_not_call:
        raise PermissionDeniedError("destination is on the do-not-call list")
    if not is_allowed_destination(to, policy.allowed_prefixes):
        raise PermissionDeniedError("destination not permitted by outbound allowlist")
    if policy.window is not None and not policy.window.allows(now):
        raise ValidationFailedError("outside the permitted calling window")
    if len(json.dumps(req.context)) > 4000:
        raise ValidationFailedError("context too large")
    return to


@dataclass(frozen=True)
class DispatchMetadata:
    tenant_id: str
    agent_id: str
    conversation_id: str
    environment: str
    to: str
    from_number: str | None
    trunk_id: str
    purpose: str
    context: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps({"kind": "outbound", **self.__dict__})

    @classmethod
    def parse(cls, raw: str) -> DispatchMetadata | None:
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("kind") != "outbound":
            return None
        data.pop("kind")
        return cls(**data)


@dataclass(frozen=True)
class WebDispatch:
    """Dispatch metadata for browser/app voice sessions (no SIP participant)."""

    tenant_id: str
    agent_id: str
    environment: str = "prod"
    conversation_id: str | None = None

    def to_json(self) -> str:
        return json.dumps({"kind": "web", **self.__dict__})

    @classmethod
    def parse(cls, raw: str) -> WebDispatch | None:
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("kind") != "web":
            return None
        data.pop("kind")
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


async def dispatch_outbound(lkapi: Any, *, agent_name: str, meta: DispatchMetadata) -> str:
    from livekit import api

    room = f"out-{meta.conversation_id}"
    await lkapi.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(agent_name=agent_name, room=room, metadata=meta.to_json())
    )
    return room
