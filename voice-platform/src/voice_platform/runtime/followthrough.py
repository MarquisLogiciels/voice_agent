"""Follow-through guard for narrated actions.

Small/fast models often answer "Let me check that for you, one moment" and then stop, without
calling the tool they just announced; the caller is left waiting in silence. (Lab finding with
Qwen 2.5 3B: the narrated reply happened in most availability requests; one follow-up
generation recovered the tool call 4/4.)

When a completed, uninterrupted agent reply announces a lookup, no tool ran since the caller's
last utterance, and the reply contains no actual answer, we run exactly one follow-up
generation per caller turn instructing the model to call the tool now. This never forces a
tool (``tool_choice`` stays automatic) and every confirmation/idempotency rule still applies.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_PENDING = re.compile(
    r"\b(let me (check|look|see|find|verify|pull|search)"
    r"|i'?ll (check|look|see|find|verify|pull|search)"
    r"|i will (check|look|see|find|verify|search)"
    r"|i'm (checking|looking)|i am (checking|looking)"
    r"|one moment|hold on|hold,? please|please hold|bear with me|just a (moment|sec|second)"
    r"|je (vais )?(vérifie|vérifier|regarde|regarder)|laissez-moi (vérifier|regarder)"
    r"|un (instant|moment)|je consulte)\b",
    re.IGNORECASE,
)

FOLLOW_UP_INSTRUCTIONS = (
    "You just told the caller you would check or look something up, but you did not call a "
    "tool. Call the appropriate tool now, without any text reply first. If no tool applies, "
    "answer briefly or say you cannot help with that."
)


def announces_pending_action(text: str) -> bool:
    """A short reply that promises a lookup and contains no answer (no figures)."""
    if not text or len(text.split()) > 25 or re.search(r"\d", text):
        return False
    return _PENDING.search(text) is not None


@dataclass
class FollowThroughGuard:
    """Tracks one caller turn at a time; call the hooks from conversation_item_added."""

    tool_count: Callable[[], int]
    trigger: Callable[[str], Any]
    on_triggered: Callable[[str], Any] | None = None
    _tools_at_turn_start: int = 0
    _used_this_turn: bool = False

    def user_turn(self) -> None:
        self._tools_at_turn_start = self.tool_count()
        self._used_this_turn = False

    def agent_reply(self, text: str, *, interrupted: bool) -> bool:
        if interrupted or self._used_this_turn:
            return False
        if self.tool_count() != self._tools_at_turn_start:
            return False
        if not announces_pending_action(text):
            return False
        self._used_this_turn = True
        if self.on_triggered is not None:
            self.on_triggered(text)
        self.trigger(FOLLOW_UP_INSTRUCTIONS)
        return True
