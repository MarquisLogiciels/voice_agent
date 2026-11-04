"""Follow-through guard: recover tool calls the model announced but never made."""

from __future__ import annotations

import pytest

from voice_platform.runtime.followthrough import FollowThroughGuard, announces_pending_action


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I'll check the availability for next Monday. Hold please.", True),
        ("Let me check our schedule for you, one moment.", True),
        ("Un instant, je vérifie les disponibilités.", True),
        ("Sure, let me transfer you to someone who can help.", True),
        ("Je vous transfère à un collègue.", True),
        ("We open at 9 on Monday.", False),  # an answer, not a promise
        ("Let me check... we have 9:30 and 10:00 available.", False),  # contains the answer
        ("Sure! What day works best for you?", False),
        ("", False),
    ],
)
def test_announces_pending_action(text: str, expected: bool) -> None:
    assert announces_pending_action(text) is expected


def _guard() -> tuple[FollowThroughGuard, dict[str, int], list[str]]:
    state = {"tools": 0}
    fired: list[str] = []
    return FollowThroughGuard(tool_count=lambda: state["tools"], trigger=fired.append), state, fired


def test_triggers_once_per_caller_turn() -> None:
    guard, _, fired = _guard()
    guard.user_turn()
    assert guard.agent_reply("Let me check that for you.", interrupted=False)
    # the follow-up itself narrates again: no loop
    assert not guard.agent_reply("One moment please.", interrupted=False)
    assert len(fired) == 1
    guard.user_turn()
    assert guard.agent_reply("Let me look that up.", interrupted=False)
    assert len(fired) == 2


def test_no_trigger_when_a_tool_ran_or_reply_was_interrupted() -> None:
    guard, state, fired = _guard()
    guard.user_turn()
    state["tools"] += 1
    assert not guard.agent_reply("Let me check that for you.", interrupted=False)
    guard.user_turn()
    assert not guard.agent_reply("Let me check that for you.", interrupted=True)
    assert fired == []


@pytest.mark.parametrize(
    "text",
    [
        "Got it, Paul. I'll add you to our records. You can expect a call back shortly.",
        "Someone will call you back tomorrow.",
        "Sure, let's check available slots for Tuesday in the morning.",  # benchmark case
        'Search_knowledge_base({"query": "cleaning cost"})',
    ],
)
def test_promises_and_textual_tool_calls_count_as_pending(text: str) -> None:
    tools = {"search_knowledge_base", "create_lead"}
    assert announces_pending_action(text, tools)


def test_textual_tool_call_needs_an_enabled_tool() -> None:
    from voice_platform.runtime.followthrough import starts_like_tool_call

    assert starts_like_tool_call("search_knowledge_base(", {"search_knowledge_base"})
    assert not starts_like_tool_call("Sure (I can help", {"search_knowledge_base"})
    assert not starts_like_tool_call("print(", {"search_knowledge_base"})
