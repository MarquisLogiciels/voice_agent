"""Scenario scoring: deterministic checks first, LLM-as-judge second."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..shared.logging import get_logger
from .simulator import SimulationResult

logger = get_logger(__name__)

CATEGORIES = ("task_completion", "tool_correctness", "conversation_quality", "voice",
              "business_outcome")

# Claims of completed actions (EN/FR) mapped to the tools that must have succeeded.
SUCCESS_CLAIMS: dict[str, list[str]] = {
    r"\b(you'?re (all )?(booked|set)|i(?:'ve| have) booked|appointment is (booked|confirmed)|"
    r"booked (you|it)|rendez-vous (est )?(confirm|réserv)|c'est réservé)\b": [
        "book_appointment"],
    r"\b(i(?:'ve| have) cancell?ed|appointment (has been|is) cancell?ed|annulé)\b": [
        "cancel_appointment"],
    r"\b(i(?:'ve| have) (sent|texted)|text (message )?(has been )?sent|message envoyé)\b": [
        "send_sms"],
    r"\b(i(?:'ve| have) (created|recorded|logged|noted) (a |your )?(lead|request|ticket))\b": [
        "create_lead", "add_note"],
}


@dataclass
class Check:
    name: str
    category: str
    passed: bool
    detail: str = ""
    critical: bool = True
    weight: float = 1.0


@dataclass
class ScenarioScore:
    scenario_id: str
    passed: bool
    score: float
    category_scores: dict[str, float]
    checks: list[Check]
    failures: list[dict[str, Any]]
    judge: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d


def _subset(expected: dict[str, Any], actual: dict[str, Any] | None) -> bool:
    if actual is None:
        return not expected
    for k, v in expected.items():
        a = actual.get(k)
        if isinstance(v, str) and isinstance(a, str):
            if v.lower() not in a.lower():
                return False
        elif a != v:
            return False
    return True


def _trigrams(text: str) -> set[tuple[str, ...]]:
    w = re.findall(r"\w+", text.lower())
    return {tuple(w[i:i + 3]) for i in range(max(0, len(w) - 2))}


def deterministic_checks(r: SimulationResult) -> list[Check]:
    s = r.scenario
    exp = s.expectations
    checks: list[Check] = []
    tools = r.tool_events
    agent_text = [t.agent for t in r.turns if t.agent]

    if r.error:
        checks.append(Check("simulation_completed", "task_completion", False, r.error))

    for te in exp.tools:
        matching = [e for e in tools if e["tool"] == te.name and _subset(te.args_subset,
                                                                         e.get("args"))]
        # replays are deduplicated by the idempotency ledger: they caused no new side effect
        matching = [e for e in matching if not e.get("replayed")]
        if te.must_succeed:
            matching_ok = [e for e in matching if e["ok"]]
        else:
            matching_ok = matching
        n = len(matching_ok)
        ok = n >= te.min_calls and (te.max_calls is None or n <= te.max_calls)
        checks.append(Check(f"tool:{te.name}", "tool_correctness", ok,
                            f"{n} matching call(s); expected ≥{te.min_calls}"
                            + (f" ≤{te.max_calls}" if te.max_calls is not None else "")))
    for name in exp.forbidden_tools:
        used = [e for e in tools if e["tool"] == name]
        checks.append(Check(f"forbidden:{name}", "tool_correctness", not used,
                            f"called {len(used)} time(s)" if used else ""))

    if exp.transfer is not None:
        checks.append(Check("transfer", "business_outcome", r.transfer_ok == exp.transfer,
                            f"transfer_ok={r.transfer_ok}, attempts={r.transfer_attempts}"))
    if exp.transfer_destination:
        dest_ok = r.transfer_ok and any(a.get("destination") == exp.transfer_destination
                                        for a in r.transfer_attempts[-1:])
        checks.append(Check("transfer_destination", "business_outcome", dest_ok,
                            str(r.transfer_attempts)))

    st = exp.state
    state = r.sandbox.state
    appts = [a for a in state.appointments if a.status != "cancelled"]
    seeded_appts = len([a for a in r.scenario.sandbox.appointments if a.status != "cancelled"])
    if st.appointments is not None:
        checks.append(Check("state:appointments", "business_outcome",
                            len(appts) - seeded_appts == st.appointments,
                            f"{len(appts) - seeded_appts} new appointment(s)"))
    if st.appointment_attendee_contains:
        hit = any(st.appointment_attendee_contains.lower() in (a.attendee_name or "").lower()
                  for a in appts)
        checks.append(Check("state:appointment_attendee", "task_completion", hit))
    if st.leads is not None:
        new = len(state.leads) - len(r.scenario.sandbox.leads)
        checks.append(Check("state:leads", "business_outcome", new == st.leads,
                            f"{new} new lead(s)"))
    if st.lead_title_contains:
        hit = any(st.lead_title_contains.lower() in lead.title.lower() for lead in state.leads)
        checks.append(Check("state:lead_title", "task_completion", hit))
    if st.contacts is not None:
        new = len(state.contacts) - len(r.scenario.sandbox.contacts)
        checks.append(Check("state:contacts", "business_outcome", new == st.contacts,
                            f"{new} new contact(s)"))
    if st.sms is not None:
        checks.append(Check("state:sms", "business_outcome", len(r.sandbox.messages) == st.sms,
                            f"{len(r.sandbox.messages)} sms"))

    all_agent = "\n".join(agent_text)
    for pattern in exp.agent_must_mention:
        checks.append(Check(f"mentions:{pattern}", "task_completion",
                            bool(re.search(pattern, all_agent, re.I)), critical=False))
    for pattern in exp.agent_must_not_mention:
        found = re.search(pattern, all_agent, re.I)
        checks.append(Check(f"never_mentions:{pattern}", "conversation_quality", not found,
                            found.group(0) if found else ""))

    if s.script:
        for i, turn in enumerate(s.script):
            if not turn.expect_agent or i >= len(r.turns):
                continue
            reply = r.turns[i].agent
            ok = any(re.search(p, reply, re.I) for p in turn.expect_agent)
            checks.append(Check(f"turn{i + 1}_reply", "conversation_quality", ok,
                                reply[:200], critical=False))

    if exp.no_false_success_claims:
        succeeded = {e["tool"] for e in tools if e["ok"]}
        claims = []
        for pattern, required in SUCCESS_CLAIMS.items():
            for text in agent_text:
                if re.search(pattern, text, re.I) and not succeeded & set(required):
                    claims.append(text[:160])
        checks.append(Check("no_false_success_claims", "conversation_quality", not claims,
                            "; ".join(claims)))

    if exp.no_repetition and len(agent_text) >= 2:
        repeats = 0
        for a, b in zip(agent_text, agent_text[1:], strict=False):
            ta, tb = _trigrams(a), _trigrams(b)
            if ta and tb and len(ta & tb) / len(ta | tb) > 0.8:
                repeats += 1
        checks.append(Check("no_repetition", "conversation_quality", repeats == 0,
                            f"{repeats} near-identical consecutive replies", critical=False))

    if exp.max_turns is not None:
        checks.append(Check("turn_budget", "conversation_quality",
                            len(r.turns) <= exp.max_turns, f"{len(r.turns)} turns",
                            critical=False))
    lat = [t.latency_s for t in r.turns]
    if exp.max_mean_turn_latency_s is not None and lat:
        mean = sum(lat) / len(lat)
        checks.append(Check("latency", "voice", mean <= exp.max_mean_turn_latency_s,
                            f"mean {mean:.2f}s", critical=False))
    replays = [e["tool"] for e in tools if e.get("replayed")]
    checks.append(Check("no_repeated_actions", "tool_correctness", not replays,
                        f"repeated (deduplicated) calls: {replays}" if replays else "",
                        critical=False))
    internal = [e for e in tools if e.get("error") == "internal_error"]
    checks.append(Check("no_internal_errors", "tool_correctness", not internal,
                        f"{len(internal)} internal error(s)"))
    return checks


_JUDGE_PROMPT = """You are an expert QA reviewer for AI phone receptionists. Evaluate the
conversation below. Score each criterion from 1 (very poor) to 5 (excellent).
Criteria:
{criteria}
Also list concrete issues (hallucinations, ignored instructions, rudeness, repetition,
unnecessary questions). Respond with JSON only:
{{"scores": {{"<criterion>": <1-5>, ...}}, "overall": <1-5>, "issues": ["..."]}}

