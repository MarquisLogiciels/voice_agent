"""Explicit requests for a person are detected deterministically (EN / FR)."""

from __future__ import annotations

import pytest

from voice_platform.runtime.escalation import handoff_summary, wants_human


@pytest.mark.parametrize(
    "text",
    [
        "Can I speak to a person, please?",
        "can i speak to a person please",  # STT output: no punctuation/case
        "I want to talk to a real person",
        "Let me talk to someone",
        "transfer me to the front desk",
        "Representative.",
        "operator please",
        "Est-ce que je peux parler à quelqu'un?",
        "Je veux parler à une vraie personne",
        "Passez-moi la réception",
        "I don't want to talk to a robot. Get me a real person please.",  # benchmark case
    ],
)
def test_detects_explicit_requests(text: str) -> None:
    assert wants_human(text)


@pytest.mark.parametrize(
    "text",
    [
        "I don't want to talk to a person, just book me in",
        "What time do you open on Monday?",
        "The person I saw last time was great",
        "Is the receptionist's name Sam?",
        "Je ne veux pas parler à quelqu'un",
        "",
        None,
    ],
)
def test_ignores_everything_else(text: str | None) -> None:
    assert not wants_human(text)


def test_handoff_summary_is_bounded_and_useful() -> None:
    lines = [("assistant", "Hi, how can I help?"), ("user", "I need to move my appointment")] * 50
    s = handoff_summary(lines, "+15145550199")
    assert s.startswith("Caller asked to speak to a person. Calling from +15145550199.")
    assert "move my appointment" in s and 10 <= len(s) <= 800
