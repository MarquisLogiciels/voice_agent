"""Salesforce adapter (REST API + OAuth2 client-credentials or refresh-token flow).

Contacts are searched with SOSL (phone fields are format-insensitive in SOSL), leads are
standard ``Lead`` sobjects, notes are completed ``Task`` records (call logging).
Optional ``external_id_field`` (a custom External ID field on Lead) turns lead creation into
an idempotent upsert.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..shared.http import EgressPolicy
from ..shared.logging import secret_registry
from .base import (
    Contact,
    ContactInput,
    ContactUpdate,
    ErrorKind,
    IntegrationContext,
    IntegrationError,
    Lead,
    LeadInput,
    LeadUpdate,
)
from .http import IntegrationHttpClient

SALESFORCE_EGRESS = EgressPolicy(
    allowed_hosts=(
        "login.salesforce.com",
        "test.salesforce.com",
        "*.my.salesforce.com",
        "*.salesforce.com",
        "*.force.com",
    )
)
_SF_ID = re.compile(r"^[a-zA-Z0-9]{15}([a-zA-Z0-9]{3})?$")
_SOSL_RESERVED = re.compile(r'([?&|!{}\[\]()^~*:\\"\'+\-])')


def sosl_escape(term: str) -> str:
    """Escape SOSL reserved characters (prevents query injection)."""
    return _SOSL_RESERVED.sub(r"\\\1", term.strip())[:200]


def _check_id(value: str) -> str:
    if not _SF_ID.match(value):
        raise IntegrationError(ErrorKind.VALIDATION, "invalid Salesforce record id")
    return value


class SalesforceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_version: str = Field(default="v61.0", pattern=r"^v[0-9]{2}\.[0-9]$")
    lead_source: str = "Phone Inquiry"
    lead_status: str | None = None
    default_company: str = "Unknown"
    external_id_field: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]+__c$")


class _TokenManager:
    def __init__(self, creds: dict[str, Any], http: httpx.AsyncClient) -> None:
        self._creds = creds
        self._http = http
        self._lock = asyncio.Lock()
        self.access_token: str | None = None
        self.instance_url: str | None = None
        self._fetched_at = 0.0

    async def get(self, *, force: bool = False) -> tuple[str, str]:
        async with self._lock:
            if self.access_token and self.instance_url and not force:
                return self.access_token, self.instance_url
            login = str(self._creds.get("login_url") or "https://login.salesforce.com").rstrip("/")
            SALESFORCE_EGRESS.check_url(login)
            form = {
                "client_id": self._creds["client_id"],
                "client_secret": self._creds["client_secret"],
            }
            if self._creds.get("refresh_token"):
                form |= {
                    "grant_type": "refresh_token",
                    "refresh_token": self._creds["refresh_token"],
                }
            else:
                form |= {"grant_type": "client_credentials"}
            try:
                resp = await self._http.post(f"{login}/services/oauth2/token", data=form)
            except httpx.HTTPError as e:
                raise IntegrationError(
                    ErrorKind.UNAVAILABLE,
                    "Salesforce token request failed",
                    integration="salesforce",
                ) from e
            if resp.status_code != 200:
                raise IntegrationError(
                    ErrorKind.AUTH,
                    "Salesforce rejected the credentials",
                    status=resp.status_code,
                    integration="salesforce",
                )
            data = resp.json()
            self.access_token = data["access_token"]
            self.instance_url = data["instance_url"].rstrip("/")
            SALESFORCE_EGRESS.check_url(self.instance_url)
            secret_registry.register(self.access_token)
            self._fetched_at = time.monotonic()
            return self.access_token, self.instance_url


class SalesforceCRM:
    def __init__(
        self, ctx: IntegrationContext, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        from ..shared.http import _GuardedTransport

        self.name = ctx.name
        self.cfg = SalesforceConfig.model_validate(ctx.config)
        creds = ctx.credentials or {}
        if not creds.get("client_id") or not creds.get("client_secret"):
            raise IntegrationError(
                ErrorKind.CONFIGURATION, "Salesforce credential missing", integration=ctx.name
            )
        self._token_http = httpx.AsyncClient(
            timeout=ctx.timeout_s,
            transport=transport or _GuardedTransport(SALESFORCE_EGRESS),
            follow_redirects=False,
            trust_env=False,
        )
        self.tokens = _TokenManager(creds, self._token_http)
        self._timeout = ctx.timeout_s
        self._transport = transport
        self._http: IntegrationHttpClient | None = None

    async def aclose(self) -> None:
        await self._token_http.aclose()
        if self._http:
            await self._http.aclose()

    async def _client(self) -> IntegrationHttpClient:
        if self._http is None:
            _, instance = await self.tokens.get()

            async def auth(req: httpx.Request) -> None:
                token, _ = await self.tokens.get()
                req.headers["Authorization"] = f"Bearer {token}"

            self._http = IntegrationHttpClient(
                integration=f"salesforce:{self.name}",
                base_url=instance,
                egress=SALESFORCE_EGRESS,
                auth=auth,
                timeout_s=self._timeout,
                transport=self._transport,
            )
        return self._http

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        http = await self._client()
        base = f"/services/data/{self.cfg.api_version}"
        try:
            return await http.json(method, base + path, **kw)
        except IntegrationError as e:
            if e.kind is not ErrorKind.AUTH:
                raise
            await self.tokens.get(force=True)  # expired session: refresh once
            return await http.json(method, base + path, **kw)

    def _contact(self, r: dict[str, Any]) -> Contact:
        account = r.get("Account") or {}
        return Contact(
            id=r["Id"],
            first_name=r.get("FirstName"),
            last_name=r.get("LastName"),
            email=r.get("Email"),
            phone=r.get("Phone") or r.get("MobilePhone"),
            company=account.get("Name") if isinstance(account, dict) else None,
            source="salesforce",
            url=f"{self.tokens.instance_url}/lightning/r/Contact/{r['Id']}/view",
        )

    async def _sosl(self, term: str, scope: str, limit: int) -> list[Contact]:
        q = (
            f"FIND {{{sosl_escape(term)}}} IN {scope} FIELDS RETURNING "
            f"Contact(Id, FirstName, LastName, Email, Phone, MobilePhone, Account.Name "
            f"LIMIT {max(1, min(limit, 20))})"
        )
        data = await self._call("GET", "/search/", params={"q": q}, operation="contacts.search")
        return [self._contact(r) for r in (data or {}).get("searchRecords", [])]

    async def find_contacts_by_phone(self, phone_e164: str) -> list[Contact]:
        return await self._sosl(phone_e164.lstrip("+"), "PHONE", 5)

    async def search_contacts(self, query: str, limit: int = 5) -> list[Contact]:
        return await self._sosl(query, "NAME", limit)

    async def get_contact(self, contact_id: str) -> Contact | None:
        try:
            r = await self._call(
                "GET",
                f"/sobjects/Contact/{_check_id(contact_id)}",
                params={"fields": "Id,FirstName,LastName,Email,Phone,MobilePhone"},
                operation="contacts.get",
            )
        except IntegrationError as e:
            if e.kind is ErrorKind.NOT_FOUND:
                return None
            raise
        return self._contact(r)

    @staticmethod
    def _fields(data: ContactInput | ContactUpdate) -> dict[str, Any]:
        m = {"first_name": "FirstName", "last_name": "LastName", "email": "Email", "phone": "Phone"}
        return {m[k]: str(v) for k, v in data.model_dump(exclude_none=True).items() if k in m}

    async def create_contact(self, data: ContactInput, *, idempotency_key: str) -> Contact:
        if data.phone:
            existing = await self.find_contacts_by_phone(data.phone)
            if existing:
                return existing[0]
        fields = self._fields(data)
        fields.setdefault("LastName", data.last_name or "Unknown")
        if data.notes:
            fields["Description"] = data.notes
        created = await self._call(
            "POST", "/sobjects/Contact/", json_body=fields, operation="contacts.create"
        )
        contact = await self.get_contact(created["id"])
        if contact is None:
            raise IntegrationError(ErrorKind.UNKNOWN, "contact created but not readable")
        return contact

    async def update_contact(self, contact_id: str, data: ContactUpdate) -> Contact:
        fields = self._fields(data)
        if not fields:
            raise IntegrationError(ErrorKind.VALIDATION, "no fields to update")
        await self._call(
            "PATCH",
            f"/sobjects/Contact/{_check_id(contact_id)}",
            json_body=fields,
            retry_unsafe=True,
            operation="contacts.update",
        )
        contact = await self.get_contact(contact_id)
        if contact is None:
            raise IntegrationError(ErrorKind.NOT_FOUND, "contact not found after update")
        return contact

    async def create_lead(self, data: LeadInput, *, idempotency_key: str) -> Lead:
        fields: dict[str, Any] = {
            "FirstName": data.first_name,
            "LastName": data.last_name or "Unknown",
            "Company": data.company or self.cfg.default_company,
            "Phone": data.phone,
            "Email": str(data.email) if data.email else None,
            "Title": data.title[:128],
            "Description": data.description,
            "LeadSource": self.cfg.lead_source,
        }
        if self.cfg.lead_status:
            fields["Status"] = self.cfg.lead_status
        fields = {k: v for k, v in fields.items() if v is not None}
        if self.cfg.external_id_field:
            key = re.sub(r"[^A-Za-z0-9_\-]", "", idempotency_key)[:80]
            result = await self._call(
                "PATCH",
                f"/sobjects/Lead/{self.cfg.external_id_field}/{key}",
                json_body=fields,
                retry_unsafe=True,
                operation="leads.upsert",
            )
            lead_id = (result or {}).get("id")
            if not lead_id:  # 204 on update of an existing record
                existing = await self._call(
                    "GET",
                    f"/sobjects/Lead/{self.cfg.external_id_field}/{key}",
                    params={"fields": "Id"},
                    operation="leads.get",
                )
                lead_id = existing["Id"]
        else:
            result = await self._call(
                "POST", "/sobjects/Lead/", json_body=fields, operation="leads.create"
            )
            lead_id = result["id"]
        return Lead(
            id=lead_id,
            contact_id=data.contact_id,
            title=data.title,
            status=self.cfg.lead_status,
            description=data.description,
            source="salesforce",
            url=f"{self.tokens.instance_url}/lightning/r/Lead/{lead_id}/view",
        )

    async def update_lead(self, lead_id: str, data: LeadUpdate) -> Lead:
        m = {"status": "Status", "description": "Description", "title": "Title"}
        fields = {m[k]: v for k, v in data.changes().items() if k in m}
        if not fields:
            raise IntegrationError(ErrorKind.VALIDATION, "no fields to update")
        await self._call(
            "PATCH",
            f"/sobjects/Lead/{_check_id(lead_id)}",
            json_body=fields,
            retry_unsafe=True,
            operation="leads.update",
        )
        r = await self._call(
            "GET",
            f"/sobjects/Lead/{lead_id}",
            params={"fields": "Id,Title,Status,Description"},
            operation="leads.get",
        )
        return Lead(
            id=r["Id"],
            title=r.get("Title") or "",
            status=r.get("Status"),
            description=r.get("Description"),
            source="salesforce",
        )

    async def add_note(self, contact_id: str, text: str, *, idempotency_key: str) -> str:
        task = {
            "WhoId": _check_id(contact_id),
            "Subject": "Phone call note (AI agent)",
            "Description": text[:32_000],
            "Status": "Completed",
            "TaskSubtype": "Call",
        }
        created = await self._call(
            "POST", "/sobjects/Task/", json_body=task, operation="notes.create"
        )
        return str(created["id"])
