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
