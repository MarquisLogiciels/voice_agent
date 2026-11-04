"""Benchmark runner: scenarios × repetitions → scored, aggregated, reproducible results."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..config.schema import AgentConfig
from ..observability.stats import summarize, wilson_interval
from ..shared.logging import get_logger
from .evaluators import LLMJudge, ScenarioScore, deterministic_checks, score
from .scenario import Scenario
from .simulator import AgentLLMFactory, SimulationResult, run_scenario, tool_calls_summary

logger = get_logger(__name__)


@dataclass
class ResultRecord:
    scenario: Scenario
    repetition: int
    sim: SimulationResult
    score: ScenarioScore

    def as_row(self) -> dict[str, Any]:
        return {
            "scenario_slug": self.scenario.id,
            "repetition": self.repetition,
            "passed": self.score.passed,
            "score": self.score.score,
            "category_scores": self.score.category_scores,
            "failures": self.score.failures,
            "transcript": self.sim.transcript(),
            "tool_calls": tool_calls_summary(self.sim),
            "metrics": self.score.metrics,
            "judge": self.score.judge,
        }


@dataclass
class BenchmarkResult:
    config_hash: str
    seed: int
    records: list[ResultRecord] = field(default_factory=list)

    def by_scenario(self) -> dict[str, list[ResultRecord]]:
        out: dict[str, list[ResultRecord]] = {}
        for r in self.records:
            out.setdefault(r.scenario.id, []).append(r)
        return out

    def scenario_pass_rates(self) -> dict[str, float]:
        return {
            sid: sum(r.score.passed for r in rs) / len(rs) for sid, rs in self.by_scenario().items()
        }

    def summary(self) -> dict[str, Any]:
        n = len(self.records)
        passed = sum(r.score.passed for r in self.records)
        weights = {r.scenario.id: r.scenario.weight for r in self.records}
        rates = self.scenario_pass_rates()
        weighted = (
            sum(rates[s] * weights[s] for s in rates) / sum(weights.values()) if rates else 0.0
        )
        cats: dict[str, list[float]] = {}
        for r in self.records:
            for k, v in r.score.category_scores.items():
                cats.setdefault(k, []).append(v)
        failure_counts = Counter(
            f["check"].split(":")[0] for r in self.records for f in r.score.failures
        )
        latencies = [t.latency_s for r in self.records for t in r.sim.turns]
        tool_events = [e for r in self.records for e in r.sim.tool_events]
        lo, hi = wilson_interval(passed, n)
        return {
            "runs": n,
            "passed": passed,
            "pass_rate": round(passed / n, 4) if n else 0.0,
            "pass_rate_ci95": [round(lo, 4), round(hi, 4)],
            "weighted_scenario_pass_rate": round(weighted, 4),
            "mean_score": round(sum(r.score.score for r in self.records) / n, 4) if n else 0.0,
            "category_scores": {k: round(sum(v) / len(v), 4) for k, v in cats.items()},
            "task_completion_rate": round(
                sum(
                    1
                    for r in self.records
                    if r.score.category_scores.get("task_completion", 1) >= 1
                )
                / n,
                4,
            )
            if n
            else 0.0,
            "tool_success_rate": round(sum(e["ok"] for e in tool_events) / len(tool_events), 4)
            if tool_events
            else None,
            "turn_latency_s": summarize(latencies),
            "failure_categories": dict(failure_counts.most_common()),
            "failed_scenarios": sorted({r.scenario.id for r in self.records if not r.score.passed}),
            "scenario_pass_rates": rates,
            "seed": self.seed,
            "config_hash": self.config_hash,
        }


def _config_hash(cfg: AgentConfig) -> str:
    return hashlib.sha256(
        json.dumps(cfg.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()[:16]


class BenchmarkRunner:
    def __init__(
        self,
        *,
        agent_llm_factory: AgentLLMFactory,
        caller_llm: Any | None = None,
        judge: LLMJudge | None = None,
        concurrency: int = 4,
        judge_threshold: float = 3.0,
    ) -> None:
        self.agent_llm_factory = agent_llm_factory
        self.caller_llm = caller_llm
        self.judge = judge
        self.concurrency = concurrency
        self.judge_threshold = judge_threshold

    async def run(
        self, config: AgentConfig, scenarios: list[Scenario], *, repetitions: int = 1, seed: int = 0
    ) -> BenchmarkResult:
        result = BenchmarkResult(config_hash=_config_hash(config), seed=seed)
        sem = asyncio.Semaphore(self.concurrency)
        rng = random.Random(seed)
        order = [(s, rep) for s in scenarios for rep in range(repetitions)]
        rng.shuffle(order)  # seeded order: reproducible, but no position bias

        async def one(s: Scenario, rep: int) -> ResultRecord:
            async with sem:
                sim = await run_scenario(
                    s, config, agent_llm=self.agent_llm_factory(config), caller_llm=self.caller_llm
                )
                checks = deterministic_checks(sim)
                judged = await self.judge.judge(sim) if self.judge is not None else None
                return ResultRecord(
                    s, rep, sim, score(sim, checks, judged, judge_threshold=self.judge_threshold)
                )

        records = await asyncio.gather(*(one(s, rep) for s, rep in order))
        result.records = sorted(records, key=lambda r: (r.scenario.id, r.repetition))
        return result


@dataclass
class Comparison:
    baseline: dict[str, Any]
    candidate: dict[str, Any]
    regressions: list[str]
    improvements: list[str]
    critical_regressions: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline,
            "candidate": self.candidate,
            "regressions": self.regressions,
            "improvements": self.improvements,
            "critical_regressions": self.critical_regressions,
            "delta_pass_rate": round(self.candidate["pass_rate"] - self.baseline["pass_rate"], 4),
        }


def compare(
    baseline: BenchmarkResult, candidate: BenchmarkResult, scenarios: list[Scenario]
) -> Comparison:
    critical = {s.id for s in scenarios if s.critical}
    b, c = baseline.scenario_pass_rates(), candidate.scenario_pass_rates()
    regressions = sorted(s for s in b if c.get(s, 0) < b[s])
    improvements = sorted(s for s in c if c[s] > b.get(s, 0))
    return Comparison(
        baseline=baseline.summary(),
        candidate=candidate.summary(),
        regressions=regressions,
        improvements=improvements,
        critical_regressions=[s for s in regressions if s in critical],
    )
