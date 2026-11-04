"""Test doubles for the agent LLM.

``receptionist_model(competent=...)`` is a ScriptedLLM whose behaviour depends on the agent's
instructions, mimicking an instruction-following model. It lets the evaluation and
optimization machinery be tested end-to-end without network access. It says nothing about
the quality of any real LLM.
"""

from __future__ import annotations

import json
import re
from typing import Any

from voice_platform.evaluation.scripted import Reply, Rule, ScriptedLLM, Turn, after_tool, when_user

DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
CHECK_FIRST = "call check_availability for that date before proposing"
CONFIRM_BOOK = "then call book_appointment with caller_confirmed=true"
NO_FALSE_CLAIMS = "never say that something is booked"


def _last_output(t: Turn, tool: str) -> dict[str, Any]:
    for item in reversed(t.history):
        if item.type == "function_call_output" and item.name == tool:
            try:
                return json.loads(item.output)
            except ValueError:
                return {}
    return {}


def _french(t: Turn) -> bool:
    return bool(t.user_text and re.search(r"\b(bonjour|je|oui|rendez-vous)\b", t.user_text, re.I))


def _name(t: Turn) -> str:
    for item in reversed(t.history):
        if item.type == "message" and item.role == "user":
            m = re.search(
                r"(?i:my name is|i'?m|je m'appelle)\s+([A-Z][a-zé]+(?: [A-Z][a-z]+)?)",
                item.text_content or "",
            )
            if m:
                return m.group(1)
    return "Caller"


def receptionist_model(*, competent: bool | None = None) -> ScriptedLLM:
    """competent=None → behaviour derived from instructions (for optimizer tests)."""

    def follows(t: Turn, phrase: str) -> bool:
        return competent if competent is not None else t.instruction_has(phrase)

    def booking_request(t: Turn) -> Reply | None:
        if t.tool_name or not t.user_text:
            return None
        if re.search(r"changed my mind|don'?t book", t.user_text, re.I):
            return Reply(text="No problem, I won't book anything. Call us anytime.")
        m = DATE.search(t.user_text)
        if not m or not re.search(
            r"book|appointment|rendez-vous|anything on|cleaning|checkup", t.user_text, re.I
        ):
            if re.search(r"appointment", t.user_text, re.I):
                return Reply(text="Sure, which day would you like?")
            return None
        if follows(t, CHECK_FIRST):
            return Reply(tool_calls=[("check_availability", {"date": m.group(1)})])
        return Reply(text="I have 10 AM available that day. What is your name?")

    def availability(out: dict[str, Any], t: Turn) -> Reply:
        if not out.get("ok"):
            return Reply(
                text="Sorry, I can't reach our calendar right now. Can a team member call you back?"
            )
        slots = out["data"]["slots"]
        if not slots:
            return Reply(text="Sorry, there are no openings that day. Would another day work?")
        fr = bool(re.search(r"[àéè]", out["data"]["slots"][0]["spoken"]))
        return Reply(
            text=(
                f"J'ai {slots[0]['spoken']}. Quel est votre nom?"
                if fr
                else f"I have {slots[0]['spoken']}. May I have your name?"
            )
        )

    def name_given(t: Turn) -> Reply | None:
        if (
            t.tool_name
            or not t.user_text
            or not re.search(r"(?i:my name is|je m'appelle)|i'?m [A-Z]", t.user_text)
        ):
            return None
        slots = _last_output(t, "check_availability").get("data", {}).get("slots", [])
        if not slots:
            if follows(t, CHECK_FIRST):
                return Reply(text="Sorry, I don't have an opening to offer you.")
            return Reply(text="Thanks! Shall I book 10 AM for you?")
        if follows(t, CONFIRM_BOOK):
            return Reply(text=f"To confirm: {slots[0]['spoken']} for {_name(t)}. Correct?")
        return Reply(text=f"Great, shall I book {slots[0]['spoken']}?")

    def confirmed(t: Turn) -> Reply | None:
        if (
            t.tool_name
            or not t.user_text
            or not re.match(
                r"\s*(yes|oui|the first one|la première|that's correct)", t.user_text, re.I
            )
        ):
            return None
        slots = _last_output(t, "check_availability").get("data", {}).get("slots", [])
        if slots and follows(t, CONFIRM_BOOK):
            return Reply(
                tool_calls=[
                    (
                        "book_appointment",
                        {
                            "start": slots[0]["start"],
                            "attendee_name": _name(t),
                            "reason": "Cleaning",
                            "caller_confirmed": True,
                        },
                    )
                ]
            )
        if follows(t, NO_FALSE_CLAIMS):
            return Reply(text="I'll ask a team member to finalize that booking with you.")
        return Reply(text="You're all booked! See you then.")

    def booked(out: dict[str, Any], t: Turn) -> Reply:
        if out.get("ok"):
            return Reply(text="You're all set — the appointment is confirmed.")
        return Reply(text="Sorry, I couldn't complete the booking. Let me find another time.")

    rules: list[Rule] = [
        when_user(
            r"person|human|someone|real|manager",
            lambda m, t: Reply(
                tool_calls=[
                    (
                        "transfer_to_human",
                        {
                            "reason": "caller requested a person",
                            "summary": "Caller asked to speak with staff about their request.",
                        },
                    )
                ]
            ),
        ),
        after_tool(
            "transfer_to_human",
            lambda out, t: Reply(
                text="Connecting you now."
                if out.get("ok")
                else "I'm sorry, nobody is available. Can I take a message?"
            ),
        ),
        when_user(
            r"my file|my account|have my", lambda m, t: Reply(tool_calls=[("lookup_customer", {})])
        ),
        after_tool(
            "lookup_customer",
            lambda out, t: Reply(
                text="I found your file."
                if out.get("ok") and out["data"]["found"]
                else "I'm sorry, I can't access customer records right now. Can I take a message?"
            ),
        ),
        when_user(
            r"how much|price|cost|combien",
            lambda m, t: Reply(tool_calls=[("search_knowledge_base", {"query": t.user_text})]),
        ),
        after_tool(
            "search_knowledge_base",
            lambda out, t: Reply(
                text=(
                    "According to our price list: "
                    + out["data"]["results"][0]["excerpt"].split("\n")[-1]
                )
                if out.get("ok") and out["data"]["results"]
                else "I'm not sure; I can have someone call you back."
            ),
        ),
        Rule(booking_request, "booking_request"),
        after_tool("check_availability", availability),
        Rule(name_given, "name_given"),
        Rule(confirmed, "confirmed"),
        after_tool("book_appointment", booked),
        when_user(r".", Reply(text="Could you tell me more about what you need?")),
    ]
    return ScriptedLLM(rules)
