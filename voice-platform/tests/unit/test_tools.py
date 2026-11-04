from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from tests.conftest import sample_config
from voice_platform.config.schema import AgentConfig
from voice_platform.integrations.base import Appointment, Contact, IntegrationContext, TimeSlot
from voice_platform.integrations.custom_rest import CustomRestIntegration
from voice_platform.integrations.registry import IntegrationSet
from voice_platform.integrations.sandbox import SandboxSuite
from voice_platform.shared.http import EgressPolicy
from voice_platform.tools.executor import ToolExecutor
from voice_platform.tools.interfaces import ToolEnv, TransferResult
from voice_platform.tools.ledger import InMemoryLedger

CALLER = "+15145550199"
# Monday 2030-01-07 09:00 Toronto (14:00 UTC)
NOW = datetime(2030, 1, 7, 13, 0, tzinfo=UTC)


def _config(**kw: Any) -> AgentConfig:
    tools = kw.pop(
        "tools",
        {
            "enabled": [
                "lookup_customer",
                "create_lead",
                "check_availability",
                "book_appointment",
                "get_appointments",
                "cancel_appointment",
                "send_sms",
            ]
        },
    )
    return AgentConfig.model_validate(
        sample_config(
            tools=tools,
            integrations={"crm": "sb", "calendar": "sb", "messaging": "sb"},
            sms={
                "enabled": True,
                "from_number": "+15145550000",
                "allowed_templates": {"confirm": "Your appointment is on {when}."},
            },
            **kw,
        )
    )


def _env(cfg: AgentConfig, suite: SandboxSuite, **kw: Any) -> ToolEnv:
    return ToolEnv(
        tenant_id=uuid.uuid4(),
        agent_id=None,
        conversation_id="conv-1",
        config=cfg,
        integrations=IntegrationSet(crm=suite, calendar=suite, messaging=suite),
        ledger=kw.pop("ledger", InMemoryLedger()),
        caller_number=CALLER,
        clock=lambda: NOW,
        **kw,
    )


async def test_booking_requires_confirmation_rechecks_availability_and_is_idempotent() -> None:
    suite = SandboxSuite()
    ex = ToolExecutor.for_agent(_env(_config(), suite))
    avail = await ex.execute("check_availability", {"date": "2030-01-07"})
    assert avail.ok and avail.data and avail.data["available"]
    slot = avail.data["slots"][0]["start"]
    assert slot.startswith("2030-01-07T09:00")  # Toronto opening time, min notice 0
    args = {"start": slot, "attendee_name": "Ana Roy", "reason": "Cleaning"}
    first = await ex.execute("book_appointment", args)
    assert not first.ok and first.error == "confirmation_required"
    assert not suite.state.appointments
    booked = await ex.execute("book_appointment", {**args, "caller_confirmed": True})
    assert booked.ok and booked.data and booked.data["confirmed"]
    again = await ex.execute("book_appointment", {**args, "caller_confirmed": True})
    assert again.ok and again.replayed
    assert len(suite.state.appointments) == 1  # no duplicate booking
    assert suite.state.appointments[0].attendee_phone == CALLER


async def test_booking_busy_or_out_of_hours_slot_is_refused() -> None:
    start = datetime(2030, 1, 7, 15, 0, tzinfo=UTC)  # 10:00 Toronto
    suite = SandboxSuite({"busy": [TimeSlot(start=start, end=start + timedelta(hours=1))]})
    ex = ToolExecutor.for_agent(_env(_config(), suite))
    busy = await ex.execute(
        "book_appointment",
        {
            "start": start.isoformat(),
            "attendee_name": "Ana Roy",
            "reason": "Checkup",
            "caller_confirmed": True,
        },
    )
    assert not busy.ok and busy.error == "slot_unavailable"
    night = await ex.execute(
        "book_appointment",
        {
            "start": "2030-01-07T22:00:00-05:00",
            "attendee_name": "Ana Roy",
            "reason": "Checkup",
            "caller_confirmed": True,
        },
    )
    assert not night.ok and night.error == "slot_unavailable"
    assert not suite.state.appointments


