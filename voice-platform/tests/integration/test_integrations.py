"""Vendor adapters against HTTP mocks shaped like the documented vendor APIs."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from voice_platform.integrations.base import (
    AppointmentInput,
    ContactInput,
    ErrorKind,
    IntegrationContext,
    IntegrationError,
    LeadInput,
)
from voice_platform.integrations.custom_rest import CustomRestConfig, CustomRestIntegration
from voice_platform.integrations.google_calendar import GoogleCalendar, event_id_for
from voice_platform.integrations.hubspot import HubSpotCRM
from voice_platform.integrations.salesforce import SalesforceCRM, sosl_escape
from voice_platform.integrations.sandbox import SandboxSuite
from voice_platform.integrations.twilio import validate_twilio_signature
from voice_platform.shared.http import EgressPolicy


class Recorder:
    """Route table + request log for httpx.MockTransport."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Any]] = []
        self.requests: list[httpx.Request] = []

    def add(self, method: str, path_prefix: str, handler: Any) -> None:
        self.routes.append((method, path_prefix, handler))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, prefix, handler in self.routes:
            if request.method == method and request.url.path.startswith(prefix):
                return handler(request) if callable(handler) else handler
        return httpx.Response(404, json={"message": "no route"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _ctx(kind: str, config: dict[str, Any], creds: dict[str, Any] | None) -> IntegrationContext:
    return IntegrationContext(tenant_id=uuid.uuid4(), name="main", kind=kind, config=config,
                              credentials=creds, egress=EgressPolicy(), timeout_s=3)


# --- HubSpot ---------------------------------------------------------------------------------
async def test_hubspot_lookup_by_phone_and_dedupe_create() -> None:
    rec = Recorder()
    rec.add("POST", "/crm/v3/objects/contacts/search", httpx.Response(200, json={"results": [
        {"id": "51", "properties": {"firstname": "Ana", "lastname": "Roy",
                                    "phone": "+15145550199"}}]}))
    crm = HubSpotCRM(_ctx("hubspot", {}, {"access_token": "pat-123456789"}),
                     transport=rec.transport)
    found = await crm.find_contacts_by_phone("+15145550199")
    assert found[0].id == "51" and found[0].full_name == "Ana Roy"
    body = json.loads(rec.requests[0].content)
    assert {"propertyName": "hs_searchable_calculated_phone_number", "operator": "EQ",
            "value": "5145550199"} in [f for g in body["filterGroups"] for f in g["filters"]]
    assert rec.requests[0].headers["authorization"] == "Bearer pat-123456789"
    # create with an existing phone returns the existing record, no POST to /contacts
    contact = await crm.create_contact(ContactInput(first_name="Ana", phone="514-555-0199"),
                                       idempotency_key="k1")
    assert contact.id == "51"
    assert not any(r.url.path == "/crm/v3/objects/contacts" for r in rec.requests)
    await crm.aclose()


async def test_hubspot_deal_creation_associates_contact() -> None:
    rec = Recorder()
    rec.add("POST", "/crm/v3/objects/deals", lambda r: httpx.Response(201, json={
        "id": "900", "properties": {"dealname": json.loads(r.content)["properties"]["dealname"],
                                    "dealstage": "appointmentscheduled"}}))
    crm = HubSpotCRM(_ctx("hubspot", {"lead_object": "deals"}, {"access_token": "pat-123456789"}),
                     transport=rec.transport)
    lead = await crm.create_lead(LeadInput(title="Cleaning inquiry", contact_id="51"),
                                 idempotency_key="k2")
    assert lead.id == "900" and lead.title == "Cleaning inquiry"
    body = json.loads(rec.requests[-1].content)
    assert body["associations"][0]["to"]["id"] == "51"
    assert body["associations"][0]["types"][0]["associationTypeId"] == 3
    with pytest.raises(IntegrationError) as ei:
        await crm.update_contact("../admin", ContactInput.model_construct())  # type: ignore[arg-type]
    assert ei.value.kind is ErrorKind.VALIDATION
    await crm.aclose()


async def test_retry_on_429_honours_retry_after_and_never_retries_post_timeouts() -> None:
    calls = {"search": 0, "create": 0}

    def search(r: httpx.Request) -> httpx.Response:
        calls["search"] += 1
        if calls["search"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"results": []})

    def create(r: httpx.Request) -> httpx.Response:
        calls["create"] += 1
        raise httpx.ReadTimeout("slow", request=r)

    rec = Recorder()
    rec.add("POST", "/crm/v3/objects/contacts/search", search)
    rec.add("POST", "/crm/v3/objects/contacts", create)
    crm = HubSpotCRM(_ctx("hubspot", {}, {"access_token": "pat-123456789"}),
                     transport=rec.transport)
    assert await crm.find_contacts_by_phone("+15145550100") == []
    assert calls["search"] == 2
    with pytest.raises(IntegrationError) as ei:
        await crm.create_contact(ContactInput(first_name="Zed", email="zed@example.com"),
                                 idempotency_key="k3")
    assert ei.value.kind is ErrorKind.TIMEOUT and ei.value.ambiguous
    assert calls["create"] == 1  # a POST that may have been applied is not blindly retried
    await crm.aclose()


async def test_auth_errors_are_not_retried() -> None:
    rec = Recorder()
    rec.add("POST", "/crm/v3/objects/contacts/search", httpx.Response(401, json={}))
    crm = HubSpotCRM(_ctx("hubspot", {}, {"access_token": "pat-123456789"}),
                     transport=rec.transport)
    with pytest.raises(IntegrationError) as ei:
        await crm.search_contacts("ana")
    assert ei.value.kind is ErrorKind.AUTH and len(rec.requests) == 1
    assert "credentials" in ei.value.safe_message()
    await crm.aclose()


async def test_circuit_breaker_opens_after_repeated_5xx() -> None:
    rec = Recorder()
    rec.add("GET", "/crm/v3/objects/contacts/", httpx.Response(503))
    crm = HubSpotCRM(_ctx("hubspot", {}, {"access_token": "pat-123456789"}),
                     transport=rec.transport)
    crm.http.breaker.failure_threshold = 2
    for _ in range(2):
        with pytest.raises(IntegrationError):
            await crm.get_contact("1")
    n = len(rec.requests)
    with pytest.raises(IntegrationError) as ei:
        await crm.get_contact("1")
    assert ei.value.kind is ErrorKind.UNAVAILABLE and "circuit" in ei.value.message
    assert len(rec.requests) == n  # short-circuited, no request sent
    await crm.aclose()


# --- Salesforce ------------------------------------------------------------------------------
def test_sosl_escape_neutralizes_injection() -> None:
    assert sosl_escape('x} RETURNING User(Id') == r"x\} RETURNING User\(Id"


async def test_salesforce_token_refresh_on_expired_session() -> None:
    tokens = iter(["tok-1", "tok-2"])
    rec = Recorder()
    rec.add("POST", "/services/oauth2/token", lambda r: httpx.Response(200, json={
        "access_token": next(tokens), "instance_url": "https://acme.my.salesforce.com"}))

    def search(r: httpx.Request) -> httpx.Response:
        if r.headers["authorization"] == "Bearer tok-1":
            return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID"}])
        return httpx.Response(200, json={"searchRecords": [
            {"Id": "003000000000001AAA", "FirstName": "Li", "LastName": "Wu",
             "Phone": "(514) 555-0101"}]})

    rec.add("GET", "/services/data/v61.0/search/", search)
    crm = SalesforceCRM(_ctx("salesforce", {}, {
        "client_id": "cid", "client_secret": "secret", "login_url":
        "https://acme.my.salesforce.com"}), transport=rec.transport)
    found = await crm.find_contacts_by_phone("+15145550101")
    assert found[0].id == "003000000000001AAA"
    q = rec.requests[-1].url.params["q"]
    assert q.startswith("FIND {15145550101} IN PHONE FIELDS")
    await crm.aclose()


async def test_salesforce_lead_upsert_with_external_id() -> None:
    rec = Recorder()
    rec.add("POST", "/services/oauth2/token", httpx.Response(200, json={
        "access_token": "tok", "instance_url": "https://acme.my.salesforce.com"}))
    rec.add("PATCH", "/services/data/v61.0/sobjects/Lead/VP_Key__c/",
            httpx.Response(201, json={"id": "00Q000000000001AAA", "success": True}))
    crm = SalesforceCRM(_ctx("salesforce", {"external_id_field": "VP_Key__c"}, {
        "client_id": "cid", "client_secret": "secret"}), transport=rec.transport)
    lead = await crm.create_lead(LeadInput(title="Quote", last_name="Wu", phone="+15145550101"),
                                 idempotency_key="conv1:create_lead:abc")
    assert lead.id == "00Q000000000001AAA"
    body = json.loads(rec.requests[-1].content)
    assert body["LastName"] == "Wu" and body["Company"] == "Unknown"
    await crm.aclose()


# --- Google Calendar -------------------------------------------------------------------------
def _sa_creds() -> dict[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {"client_email": "agent@proj.iam.gserviceaccount.com", "private_key": pem}


async def test_google_calendar_freebusy_and_idempotent_insert() -> None:
    created: dict[str, Any] = {}
    rec = Recorder()
    rec.add("POST", "/token", httpx.Response(200, json={"access_token": "ya29.x", "expires_in": 3600}))
    rec.add("POST", "/calendar/v3/freeBusy", httpx.Response(200, json={"calendars": {"primary": {
        "busy": [{"start": "2030-01-07T15:00:00Z", "end": "2030-01-07T16:00:00Z"}]}}}))

    def insert(r: httpx.Request) -> httpx.Response:
        body = json.loads(r.content)
        if body["id"] in created:
            return httpx.Response(409, json={"error": {"message": "duplicate"}})
        created[body["id"]] = {**body, "status": "confirmed"}
        return httpx.Response(200, json=created[body["id"]])

    rec.add("POST", "/calendar/v3/calendars/primary/events", insert)
    rec.add("GET", "/calendar/v3/calendars/primary/events/",
            lambda r: httpx.Response(200, json=created[r.url.path.rsplit("/", 1)[-1]]))
    cal = GoogleCalendar(_ctx("google_calendar", {}, _sa_creds()), transport=rec.transport)
    busy = await cal.busy_intervals(datetime(2030, 1, 7, tzinfo=UTC),
                                    datetime(2030, 1, 8, tzinfo=UTC))
    assert busy[0].start.hour == 15
    token_req = rec.requests[0]
    assert b"jwt-bearer" in token_req.content
    data = AppointmentInput(start=datetime(2030, 1, 7, 17, tzinfo=UTC),
                            end=datetime(2030, 1, 7, 17, 30, tzinfo=UTC), title="Cleaning",
                            attendee_name="Ana Roy", attendee_phone="+15145550199")
    a1 = await cal.create_appointment(data, idempotency_key="conv1:book:x")
    a2 = await cal.create_appointment(data, idempotency_key="conv1:book:x")
    assert a1.id == a2.id == event_id_for("conv1:book:x") and len(created) == 1
    assert a1.attendee_name == "Ana Roy"
    await cal.aclose()


def test_event_ids_are_valid_base32hex() -> None:
    eid = event_id_for("anything")
    assert 5 <= len(eid) <= 1024 and set(eid) <= set("0123456789abcdefghijklmnopqrstuv")


# --- custom REST -----------------------------------------------------------------------------
def _custom_cfg(**kw: Any) -> dict[str, Any]:
    return {
        "base_url": "https://crm.internal.example.com/api",
        "auth": {"type": "bearer"},
        "actions": [
            {"name": "lookup_customer", "description": "Find a customer by phone",
             "method": "GET", "path": "/customers/{phone}",
             "parameters": [{"name": "phone", "description": "E.164"}],
             "response": {"fields": {"name": "data.name", "tier": "data.tier"}}},
            {"name": "create_lead", "description": "Create a lead", "method": "POST",
             "path": "/leads", "parameters": [{"name": "name"}, {"name": "issue",
                                                                  "max_length": 50}]},
        ], **kw}


async def test_custom_rest_executes_fixed_actions_safely() -> None:
    rec = Recorder()
    rec.add("GET", "/api/customers/", httpx.Response(200, json={"data": {
        "name": "Ana", "tier": "gold", "ssn": "secret"}}))
    rec.add("POST", "/api/leads", httpx.Response(201, json={"id": 7}))
    integ = CustomRestIntegration(_ctx("custom_rest", _custom_cfg(), {"token": "tok-abcdefgh"}),
                                  transport=rec.transport)
    out = await integ.execute("lookup_customer", {"phone": "../../admin?x=1"})
    assert out == {"name": "Ana", "tier": "gold"}  # only mapped fields reach the LLM
    assert rec.requests[0].url.raw_path == b"/api/customers/..%2F..%2Fadmin%3Fx%3D1"
    assert rec.requests[0].headers["authorization"] == "Bearer tok-abcdefgh"
    assert integ.actions["create_lead"].consequential  # writes are always consequential
    with pytest.raises(Exception):
        await integ.execute("create_lead", {"name": "x", "issue": "y" * 51})
    with pytest.raises(Exception):
        await integ.execute("create_lead", {"name": "x", "issue": "ok", "extra": "nope"})
    await integ.aclose()


def test_custom_rest_config_rejects_dangerous_settings() -> None:
    with pytest.raises(ValueError):
        CustomRestConfig.model_validate(_custom_cfg(default_headers={"Authorization": "x"}))
    with pytest.raises(ValueError):
        CustomRestConfig.model_validate(_custom_cfg(allowed_hosts=["other.example.com"]))
    with pytest.raises(ValueError):
        CustomRestConfig.model_validate({**_custom_cfg(), "actions": [
            {"name": "x", "description": "bad path", "path": "/a/{missing}"}]})


async def test_custom_rest_blocks_private_hosts_without_dev_flag() -> None:
    cfg = _custom_cfg(base_url="https://localhost/api", allowed_hosts=["localhost"])
    integ = CustomRestIntegration(_ctx("custom_rest", cfg, {"token": "tok-abcdefgh"}))
    with pytest.raises(IntegrationError) as ei:
        await integ.execute("lookup_customer", {"phone": "1"})
    assert ei.value.kind is ErrorKind.CONFIGURATION
    await integ.aclose()


# --- Twilio webhook signatures ---------------------------------------------------------------
def test_twilio_signature_validation() -> None:
    import base64
    import hashlib
    import hmac

    url = "https://example.com/v1/webhooks/twilio/sms"
    params = {"MessageSid": "SM1", "From": "+15145550199", "Body": "hi"}
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    sig = base64.b64encode(hmac.new(b"tok", payload.encode(), hashlib.sha1).digest()).decode()
    assert validate_twilio_signature("tok", url, params, sig)
    assert not validate_twilio_signature("tok", url, {**params, "Body": "tampered"}, sig)
    assert not validate_twilio_signature("other", url, params, sig)


# --- sandbox ---------------------------------------------------------------------------------
async def test_sandbox_faults_and_conflicts() -> None:
    suite = SandboxSuite({"faults": {"create_lead": {"error": "unavailable", "times": 1}}})
    with pytest.raises(IntegrationError):
        await suite.create_lead(LeadInput(title="x"), idempotency_key="a")
    lead = await suite.create_lead(LeadInput(title="x"), idempotency_key="a")
    assert lead.id.startswith("lead-")
    start = datetime(2030, 1, 7, 14, tzinfo=UTC)
    data = AppointmentInput(start=start, end=start + timedelta(minutes=30), title="t",
                            attendee_name="n")
    await suite.create_appointment(data, idempotency_key="b")
    with pytest.raises(IntegrationError) as ei:
        await suite.create_appointment(data, idempotency_key="c")
    assert ei.value.kind is ErrorKind.CONFLICT
    assert [o.operation for o in suite.operations].count("create_lead") == 2
