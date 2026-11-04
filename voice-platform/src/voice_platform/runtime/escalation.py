"""Deterministic "let me talk to a person" escape hatch (``transfer.transfer_on_request``).

Reaching a human must not depend on the model choosing the right tool: in the SIP lab,
Qwen 2.5 3B answered "Can I speak to a person, please?" by calling ``check_availability``.
When the caller explicitly asks for a person, the runtime runs ``transfer_to_human`` itself
(through the normal tool executor: destinations, hours, fallbacks, announcement, failure
message and audit all still apply) and skips the LLM reply for that turn.
"""

from __future__ import annotations

import re

_EN = re.compile(
    r"\b(speak|talk|chat)\s+(to|with)\s+(a|an|the|some|your|real|actual|live)?\s*"
    r"(real\s+|live\s+|actual\s+)?(person|human|someone|somebody|representative|rep|agent|"
    r"operator|receptionist|staff|employee|people)\b"
    r"|\b(transfer|connect|put)\s+me\s+(to|through|with)\b"
    r"|\b(real|live|actual)\s+(person|human)\b"
    r"|\b(representative|operator)\s*(please)?\s*[.!?]*$",
    re.IGNORECASE,
)
_FR = re.compile(
    r"\b(parler|discuter)\s+(à|a|avec)\s+(un|une|quelqu'un|quelqu’un|la|le|votre|un\s+vrai|"
    r"une\s+vraie)?\s*(personne|humain|agent|agente|employé|employée|réceptionniste|"
    r"quelqu'un|quelqu’un|préposé|préposée)\b"
    r"|\b(transférez|transferez|passez|mettez)[- ]moi\b"
    r"|\bvrai(e)?\s+personne\b",
    re.IGNORECASE,
)
_PERSON = (
    r"(a|an|the|some|any|your)?\s*(real\s+|live\s+|actual\s+)?(person|human|someone|somebody|"
    r"representative|rep|agent|operator|receptionist|staff|employee|people)"
)
# Negation only counts when it is about talking to a *person* ("I don't want to talk to a
# person"); "I don't want to talk to a robot, get me a real person" is a request (benchmark
# scenario wants-human failed 0/2 on the first version of this rule).
_NEGATED = re.compile(
    r"\b(don'?t|do not|no need to|never|not)\s+(want|need|wish)?\s*(to\s+)?"
    r"(speak|talk|be transferred|transfer|connect)\s+(to|with)\s+" + _PERSON + r"\b"
    r"|\bne\s+(veux|souhaite)\s+pas\s+(parler|être transféré)",
    re.IGNORECASE,
)


def wants_human(text: str | None) -> bool:
    if not text:
        return False
    if _NEGATED.search(text):
        return False
    return bool(_EN.search(text) or _FR.search(text))


def handoff_summary(lines: list[tuple[str, str]], caller_number: str | None) -> str:
    """Plain summary for the human who picks up (TransferArgs.summary: 10–800 chars)."""
    head = "Caller asked to speak to a person."
    if caller_number:
        head += f" Calling from {caller_number}."
    recent = " ".join(f"{'Caller' if r == 'user' else 'Agent'}: {t}" for r, t in lines[-6:] if t)
    return (f"{head} Recent conversation: {recent}" if recent else head)[:800]
