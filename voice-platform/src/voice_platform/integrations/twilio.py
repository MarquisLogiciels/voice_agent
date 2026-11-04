"""Twilio: SMS messaging, webhook signature validation and Elastic SIP trunk provisioning.

Live verification needs a Twilio account (test credentials cannot send real SMS; use a
verified number on a trial account).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from ..shared.http import EgressPolicy
from ..shared.phone import normalize_e164
from .base import ErrorKind, IntegrationContext, IntegrationError, MessageResult
from .http import IntegrationHttpClient

TWILIO_EGRESS = EgressPolicy(allowed_hosts=("api.twilio.com", "trunking.twilio.com"))


def validate_twilio_signature(
    auth_token: str, url: str, params: Mapping[str, str], signature: str
) -> bool:
    """Twilio request validation (X-Twilio-Signature): HMAC-SHA1 over URL + sorted params."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    expected = base64.b64encode(
        hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(expected, signature or "")


class TwilioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status_callback_url: str | None = None
    messaging_service_sid: str | None = None


def _auth(creds: dict[str, Any]) -> Any:
    sid, token = creds["account_sid"], creds["auth_token"]
    header = "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()

    async def hook(req: httpx.Request) -> None:
        req.headers["Authorization"] = header

    return hook


class TwilioMessaging:
    def __init__(
        self, ctx: IntegrationContext, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.name = ctx.name
        self.cfg = TwilioConfig.model_validate(ctx.config)
        creds = ctx.credentials or {}
        if not creds.get("account_sid") or not creds.get("auth_token"):
            raise IntegrationError(ErrorKind.CONFIGURATION, "Twilio credential missing")
        self.account_sid = creds["account_sid"]
        self.http = IntegrationHttpClient(
            integration=f"twilio:{ctx.name}",
            base_url="https://api.twilio.com",
            egress=TWILIO_EGRESS,
            auth=_auth(creds),
            timeout_s=ctx.timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def send_sms(
        self, *, to: str, body: str, from_number: str, idempotency_key: str
    ) -> MessageResult:
        # Twilio's Messages API has no idempotency key; POSTs are therefore never retried on
        # ambiguous failures and duplicate prevention relies on the ToolExecutor's ledger.
        form: dict[str, Any] = {"To": normalize_e164(to), "Body": body[:1600]}
        if self.cfg.messaging_service_sid:
            form["MessagingServiceSid"] = self.cfg.messaging_service_sid
        else:
            form["From"] = normalize_e164(from_number)
        if self.cfg.status_callback_url:
            form["StatusCallback"] = self.cfg.status_callback_url
        data = await self.http.json(
            "POST",
            f"/2010-04-01/Accounts/{self.account_sid}/Messages.json",
            data=form,
            operation="messages.create",
        )
        return MessageResult(id=data["sid"], status=data.get("status", "queued"))


class TwilioTrunking:
    """Provisions an Elastic SIP trunk pointing at LiveKit SIP and attaches phone numbers."""

    def __init__(
        self,
        creds: dict[str, Any],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.api = IntegrationHttpClient(
            integration="twilio:trunking",
            base_url="https://trunking.twilio.com",
            egress=TWILIO_EGRESS,
            auth=_auth(creds),
            timeout_s=timeout_s,
            transport=transport,
        )
        self.core = IntegrationHttpClient(
            integration="twilio:core",
            base_url="https://api.twilio.com",
            egress=TWILIO_EGRESS,
            auth=_auth(creds),
            timeout_s=timeout_s,
            transport=transport,
        )
        self.account_sid = creds["account_sid"]

    async def aclose(self) -> None:
        await self.api.aclose()
        await self.core.aclose()

    async def ensure_trunk(self, *, friendly_name: str, livekit_sip_uri: str) -> str:
        """Create (or reuse) a trunk whose origination URL is LiveKit's SIP endpoint."""
        listing = await self.api.json(
            "GET", "/v1/Trunks", params={"PageSize": 100}, operation="trunks.list"
        )
        for trunk in (listing or {}).get("trunks", []):
            if trunk.get("friendly_name") == friendly_name:
                return str(trunk["sid"])
        trunk = await self.api.json(
            "POST",
            "/v1/Trunks",
            data={"FriendlyName": friendly_name, "TransferMode": "enable-all"},
            operation="trunks.create",
        )
        await self.api.json(
            "POST",
            f"/v1/Trunks/{trunk['sid']}/OriginationUrls",
            data={
                "FriendlyName": "livekit",
                "SipUrl": livekit_sip_uri,
                "Priority": 1,
                "Weight": 1,
                "Enabled": "true",
            },
            operation="trunks.origination",
        )
        return str(trunk["sid"])

    async def attach_number(self, trunk_sid: str, e164: str) -> str:
        found = await self.core.json(
            "GET",
            f"/2010-04-01/Accounts/{self.account_sid}/IncomingPhoneNumbers.json",
            params={"PhoneNumber": normalize_e164(e164)},
            operation="numbers.lookup",
        )
        numbers = (found or {}).get("incoming_phone_numbers", [])
        if not numbers:
            raise IntegrationError(ErrorKind.NOT_FOUND, "phone number not found in Twilio account")
        pn_sid = numbers[0]["sid"]
        try:
            await self.api.json(
                "POST",
                f"/v1/Trunks/{trunk_sid}/PhoneNumbers",
                data={"PhoneNumberSid": pn_sid},
                operation="trunks.attach",
            )
        except IntegrationError as e:
            if e.kind is not ErrorKind.CONFLICT:
                raise
        return str(pn_sid)