async def test_appointment_privacy_and_cancel_ownership() -> None:
    mine = Appointment(
        id="a1",
        start=NOW + timedelta(days=1),
        end=NOW + timedelta(days=1, hours=1),
        title="Mine",
        attendee_phone=CALLER,
    )
    theirs = Appointment(
        id="a2",
        start=NOW + timedelta(days=1),
        end=NOW + timedelta(days=1, hours=1),
        title="Theirs",
        attendee_phone="+15145559999",
    )
    suite = SandboxSuite({"appointments": [mine, theirs]})
    ex = ToolExecutor.for_agent(_env(_config(), suite))
    listed = await ex.execute("get_appointments", {})
    assert listed.ok and [a["appointment_id"] for a in listed.data["appointments"]] == ["a1"]
    denied = await ex.execute(
        "cancel_appointment", {"appointment_id": "a2", "caller_confirmed": True}
    )
    assert not denied.ok and denied.error == "not_permitted"
    ok = await ex.execute("cancel_appointment", {"appointment_id": "a1", "caller_confirmed": True})
    assert ok.ok


async def test_integration_failures_are_structured_and_suggest_transfer() -> None:
    suite = SandboxSuite({"faults": {"find_contacts_by_phone": {"error": "unavailable"}}})

    class Transfer:
        def destinations(self) -> list[Any]:
            return []

        async def transfer(self, **kw: Any) -> TransferResult:
            return TransferResult(
                ok=True, status="completed", destination="x", mode="blind", message=""
            )

    cfg = _config(
        transfer={
            "enabled": True,
            "max_failed_tool_calls_before_transfer": 2,
            "destinations": [{"name": "front", "phone_number": "+15145550100"}],
        }
    )
    ex = ToolExecutor.for_agent(_env(cfg, suite, transfer=Transfer()))
    out1 = await ex.execute("lookup_customer", {})
    assert not out1.ok and out1.error == "unavailable" and "transfer" not in out1.message
    out2 = await ex.execute("lookup_customer", {})
    assert "transfer" in out2.message
    payload = json.loads(out2.to_llm())
    assert payload["ok"] is False and payload["error"] == "unavailable"


async def test_timeouts_never_report_success() -> None:
    suite = SandboxSuite({"faults": {"create_lead": {"delay_s": 2.0}}})
    cfg = _config()
    cfg.tools.timeout_s = 0.2
    ex = ToolExecutor.for_agent(_env(cfg, suite))
    out = await ex.execute("create_lead", {"title": "Quote request", "caller_confirmed": True})
    assert not out.ok and out.error == "timeout" and "Do not tell the caller" in out.message


async def test_validation_permissions_and_limits() -> None:
    suite = SandboxSuite({"contacts": [Contact(id="c1", first_name="Ana", phone=CALLER)]})
    cfg = _config()
    cfg.tools.max_calls_per_conversation = 3
    ex = ToolExecutor.for_agent(_env(cfg, suite))
    assert (await ex.execute("search_contacts", {"query": "ana"})).error == "not_permitted"
    bad = await ex.execute("check_availability", {"date": "tomorrow", "evil": 1})
    assert bad.error == "invalid_arguments"
    found = await ex.execute("lookup_customer", {})
    assert found.ok and found.data and found.data["contacts"][0]["contact_id"] == "c1"
    assert (await ex.execute("lookup_customer", {})).ok  # third call: still within budget
    assert (await ex.execute("lookup_customer", {})).error == "limit_reached"


