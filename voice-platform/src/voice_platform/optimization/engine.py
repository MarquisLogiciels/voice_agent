"""Evidence-driven optimization loop with a promotion gate.

baseline benchmark → failure analysis → proposal → isolated candidate → same seeded benchmark →
compare → reject regressions → keep best → report. Promotion to production always requires a
human approval (enforced by the API); this engine can only mark a candidate *eligible*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config.schema import AgentConfig
from ..evaluation.runner import BenchmarkResult, BenchmarkRunner, Comparison, compare
from ..evaluation.scenario import Scenario
from ..shared.logging import get_logger
from .patches import Patch, PatchPolicy, apply_patches
from .proposers import FailureReport, Proposer, analyze_failures

logger = get_logger(__name__)


@dataclass(frozen=True)
class GateThresholds:
    min_pass_rate: float = 0.8
    min_improvement: float = 0.0
    """Candidate pass rate must exceed baseline by more than this (strictly better)."""
    allow_critical_regressions: bool = False
    max_scenario_regressions: int = 0
    max_latency_regression_s: float = 0.5
    """Allowed increase of p95 turn latency."""
    min_interim_score_gain: float = 0.01
    """Between iterations, a candidate with equal pass rate is kept if its mean score improves
    by at least this much (partial credit lets complementary fixes accumulate)."""


@dataclass
class GateDecision:
    eligible: bool
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "reasons": self.reasons}


def gate(cmp: Comparison, t: GateThresholds) -> GateDecision:
    reasons: list[str] = []
    b, c = cmp.baseline, cmp.candidate
    if c["pass_rate"] < t.min_pass_rate:
        reasons.append(f"pass rate {c['pass_rate']:.2f} below minimum {t.min_pass_rate:.2f}")
    if c["pass_rate"] - b["pass_rate"] <= t.min_improvement:
        reasons.append(f"no improvement over baseline ({b['pass_rate']:.2f} → "
                       f"{c['pass_rate']:.2f})")
    if cmp.critical_regressions and not t.allow_critical_regressions:
        reasons.append(f"critical scenario regressions: {cmp.critical_regressions}")
    if len(cmp.regressions) > t.max_scenario_regressions:
        reasons.append(f"scenario regressions: {cmp.regressions}")
    bp95 = (b.get("turn_latency_s") or {}).get("p95")
    cp95 = (c.get("turn_latency_s") or {}).get("p95")
    if bp95 is not None and cp95 is not None and cp95 - bp95 > t.max_latency_regression_s:
        reasons.append(f"p95 turn latency regressed {bp95:.2f}s → {cp95:.2f}s")
    return GateDecision(eligible=not reasons, reasons=reasons)


@dataclass
class Iteration:
    index: int
    proposer: str
    patches_applied: list[Patch]
    patches_rejected: list[tuple[Patch, str]]
    summary: dict[str, Any] | None
    comparison: dict[str, Any] | None
    decision: GateDecision | None
    kept: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "proposer": self.proposer,
            "patches_applied": [p.as_dict() for p in self.patches_applied],
            "patches_rejected": [{"patch": p.as_dict(), "reason": r}
                                 for p, r in self.patches_rejected],
            "summary": self.summary, "comparison": self.comparison,
            "decision": self.decision.as_dict() if self.decision else None, "kept": self.kept,
        }


@dataclass
class OptimizationOutcome:
    baseline: BenchmarkResult
    best_config: AgentConfig | None
    best_result: BenchmarkResult | None
    iterations: list[Iteration] = field(default_factory=list)
    decision: str = "no_improvement"  # eligible | no_improvement
    final_gate: GateDecision | None = None
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def report(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at, "decision": self.decision,
            "baseline": self.baseline.summary(),
            "best": self.best_result.summary() if self.best_result else None,
            "final_gate": self.final_gate.as_dict() if self.final_gate else None,
            "iterations": [i.as_dict() for i in self.iterations],
        }

    def markdown(self) -> str:
        b = self.baseline.summary()
        lines = ["# Optimization report", "",
                 f"- Decision: **{self.decision}** (production promotion still requires human "
                 "approval)",
                 f"- Baseline pass rate: {b['pass_rate']:.0%} (95% CI {b['pass_rate_ci95'][0]:.0%}"
                 f"–{b['pass_rate_ci95'][1]:.0%}) over {b['runs']} runs"]
        if self.best_result is not None:
            c = self.best_result.summary()
            lines.append(f"- Best candidate pass rate: {c['pass_rate']:.0%} (95% CI "
                         f"{c['pass_rate_ci95'][0]:.0%}–{c['pass_rate_ci95'][1]:.0%})")
        if self.final_gate is not None and self.final_gate.reasons:
            lines.append("- Gate: " + "; ".join(self.final_gate.reasons))
        lines += ["", "## Baseline failure categories", ""]
        for k, v in b["failure_categories"].items():
            lines.append(f"- {k}: {v}")
        lines += ["", "## Iterations", ""]
        for it in self.iterations:
            status = "kept" if it.kept else "rejected"
            lines.append(f"### Iteration {it.index} ({it.proposer}) — {status}")
            for p in it.patches_applied:
                lines.append(f"- `{p.op}` `{p.path}`: {str(p.value)[:160]!r} — {p.rationale}")
            for p, reason in it.patches_rejected:
                lines.append(f"- rejected `{p.path}`: {reason}")
            if it.comparison:
                lines.append(f"- pass rate Δ {it.comparison['delta_pass_rate']:+.2f}; "
                             f"regressions: {it.comparison['regressions'] or 'none'}; "
                             f"improvements: {it.comparison['improvements'] or 'none'}")
            if it.decision and it.decision.reasons:
                lines.append("- gate: " + "; ".join(it.decision.reasons))
            lines.append("")
        return "\n".join(lines)


class Optimizer:
    def __init__(self, runner: BenchmarkRunner, proposers: list[Proposer], *,
                 policy: PatchPolicy | None = None, thresholds: GateThresholds | None = None,
                 repetitions: int = 1, seed: int = 0, max_iterations: int = 4) -> None:
        self.runner = runner
        self.proposers = proposers
        self.policy = policy or PatchPolicy()
        self.thresholds = thresholds or GateThresholds()
        self.repetitions = repetitions
        self.seed = seed
        self.max_iterations = max_iterations

    def _interim_decision(self, cmp: Comparison) -> GateDecision:
        t = self.thresholds
        strict = gate(cmp, GateThresholds(
            min_pass_rate=0.0, min_improvement=t.min_improvement,
            allow_critical_regressions=t.allow_critical_regressions,
            max_scenario_regressions=t.max_scenario_regressions,
            max_latency_regression_s=t.max_latency_regression_s))
        if strict.eligible:
            return strict
        # Only "no improvement in pass rate" may be waived, and only for a real score gain.
        others = [r for r in strict.reasons if not r.startswith("no improvement")]
        d_pass = cmp.candidate["pass_rate"] - cmp.baseline["pass_rate"]
        d_score = cmp.candidate["mean_score"] - cmp.baseline["mean_score"]
        if not others and d_pass >= 0 and d_score >= t.min_interim_score_gain:
            return GateDecision(True, [f"kept for partial improvement (mean score "
                                       f"{cmp.baseline['mean_score']:.3f} → "
                                       f"{cmp.candidate['mean_score']:.3f})"])
        return strict

    async def run(self, baseline: AgentConfig, scenarios: list[Scenario],
                  baseline_result: BenchmarkResult | None = None) -> OptimizationOutcome:
        base = baseline_result or await self.runner.run(
            baseline, scenarios, repetitions=self.repetitions, seed=self.seed)
        outcome = OptimizationOutcome(baseline=base, best_config=None, best_result=None)
        current_cfg, current_res = baseline, base
        for i in range(1, self.max_iterations + 1):
            report: FailureReport = analyze_failures(current_res)
            if not report.counts:
                break
            patches: list[Patch] = []
            proposer_name = ""
            for proposer in self.proposers:
                patches = await proposer.propose(current_cfg, report, iteration=i)
                proposer_name = proposer.name
                if patches:
                    break
            if not patches:
                break
            candidate_cfg, applied, rejected = apply_patches(current_cfg, patches, self.policy)
            if not applied:
                outcome.iterations.append(Iteration(i, proposer_name, [], rejected, None, None,
                                                    None, False))
                continue
            cand = await self.runner.run(candidate_cfg, scenarios, repetitions=self.repetitions,
                                         seed=self.seed)
            # compare against the current best (incremental) — no regressions allowed
            cmp = compare(current_res, cand, scenarios)
            decision = self._interim_decision(cmp)
            kept = decision.eligible
            outcome.iterations.append(Iteration(i, proposer_name, applied, rejected,
                                                cand.summary(), cmp.as_dict(), decision, kept))
            if kept:
                current_cfg, current_res = candidate_cfg, cand
        if current_res is not base:
            final = gate(compare(base, current_res, scenarios), self.thresholds)
            outcome.final_gate = final
            outcome.best_config, outcome.best_result = current_cfg, current_res
            outcome.decision = "eligible" if final.eligible else "no_improvement"
        return outcome
