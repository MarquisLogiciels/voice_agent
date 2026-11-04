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
    r"|let'?s (check|look|see|find)"
    r"|i'?ll (check|look|see|find|verify|pull|search)"
    r"|i will (check|look|see|find|verify|search)"
    r"|i'm (checking|looking)|i am (checking|looking)"
    r"|one moment|hold on|hold,? please|please hold|bear with me|just a (moment|sec|second)"
    r"|(let me|i'?ll|i will|i'm going to) (transfer|connect|put) you"
    r"|je vous (transfère|transfere|mets en relation|passe)"
    r"|je (vais )?(vérifie|vérifier|regarde|regarder)|laissez-moi (vérifier|regarder)"
    r"|un (instant|moment)|je consulte)\b",
    re.IGNORECASE,
)

# Promises of an action ("I'll add you to our records", "someone will call you back").
_PROMISE = re.compile(
    r"\b(i'?ll|i will|let me|i'm going to|i am going to)\s+(add|note|record|log|create|"
    r"schedule|pass (this|that|it) (along|on)|let (the|our) team know|have someone|"
    r"get someone|make (a )?note)"
    r"|\b(someone|a team member|our team|we)\s+will\s+(call|contact|reach out to)\s+you"
    r"|\byou(?:'ll| will)\s+(get|receive)\s+a\s+call"
    r"|\bje\s+(vais|vous)\s+(ajoute|noter|inscri)",
    re.IGNORECASE,
)
# A tool call written out as text: 'search_knowledge_base({"query": ...})'.
_TEXT_CALL = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\{", re.DOTALL)


def starts_like_tool_call(head: str, tool_names: set[str]) -> bool:
    """True once the beginning of a reply reads ``<enabled tool name>(`` (streamed text)."""
    m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", head or "")
    return bool(m and m.group(1).lower() in tool_names)


def textual_tool_call(text: str, tool_names: set[str]) -> str | None:
    """Name of the tool the model wrote as text instead of calling (small models do this)."""
    m = _TEXT_CALL.match(text or "")
    if m and m.group(1).lower() in tool_names:
        return m.group(1).lower()
    return None


FOLLOW_UP_INSTRUCTIONS = (
    "You just told the caller you would check or look something up, but you did not call a "
    "tool. Call the appropriate tool now, without any text reply first. If no tool applies, "
    "answer briefly or say you cannot help with that."
)


def announces_pending_action(text: str, tool_names: set[str] | None = None) -> bool:
    """A reply that announces or promises an action instead of taking it: a short "let me
    check" with no answer (no figures), a promise ("I'll add you…", "someone will call you"),
    or a tool call written out as text."""
    if not text:
        return False
    if tool_names and textual_tool_call(text, tool_names):
        return True
    if len(text.split()) > 25 or re.search(r"\d", text):
        return False
    return _PENDING.search(text) is not None or _PROMISE.search(text) is not None


@dataclass
class FollowThroughGuard:
    """Tracks one caller turn at a time; call the hooks from conversation_item_added."""

    tool_count: Callable[[], int]
    trigger: Callable[[str], Any]
    on_triggered: Callable[[str], Any] | None = None
    tool_names: set[str] | None = None
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
        if not announces_pending_action(text, self.tool_names):
            return False
        self._used_this_turn = True
        if self.on_triggered is not None:
            self.on_triggered(text)
        self.trigger(FOLLOW_UP_INSTRUCTIONS)
        return True