async def test_sms_templates_and_destination_restrictions() -> None:
    suite = SandboxSuite()
    ex = ToolExecutor.for_agent(_env(_config(), suite))
    other = await ex.execute(
        "send_sms",
        {
            "template": "confirm",
            "variables": {"when": "Monday"},
            "to": "+15145551111",
            "caller_confirmed": True,
        },
    )
    assert other.error == "not_permitted"
    unknown = await ex.execute("send_sms", {"template": "free", "caller_confirmed": True})
    assert unknown.error == "invalid_arguments"
    sent = await ex.execute(
        "send_sms",
        {"template": "confirm", "variables": {"when": "Monday"}, "caller_confirmed": True},
    )
    assert sent.ok and suite.messages[0]["body"] == "Your appointment is on Monday."


def test_tool_schemas_are_provider_safe() -> None:
    ex = ToolExecutor.for_agent(_env(_config(), SandboxSuite()))
    for schema in ex.schemas():
        text = json.dumps(schema)
        assert "$ref" not in text and "$defs" not in text
        assert all("title" not in prop for prop in schema["parameters"]["properties"].values())
        params = schema["parameters"]
        assert params["type"] == "object" and params["additionalProperties"] is False
    book = next(s for s in ex.schemas() if s["name"] == "book_appointment")
    assert "caller_confirmed" in book["parameters"]["properties"]
    lookup = next(s for s in ex.schemas() if s["name"] == "lookup_customer")
    assert "caller_confirmed" not in lookup["parameters"]["properties"]


async def test_custom_rest_tool_through_executor() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.method == "POST":
            return httpx.Response(201, json={"id": 7, "status": "open"})
        return httpx.Response(200, json={"data": {"name": "Ana", "tier": "gold"}})

    integ = CustomRestIntegration(
        IntegrationContext(
            tenant_id=uuid.uuid4(),
            name="crm",
            kind="custom_rest",
            credentials={"token": "t" * 10},
            egress=EgressPolicy(),
            config={
                "base_url": "https://crm.example.com",
                "auth": {"type": "bearer"},
                "actions": [
                    {
                        "name": "lookup",
                        "description": "Find customer",
                        "path": "/c/{phone}",
                        "parameters": [{"name": "phone"}],
                        "response": {"fields": {"name": "data.name"}},
                    },
                    {
                        "name": "open_ticket",
                        "description": "Open a ticket",
                        "method": "POST",
                        "path": "/tickets",
                        "parameters": [{"name": "issue"}],
                        "response": {"fields": {"ticket": "id"}},
                    },
                ],
            },
        ),
        transport=httpx.MockTransport(handler),
    )
    cfg = AgentConfig.model_validate(
        sample_config(
            tools={"enabled": ["custom.crm.lookup", "custom.crm.open_ticket"]},
            integrations={"custom": ["crm"]},
        )
    )
    env = ToolEnv(
        tenant_id=uuid.uuid4(),
        agent_id=None,
        conversation_id="c",
        config=cfg,
        integrations=IntegrationSet(custom={"crm": integ}),
        ledger=InMemoryLedger(),
        caller_number=CALLER,
    )
    ex = ToolExecutor.for_agent(env)
    assert set(ex.tools) == {"crm__lookup", "crm__open_ticket"}
    out = await ex.execute("crm__lookup", {"phone": CALLER})
    assert out.ok and out.data == {"name": "Ana"}
    needs = await ex.execute("crm__open_ticket", {"issue": "broken"})
    assert needs.error == "confirmation_required"
    done = await ex.execute("crm__open_ticket", {"issue": "broken", "caller_confirmed": True})
    assert done.ok and done.data == {"ticket": 7}
    await integ.aclose()


async def test_ledger_outage_refuses_side_effects() -> None:
    class DeadLedger(InMemoryLedger):
        async def begin(self, *a: Any, **k: Any) -> Any:
            raise ConnectionError("database down")

    suite = SandboxSuite()
    ex = ToolExecutor.for_agent(_env(_config(), suite, ledger=DeadLedger()))
    out = await ex.execute("create_lead", {"title": "Quote request", "caller_confirmed": True})
    assert not out.ok and out.error == "unavailable" and not suite.state.leads
