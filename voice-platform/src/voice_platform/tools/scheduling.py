"""Availability computation, independent of the calendar vendor."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from ..config.schema import AppointmentPolicy, BusinessHours
from ..integrations.base import TimeSlot

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime


def opening_windows(hours: BusinessHours, day: date, tz: ZoneInfo) -> list[Window]:
    ranges = hours.schedule.get(DAY_KEYS[day.weekday()], [])
    out: list[Window] = []
    for r in ranges:
        a, b = r.split("-")
        start = datetime.combine(day, time.fromisoformat(a), tz)
        end = (
            datetime.combine(day + timedelta(days=1), time(0), tz)
            if b == "24:00"
            else datetime.combine(day, time.fromisoformat(b), tz)
        )
        out.append(Window(start, end))
    return out


def is_within_hours(hours: BusinessHours, start: datetime, end: datetime, tz: ZoneInfo) -> bool:
    local_start = start.astimezone(tz)
    for w in opening_windows(hours, local_start.date(), tz):
        if w.start <= local_start and end.astimezone(tz) <= w.end:
            return True
    return False


def overlaps(a_start: datetime, a_end: datetime, busy: list[TimeSlot]) -> bool:
    return any(b.start < a_end and a_start < b.end for b in busy)


def free_slots(
    *,
    day: date,
    policy: AppointmentPolicy,
    tz: ZoneInfo,
    busy: list[TimeSlot],
    now: datetime,
    duration_min: int | None = None,
    preference: Literal["morning", "afternoon", "evening", "any"] = "any",
    step_min: int = 15,
    limit: int = 4,
) -> list[TimeSlot]:
    duration = timedelta(minutes=duration_min or policy.default_duration_min)
    earliest = now + timedelta(minutes=policy.min_notice_min)
    latest_day = (now.astimezone(tz) + timedelta(days=policy.max_days_ahead)).date()
    if day > latest_day or day < now.astimezone(tz).date():
        return []
    out: list[TimeSlot] = []
    for w in opening_windows(policy.hours, day, tz):
        cursor = w.start
        while cursor + duration <= w.end:
            end = cursor + duration
            hour = cursor.hour
            pref_ok = (
                preference == "any"
                or (preference == "morning" and hour < 12)
                or (preference == "afternoon" and 12 <= hour < 17)
                or (preference == "evening" and hour >= 17)
            )
            if cursor >= earliest and pref_ok and not overlaps(cursor, end, busy):
                out.append(TimeSlot(start=cursor, end=end))
                if len(out) >= limit:
                    return out
                cursor = end  # spread suggestions instead of 15-minute neighbours
                continue
            cursor += timedelta(minutes=step_min)
    return out


def speakable(dt: datetime, tz: ZoneInfo, language: str = "en") -> str:
    local = dt.astimezone(tz)
    if language.startswith("fr"):
        days = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
        months = [
            "janvier",
            "février",
            "mars",
            "avril",
            "mai",
            "juin",
            "juillet",
            "août",
            "septembre",
            "octobre",
            "novembre",
            "décembre",
        ]
        minute = f"{local.minute:02d}" if local.minute else ""
        day = f"{days[local.weekday()]} {local.day} {months[local.month - 1]}"
        return f"{day} à {local.hour} h {minute}".strip()
    return local.strftime("%A %B %-d at %-I:%M %p").replace(":00 ", " ")


_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5,
    "sunday": 6, "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4,
    "samedi": 5, "dimanche": 6,
}  # fmt: skip
_RELATIVE = {
    "today": 0, "tomorrow": 1, "day after tomorrow": 2, "aujourd'hui": 0, "aujourdhui": 0,
    "demain": 1, "après-demain": 2, "apres-demain": 2, "après demain": 2,
}  # fmt: skip
_QUALIFIERS = ("next ", "this ", "coming ", "this coming ", "ce ", "prochain ")
_SUFFIXES = (" prochain", " qui vient", " next week")


def resolve_day(value: str, today: date) -> date | None:
    """Resolve what callers (and LLMs) put in a date field: ISO ``YYYY-MM-DD`` (a datetime
    prefix is accepted), ``today``/``tomorrow``, or a weekday name in English or French.

    Small models are unreliable at calendar arithmetic, so relative dates are resolved here,
    deterministically. A bare or "next" weekday means its next occurrence after today (on a
    Wednesday, "Monday" and "next Monday" are both the coming Monday); the tool result echoes
    the resolved date so the agent reads it back and the caller can correct it.
    """
    v = " ".join(value.strip().lower().replace(",", " ").split())
    if len(v) >= 10 and v[:4].isdigit() and v[4] == "-":
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    if v in _RELATIVE:
        return today + timedelta(days=_RELATIVE[v])
    for q in sorted(_QUALIFIERS, key=len, reverse=True):
        if v.startswith(q):
            v = v[len(q) :]
            break
    for suffix in _SUFFIXES:
        if v.endswith(suffix):
            v = v[: -len(suffix)]
    if v in _WEEKDAYS:
        delta = (_WEEKDAYS[v] - today.weekday()) % 7
        return today + timedelta(days=delta or 7)
    return None


def speakable_day(day: date, language: str = "en") -> str:
    if language.startswith("fr"):
        return speakable(datetime.combine(day, time(12)), ZoneInfo("UTC"), language).split(" à ")[0]
    return datetime.combine(day, time(12)).strftime("%A %B %-d")


def upcoming_days(today: date, days: int = 8) -> str:
    """Compact calendar for prompts: 'Thursday 2026-11-05, Friday 2026-11-06, ...'."""
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    out = []
    for i in range(1, days + 1):
        d = today + timedelta(days=i)
        out.append(f"{names[d.weekday()]} {d.isoformat()}")
    return ", ".join(out)


_MONTH_OR_DAY_NUMBER = re.compile(
    r"\b(\d{1,2}(st|nd|rd|th|er)?|january|february|march|april|may|june|july|august|"
    r"september|october|november|december|janvier|février|fevrier|mars|avril|mai|juin|"
    r"juillet|août|aout|septembre|octobre|novembre|décembre|decembre)\b"
)


_AMBIGUOUS = re.compile(
    r"\b(not|n't|no|except|other than|instead|after|before|besides|or|pas|sauf|plutôt|plutot|"
    r"après|apres|avant|ou)\b|n't"
)


def weekday_conflict(said: str | None, value: str, day: date, today: date) -> str | None:
    """Detect a model-computed ISO date that contradicts the day the caller named.

    Returns the caller's own word (e.g. ``"monday"``) only in the unambiguous case: the caller
    named exactly one weekday / today / tomorrow, with no negation or alternative ("not",
    "or", "after"...), no day-of-month and no month; ``value`` is an ISO date on a different
    weekday. Anything else returns None and the model's date is used as given.
    """
    if not said or not value[:4].isdigit():
        return None
    text = said.lower()
    unambiguous = text.replace("day after tomorrow", " ").replace("après-demain", " ")
    if _MONTH_OR_DAY_NUMBER.search(text) or _AMBIGUOUS.search(unambiguous):
        return None
    candidates: dict[int, str] = {}
    for word, wd in _WEEKDAYS.items():
        if re.search(rf"\b{word}\b", text):
            candidates[wd] = word
    for word, offset in sorted(_RELATIVE.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(word)}\b", text):
            candidates[(today + timedelta(days=offset)).weekday()] = word
            break  # "day after tomorrow" also contains "tomorrow"
    if len(candidates) != 1 or day.weekday() in candidates:
        return None
    return next(iter(candidates.values()))
