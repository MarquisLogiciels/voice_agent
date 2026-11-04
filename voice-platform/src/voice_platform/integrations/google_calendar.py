"""Google Calendar adapter (Calendar API v3).

Auth: service account (optionally with domain-wide delegation) or an OAuth refresh token.
Event creation is idempotent: the event id is derived from the idempotency key, so a retried
insert returns 409 and the existing event is returned instead of creating a duplicate.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from datetime import datetime
from typing import Any

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field

from ..shared.http import EgressPolicy
from ..shared.logging import secret_registry
from .base import (
    Appointment,
    AppointmentInput,
    ErrorKind,
    IntegrationContext,
    IntegrationError,
    TimeSlot,
)
from .http import IntegrationHttpClient

GOOGLE_EGRESS = EgressPolicy(allowed_hosts=("oauth2.googleapis.com", "www.googleapis.com"))
SCOPE = "https://www.googleapis.com/auth/calendar"
_B32HEX = "0123456789abcdefghijklmnopqrstuv"


def event_id_for(key: str) -> str:
    """Google requires ids in base32hex (0-9, a-v), 5-1024 chars."""
    digest = hashlib.sha256(key.encode()).digest()
    bits = int.from_bytes(digest, "big")
    out = []
    for _ in range(40):
        out.append(_B32HEX[bits & 31])
        bits >>= 5
    return "vp" + "".join(out)


class GoogleCalendarConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calendar_id: str = Field(default="primary", max_length=300)
    send_updates: str = Field(default="none", pattern=r"^(all|externalOnly|none)$")
    event_title_prefix: str = Field(default="", max_length=50)


class _GoogleAuth:
    def __init__(self, creds: dict[str, Any], http: httpx.AsyncClient) -> None:
        self._creds = creds
        self._http = http
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires = 0.0

    async def token(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._expires - 60:
                return self._token
            c = self._creds
            if "private_key" in c:
                now = int(time.time())
                claims = {
                    "iss": c["client_email"],
                    "scope": SCOPE,
                    "aud": c.get("token_uri", "https://oauth2.googleapis.com/token"),
                    "iat": now,
                    "exp": now + 3600,
                }
                if c.get("delegated_user"):
                    claims["sub"] = c["delegated_user"]
                assertion = jwt.encode(claims, c["private_key"], algorithm="RS256")
                form = {
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                }
                url = c.get("token_uri", "https://oauth2.googleapis.com/token")
            elif "refresh_token" in c:
                form = {
                    "grant_type": "refresh_token",
                    "refresh_token": c["refresh_token"],
                    "client_id": c["client_id"],
                    "client_secret": c["client_secret"],
                }
                url = "https://oauth2.googleapis.com/token"
            else:
                raise IntegrationError(ErrorKind.CONFIGURATION, "Google credential incomplete")
            GOOGLE_EGRESS.check_url(url)
            try:
                resp = await self._http.post(url, data=form)
            except httpx.HTTPError as e:
                raise IntegrationError(ErrorKind.UNAVAILABLE, "Google token request failed") from e
            if resp.status_code != 200:
                raise IntegrationError(
                    ErrorKind.AUTH, "Google rejected the credentials", status=resp.status_code
                )
            data = resp.json()
            self._token = str(data["access_token"])
            self._expires = time.time() + float(data.get("expires_in", 3600))
            secret_registry.register(self._token)
            return self._token


def _dt(value: dict[str, Any]) -> datetime:
    raw = value.get("dateTime") or value.get("date")
    if raw is None:
        raise IntegrationError(ErrorKind.UNKNOWN, "event without start/end")
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


class GoogleCalendar:
    def __init__(
        self,
        ctx: IntegrationContext,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timezone: str = "UTC",
    ) -> None:
        from ..shared.http import _GuardedTransport

        self.name = ctx.name
        self.cfg = GoogleCalendarConfig.model_validate(ctx.config)
        self.timezone = ctx.extra.get("timezone", timezone)
        creds = ctx.credentials or {}
        if not creds:
            raise IntegrationError(
                ErrorKind.CONFIGURATION, "Google credential missing", integration=ctx.name
            )
        self._token_http = httpx.AsyncClient(
            timeout=ctx.timeout_s,
            transport=transport or _GuardedTransport(GOOGLE_EGRESS),
            follow_redirects=False,
            trust_env=False,
        )
        self._auth = _GoogleAuth(creds, self._token_http)

        async def auth(req: httpx.Request) -> None:
            req.headers["Authorization"] = f"Bearer {await self._auth.token()}"

        self.http = IntegrationHttpClient(
            integration=f"google_calendar:{ctx.name}",
            base_url="https://www.googleapis.com",
            egress=GOOGLE_EGRESS,
            auth=auth,
            timeout_s=ctx.timeout_s,
            transport=transport,
        )
        from urllib.parse import quote

        self._cal = quote(self.cfg.calendar_id, safe="")

    async def aclose(self) -> None:
        await self.http.aclose()
        await self._token_http.aclose()

    async def busy_intervals(self, start: datetime, end: datetime) -> list[TimeSlot]:
        body = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "items": [{"id": self.cfg.calendar_id}],
        }
        data = await self.http.json(
            "POST", "/calendar/v3/freeBusy", json_body=body, retry_unsafe=True, operation="freebusy"
        )
        cal = (data or {}).get("calendars", {}).get(self.cfg.calendar_id, {})
        if cal.get("errors"):
            raise IntegrationError(
                ErrorKind.PERMISSION
                if any(e.get("reason") == "notFound" for e in cal["errors"])
                else ErrorKind.UNKNOWN,
                "calendar not accessible",
                integration=self.name,
            )
        return [
            TimeSlot(
                start=datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                end=datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
            )
            for b in cal.get("busy", [])
        ]

    def _appointment(self, ev: dict[str, Any]) -> Appointment:
        priv = (ev.get("extendedProperties") or {}).get("private") or {}
        return Appointment(
            id=ev["id"],
            start=_dt(ev["start"]),
            end=_dt(ev["end"]),
            title=ev.get("summary") or "",
            status=ev.get("status", "confirmed"),
            attendee_name=priv.get("vp_attendee_name"),
            attendee_phone=priv.get("vp_attendee_phone"),
            attendee_email=priv.get("vp_attendee_email") or None,
            notes=ev.get("description"),
            source="google_calendar",
        )

    async def list_appointments(self, start: datetime, end: datetime) -> list[Appointment]:
        out: list[Appointment] = []

        def next_page(data: Any) -> tuple[str | None, dict[str, Any] | None]:
            token = (data or {}).get("nextPageToken")
            if not token:
                return None, None
            return path, {"params": {**params, "pageToken": token}}

        path = f"/calendar/v3/calendars/{self._cal}/events"
        params = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "100",
        }
        async for ev in self.http.paginate(
            "GET",
            path,
            params=params,
            next_page=next_page,
            items=lambda d: (d or {}).get("items", []),
            max_items=200,
            operation="events.list",
        ):
            if ev.get("status") != "cancelled" and "dateTime" in (ev.get("start") or {}):
                out.append(self._appointment(ev))
        return out

    async def get_appointment(self, appointment_id: str) -> Appointment | None:
        from urllib.parse import quote

        try:
            ev = await self.http.json(
                "GET",
                f"/calendar/v3/calendars/{self._cal}/events/{quote(appointment_id, safe='')}",
                operation="events.get",
            )
        except IntegrationError as e:
            if e.kind is ErrorKind.NOT_FOUND:
                return None
            raise
        if ev.get("status") == "cancelled":
            return None
        return self._appointment(ev)

    async def create_appointment(
        self, data: AppointmentInput, *, idempotency_key: str
    ) -> Appointment:
        event_id = event_id_for(idempotency_key)
        description = "\n".join(
            filter(
                None,
                [
                    f"Booked by phone agent for {data.attendee_name}",
                    f"Phone: {data.attendee_phone}" if data.attendee_phone else None,
                    f"Email: {data.attendee_email}" if data.attendee_email else None,
                    data.notes,
                ],
            )
        )
        body = {
            "id": event_id,
            "summary": f"{self.cfg.event_title_prefix}{data.title}",
            "description": description,
            "start": {"dateTime": data.start.isoformat(), "timeZone": self.timezone},
            "end": {"dateTime": data.end.isoformat(), "timeZone": self.timezone},
            "extendedProperties": {
                "private": {
                    "vp_attendee_name": data.attendee_name,
                    "vp_attendee_phone": data.attendee_phone or "",
                    "vp_attendee_email": str(data.attendee_email or ""),
                }
            },
        }
        try:
            ev = await self.http.json(
                "POST",
                f"/calendar/v3/calendars/{self._cal}/events",
                json_body=body,
                params={"sendUpdates": self.cfg.send_updates},
                retry_unsafe=True,
                operation="events.insert",
            )
        except IntegrationError as e:
            if e.kind is not ErrorKind.CONFLICT:
                raise
            existing = await self.get_appointment(event_id)
            if existing is None:
                raise
            return existing
        return self._appointment(ev)

    async def cancel_appointment(self, appointment_id: str) -> None:
        from urllib.parse import quote

        await self.http.request(
            "DELETE",
            f"/calendar/v3/calendars/{self._cal}/events/{quote(appointment_id, safe='')}",
            params={"sendUpdates": self.cfg.send_updates},
            expected=(410,),
            operation="events.delete",
        )


def encode_state(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode()
