"""Availability computation, independent of the calendar vendor."""

from __future__ import annotations

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
        end = (datetime.combine(day + timedelta(days=1), time(0), tz) if b == "24:00"
               else datetime.combine(day, time.fromisoformat(b), tz))
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
            pref_ok = (preference == "any" or (preference == "morning" and hour < 12)
                       or (preference == "afternoon" and 12 <= hour < 17)
                       or (preference == "evening" and hour >= 17))
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
        months = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
                  "septembre", "octobre", "novembre", "décembre"]
        minute = f"{local.minute:02d}" if local.minute else ""
        return f"{days[local.weekday()]} {local.day} {months[local.month - 1]} à {local.hour} h {minute}".strip()
    return local.strftime("%A %B %-d at %-I:%M %p").replace(":00 ", " ")
