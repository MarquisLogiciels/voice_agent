"""Contract tests: the requests our adapters send, checked against the vendors' own
machine-readable specifications (not against fixtures written by us).

* Google Calendar — the official v3 discovery document shipped with
  ``google-api-python-client`` (methods, HTTP verbs, paths, parameter enums, body schemas).
* HubSpot — the request models of the official ``hubspot-api-client`` SDK, generated from
  HubSpot's OpenAPI specs (field names, types, enum values such as filter operators).

What this cannot show: server-side behaviour (validation rules not expressed in the spec,
pipeline/stage ids that exist in a given portal, rate limits). That needs a vendor sandbox —
see docs/providers-and-integrations.md "Sandbox verification".
"""

from __future__ import annotations

import importlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.test_integrations import Recorder, _ctx, _sa_creds
from voice_platform.integrations.base import AppointmentInput, ContactInput, LeadInput

googleapiclient = pytest.importorskip("googleapiclient")
hubspot_models = pytest.importorskip("hubspot.crm.contacts.models")


# ------------------------------------------------------------------------------------------
# Google Calendar (discovery document)
# ------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def calendar_spec() -> dict[str, Any]:
    root = Path(googleapiclient.__file__).parent / "discovery_cache" / "documents"
    return dict(json.loads((root / "calendar.v3.json").read_text()))


def _method(spec: dict[str, Any], resource: str, name: str) -> dict[str, Any]:
    return dict(spec["resources"][resource]["methods"][name])


def _path_matches(spec: dict[str, Any], method: dict[str, Any], url: httpx.URL) -> bool:
    template = (
        spec["servicePath"] + method["path"]
    )  # e.g. calendar/v3/calendars/{calendarId}/events
    rx = "^/" + re.sub(r"\{[^}]+\}", "[^/]+", template) + "$"
    return re.match(rx, url.path) is not None


def _check_schema(spec: dict[str, Any], schema: dict[str, Any], value: Any, where: str) -> None:
    """Validate ``value`` against a discovery-document schema (names and JSON types)."""
    if "$ref" in schema:
        schema = spec["schemas"][schema["$ref"]]
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        assert isinstance(value, dict), f"{where}: expected object"
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties")
        for k, v in value.items():
            if k in props:
                _check_schema(spec, props[k], v, f"{where}.{k}")
            elif extra is not None:
                _check_schema(spec, extra, v, f"{where}.{k}")
            else:
                raise AssertionError(f"{where}: field '{k}' is not in the Calendar API schema")
    elif t == "array":
        assert isinstance(value, list), f"{where}: expected array"
        for i, v in enumerate(value):
            _check_schema(spec, schema["items"], v, f"{where}[{i}]")
    elif t == "string":
        assert isinstance(value, str), f"{where}: expected string"
        if "enum" in schema:
            assert value in schema["enum"], f"{where}: '{value}' not in {schema['enum']}"
    elif t == "integer":
        assert isinstance(value, int), f"{where}: expected integer"
    elif t == "boolean":
        assert isinstance(value, bool), f"{where}: expected boolean"


def _check_params(method: dict[str, Any], url: httpx.URL) -> None:
    params = method.get("parameters", {})
    for k, v in url.params.multi_items():
        assert k in params, f"query parameter '{k}' is not defined for {method['id']}"
        if "enum" in params[k]:
            assert v in params[k]["enum"], f"{k}={v} not in {params[k]['enum']}"


