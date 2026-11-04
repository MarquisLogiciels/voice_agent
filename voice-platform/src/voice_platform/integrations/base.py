"""Integration framework: normalized models, capability protocols and error taxonomy.

Adapters (HubSpot, Salesforce, Google Calendar, custom REST, sandbox) implement the capability
protocols; agent tools only ever talk to the protocols, never to vendor APIs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from ..shared.http import EgressPolicy


class ErrorKind(str, Enum):
    AUTH = "auth"                    # credentials rejected — not retryable, alert tenant admin
    PERMISSION = "permission"        # authenticated but forbidden
    NOT_FOUND = "not_found"
    VALIDATION = "validation"        # vendor rejected the payload
    CONFLICT = "conflict"            # duplicate / version conflict
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"      # 5xx / network / circuit open
    TIMEOUT = "timeout"
    CONFIGURATION = "configuration"  # integration misconfigured on our side
    UNKNOWN = "unknown"


RETRYABLE_KINDS = {ErrorKind.RATE_LIMITED, ErrorKind.UNAVAILABLE, ErrorKind.TIMEOUT}


class IntegrationError(Exception):
    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
        integration: str | None = None,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.retry_after_s = retry_after_s
        self.integration = integration
        self.ambiguous = ambiguous
        """True when a write may have been applied despite the error (e.g. timeout on POST)."""

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS

    def safe_message(self) -> str:
        """Message suitable for the LLM/caller: no vendor internals."""
        return {
            ErrorKind.AUTH: "The business system rejected our credentials.",
            ErrorKind.PERMISSION: "We are not allowed to perform this action.",
            ErrorKind.NOT_FOUND: "The record was not found.",
            ErrorKind.VALIDATION: "The business system rejected the provided details.",
            ErrorKind.CONFLICT: "A conflicting record already exists.",
            ErrorKind.RATE_LIMITED: "The business system is busy right now.",
            ErrorKind.UNAVAILABLE: "The business system is temporarily unavailable.",
            ErrorKind.TIMEOUT: "The business system did not respond in time.",
            ErrorKind.CONFIGURATION: "This integration is not configured correctly.",
            ErrorKind.UNKNOWN: "An unexpected error occurred in the business system.",
        }[self.kind]


# ------------------------------------------------------------------------------------------
# Normalized records
# ------------------------------------------------------------------------------------------


class _Rec(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Contact(_Rec):
    id: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    company: str | None = None
    source: str = ""
    url: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()


class ContactInput(_Rec):
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    company: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)


class ContactUpdate(_Rec):
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    company: str | None = Field(default=None, max_length=200)

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class Lead(_Rec):
    id: str
    contact_id: str | None = None
    title: str
    status: str | None = None
    description: str | None = None
    source: str = ""
    url: str | None = None


class LeadInput(_Rec):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    contact_id: str | None = None
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None
    company: str | None = Field(default=None, max_length=200)


class LeadUpdate(_Rec):
    status: str | None = Field(default=None, max_length=50)
    description: str | None = Field(default=None, max_length=4000)
    title: str | None = Field(default=None, max_length=200)

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class TimeSlot(_Rec):
    start: datetime
    end: datetime


class Appointment(_Rec):
    id: str
    start: datetime
    end: datetime
    title: str
    status: str = "confirmed"
    attendee_name: str | None = None
    attendee_phone: str | None = None
    attendee_email: str | None = None
    notes: str | None = None
    source: str = ""


class AppointmentInput(_Rec):
    start: datetime
    end: datetime
    title: str = Field(min_length=1, max_length=200)
    attendee_name: str = Field(min_length=1, max_length=200)
    attendee_phone: str | None = Field(default=None, max_length=32)
    attendee_email: EmailStr | None = None
    notes: str | None = Field(default=None, max_length=2000)


class MessageResult(_Rec):
    id: str
    status: str


# ------------------------------------------------------------------------------------------
# Capability protocols
# ------------------------------------------------------------------------------------------


@runtime_checkable
class CRM(Protocol):
    name: str

    async def find_contacts_by_phone(self, phone_e164: str) -> list[Contact]: ...
    async def search_contacts(self, query: str, limit: int = 5) -> list[Contact]: ...
    async def get_contact(self, contact_id: str) -> Contact | None: ...
    async def create_contact(self, data: ContactInput, *, idempotency_key: str) -> Contact: ...
    async def update_contact(self, contact_id: str, data: ContactUpdate) -> Contact: ...
    async def create_lead(self, data: LeadInput, *, idempotency_key: str) -> Lead: ...
    async def update_lead(self, lead_id: str, data: LeadUpdate) -> Lead: ...
    async def add_note(self, contact_id: str, text: str, *, idempotency_key: str) -> str: ...


@runtime_checkable
class Calendar(Protocol):
    name: str

    async def busy_intervals(self, start: datetime, end: datetime) -> list[TimeSlot]: ...
    async def list_appointments(self, start: datetime, end: datetime) -> list[Appointment]: ...
    async def get_appointment(self, appointment_id: str) -> Appointment | None: ...
    async def create_appointment(
        self, data: AppointmentInput, *, idempotency_key: str
    ) -> Appointment: ...
    async def cancel_appointment(self, appointment_id: str) -> None: ...


@runtime_checkable
class Messaging(Protocol):
    name: str

    async def send_sms(
        self, *, to: str, body: str, from_number: str, idempotency_key: str
    ) -> MessageResult: ...


@dataclass
class IntegrationContext:
    tenant_id: uuid.UUID
    name: str
    kind: str
    config: dict[str, Any]
    credentials: dict[str, Any] | None
    egress: EgressPolicy
    timeout_s: float = 8.0
    extra: dict[str, Any] = field(default_factory=dict)
