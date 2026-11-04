"""The receptionist business workflow end to end, through the real vendor adapters, with the
failures that cause duplicates in production: a create that times out *after* the vendor
stored the record, followed by a retry, and a worker restart in the middle of the call.

lookup (HubSpot) → qualify + create lead (HubSpot deal) → real availability (Google free/busy)
→ book (Google event) → confirm only after the calendar confirmed → transfer.

The vendor fakes are stateful and follow the documented semantics the adapters rely on:
HubSpot has no idempotency keys (a repeated POST creates a second deal) and supports
search filters on properties; Google Calendar rejects a client-chosen duplicate event id
with 409. Nothing here contacts a real vendor.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from tests.conftest import sample_config
from tests.integration.test_integrations import _ctx, _sa_creds
from voice_platform.config.schema import AgentConfig
from voice_platform.integrations.google_calendar import GoogleCalendar
from voice_platform.integrations.hubspot import HubSpotCRM
from voice_platform.integrations.registry import IntegrationSet
from voice_platform.tools.executor import ToolExecutor
from voice_platform.tools.interfaces import ToolEnv, TransferResult
from voice_platform.tools.ledger import PostgresLedger

CALLER = "+15145550199"
NOW = datetime(2030, 1, 7, 13, 0, tzinfo=UTC)  # Monday 08:00 Toronto


class FakeHubSpot:
    def __init__(self) -> None:
        self.contacts: list[dict[str, Any]] = []
        self.deals: list[dict[str, Any]] = []
        self.timeout_next_deal_create = False
        self.posts: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        path = req.url.path
        if req.method == "POST":
            self.posts.append(path)
        if path == "/crm/v3/objects/contacts/search":
            values = {f["value"] for g in body.get("filterGroups", []) for f in g["filters"]}
            hits = [
                c for c in self.contacts if values & {c["properties"].get("phone"), "5145550199"}
            ]
            return httpx.Response(200, json={"results": hits, "total": len(hits)})
        if path == "/crm/v3/objects/contacts":
            c = {"id": str(100 + len(self.contacts)), "properties": body["properties"]}
            self.contacts.append(c)
            return httpx.Response(201, json=c)
        if path == "/crm/v3/objects/deals/search":
            f = body["filterGroups"][0]["filters"][0]
            hits = [d for d in self.deals if d["properties"].get(f["propertyName"]) == f["value"]]
            return httpx.Response(200, json={"results": hits[:1], "total": len(hits)})
        if path == "/crm/v3/objects/deals":
            d = {"id": str(900 + len(self.deals)), "properties": body["properties"]}
            self.deals.append(d)  # stored server-side…
            if self.timeout_next_deal_create:
                self.timeout_next_deal_create = False
                raise httpx.ReadTimeout("response lost", request=req)  # …but the reply is lost
            return httpx.Response(201, json=d)
        return httpx.Response(404, json={"message": f"no route {path}"})


class FakeGoogle:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.timeout_next_insert = False
        self.inserts = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/calendar/v3/freeBusy":
            busy = [{"start": e["start"]["dateTime"], "end": e["end"]["dateTime"]}
                    for e in self.events.values()]  # fmt: skip
            return httpx.Response(200, json={"calendars": {"primary": {"busy": busy}}})
        base = "/calendar/v3/calendars/primary/events"
        if path == base and req.method == "POST":
            self.inserts += 1
            ev = json.loads(req.content)
            if ev["id"] in self.events:
                return httpx.Response(409, json={"error": {"code": 409, "message": "duplicate"}})
            self.events[ev["id"]] = {**ev, "status": "confirmed"}
            if self.timeout_next_insert:
                self.timeout_next_insert = False
                raise httpx.ReadTimeout("response lost", request=req)
            return httpx.Response(200, json=self.events[ev["id"]])
        if path == base and req.method == "GET":
            return httpx.Response(200, json={"items": list(self.events.values())})
        if path.startswith(base + "/") and req.method == "GET":
            ev = self.events.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=ev) if ev else httpx.Response(404, json={})
        return httpx.Response(404, json={"message": f"no route {path}"})


class FakeTransfer:
    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg
        self.summaries: list[str] = []

    def destinations(self) -> list[Any]:
        return list(self.cfg.transfer.destinations)

    async def transfer(self, *, reason: str, destination: str | None, summary: str) -> Any:
        self.summaries.append(summary)
        return TransferResult(True, "completed", "front", "blind", "transferred")


def _config() -> AgentConfig:
    return AgentConfig.model_validate(
        sample_config(
            tools={
                "enabled": [
                    "lookup_customer",
                    "create_lead",
                    "check_availability",
                    "book_appointment",
                ],
                "timeout_s": 5,
            },
            integrations={"crm": "hubspot", "calendar": "gcal"},
            transfer={
                "enabled": True,
                "destinations": [{"name": "front", "phone_number": "+15145550100"}],
            },
        )
    )


async def test_full_workflow_survives_lost_responses_and_a_restart(db: Any, tenant: Any) -> None:
    hub, goog = FakeHubSpot(), FakeGoogle()
    crm = HubSpotCRM(
        _ctx("hubspot", {"lead_object": "deals", "idempotency_property": "vp_idempotency_key"},
             {"access_token": "pat-123456789"}),
        transport=httpx.MockTransport(hub),
    )  # fmt: skip
    cal = GoogleCalendar(
        _ctx("google_calendar", {}, _sa_creds()), transport=httpx.MockTransport(goog)
    )
    cfg = _config()
    conv = f"conv-{uuid.uuid4().hex[:8]}"

    def executor() -> tuple[ToolExecutor, FakeTransfer]:
        """A fresh executor = what a restarted worker builds for the same conversation."""
        transfer = FakeTransfer(cfg)
        env = ToolEnv(
            tenant_id=tenant.id,
            agent_id=None,
            conversation_id=conv,
            config=cfg,
            integrations=IntegrationSet(crm=crm, calendar=cal),
            ledger=PostgresLedger(db, tenant_id=tenant.id, conversation_id=None, agent_id=None),
            caller_number=CALLER,
            transfer=transfer,
            clock=lambda: NOW,
        )
        return ToolExecutor.for_agent(env), transfer

    ex, _ = executor()

    # 1. lookup: unknown caller
    found = await ex.execute("lookup_customer", {})
    assert found.ok and found.data and not found.data["found"]

    # 2. qualify + lead; HubSpot stores the deal but the response is lost
    lead_args = {"title": "Invisalign consultation", "first_name": "Ana", "last_name": "Roy",
                 "caller_confirmed": True}  # fmt: skip
    hub.timeout_next_deal_create = True
    first = await ex.execute("create_lead", lead_args)
    assert not first.ok and first.error == "timeout"
    assert "succeeded" not in first.message.lower() or "not" in first.message.lower()
    assert len(hub.deals) == 1  # it did happen at the vendor

    # 3. the model retries: found by idempotency key, no second deal, no second contact
    again = await ex.execute("create_lead", lead_args)
    assert again.ok and again.data and again.data["lead_id"] == hub.deals[0]["id"]
    assert len(hub.deals) == 1 and len(hub.contacts) == 1

    # 4. real availability from free/busy
    avail = await ex.execute("check_availability", {"date": "tomorrow"})
    assert avail.ok and avail.data and avail.data["date"] == "2030-01-08"
    slot = avail.data["slots"][0]["start"]

    # 5. booking: the calendar stores the event but the response is lost. The insert uses a
    #    deterministic event id, so the client may safely re-POST: 409 → existing event.
    goog.timeout_next_insert = True
    book = {"start": slot, "attendee_name": "Ana Roy", "reason": "Consultation",
            "caller_confirmed": True}  # fmt: skip
    booked = await ex.execute("book_appointment", book)
    assert booked.ok and booked.data and booked.data["confirmed"]
    assert len(goog.events) == 1 and goog.inserts == 2  # one stored, one 409

    # 6. worker restarts mid-call; the new process re-issues the same booking
    ex2, transfer = executor()
    # 7. a third identical request is replayed from the durable ledger: no vendor call
    inserts = goog.inserts
    replay = await ex2.execute("book_appointment", book)
    assert replay.ok and replay.replayed and goog.inserts == inserts

    # 8. hand off to a person with a summary
    t = await ex2.execute(
        "transfer_to_human",
        {"reason": "wants to discuss pricing", "summary": "Ana Roy, new lead, booked Jan 8."},
    )
    assert t.ok and transfer.summaries

    async with db.session(__import__("voice_platform.core.db", fromlist=["DbScope"]).DbScope
                          .for_tenant(tenant.id)) as s:  # fmt: skip
        import sqlalchemy as sa

        rows = (await s.execute(sa.text(
            "select tool_name, status, attempts from tool_executions order by started_at"
        ))).all()  # fmt: skip
    by_tool = {r.tool_name: (r.status, r.attempts) for r in rows}
    assert by_tool["create_lead"] == ("succeeded", 2)
    assert by_tool["book_appointment"] == ("succeeded", 1)
    await crm.aclose()
    await cal.aclose()