Scenario: {scenario}
Caller goal / persona: {persona}
Tool calls: {tools}
Transcript:
{transcript}"""

DEFAULT_RUBRIC = [
    "relevance: replies address what the caller said",
    "naturalness: concise, spoken-style, polite",
    "instruction_following: follows the business instructions and policies",
    "groundedness: no invented facts; uncertainty acknowledged",
    "task_progress: moves the caller efficiently toward their goal",
]


class LLMJudge:
    """LLM-as-judge. Must be configured with a model independent from the agent under test."""

    def __init__(self, llm: Any, *, model_label: str = "") -> None:
        self.llm = llm
        self.model_label = model_label

    async def judge(self, r: SimulationResult) -> dict[str, Any]:
        from livekit.agents.llm import ChatContext

        rubric = r.scenario.judge_rubric or DEFAULT_RUBRIC
        prompt = _JUDGE_PROMPT.format(
            criteria="\n".join(f"- {c}" for c in rubric),
            scenario=r.scenario.name,
            persona=(r.scenario.simulated.goal if r.scenario.simulated else
                     r.scenario.persona.description),
            tools=json.dumps([{k: e.get(k) for k in ("tool", "ok", "error")}
                              for e in r.tool_events]),
            transcript="\n".join(f"{t['role']}: {t['text']}" for t in r.transcript()),
        )
        ctx = ChatContext()
        ctx.add_message(role="user", content=prompt)
        out: list[str] = []
        try:
            async with self.llm.chat(chat_ctx=ctx) as stream:
                async for chunk in stream:
                    if chunk.delta and chunk.delta.content:
                        out.append(chunk.delta.content)
            from json_repair import repair_json

            data = json.loads(repair_json("".join(out)))
            overall = float(data.get("overall", 0))
            if not 1 <= overall <= 5:
                raise ValueError("overall out of range")
            return {"overall": overall, "scores": data.get("scores", {}),
                    "issues": data.get("issues", [])[:10], "model": self.model_label}
        except Exception as e:
            logger.warning("judge failed", exc_info=True)
            return {"error": f"{type(e).__name__}: {e}"[:200], "model": self.model_label}


def score(r: SimulationResult, checks: list[Check], judge: dict[str, Any] | None = None, *,
          judge_threshold: float = 3.0) -> ScenarioScore:
    cats: dict[str, float] = {}
    for cat in CATEGORIES:
        relevant = [c for c in checks if c.category == cat]
        if relevant:
            total = sum(c.weight for c in relevant)
            cats[cat] = round(sum(c.weight for c in relevant if c.passed) / total, 4)
    if judge and "overall" in judge:
        cats["judge"] = round((judge["overall"] - 1) / 4, 4)
    overall = round(sum(cats.values()) / len(cats), 4) if cats else 0.0
    critical_ok = all(c.passed for c in checks if c.critical)
    judge_ok = not judge or "overall" not in judge or judge["overall"] >= judge_threshold
    failures = [{"check": c.name, "category": c.category, "detail": c.detail,
                 "critical": c.critical} for c in checks if not c.passed]
    if judge and judge.get("issues"):
        failures += [{"check": "judge", "category": "conversation_quality", "detail": i,
                      "critical": False} for i in judge["issues"]]
    lat = [t.latency_s for t in r.turns]
    return ScenarioScore(
        scenario_id=r.scenario.id, passed=critical_ok and judge_ok, score=overall,
        category_scores=cats, checks=checks, failures=failures, judge=judge or {},
        metrics={"turns": len(r.turns), "tool_calls": len(r.tool_events),
                 "tool_failures": sum(1 for e in r.tool_events if not e["ok"]),
                 "mean_turn_latency_s": round(sum(lat) / len(lat), 4) if lat else None,
                 "max_turn_latency_s": round(max(lat), 4) if lat else None},
    )
