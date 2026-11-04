"""In-memory CRM/Calendar/Messaging with seed data and fault injection.

Used by evaluations (never touches real systems) and as the ``sandbox`` integration kind for
development tenants. Every operation is recorded so evaluators can assert on side effects.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..shared.phone import try_normalize_e164
from .base import (
    Appointment,
    AppointmentInput,
    Contact,
    ContactInput,
    ContactUpdate,
    ErrorKind,
    IntegrationError,
    Lead,
    LeadInput,
    LeadUpdate,
    MessageResult,
    TimeSlot,
)


class FaultSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error: ErrorKind | None = None
    delay_s: float = Field(default=0.0, ge=0, le=30)
    times: int | None = Field(default=None, ge=1, description="None = every call")


class SandboxState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contacts: list[Contact] = Field(default_factory=list)
    leads: list[Lead] = Field(default_factory=list)
    appointments: list[Appointment] = Field(default_factory=list)
    busy: list[TimeSlot] = Field(default_factory=list)
    faults: dict[str, FaultSpec] = Field(default_factory=dict)
    """operation name (e.g. ``create_appointment``) → fault."""


@dataclass
class OperationRecord:
    operation: str
    args: dict[str, Any]
    ok: bool
    error: str | None = None


@dataclass
class _Shared:
    state: SandboxState
    log: list[OperationRecord] = field(default_factory=list)
    fault_counts: dict[str, int] = field(default_factory=dict)
    ids: itertools.count = field(default_factory=lambda: itertools.count(1001))
    idem: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)


class SandboxSuite:
    """One object implementing CRM, Calendar and Messaging over shared state."""

    def __init__(self, state: SandboxState | dict[str, Any] | None = None, *, name: str = "sandbox"
                 ) -> None:
        if isinstance(state, dict):
            state = SandboxState.model_validate(state)
        self.name = name
        self._s = _Shared(state=state or SandboxState())

    # --- introspection for evaluators ----------------------------------------------------
    @property
    def state(self) -> SandboxState:
        return self._s.state

    @property
    def operations(self) -> list[OperationRecord]:
        return list(self._s.log)

    @property
    def messages(self) -> list[dict[str, Any]]:
        return list(self._s.messages)

    async def aclose(self) -> None:
        return None

    async def _op(self, name: str, args: dict[str, Any]) -> None:
        fault = self._s.state.faults.get(name)
        if fault is not None:
            n = self._s.fault_counts.get(name, 0)
            active = fault.times is None or n < fault.times
            if active:
                self._s.fault_counts[name] = n + 1
                if fault.delay_s:
                    await asyncio.sleep(fault.delay_s)
                if fault.error is not None:
                    self._s.log.append(OperationRecord(name, args, False, fault.error.value))
                    raise IntegrationError(fault.error, f"sandbox fault: {name}",
                                           integration=self.name)
        self._s.log.append(OperationRecord(name, args, True))

    def _id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._s.ids)}"

    # --- CRM ---------------------------------------------------------------------------------
    async def find_contacts_by_phone(self, phone_e164: str) -> list[Contact]:
        await self._op("find_contacts_by_phone", {"phone": phone_e164})
        target = try_normalize_e164(phone_e164)
        return [c for c in self._s.state.contacts if c.phone and
                try_normalize_e164(c.phone) == target]

    async def search_contacts(self, query: str, limit: int = 5) -> list[Contact]:
        await self._op("search_contacts", {"query": query})
        q = query.lower().strip()
        hits = [c for c in self._s.state.contacts if q and (
            q in c.full_name.lower() or q in (c.email or "").lower() or q in (c.company or "").lower()
            or q.replace(" ", "") in (c.phone or ""))]
        return hits[:limit]

    async def get_contact(self, contact_id: str) -> Contact | None:
        await self._op("get_contact", {"id": contact_id})
        return next((c for c in self._s.state.contacts if c.id == contact_id), None)

    async def create_contact(self, data: ContactInput, *, idempotency_key: str) -> Contact:
        if idempotency_key in self._s.idem:
            return self._s.idem[idempotency_key]  # type: ignore[no-any-return]
        await self._op("create_contact", data.model_dump(mode="json", exclude_none=True))
        contact = Contact(id=self._id("contact"), first_name=data.first_name,
                          last_name=data.last_name, email=str(data.email) if data.email else None,
                          phone=try_normalize_e164(data.phone) or data.phone,
                          company=data.company, source="sandbox")
        self._s.state.contacts.append(contact)
        self._s.idem[idempotency_key] = contact
        return contact

    async def update_contact(self, contact_id: str, data: ContactUpdate) -> Contact:
        await self._op("update_contact", {"id": contact_id, **data.model_dump(mode="json",
                                                                             exclude_none=True)})
        for i, c in enumerate(self._s.state.contacts):
            if c.id == contact_id:
                updated = c.model_copy(update={k: (str(v) if k == "email" else v)
                                               for k, v in data.changes().items()})
                self._s.state.contacts[i] = updated
                return updated
        raise IntegrationError(ErrorKind.NOT_FOUND, "contact not found", integration=self.name)

    async def create_lead(self, data: LeadInput, *, idempotency_key: str) -> Lead:
        if idempotency_key in self._s.idem:
            return self._s.idem[idempotency_key]  # type: ignore[no-any-return]
        await self._op("create_lead", data.model_dump(mode="json", exclude_none=True))
        lead = Lead(id=self._id("lead"), contact_id=data.contact_id, title=data.title,
                    status="new", description=data.description, source="sandbox")
        self._s.state.leads.append(lead)
        self._s.idem[idempotency_key] = lead
        return lead

    async def update_lead(self, lead_id: str, data: LeadUpdate) -> Lead:
        await self._op("update_lead", {"id": lead_id, **data.changes()})
        for i, lead in enumerate(self._s.state.leads):
            if lead.id == lead_id:
                updated = lead.model_copy(update=data.changes())
                self._s.state.leads[i] = updated
                return updated
        raise IntegrationError(ErrorKind.NOT_FOUND, "lead not found", integration=self.name)

    async def add_note(self, contact_id: str, text: str, *, idempotency_key: str) -> str:
        await self._op("add_note", {"contact_id": contact_id, "text": text})
        return self._id("note")

    # --- Calendar ----------------------------------------------------------------------------
    async def busy_intervals(self, start: datetime, end: datetime) -> list[TimeSlot]:
        await self._op("busy_intervals", {"start": start.isoformat(), "end": end.isoformat()})
        out = [b for b in self._s.state.busy if b.end > start and b.start < end]
        out += [TimeSlot(start=a.start, end=a.end) for a in self._s.state.appointments
                if a.status != "cancelled" and a.end > start and a.start < end]
        return sorted(out, key=lambda s: s.start)

    async def list_appointments(self, start: datetime, end: datetime) -> list[Appointment]:
        await self._op("list_appointments", {"start": start.isoformat(), "end": end.isoformat()})
        return [a for a in self._s.state.appointments
                if a.status != "cancelled" and a.end > start and a.start < end]

    async def get_appointment(self, appointment_id: str) -> Appointment | None:
        await self._op("get_appointment", {"id": appointment_id})
        return next((a for a in self._s.state.appointments
                     if a.id == appointment_id and a.status != "cancelled"), None)

    async def create_appointment(self, data: AppointmentInput, *, idempotency_key: str
                                 ) -> Appointment:
        if idempotency_key in self._s.idem:
            return self._s.idem[idempotency_key]  # type: ignore[no-any-return]
        await self._op("create_appointment", data.model_dump(mode="json", exclude_none=True))
        for b in await self.busy_intervals(data.start, data.end):
            if b.end > data.start and b.start < data.end:
                raise IntegrationError(ErrorKind.CONFLICT, "slot already taken",
                                       integration=self.name)
        appt = Appointment(id=self._id("appt"), start=data.start, end=data.end, title=data.title,
                           attendee_name=data.attendee_name, attendee_phone=data.attendee_phone,
                           attendee_email=str(data.attendee_email) if data.attendee_email else None,
                           notes=data.notes, source="sandbox")
        self._s.state.appointments.append(appt)
        self._s.idem[idempotency_key] = appt
        return appt

    async def cancel_appointment(self, appointment_id: str) -> None:
        await self._op("cancel_appointment", {"id": appointment_id})
        for i, a in enumerate(self._s.state.appointments):
            if a.id == appointment_id:
                self._s.state.appointments[i] = a.model_copy(update={"status": "cancelled"})
                return
        raise IntegrationError(ErrorKind.NOT_FOUND, "appointment not found", integration=self.name)

    # --- Messaging ---------------------------------------------------------------------------
    async def send_sms(self, *, to: str, body: str, from_number: str, idempotency_key: str
                       ) -> MessageResult:
        if idempotency_key in self._s.idem:
            return self._s.idem[idempotency_key]  # type: ignore[no-any-return]
        await self._op("send_sms", {"to": to, "body": body})
        msg = MessageResult(id=self._id("sms"), status="queued")
        self._s.messages.append({"to": to, "body": body, "from": from_number, "id": msg.id})
        self._s.idem[idempotency_key] = msg
        return msg
