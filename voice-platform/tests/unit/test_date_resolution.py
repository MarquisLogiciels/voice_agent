"""Relative dates are resolved by the platform, not by the model's calendar arithmetic.

Found in the lab: Qwen 2.5 3B turned "next Monday" (asked on a Wednesday) into a Sunday and
into a date 12 days out.
"""

from __future__ import annotations

from datetime import date

import pytest

from voice_platform.tools.scheduling import resolve_day, speakable_day, upcoming_days

WED = date(2026, 11, 4)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-09", date(2026, 11, 9)),
        ("2026-11-09T10:00:00-05:00", date(2026, 11, 9)),
        ("today", WED),
        ("Tomorrow", date(2026, 11, 5)),
        ("day after tomorrow", date(2026, 11, 6)),
        ("monday", date(2026, 11, 9)),
        ("next Monday", date(2026, 11, 9)),
        ("this friday", date(2026, 11, 6)),
        ("wednesday", date(2026, 11, 11)),  # a bare weekday never means today
        ("demain", date(2026, 11, 5)),
        ("lundi prochain", date(2026, 11, 9)),
        ("vendredi", date(2026, 11, 6)),
        ("après-demain", date(2026, 11, 6)),
    ],
)
def test_resolve_day(text: str, expected: date) -> None:
    assert resolve_day(text, WED) == expected


@pytest.mark.parametrize("text", ["", "soon", "2026-13-01", "the 9th", "next month"])
def test_unresolvable_dates_are_refused(text: str) -> None:
    assert resolve_day(text, WED) is None


def test_prompt_calendar_and_spoken_day() -> None:
    cal = upcoming_days(WED, days=5)
    assert cal.startswith("Thursday 2026-11-05") and "Monday 2026-11-09" in cal
    assert speakable_day(date(2026, 11, 9)) == "Monday November 9"
    assert speakable_day(date(2026, 11, 9), "fr-CA") == "lundi 9 novembre"


@pytest.mark.parametrize(
    ("said", "value", "expected"),
    [
        ("Do you have openings next Monday?", "2026-11-11", "monday"),  # model said Wed
        ("Do you have openings next Monday?", "2026-11-09", None),  # consistent
        ("Anything tomorrow morning?", "2026-11-06", "tomorrow"),
        ("Not Monday, maybe Tuesday?", "2026-11-10", None),
        ("Monday is bad, what about the day after?", "2026-11-10", None),  # ambiguous: trust
        ("Monday or Wednesday?", "2026-11-11", None),
        ("Anything the day after tomorrow?", "2026-11-05", "day after tomorrow"),
        ("Can I come Monday the 16th?", "2026-11-16", None),  # explicit day number: trust it
        ("lundi prochain, c'est possible?", "2026-11-11", "lundi"),
        ("Do you have openings next Monday?", "monday", None),  # words: resolved by platform
        (None, "2026-11-11", None),
    ],
)
def test_weekday_conflict(said: str | None, value: str, expected: str | None) -> None:
    from voice_platform.tools.scheduling import weekday_conflict

    day = resolve_day(value, WED)
    assert day is not None
    assert weekday_conflict(said, value, day, WED) == expected
