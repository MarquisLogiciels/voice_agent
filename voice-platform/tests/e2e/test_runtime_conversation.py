"""Complete conversation through the real LiveKit AgentSession (text mode).

Caller books an appointment (with confirmation round-trip), then asks for a human and is
transferred via SIP REFER (mocked transport). Asserts external side effects, idempotency,
transcript/tool persistence and the final conversation record.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from tests.conftest import sample_config
from voice_platform.config.schema import AgentConfig, PipelineMode
from voice_platform.config.service import ResolvedVersion
from voice_platform.evaluation.scripted import Reply, ScriptedLLM, after_tool, when_user
from voice_platform.integrations.registry import IntegrationSet
from voice_platform.integrations.sandbox import SandboxSuite
from voice_platform.providers.builder import BuiltPipeline, PipelineBuilder
from voice_platform.providers.registry import default_registry
from voice_platform.runtime.recorder import MemorySink
from voice_platform.runtime.session import CallInfo, RuntimeDeps, build_runtime_session
from voice_platform.telephony.sip import SipCallError
from voice_platform.telephony.transfer import TransferManager

NOW = datetime(2030, 1, 7, 13, 0, tzinfo=UTC)
CALLER = "+15145550199"


def _last_output(turn: Any, tool: str) -> dict[str, Any]:
    for item in reversed(turn.history):
        if item.type == "function_call_output" and item.name == tool:
            return json.loads(item.output)
    return {}


def scripted_receptionist() -> ScriptedLLM:
    def book(confirmed: bool) -> Any:
        def reply(m: Any, t: Any) -> Reply:
            slots = _last_output(t, "check_availability").get("data", {}).get("slots", [])
            args = {"start": slots[0]["start"], "attendee_name": "Ana Roy", "reason": "Cleaning"}
            if confirmed:
                args["caller_confirmed"] = True
            return Reply(tool_calls=[("book_appointment", args)])

        return reply

    return ScriptedLLM(
        [
            when_user(
                r"book .*on (\d{4}-\d{2}-\d{2})",
                lambda m, t: Reply(tool_calls=[("check_availability", {"date": m.group(1)})]),
            ),
            after_tool(
                "check_availability",
                lambda out, t: Reply(
                    text=f"I have {out['data']['slots'][0]['spoken']}. May I have your name?"
                ),
            ),
            when_user(r"my name is", book(confirmed=False)),
            after_tool(
                "book_appointment",
                lambda out, t: (
                    Reply(text="Just to confirm: a cleaning for Ana Roy at that time. Correct?")
                    if out.get("error") == "confirmation_required"
                    else Reply(text="You're all set!")
                    if out.get("ok")
                    else Reply(text="Sorry, that didn't work.")
                ),
            ),
            when_user(r"^yes", book(confirmed=True)),
            when_user(
                r"person|human",
                lambda m, t: Reply(
                    tool_calls=[
                        (
                            "transfer_to_human",
                            {
                                "reason": "caller asked for a person",
                                "summary": "Ana Roy booked a cleaning and wants to speak to staff.",
                            },
                        )
                    ]
                ),
            ),
            after_tool(
                "transfer_to_human",
                lambda out, t: Reply(
                    text="Connecting you now." if out.get("ok") else "Nobody is available."
                ),
            ),
        ]
    )


class RecordingTransport:
    def __init__(self, fail_first: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_first = fail_first

    async def blind_transfer(self, **kw: Any) -> None:
        self.calls.append(kw)
        if self.fail_first and len(self.calls) == 1:
            raise SipCallError("busy", "486 Busy Here", sip_code=486)

    async def dial(self, **kw: Any) -> Any:
        raise NotImplementedError

    async def hangup(self, **kw: Any) -> None:
        self.calls.append({"hangup": kw})


def _resolved() -> ResolvedVersion:
    cfg = AgentConfig.model_validate(
        sample_config(
            tools={"enabled": ["check_availability", "book_appointment", "transfer_to_human"]},
            integrations={"calendar": "sandbox"},
            transfer={
                "enabled": True,
                "default_destination": "front",
                "destinations": [
                    {"name": "front", "phone_number": "+15145550100", "fallback": ["manager"]},
                    {"name": "manager", "phone_number": "+15145550101"},
                ],
            },
            conversation={"end_call_enabled": False},
        )
    )
    return ResolvedVersion(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        version=3,
        environment="prod",
        config=cfg,
        is_canary=False,
    )


async def test_book_then_transfer_full_conversation() -> None:
    suite = SandboxSuite()
    transport = RecordingTransport(fail_first=True)
    sink = MemorySink()
    resolved = _resolved()
    llm = scripted_receptionist()

    def transfer_factory(session: Any, env: Any) -> TransferManager:
        return TransferManager(
            config=env.config.transfer,
            timezone=env.config.business.timezone,
            transport=transport,
            room_name="call-1",
            caller_identity="sip_+1",
            clock=lambda: NOW,
        )

    deps = RuntimeDeps(
        builder=PipelineBuilder(default_registry()),
        integrations=IntegrationSet(calendar=suite),
        sink=sink,
        clock=lambda: NOW,
        transfer_factory=transfer_factory,
        pipeline_override=BuiltPipeline(
            mode=PipelineMode.CASCADED,
            llm=llm,
            turn_detection="manual",
            providers={"llm": ["llm:scripted:-"]},
        ),
        enable_policies=False,
    )
    rs = build_runtime_session(
        resolved, CallInfo(conversation_id="conv-e2e", caller_number=CALLER), deps
    )
    async with rs.session as session:
        await session.start(rs.agent)
        r1 = await session.run(user_input="Hi, I'd like to book a cleaning on 2030-01-07")
        r1.expect.contains_function_call(name="check_availability")
        r2 = await session.run(user_input="9 works, my name is Ana Roy")
        r2.expect.contains_function_call_output()
        assert not suite.state.appointments  # not booked without confirmation
        r3 = await session.run(user_input="yes that's right")
        r3.expect.contains_function_call(name="book_appointment")
        assert len(suite.state.appointments) == 1
        appt = suite.state.appointments[0]
        assert appt.attendee_phone == CALLER
        assert appt.start.astimezone(UTC).hour == 14  # 09:00 Toronto
        r4 = await session.run(user_input="Can I talk to a person please?")
        r4.expect.contains_function_call(name="transfer_to_human")
    final = await rs.close(status="transferred")

    # first destination busy → fallback destination succeeded
    assert [c["to_uri"] for c in transport.calls] == ["tel:+15145550100", "tel:+15145550101"]
    assert final["transfer"]["ok"] and final["transfer"]["destination"] == "manager"
    assert [a["status"] for a in final["transfer"]["attempts"]] == ["busy", "completed"]
    # persistence
    kinds = [e["type"] for e in sink.events]
    assert "transcript" in kinds and "tool" in kinds and "lifecycle" in kinds
    tools = [e for e in sink.events if e["type"] == "tool"]
    assert [t["tool"] for t in tools] == [
        "check_availability",
        "book_appointment",
        "book_appointment",
        "transfer_to_human",
    ]
    assert [t["ok"] for t in tools] == [True, False, True, True]
    assert sink.final is not None and sink.final["status"] == "transferred"
    assert sink.final["metrics"]["tool_calls"] == 4
    user_lines = [
        e["text"] for e in sink.events if e["type"] == "transcript" and e["role"] == "user"
    ]
    assert user_lines[0].startswith("Hi, I'd like to book")
    assert "Operating rules" in rs.agent.instructions


async def test_transfer_failure_everywhere_reports_failure_to_llm() -> None:
    class AlwaysBusy(RecordingTransport):
        async def blind_transfer(self, **kw: Any) -> None:
            self.calls.append(kw)
            raise SipCallError("no_answer", "480", sip_code=480)

    transport = AlwaysBusy()
    resolved = _resolved()
    deps = RuntimeDeps(
        builder=PipelineBuilder(default_registry()),
        integrations=IntegrationSet(calendar=SandboxSuite()),
        clock=lambda: NOW,
        transfer_factory=lambda s, env: TransferManager(
            config=env.config.transfer,
            timezone="America/Toronto",
            transport=transport,
            room_name="r",
            caller_identity="c",
            clock=lambda: NOW,
        ),
        pipeline_override=BuiltPipeline(
            mode=PipelineMode.CASCADED, llm=scripted_receptionist(), turn_detection="manual"
        ),
        enable_policies=False,
    )
    rs = build_runtime_session(resolved, CallInfo(conversation_id="c2", caller_number=CALLER), deps)
    async with rs.session as session:
        await session.start(rs.agent)
        result = await session.run(user_input="I want a human")
        result.expect.contains_function_call_output()
        outputs = [e for e in result.events if e.type == "function_call_output"]
        payload = json.loads(outputs[0].item.output)
        assert payload["ok"] is False and payload["error"] == "transfer_no_answer"
    await rs.close(status="completed")
    assert len(transport.calls) == 2  # both destinations attempted, no success claimed