async def test_google_calendar_requests_match_the_discovery_document(
    calendar_spec: dict[str, Any],
) -> None:
    from voice_platform.integrations.google_calendar import GoogleCalendar

    rec = Recorder()
    rec.add("POST", "/token", httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
    rec.add(
        "POST",
        "/calendar/v3/freeBusy",
        httpx.Response(200, json={"calendars": {"primary": {"busy": []}}}),
    )
    event = {
        "id": "abc12345",
        "status": "confirmed",
        "summary": "x",
        "start": {"dateTime": "2030-01-07T17:00:00Z"},
        "end": {"dateTime": "2030-01-07T17:30:00Z"},
    }
    rec.add("POST", "/calendar/v3/calendars/primary/events", httpx.Response(200, json=event))
    rec.add("GET", "/calendar/v3/calendars/primary/events/", httpx.Response(200, json=event))
    rec.add("GET", "/calendar/v3/calendars/primary/events", httpx.Response(200, json={"items": []}))
    rec.add("DELETE", "/calendar/v3/calendars/primary/events/", httpx.Response(204))
    cal = GoogleCalendar(_ctx("google_calendar", {}, _sa_creds()), transport=rec.transport)
    start, end = datetime(2030, 1, 7, tzinfo=UTC), datetime(2030, 1, 8, tzinfo=UTC)
    await cal.busy_intervals(start, end)
    await cal.list_appointments(start, end)
    await cal.create_appointment(
        AppointmentInput(
            start=datetime(2030, 1, 7, 17, tzinfo=UTC),
            end=datetime(2030, 1, 7, 17, 30, tzinfo=UTC),
            title="Cleaning",
            attendee_name="Ana Roy",
            attendee_phone="+15145550199",
            attendee_email="ana@example.com",
        ),
        idempotency_key="conv:book:1",
    )
    await cal.get_appointment("abc12345")
    await cal.cancel_appointment("abc12345")
    await cal.aclose()

    api_requests = [r for r in rec.requests if r.url.path.startswith("/calendar/")]
    checked = set()
    for req in api_requests:
        candidates = [
            ("freebusy", "query"),
            ("events", "insert"),
            ("events", "list"),
            ("events", "get"),
            ("events", "delete"),
        ]
        matched = [
            (res, name)
            for res, name in candidates
            if _method(calendar_spec, res, name)["httpMethod"] == req.method
            and _path_matches(calendar_spec, _method(calendar_spec, res, name), req.url)
        ]
        assert matched, f"{req.method} {req.url.path} is not a Calendar API v3 method"
        res, name = matched[0]
        m = _method(calendar_spec, res, name)
        _check_params(m, req.url)
        if "request" in m:
            _check_schema(calendar_spec, m["request"], json.loads(req.content), m["id"])
        checked.add(m["id"])
    assert checked == {
        "calendar.freebusy.query",
        "calendar.events.insert",
        "calendar.events.list",
        "calendar.events.get",
        "calendar.events.delete",
    }


def test_google_event_id_rule_is_the_documented_one(calendar_spec: dict[str, Any]) -> None:
    from voice_platform.integrations.google_calendar import event_id_for

    doc = calendar_spec["schemas"]["Event"]["properties"]["id"]["description"]
    assert "base32hex" in doc and "5 and 1024" in doc  # the rule our generator implements
    assert re.fullmatch(r"[a-v0-9]{5,1024}", event_id_for("conv:book:2030-01-07T17:00"))


# ------------------------------------------------------------------------------------------
# HubSpot (official SDK request models)
# ------------------------------------------------------------------------------------------
def _model(module: str, name: str) -> Any:
    return getattr(importlib.import_module(module), name)


def _check_model(cls: Any, value: Any, module: str, where: str) -> None:
    """Validate JSON against an OpenAPI-generated model: wire names, types, enums."""
    assert isinstance(value, dict), f"{where}: expected object"
    by_wire = {wire: attr for attr, wire in cls.attribute_map.items()}
    kwargs = {}
    for k, v in value.items():
        assert k in by_wire, f"{where}: '{k}' is not a field of {cls.__name__}"
        attr = by_wire[k]
        t = cls.openapi_types[attr]
        inner = re.fullmatch(r"list\[(\w+)\]", t)
        if inner and inner.group(1)[0].isupper():
            sub = _model(module, inner.group(1))
            kwargs[attr] = [
                _check_model(sub, x, module, f"{where}.{k}[{i}]") for i, x in enumerate(v)
            ]
        elif t[0].isupper():
            kwargs[attr] = _check_model(_model(module, t), v, module, f"{where}.{k}")
        elif t == "dict[str, str]":
            assert all(isinstance(x, str) for x in v.values()), f"{where}.{k}: values must be str"
            kwargs[attr] = v
        elif t == "int":
            assert isinstance(v, int), f"{where}.{k}: expected int"
            kwargs[attr] = v
        else:
            kwargs[attr] = v
    # the generated setters enforce required fields and enum values when validation is on
    conf = importlib.import_module(module.rsplit(".models", 1)[0] + ".configuration")
    strict = conf.Configuration()
    strict.client_side_validation = True
    return cls(**kwargs, local_vars_configuration=strict)


async def test_hubspot_requests_match_the_official_sdk_models() -> None:
    from voice_platform.integrations.hubspot import HubSpotCRM

    rec = Recorder()
    rec.add("POST", "/crm/v3/objects/contacts/search", httpx.Response(200, json={"results": []}))
    rec.add(
        "POST",
        "/crm/v3/objects/contacts",
        httpx.Response(201, json={"id": "51", "properties": {"firstname": "Ana"}}),
    )
    rec.add(
        "POST", "/crm/v3/objects/deals", httpx.Response(201, json={"id": "9", "properties": {}})
    )
    rec.add(
        "POST", "/crm/v3/objects/leads", httpx.Response(201, json={"id": "8", "properties": {}})
    )
    rec.add(
        "POST", "/crm/v3/objects/notes", httpx.Response(201, json={"id": "7", "properties": {}})
    )
    token = {"access_token": "pat-123456789"}
    deals = HubSpotCRM(_ctx("hubspot", {"lead_object": "deals"}, token), transport=rec.transport)
    leads = HubSpotCRM(_ctx("hubspot", {"lead_object": "leads"}, token), transport=rec.transport)
    await deals.find_contacts_by_phone("+15145550199")
    await deals.search_contacts("Ana Roy")
    await deals.create_contact(
        ContactInput(first_name="Ana", last_name="Roy", phone="+15145550199"),
        idempotency_key="k1",
    )
    await deals.create_lead(
        LeadInput(contact_id="51", title="Cleaning", description="new patient"),
        idempotency_key="k2",
    )
    await leads.create_lead(LeadInput(contact_id="51", title="Cleaning"), idempotency_key="k3")
    await deals.add_note("51", "Called about a cleaning", idempotency_key="k4")
    await deals.aclose()
    await leads.aclose()

    pkg = {
        "contacts": "hubspot.crm.contacts.models",
        "deals": "hubspot.crm.deals.models",
        "leads": "hubspot.crm.objects.leads.models",
        "notes": "hubspot.crm.objects.notes.models",
    }
    seen = set()
    for req in rec.requests:
        body = json.loads(req.content)
        m = re.fullmatch(r"/crm/v3/objects/(\w+)(/search)?", req.url.path)
        assert m, req.url.path
        obj, search = m.group(1), bool(m.group(2))
        module = pkg[obj]
        if search:
            _check_model(_model(module, "PublicObjectSearchRequest"), body, module, "search")
        else:
            _check_model(
                _model(module, "SimplePublicObjectInputForCreate"), body, module, f"{obj}.create"
            )
        seen.add((obj, search))
    assert seen == {
        ("contacts", True),
        ("contacts", False),
        ("deals", False),
        ("leads", False),
        ("notes", False),
    }
