"""Integration kinds → adapter factories, config validation and per-call resolution."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.credentials import CredentialVault
from ..core.models import Integration
from ..shared.errors import ValidationFailedError
from ..shared.http import EgressPolicy
from ..shared.logging import get_logger
from .base import CRM, Calendar, ErrorKind, IntegrationContext, IntegrationError, Messaging
from .custom_rest import CustomRestConfig, CustomRestIntegration
from .google_calendar import GoogleCalendar, GoogleCalendarConfig
from .hubspot import HubSpotConfig, HubSpotCRM
from .salesforce import SalesforceConfig, SalesforceCRM
from .sandbox import SandboxState, SandboxSuite
from .twilio import TwilioConfig, TwilioMessaging

logger = get_logger(__name__)


@dataclass(frozen=True)
class IntegrationKind:
    name: str
    capabilities: frozenset[str]
    config_model: type[BaseModel]
    credential_kinds: frozenset[str]
    factory: Callable[[IntegrationContext], Any]
    description: str


def _sandbox(ctx: IntegrationContext) -> SandboxSuite:
    return SandboxSuite(ctx.config, name=ctx.name)


KINDS: dict[str, IntegrationKind] = {
    k.name: k for k in [
        IntegrationKind("hubspot", frozenset({"crm"}), HubSpotConfig, frozenset({"hubspot"}),
                        HubSpotCRM, "HubSpot CRM (contacts, deals/leads, notes)"),
        IntegrationKind("salesforce", frozenset({"crm"}), SalesforceConfig,
                        frozenset({"salesforce"}), SalesforceCRM,
                        "Salesforce (contacts, leads, call tasks)"),
        IntegrationKind("google_calendar", frozenset({"calendar"}), GoogleCalendarConfig,
                        frozenset({"google_service_account", "google_oauth"}), GoogleCalendar,
                        "Google Calendar availability and booking"),
        IntegrationKind("custom_rest", frozenset({"custom"}), CustomRestConfig,
                        frozenset({"bearer", "basic", "api_key_header"}),
                        lambda ctx: CustomRestIntegration(
                            ctx, allow_insecure=bool(ctx.extra.get("allow_insecure"))),
                        "Declarative REST actions with SSRF protection"),
        IntegrationKind("twilio", frozenset({"messaging"}), TwilioConfig, frozenset({"twilio"}),
                        TwilioMessaging, "Twilio SMS"),
        IntegrationKind("sandbox", frozenset({"crm", "calendar", "messaging"}), SandboxState,
                        frozenset(), _sandbox, "In-memory sandbox for testing (no real systems)"),
    ]
}


def validate_integration_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    spec = KINDS.get(kind)
    if spec is None:
        raise ValidationFailedError(f"unknown integration kind '{kind}'",
                                    details={"kinds": sorted(KINDS)})
    try:
        return spec.config_model.model_validate(config).model_dump(mode="json", exclude_none=True)
    except ValidationError as e:
        raise ValidationFailedError("invalid integration config", details={"errors": [
            {"loc": list(err["loc"]), "msg": err["msg"]} for err in e.errors()]}) from e


@dataclass
class IntegrationSet:
    """Adapters bound to one call/session, keyed by capability and by name."""

    crm: CRM | None = None
    calendar: Calendar | None = None
    messaging: Messaging | None = None
    custom: dict[str, CustomRestIntegration] = field(default_factory=dict)
    _all: list[Any] = field(default_factory=list)

    async def aclose(self) -> None:
        for adapter in self._all:
            close = getattr(adapter, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    logger.warning("integration close failed", exc_info=True)


class IntegrationResolver:
    def __init__(self, vault: CredentialVault, *, timeout_s: float = 8.0,
                 allow_insecure: bool = False) -> None:
        self._vault = vault
        self._timeout = timeout_s
        self._allow_insecure = allow_insecure

    async def build(self, session: AsyncSession, tenant_id: uuid.UUID, name: str,
                    extra: dict[str, Any] | None = None) -> Any:
        row = await session.scalar(sa.select(Integration).where(
            Integration.tenant_id == tenant_id, Integration.name == name))
        if row is None or not row.enabled:
            raise IntegrationError(ErrorKind.CONFIGURATION,
                                   f"integration '{name}' is missing or disabled")
        spec = KINDS[row.kind]
        creds = None
        if row.credential_id is not None:
            creds = await self._vault.resolve(session, tenant_id=tenant_id,
                                              credential_id=row.credential_id)
        ctx = IntegrationContext(
            tenant_id=tenant_id, name=row.name, kind=row.kind, config=row.config,
            credentials=creds, egress=EgressPolicy(), timeout_s=self._timeout,
            extra={"allow_insecure": self._allow_insecure, **(extra or {})},
        )
        return spec.factory(ctx)

    async def resolve_set(
        self, session: AsyncSession, tenant_id: uuid.UUID, *, crm: str | None,
        calendar: str | None, messaging: str | None, custom: list[str],
        timezone: str = "UTC",
    ) -> IntegrationSet:
        out = IntegrationSet()
        cache: dict[str, Any] = {}

        async def get(name: str) -> Any:
            if name not in cache:
                cache[name] = await self.build(session, tenant_id, name, {"timezone": timezone})
                out._all.append(cache[name])
            return cache[name]

        try:
            if crm:
                out.crm = await get(crm)
            if calendar:
                out.calendar = await get(calendar)
            if messaging:
                out.messaging = await get(messaging)
            for name in custom:
                adapter = await get(name)
                if not isinstance(adapter, CustomRestIntegration):
                    raise IntegrationError(ErrorKind.CONFIGURATION,
                                           f"integration '{name}' is not a custom_rest integration")
                out.custom[name] = adapter
        except Exception:
            await out.aclose()
            raise
        return out
