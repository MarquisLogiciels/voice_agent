"""HubSpot CRM adapter (CRM v3 objects API, private-app or OAuth bearer token).

Live verification checklist (needs a HubSpot developer test account):
contact search by phone, create/update contact, deal or lead creation with association, note.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..shared.http import EgressPolicy
from ..shared.phone import national_digits, try_normalize_e164
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

HUBSPOT_EGRESS = EgressPolicy(allowed_hosts=("api.hubapi.com",))
CONTACT_PROPS = ["firstname", "lastname", "email", "phone", "mobilephone", "company"]
_ID = re.compile(r"^[0-9]{1,20}$")


class HubSpotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_object: Literal["deals", "leads"] = "deals"
    deal_pipeline: str = "default"
    deal_stage: str = "appointmentscheduled"
    lead_pipeline_stage: str | None = None
    portal_id: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")
    # HubSpot-defined association type ids (see HubSpot associations v4 docs)
    deal_to_contact_type: int = 3
    lead_to_contact_type: int = 578
    note_to_contact_type: int = 202
    idempotency_property: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,99}$",
        description="Internal name of a single-line text property (create it on deals/leads "
        "in the portal) that stores the platform's idempotency key. HubSpot has no idempotency "
        "keys: without this, a create that times out after HubSpot stored it can be "
        "duplicated by a retry.",
    )


def _check_id(value: str) -> str:
    if not _ID.match(value):
        raise IntegrationError(ErrorKind.VALIDATION, "invalid HubSpot record id")
    return value


class HubSpotCRM:
    def __init__(
        self, ctx: IntegrationContext, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.name = ctx.name
        self.cfg = HubSpotConfig.model_validate(ctx.config)
        token = (ctx.credentials or {}).get("access_token")
        if not token:
            raise IntegrationError(
                ErrorKind.CONFIGURATION, "HubSpot credential missing", integration=ctx.name
            )

        async def auth(req: httpx.Request) -> None:
            req.headers["Authorization"] = f"Bearer {token}"

        self.http = IntegrationHttpClient(
            integration=f"hubspot:{ctx.name}",
            base_url="https://api.hubapi.com",
            egress=HUBSPOT_EGRESS,
            auth=auth,
            timeout_s=ctx.timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    def _contact(self, obj: dict[str, Any]) -> Contact:
        p = obj.get("properties") or {}
        url = None
        if self.cfg.portal_id:
            url = f"https://app.hubspot.com/contacts/{self.cfg.portal_id}/record/0-1/{obj['id']}"
        return Contact(
            id=str(obj["id"]),
            first_name=p.get("firstname"),
            last_name=p.get("lastname"),
            email=p.get("email"),
            phone=p.get("phone") or p.get("mobilephone"),
            company=p.get("company"),
            source="hubspot",
            url=url,
        )

    async def _search(self, body: dict[str, Any]) -> list[Contact]:
        data = await self.http.json(
            "POST",
            "/crm/v3/objects/contacts/search",
            json_body=body,
            retry_unsafe=True,
            operation="contacts.search",
        )
        return [self._contact(r) for r in (data or {}).get("results", [])]

    async def find_contacts_by_phone(self, phone_e164: str) -> list[Contact]:
        digits = national_digits(phone_e164)
        groups = [
            {"filters": [{"propertyName": "phone", "operator": "EQ", "value": phone_e164}]},
            {"filters": [{"propertyName": "mobilephone", "operator": "EQ", "value": phone_e164}]},
            {
                "filters": [
                    {
                        "propertyName": "hs_searchable_calculated_phone_number",
                        "operator": "EQ",
                        "value": digits,
                    }
                ]
            },
        ]
        return await self._search({"filterGroups": groups, "properties": CONTACT_PROPS, "limit": 5})

    async def search_contacts(self, query: str, limit: int = 5) -> list[Contact]:
        return await self._search(
            {"query": query[:200], "properties": CONTACT_PROPS, "limit": max(1, min(limit, 20))}
        )

    async def get_contact(self, contact_id: str) -> Contact | None:
        try:
            data = await self.http.json(
                "GET",
                f"/crm/v3/objects/contacts/{_check_id(contact_id)}",
                params={"properties": ",".join(CONTACT_PROPS)},
                operation="contacts.get",
            )
        except IntegrationError as e:
            if e.kind is ErrorKind.NOT_FOUND:
                return None
            raise
        return self._contact(data)

    @staticmethod
    def _props(data: ContactInput | ContactUpdate) -> dict[str, Any]:
        mapping = {
            "first_name": "firstname",
            "last_name": "lastname",
            "email": "email",
            "phone": "phone",
            "company": "company",
        }
        raw = data.model_dump(exclude_none=True)
        props = {mapping[k]: v for k, v in raw.items() if k in mapping}
        if "phone" in props:
            props["phone"] = try_normalize_e164(props["phone"]) or props["phone"]
        return props

    async def create_contact(self, data: ContactInput, *, idempotency_key: str) -> Contact:
        # Dedupe first: HubSpot has no idempotency keys, and duplicate contacts are costly.
        if data.phone:
            e164 = try_normalize_e164(data.phone)
            if e164:
                existing = await self.find_contacts_by_phone(e164)
                if existing:
                    return existing[0]
        try:
            created = await self.http.json(
                "POST",
                "/crm/v3/objects/contacts",
                json_body={"properties": self._props(data)},
                operation="contacts.create",
            )
        except IntegrationError as e:
            if e.kind is ErrorKind.CONFLICT and data.email:
                found = await self._search(
                    {
                        "filterGroups": [
                            {
                                "filters": [
                                    {
                                        "propertyName": "email",
                                        "operator": "EQ",
                                        "value": str(data.email),
                                    }
                                ]
                            }
                        ],
                        "properties": CONTACT_PROPS,
                        "limit": 1,
                    }
                )
                if found:
                    return found[0]
            if e.ambiguous and data.phone and (e164 := try_normalize_e164(data.phone)):
                found = await self.find_contacts_by_phone(e164)
                if found:
                    return found[0]
            raise
        contact = self._contact(created)
        if data.notes:
            await self.add_note(contact.id, data.notes, idempotency_key=idempotency_key + ":note")
        return contact

    async def update_contact(self, contact_id: str, data: ContactUpdate) -> Contact:
        props = self._props(data)
        if not props:
            raise IntegrationError(ErrorKind.VALIDATION, "no fields to update")
        updated = await self.http.json(
            "PATCH",
            f"/crm/v3/objects/contacts/{_check_id(contact_id)}",
            json_body={"properties": props},
            retry_unsafe=True,
            operation="contacts.update",
        )
        return self._contact(updated)

    async def create_lead(self, data: LeadInput, *, idempotency_key: str) -> Lead:
        contact_id = data.contact_id
        if contact_id is None and (data.phone or data.email or data.last_name):
            contact = await self.create_contact(
                ContactInput(
                    first_name=data.first_name,
                    last_name=data.last_name,
                    email=data.email,
                    phone=data.phone,
                    company=data.company,
                ),
                idempotency_key=idempotency_key + ":contact",
            )
            contact_id = contact.id
        if self.cfg.lead_object == "deals":
            path = "/crm/v3/objects/deals"
            props: dict[str, Any] = {
                "dealname": data.title,
                "pipeline": self.cfg.deal_pipeline,
                "dealstage": self.cfg.deal_stage,
            }
            if data.description:
                props["description"] = data.description
            assoc_type = self.cfg.deal_to_contact_type
        else:
            if contact_id is None:
                raise IntegrationError(
                    ErrorKind.VALIDATION, "HubSpot leads must be associated with a contact"
                )
            path = "/crm/v3/objects/leads"
            props = {"hs_lead_name": data.title}
            if self.cfg.lead_pipeline_stage:
                props["hs_pipeline_stage"] = self.cfg.lead_pipeline_stage
            assoc_type = self.cfg.lead_to_contact_type
        obj = path.rsplit("/", 1)[-1]
        key_prop = self.cfg.idempotency_property
        key = re.sub(r"[^A-Za-z0-9_:\-]", "", idempotency_key)[:200]
        if key_prop:
            existing = await self._find_by_key(obj, key_prop, key)
            if existing is not None:
                return self._lead(existing, contact_id, data.title)
            props[key_prop] = key
        body: dict[str, Any] = {"properties": props}
        if contact_id:
            body["associations"] = [
                {
                    "to": {"id": _check_id(contact_id)},
                    "types": [
                        {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": assoc_type}
                    ],
                }
            ]
        created = await self.http.json("POST", path, json_body=body, operation="leads.create")
        return self._lead(created, contact_id, data.title)

    def _lead(self, obj: dict[str, Any], contact_id: str | None, title: str) -> Lead:
        p = obj.get("properties") or {}
        return Lead(
            id=str(obj["id"]),
            contact_id=contact_id,
            title=p.get("dealname") or p.get("hs_lead_name") or title,
            status=p.get("dealstage") or p.get("hs_pipeline_stage"),
            description=p.get("description"),
            source="hubspot",
        )

    async def _find_by_key(self, obj: str, prop: str, key: str) -> dict[str, Any] | None:
        """Record created earlier with this idempotency key (a retry after an ambiguous
        failure). HubSpot search is eventually consistent: a record created seconds ago may
        not be indexed yet, so this narrows the duplicate window rather than closing it."""
        data = await self.http.json(
            "POST",
            f"/crm/v3/objects/{obj}/search",
            json_body={
                "filterGroups": [
                    {"filters": [{"propertyName": prop, "operator": "EQ", "value": key}]}
                ],
                "properties": ["dealname", "dealstage", "hs_lead_name", "hs_pipeline_stage", prop],
                "limit": 1,
            },
            retry_unsafe=True,
            operation="leads.find_by_key",
        )
        results = (data or {}).get("results") or []
        return dict(results[0]) if results else None

    async def update_lead(self, lead_id: str, data: LeadUpdate) -> Lead:
        changes = data.changes()
        if self.cfg.lead_object == "deals":
            mapping = {"status": "dealstage", "description": "description", "title": "dealname"}
            path = f"/crm/v3/objects/deals/{_check_id(lead_id)}"
        else:
            mapping = {"status": "hs_pipeline_stage", "title": "hs_lead_name"}
            path = f"/crm/v3/objects/leads/{_check_id(lead_id)}"
        props = {mapping[k]: v for k, v in changes.items() if k in mapping}
        if not props:
            raise IntegrationError(ErrorKind.VALIDATION, "no supported fields to update")
        updated = await self.http.json(
            "PATCH",
            path,
            json_body={"properties": props},
            retry_unsafe=True,
            operation="leads.update",
        )
        p = updated.get("properties") or {}
        return Lead(
            id=str(updated["id"]),
            title=p.get("dealname") or p.get("hs_lead_name") or "",
            status=p.get("dealstage") or p.get("hs_pipeline_stage"),
            description=p.get("description"),
            source="hubspot",
        )

    async def add_note(self, contact_id: str, text: str, *, idempotency_key: str) -> str:
        body = {
            "properties": {
                "hs_note_body": text[:65_000],
                "hs_timestamp": datetime.now(UTC).isoformat(),
            },
            "associations": [
                {
                    "to": {"id": _check_id(contact_id)},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": self.cfg.note_to_contact_type,
                        }
                    ],
                }
            ],
        }
        created = await self.http.json(
            "POST", "/crm/v3/objects/notes", json_body=body, operation="notes.create"
        )
        return str(created["id"])
