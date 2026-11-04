"""The standardized tool catalogue the LLM interacts with.

Each tool: an input model (the only arguments the LLM may pass), a capability it needs, whether
it changes external state (consequential), and a handler that talks to capability protocols.
Handlers return plain data; the executor turns errors into structured outcomes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from ..config.schema import AgentConfig
from ..integrations.base import (
    AppointmentInput,
    ContactInput,
    ContactUpdate,
    ErrorKind,
    IntegrationError,
    LeadInput,
    LeadUpdate,
)
from ..shared.crypto import stable_hash
from ..shared.phone import try_normalize_e164
from .interfaces import ToolEnv
from .scheduling import (
    free_slots,
    is_within_hours,
    overlaps,
    resolve_day,
    speakable,
    speakable_day,
    weekday_conflict,
)


class ToolRefusal(Exception):
    """A business-rule refusal (not a system failure)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Confirmed(_Args):
    caller_confirmed: bool = Field(
        default=False,
        description="True only after you read the details back and the caller explicitly said yes.",
    )


Handler = Callable[[ToolEnv, Any], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[_Args]
    handler: Handler
    capability: str | None  # crm | calendar | messaging | transfer | knowledge | None
    consequential: bool = False
    dedupe: bool = False
    """Identical calls within a conversation replay the first result (idempotency)."""
    timeout: Callable[[AgentConfig], float] | None = None
    """Per-tool time budget; defaults to ``tools.timeout_s``."""

    def time_budget(self, config: AgentConfig) -> float:
        return self.timeout(config) if self.timeout is not None else config.tools.timeout_s

    def llm_schema(
        self, description: str | None = None, *, require_confirmation: bool = False
    ) -> dict[str, Any]:
        schema = _simplify(self.args.model_json_schema())
        if not require_confirmation:
            schema["properties"].pop("caller_confirmed", None)
            if "required" in schema:
                schema["required"] = [r for r in schema["required"] if r != "caller_confirmed"]
        return {
            "name": self.name,
            "description": description or self.description,
            "parameters": schema,
        }


def _simplify(schema: dict[str, Any]) -> dict[str, Any]:
    """Flatten pydantic JSON schema into the subset every LLM provider accepts."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            if "anyOf" in node:
                options = [o for o in node["anyOf"] if o.get("type") != "null"]
                base = walk(options[0]) if len(options) == 1 else {"anyOf": walk(options)}
                for k in ("description", "default"):
                    if k in node:
                        base[k] = node[k]
                return {k: v for k, v in base.items() if k != "default" or v is not None}
            return {k: walk(v) for k, v in node.items() if k not in ("title", "$defs")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    out: dict[str, Any] = walk(schema)
    out.setdefault("properties", {})
    out["type"] = "object"
    out["additionalProperties"] = False
    return out


def _require(env: ToolEnv, capability: str) -> Any:
    target = getattr(env.integrations, capability, None)
    if target is None:
        raise IntegrationError(ErrorKind.CONFIGURATION, f"no {capability} integration is bound")
    return target


def _tz(env: ToolEnv) -> ZoneInfo:
    return ZoneInfo(env.config.business.timezone)


# ============================================================================================
# CRM
# ============================================================================================


class LookupCustomerArgs(_Args):
    phone: str | None = Field(
        default=None, description="Phone number to look up. Omit to use the caller's number."
    )


async def lookup_customer(env: ToolEnv, a: LookupCustomerArgs) -> dict[str, Any]:
    crm = _require(env, "crm")
    phone = try_normalize_e164(a.phone or env.caller_number or "")
    if not phone:
        raise ToolRefusal("invalid_arguments", "No valid phone number available; ask the caller.")
    contacts = await crm.find_contacts_by_phone(phone)
    if contacts:
        env.state.identified_contact_id = contacts[0].id
    return {
        "found": bool(contacts),
        "contacts": [
            {"contact_id": c.id, "name": c.full_name, "email": c.email, "company": c.company}
            for c in contacts[:3]
        ],
    }


class SearchContactsArgs(_Args):
    query: str = Field(min_length=2, max_length=100, description="Name, email or company")


async def search_contacts(env: ToolEnv, a: SearchContactsArgs) -> dict[str, Any]:
    crm = _require(env, "crm")
    contacts = await crm.search_contacts(a.query, limit=5)
    return {
        "count": len(contacts),
        "contacts": [
            {"contact_id": c.id, "name": c.full_name, "company": c.company} for c in contacts
        ],
    }


class CreateContactArgs(_Confirmed):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, description="Defaults to the caller's number")
    company: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=1000)


async def create_contact(env: ToolEnv, a: CreateContactArgs) -> dict[str, Any]:
    crm = _require(env, "crm")
    phone = try_normalize_e164(a.phone or env.caller_number or "")
    contact = await crm.create_contact(
        ContactInput(
            first_name=a.first_name,
            last_name=a.last_name,
            email=a.email,
            phone=phone,
            company=a.company,
            notes=a.notes,
        ),
        idempotency_key=f"{env.conversation_id}:create_contact:{phone or a.first_name}",
    )
    env.state.identified_contact_id = contact.id
    return {"contact_id": contact.id, "name": contact.full_name}


class UpdateContactArgs(_Confirmed):
    contact_id: str = Field(min_length=1, max_length=64)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = None
    company: str | None = Field(default=None, max_length=200)


async def update_contact(env: ToolEnv, a: UpdateContactArgs) -> dict[str, Any]:
    crm = _require(env, "crm")
    if env.state.identified_contact_id and a.contact_id != env.state.identified_contact_id:
        raise ToolRefusal("not_permitted", "You can only update the caller's own record.")
    upd = ContactUpdate(
        first_name=a.first_name,
        last_name=a.last_name,
        email=a.email,
        phone=try_normalize_e164(a.phone) if a.phone else None,
        company=a.company,
    )
    if not upd.changes():
        raise ToolRefusal("invalid_arguments", "No fields to update were provided.")
    contact = await crm.update_contact(a.contact_id, upd)
    return {"contact_id": contact.id, "updated_fields": sorted(upd.changes())}


class CreateLeadArgs(_Confirmed):
    title: str = Field(min_length=3, max_length=200, description="Short summary of the request")
    description: str | None = Field(default=None, max_length=2000)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    phone: str | None = None
    email: EmailStr | None = None
    company: str | None = Field(default=None, max_length=200)
    contact_id: str | None = Field(default=None, max_length=64)


async def create_lead(env: ToolEnv, a: CreateLeadArgs) -> dict[str, Any]:
    crm = _require(env, "crm")
    phone = try_normalize_e164(a.phone or env.caller_number or "")
    lead = await crm.create_lead(
        LeadInput(
            title=a.title,
            description=a.description,
            first_name=a.first_name,
            last_name=a.last_name,
            phone=phone,
            email=a.email,
            company=a.company,
            contact_id=a.contact_id or env.state.identified_contact_id,
        ),
        idempotency_key=f"{env.conversation_id}:create_lead:{a.title.lower()}",
    )
    return {"lead_id": lead.id, "title": lead.title}


class UpdateLeadArgs(_Confirmed):
    lead_id: str = Field(min_length=1, max_length=64)
    status: str | None = Field(default=None, max_length=50)
    description: str | None = Field(default=None, max_length=2000)


async def update_lead(env: ToolEnv, a: UpdateLeadArgs) -> dict[str, Any]:
    crm = _require(env, "crm")
    lead = await crm.update_lead(a.lead_id, LeadUpdate(status=a.status, description=a.description))
    return {"lead_id": lead.id, "status": lead.status}


class AddNoteArgs(_Args):
    text: str = Field(min_length=3, max_length=2000)
    contact_id: str | None = Field(default=None, max_length=64)


async def add_note(env: ToolEnv, a: AddNoteArgs) -> dict[str, Any]:
    crm = _require(env, "crm")
    contact_id = a.contact_id or env.state.identified_contact_id
    if not contact_id:
        raise ToolRefusal("invalid_arguments", "Look up or create the contact first.")
    note_id = await crm.add_note(
        contact_id, a.text, idempotency_key=f"{env.conversation_id}:note:{stable_hash(a.text)[:16]}"
    )
    return {"note_id": note_id}


# ============================================================================================
# Calendar
# ============================================================================================


def _parse_day(value: str, env: ToolEnv) -> tuple[date, str | None]:
    """Resolve the requested day; returns (day, note for the model or None)."""
    today = env.clock().astimezone(_tz(env)).date()
    day = resolve_day(value, today)
    if day is None:
        raise ToolRefusal(
            "invalid_arguments", "Use the date format YYYY-MM-DD (or 'tomorrow' or a weekday)."
        )
    word = weekday_conflict(env.state.last_user_text, value, day, today)
    if word is not None:
        corrected = resolve_day(word, today)
        if corrected is not None:
            # The model's calendar arithmetic contradicted the caller's own words; the
            # spoken day in the result lets the caller catch any remaining misunderstanding.
            return corrected, (
                f"{value} is a {day.strftime('%A')}, but the caller said '{word}', so this is "
                f"{speakable_day(corrected, env.config.language)}. Mention the day when you "
                "answer."
            )
    return day, None


class CheckAvailabilityArgs(_Args):
    # Small models compute dates badly but copy words well (lab finding: Qwen 2.5 3B went
    # from wrong weekdays to 5/5 correct with this wording); the platform resolves the words.
    date: str = Field(
        description="The day exactly as the caller said it, e.g. 'tomorrow', 'next monday', "
        "'friday', or YYYY-MM-DD if the caller gave a full date. The system converts it."
    )
    time_preference: Literal["morning", "afternoon", "evening", "any"] = "any"
    duration_minutes: int | None = Field(
        default=None,
        ge=5,
        le=480,
        description="Only if the caller explicitly states how long; otherwise omit.",
    )


async def check_availability(env: ToolEnv, a: CheckAvailabilityArgs) -> dict[str, Any]:
    cal = _require(env, "calendar")
    tz = _tz(env)
    policy = env.config.business.appointments
    day, date_note = _parse_day(a.date, env)
    start = datetime.combine(day, datetime.min.time(), tz)
    busy = await cal.busy_intervals(start, start + timedelta(days=1))
    slots = free_slots(
        day=day,
        policy=policy,
        tz=tz,
        busy=busy,
        now=env.clock(),
        duration_min=a.duration_minutes,
        preference=a.time_preference,
    )
    env.state.offered_slots = slots
    lang = env.config.language
    return {
        "date": day.isoformat(),
        "day": speakable_day(day, lang),
        "available": bool(slots),
        "slots": [
            {"start": s.start.isoformat(), "spoken": speakable(s.start, tz, lang)} for s in slots
        ],
        "note": " ".join(
            n
            for n in (
                date_note,
                None if slots else "No openings that day; offer to check another day.",
            )
            if n
        )
        or None,
    }


class GetAppointmentsArgs(_Args):
    date: str | None = Field(
        default=None,
        description="The day as the caller said it (e.g. 'tomorrow', 'friday') or "
        "YYYY-MM-DD; omit for the next 30 days",
    )


async def get_appointments(env: ToolEnv, a: GetAppointmentsArgs) -> dict[str, Any]:
    cal = _require(env, "calendar")
    tz = _tz(env)
    caller = try_normalize_e164(env.caller_number or "")
    if not caller:
        raise ToolRefusal(
            "not_permitted",
            "Appointments can only be looked up for the caller's "
            "own phone number, which is unavailable.",
        )
    if a.date:
        day, _ = _parse_day(a.date, env)
        start = datetime.combine(day, datetime.min.time(), tz)
        end = start + timedelta(days=1)
    else:
        start = env.clock()
        end = start + timedelta(days=30)
    appts = await cal.list_appointments(start, end)
    # Privacy: never disclose other people's appointments.
    mine = [x for x in appts if try_normalize_e164(x.attendee_phone or "") == caller]
    return {
        "appointments": [
            {
                "appointment_id": x.id,
                "title": x.title,
                "start": x.start.isoformat(),
                "spoken": speakable(x.start, tz, env.config.language),
            }
            for x in mine
        ]
    }


class BookAppointmentArgs(_Confirmed):
    start: str = Field(description="Slot start as ISO 8601 from check_availability")
    attendee_name: str = Field(min_length=2, max_length=200)
    reason: str = Field(min_length=2, max_length=200, description="Purpose of the visit")
    attendee_phone: str | None = Field(default=None, description="Defaults to caller's number")
    attendee_email: EmailStr | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=480)

    @field_validator("start")
    @classmethod
    def _iso(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v)
        except ValueError as e:
            raise ValueError("start must be ISO 8601") from e
        return v


async def book_appointment(env: ToolEnv, a: BookAppointmentArgs) -> dict[str, Any]:
    cal = _require(env, "calendar")
    tz = _tz(env)
    policy = env.config.business.appointments
    start = datetime.fromisoformat(a.start)
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    end = start + timedelta(minutes=a.duration_minutes or policy.default_duration_min)
    now = env.clock()
    if start < now + timedelta(minutes=policy.min_notice_min):
        raise ToolRefusal("slot_unavailable", "That time is too soon to book.")
    if start > now + timedelta(days=policy.max_days_ahead):
        raise ToolRefusal("slot_unavailable", "That date is too far in the future.")
    if not is_within_hours(policy.hours, start, end, tz):
        raise ToolRefusal("slot_unavailable", "That time is outside booking hours.")
    # Never trust the LLM: re-check the calendar right before booking.
    busy = await cal.busy_intervals(start - timedelta(minutes=1), end + timedelta(minutes=1))
    if overlaps(start, end, busy):
        raise ToolRefusal(
            "slot_unavailable",
            "That slot was just taken. Call check_availability again and offer other times.",
        )
    phone = try_normalize_e164(a.attendee_phone or env.caller_number or "")
    appt = await cal.create_appointment(
        AppointmentInput(
            start=start,
            end=end,
            title=f"{a.reason} — {a.attendee_name}",
            attendee_name=a.attendee_name,
            attendee_phone=phone,
            attendee_email=a.attendee_email,
        ),
        idempotency_key=f"{env.conversation_id}:book:{start.isoformat()}",
    )
    # Check-then-insert race: two calls can both pass the re-check above. Verify after the
    # insert; if another live appointment overlaps, exactly one survives — the smallest id, so
    # every racer computes the same winner — and the others cancel their own booking.
    try:
        around = await cal.list_appointments(
            start - timedelta(minutes=1), end + timedelta(minutes=1)
        )
    except IntegrationError:
        around = []  # cannot verify; keep the booking rather than cancel blindly
    rivals = [
        x.id
        for x in around
        if x.id != appt.id and x.status != "cancelled" and x.start < end and x.end > start
    ]
    if rivals and min([appt.id, *rivals]) != appt.id:
        await cal.cancel_appointment(appt.id)
        raise ToolRefusal(
            "slot_unavailable",
            "That slot was just taken by another caller. Call check_availability again and "
            "offer other times.",
        )
    return {
        "appointment_id": appt.id,
        "start": appt.start.isoformat(),
        "spoken": speakable(appt.start, tz, env.config.language),
        "confirmed": True,
    }


class CancelAppointmentArgs(_Confirmed):
    appointment_id: str = Field(min_length=1, max_length=200)


async def cancel_appointment(env: ToolEnv, a: CancelAppointmentArgs) -> dict[str, Any]:
    cal = _require(env, "calendar")
    appt = await cal.get_appointment(a.appointment_id)
    if appt is None:
        raise ToolRefusal("not_found", "No such upcoming appointment.")
    caller = try_normalize_e164(env.caller_number or "")
    if not caller or try_normalize_e164(appt.attendee_phone or "") != caller:
        raise ToolRefusal(
            "not_permitted",
            "Only the person who booked can cancel by phone; offer to transfer to staff.",
        )
    await cal.cancel_appointment(a.appointment_id)
    return {"appointment_id": a.appointment_id, "cancelled": True}


# ============================================================================================
# Knowledge, transfer, messaging
# ============================================================================================


class SearchKnowledgeArgs(_Args):
    query: str = Field(min_length=2, max_length=300, description="The caller's question")


async def search_knowledge_base(env: ToolEnv, a: SearchKnowledgeArgs) -> dict[str, Any]:
    if env.knowledge is None:
        raise IntegrationError(ErrorKind.CONFIGURATION, "no knowledge base is configured")
    hits = await env.knowledge.search(a.query, top_k=env.config.knowledge.top_k)
    hits = [h for h in hits if h.score >= env.config.knowledge.min_score]
    return {
        "results": [
            {
                "source": h.document_title,
                "excerpt": h.content[:1200],
                "relevance": round(h.score, 3),
            }
            for h in hits
        ],
        "guidance": "Answer only from these excerpts. If they do not contain the answer, say "
        "you don't know and offer to take a message or transfer.",
    }


class TransferArgs(_Args):
    reason: str = Field(min_length=3, max_length=300, description="Why the caller needs a human")
    summary: str = Field(
        min_length=10,
        max_length=800,
        description="Short handoff summary for "
        "the human: who is calling, what they need, what was done",
    )
    destination: str | None = Field(default=None, description="Destination name, if known")


def transfer_time_budget(config: AgentConfig) -> float:
    """A transfer legitimately takes the announcement plus ringing every destination in the
    fallback chain. Lab finding: under the generic 8 s tool timeout the tool reported failure
    (and the agent said nobody was available) while the SIP transfer was still ringing."""
    ringing = sum(d.ringing_timeout_s + 5 for d in config.transfer.destinations)
    return min(30 + ringing, 600)


async def transfer_to_human(env: ToolEnv, a: TransferArgs) -> dict[str, Any]:
    if env.transfer is None:
        raise IntegrationError(ErrorKind.CONFIGURATION, "transfers are not configured")
    names = [d.name for d in env.transfer.destinations()]
    if a.destination and a.destination not in names:
        raise ToolRefusal("invalid_arguments", f"Unknown destination. Valid: {', '.join(names)}")
    env.state.transfer_attempted = True
    result = await env.transfer.transfer(
        reason=a.reason, destination=a.destination, summary=a.summary
    )
    env.state.transfer_result = result
    if not result.ok:
        raise ToolRefusal(f"transfer_{result.status}", result.message)
    return {
        "transferred": True,
        "destination": result.destination,
        "mode": result.mode,
        "status": result.status,
    }


class SendSmsArgs(_Confirmed):
    template: str = Field(description="Name of an approved message template")
    variables: dict[str, str] = Field(default_factory=dict)
    to: str | None = Field(default=None, description="Defaults to the caller's number")


async def send_sms(env: ToolEnv, a: SendSmsArgs) -> dict[str, Any]:
    messaging = _require(env, "messaging")
    sms = env.config.sms
    if env.state.sms_sent >= sms.max_per_conversation:
        raise ToolRefusal("limit_reached", "The message limit for this call was reached.")
    template = sms.allowed_templates.get(a.template)
    if template is None:
        raise ToolRefusal(
            "invalid_arguments", f"Unknown template. Valid: {', '.join(sms.allowed_templates)}"
        )
    to = try_normalize_e164(a.to or env.caller_number or "")
    caller = try_normalize_e164(env.caller_number or "")
    if not to:
        raise ToolRefusal("invalid_arguments", "No valid destination number.")
    if to != caller:
        raise ToolRefusal("not_permitted", "Messages can only be sent to the caller's number.")
    try:
        body = template.format_map({k: v[:200] for k, v in a.variables.items()})
    except (KeyError, ValueError) as e:
        raise ToolRefusal("invalid_arguments", f"Missing template variable: {e}") from e
    from_number = sms.from_number or env.sms_from_number
    if not from_number:
        raise IntegrationError(ErrorKind.CONFIGURATION, "no SMS sender number configured")
    result = await messaging.send_sms(
        to=to,
        body=body,
        from_number=from_number,
        idempotency_key=f"{env.conversation_id}:sms:{a.template}",
    )
    env.state.sms_sent += 1
    return {"message_id": result.id, "status": result.status}


CATALOG: dict[str, ToolSpec] = {
    t.name: t
    for t in [
        ToolSpec(
            "lookup_customer",
            "Look up the caller (or a given phone number) in the CRM. Call "
            "this early to personalize the conversation.",
            LookupCustomerArgs,
            lookup_customer,
            "crm",
        ),
        ToolSpec(
            "search_contacts",
            "Search CRM contacts by name, email or company.",
            SearchContactsArgs,
            search_contacts,
            "crm",
        ),
        ToolSpec(
            "create_contact",
            "Create a CRM contact for a new caller.",
            CreateContactArgs,
            create_contact,
            "crm",
            consequential=True,
            dedupe=True,
        ),
        ToolSpec(
            "update_contact",
            "Update the caller's CRM contact details.",
            UpdateContactArgs,
            update_contact,
            "crm",
            consequential=True,
            dedupe=True,
        ),
        ToolSpec(
            "create_lead",
            "Record a sales lead / service request in the CRM.",
            CreateLeadArgs,
            create_lead,
            "crm",
            consequential=True,
            dedupe=True,
        ),
        ToolSpec(
            "update_lead",
            "Update an existing lead's status or description.",
            UpdateLeadArgs,
            update_lead,
            "crm",
            consequential=True,
            dedupe=True,
        ),
        ToolSpec(
            "add_note",
            "Attach a short note about this call to the caller's CRM record.",
            AddNoteArgs,
            add_note,
            "crm",
            consequential=True,
            dedupe=True,
        ),
        ToolSpec(
            "check_availability",
            "List open appointment slots on a given day. Always call this before offering times.",
            CheckAvailabilityArgs,
            check_availability,
            "calendar",
        ),
        ToolSpec(
            "get_appointments",
            "List the caller's own upcoming appointments.",
            GetAppointmentsArgs,
            get_appointments,
            "calendar",
        ),
        ToolSpec(
            "book_appointment",
            "Book an appointment in a slot returned by check_availability. "
            "Only call after the caller confirmed the time, name and reason.",
            BookAppointmentArgs,
            book_appointment,
            "calendar",
            consequential=True,
            dedupe=True,
        ),
        ToolSpec(
            "cancel_appointment",
            "Cancel one of the caller's appointments.",
            CancelAppointmentArgs,
            cancel_appointment,
            "calendar",
            consequential=True,
            dedupe=True,
        ),
        ToolSpec(
            "search_knowledge_base",
            "Search the business knowledge base for facts (services, "
            "prices, policies). Use before answering factual questions.",
            SearchKnowledgeArgs,
            search_knowledge_base,
            "knowledge",
        ),
        ToolSpec(
            "transfer_to_human",
            "Transfer the live call to a human when the caller asks for a "
            "person, or when you cannot resolve the request.",
            TransferArgs,
            transfer_to_human,
            "transfer",
            consequential=True,
            dedupe=True,
            timeout=transfer_time_budget,
        ),
        ToolSpec(
            "send_sms",
            "Send an approved text message template to the caller.",
            SendSmsArgs,
            send_sms,
            "messaging",
            consequential=True,
            dedupe=True,
        ),
    ]
}
