"""Proposal generators: turn failure evidence into candidate patches."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol

from ..config.schema import AgentConfig
from ..evaluation.runner import BenchmarkResult
from ..shared.logging import get_logger
from .patches import Patch

logger = get_logger(__name__)


@dataclass
class FailureReport:
    counts: dict[str, int]
    examples: dict[str, list[dict[str, Any]]]
    failed_scenarios: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts,
            "examples": self.examples,
            "failed_scenarios": self.failed_scenarios,
        }


def analyze_failures(result: BenchmarkResult, *, max_examples: int = 3) -> FailureReport:
    counts: Counter[str] = Counter()
    examples: dict[str, list[dict[str, Any]]] = {}
    for rec in result.records:
        for f in rec.score.failures:
            key = f["check"].split(":")[0] if f["check"] != "judge" else "judge_issue"
            if f["check"].startswith("tool:"):
                key = f["check"]  # keep which tool was missing
            counts[key] += 1
            bucket = examples.setdefault(key, [])
            if len(bucket) < max_examples:
                bucket.append(
                    {
                        "scenario": rec.scenario.id,
                        "detail": f["detail"][:300],
                        "transcript_tail": rec.sim.transcript()[-4:],
                    }
                )
    failed = sorted({r.scenario.id for r in result.records if not r.score.passed})
    return FailureReport(
        counts=dict(counts.most_common()), examples=examples, failed_scenarios=failed
    )


class Proposer(Protocol):
    name: str

    async def propose(
        self, config: AgentConfig, report: FailureReport, *, iteration: int
    ) -> list[Patch]: ...


# Evidence-driven, deterministic remedies. Each maps a failure signature to a bounded change.
_REMEDIES: dict[str, list[Patch]] = {
    "no_false_success_claims": [
        Patch(
            "append_text",
            "instructions",
            "Never say that something is booked, cancelled, sent or recorded unless the tool you "
            "called returned ok=true in this conversation. If it failed, say so plainly.",
            "agent claimed success without a successful tool result",
        )
    ],
    "tool:check_availability": [
        Patch(
            "append_text",
            "instructions",
            "Whenever the caller mentions a day or time for an appointment, call "
            "check_availability for that date before proposing any time.",
            "agent proposed or skipped times without checking availability",
        )
    ],
    "tool:book_appointment": [
        Patch(
            "append_text",
            "instructions",
            "Once the caller agrees to a specific slot and you have their name and the reason for "
            "the visit, read the details back, get a clear yes, then call book_appointment with "
            "caller_confirmed=true.",
            "bookings were not completed",
        )
    ],
    "tool:transfer_to_human": [
        Patch(
            "append_text",
            "instructions",
            "If the caller asks for a person, a manager or a human, do not argue: call "
            "transfer_to_human right away with a one-sentence summary.",
            "agent did not transfer when asked",
        )
    ],
    "tool:create_lead": [
        Patch(
            "append_text",
            "instructions",
            "For sales or service inquiries you cannot complete on the call, record a lead with "
            "create_lead (after confirming the details) so a team member can follow up.",
            "leads were not captured",
        )
    ],
    "tool:search_knowledge_base": [
        Patch(
            "append_text",
            "instructions",
            "For any question about services, prices, insurance or policies, call "
            "search_knowledge_base first and answer only from what it returns.",
            "factual questions answered without retrieval",
        )
    ],
    "never_mentions": [
        Patch(
            "append_text",
            "instructions",
            "Do not speculate about prices, diagnoses or availability. If unsure, offer to have a "
            "team member follow up.",
            "agent said something it must not say",
        )
    ],
    "no_repetition": [
        Patch(
            "append_text",
            "persona.speaking_style",
            "Vary your wording; never repeat the same sentence twice in a row.",
            "near-identical consecutive replies",
        )
    ],
    "turn_budget": [
        Patch(
            "append_text",
            "instructions",
            "Be efficient: ask for the information you need in as few turns as possible, and "
            "combine related questions.",
            "conversations took too many turns",
        )
    ],
    "latency": [
        Patch(
            "set",
            "turn_handling.endpointing.min_delay_s",
            0.35,
            "responses slow; reduce endpointing delay within safe bounds",
        )
    ],
}


class HeuristicProposer:
    """Applies the documented remedy for the most frequent failure signature not yet tried."""

    name = "heuristic"

    def __init__(self) -> None:
        self.tried: set[str] = set()

    async def propose(
        self, config: AgentConfig, report: FailureReport, *, iteration: int
    ) -> list[Patch]:
        for signature, _count in sorted(report.counts.items(), key=lambda kv: -kv[1]):
            remedy = _REMEDIES.get(signature)
            if remedy and signature not in self.tried:
                self.tried.add(signature)
                return list(remedy)
        return []


_LLM_PROMPT = """You improve the configuration of an AI phone agent based on test failures.
You may ONLY propose changes to these paths: instructions, persona.personality,
persona.speaking_style, greeting.text, tools.description_overrides.<tool>,
pipeline.llm.temperature (0-1), turn_handling.endpointing.min_delay_s (0.2-1.5),
turn_handling.endpointing.max_delay_s (1-6), knowledge.top_k (1-10), knowledge.mode (tool|auto),
conversation.max_tool_steps (2-8){models}.
Prefer small, targeted changes that address the most frequent failures. Do not remove existing
business rules. Respond with JSON only:
{{"patches": [{{"op": "set"|"append_text", "path": "...", "value": ..., "rationale": "..."}}]}}

Current instructions:
{instructions}

Enabled tools: {tools}

Failure report (counts and examples):
{report}"""


class LLMProposer:
    """Asks an optimizer LLM for patches. Output is untrusted: every patch is re-validated
    against the allowlist before use."""

    name = "llm"

    def __init__(
        self,
        llm: Any,
        *,
        allowed_models: dict[str, tuple[str, ...]] | None = None,
        max_patches: int = 4,
    ) -> None:
        self.llm = llm
        self.allowed_models = allowed_models or {}
        self.max_patches = max_patches

    async def propose(
        self, config: AgentConfig, report: FailureReport, *, iteration: int
    ) -> list[Patch]:
        from json_repair import repair_json
        from livekit.agents.llm import ChatContext

        models = ""
        if self.allowed_models:
            models = ", pipeline.llm.model (one of " + json.dumps(self.allowed_models) + ")"
        prompt = _LLM_PROMPT.format(
            models=models,
            instructions=config.instructions[:6000],
            tools=", ".join(config.tools.enabled),
            report=json.dumps(report.as_dict())[:8000],
        )
        ctx = ChatContext()
        ctx.add_message(role="user", content=prompt)
        out: list[str] = []
        try:
            async with self.llm.chat(chat_ctx=ctx) as stream:
                async for chunk in stream:
                    if chunk.delta and chunk.delta.content:
                        out.append(chunk.delta.content)
            data = json.loads(repair_json("".join(out)))
            patches = []
            for p in (data.get("patches") or [])[: self.max_patches]:
                if p.get("op") in ("set", "append_text") and isinstance(p.get("path"), str):
                    patches.append(
                        Patch(p["op"], p["path"], p.get("value"), str(p.get("rationale", ""))[:300])
                    )
            return patches
        except Exception:
            logger.warning("LLM proposer failed", exc_info=True)
            return []
