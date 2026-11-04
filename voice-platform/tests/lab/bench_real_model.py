"""Real-model benchmark + optimization run (self-hosted model; reproducible, opt-in).

    uv run python -m tests.lab.bench_real_model --reps 2 --iterations 3

Runs the receptionist and robustness scenario libraries against an OpenAI-compatible model
(default: Qwen 2.5 3B Instruct served by the lab's Ollama), scores every run with the
deterministic checks, optionally adds an LLM judge, then runs the optimizer (heuristic
proposer) with the same seeded scenarios and the standard promotion gate.

Honesty rules this script follows:
* the same scenarios, seed and repetitions are used for baseline and candidates;
* the evaluator and thresholds are never changed between runs;
* the model-based judge, when enabled, is the *same* model as the agent (only one model is
  available offline) — it is reported separately as low-trust and never used for gating;
* "eligible" only means the gate passed; promotion still needs a human (API-enforced).

Writes docs/evidence/benchmark_<label>.json and .md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.conftest import sample_config

from voice_platform.config.schema import AgentConfig, ProviderRef
from voice_platform.evaluation.evaluators import LLMJudge
from voice_platform.evaluation.runner import BenchmarkResult, BenchmarkRunner
from voice_platform.evaluation.scenario import load_scenarios
from voice_platform.optimization.engine import GateThresholds, Optimizer
from voice_platform.optimization.proposers import HeuristicProposer
from voice_platform.providers.registry import default_registry
from voice_platform.providers.types import BuildContext, ProviderKind

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "evidence"


def benchmark_config(instructions: str) -> AgentConfig:
    return AgentConfig.model_validate(
        sample_config(
            instructions=instructions,
            supported_languages=["fr-CA"],
            tools={
                "enabled": [
                    "lookup_customer",
                    "create_lead",
                    "check_availability",
                    "book_appointment",
                    "search_knowledge_base",
                ],
                "timeout_s": 3.0,
            },
            integrations={"crm": "sandbox", "calendar": "sandbox"},
            knowledge={"knowledge_base_ids": ["scenario-kb"]},
            transfer={
                "enabled": True,
                "default_destination": "front",
                "destinations": [
                    {"name": "front", "phone_number": "+15145550100", "fallback": ["manager"]},
                    {"name": "manager", "phone_number": "+15145550101"},
                ],
            },
            pipeline={
                "llm": {"temperature": 0.2, "connection": {"timeout_s": 120, "max_retries": 0}}
            },
        )
    )


def make_llm(base_url: str, model: str, temperature: float | None) -> Any:
    ref = ProviderRef(
        provider="openai-compatible",
        model=model,
        temperature=temperature,
        options={"base_url": base_url},
        connection={"timeout_s": 120, "max_retries": 0},
    )
    return default_registry().create(
        ProviderKind.LLM, ref, BuildContext(env={}, trusted_endpoints=(base_url,))
    )


def _per_scenario(res: BenchmarkResult) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sid, recs in res.by_scenario().items():
        out[sid] = {
            "pass_rate": round(sum(r.score.passed for r in recs) / len(recs), 3),
            "critical": recs[0].scenario.critical,
            "failures": sorted({f["check"] for r in recs for f in r.score.failures}),
            "example": next(
                (
                    {"transcript": r.sim.transcript(), "tools": r.as_row()["tool_calls"]}
                    for r in recs
                    if not r.score.passed
                ),
                None,
            ),
        }
    return out


def _usage(res: BenchmarkResult) -> dict[str, Any]:
    turns = [t for r in res.records for t in r.sim.turns]
    return {"turns": len(turns), "llm_cost_usd": 0.0, "note": "self-hosted model: no API cost"}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    ap.add_argument("--model", default="qwen2.5-3b")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--iterations", type=int, default=3)
    ap.add_argument("--judge", action="store_true", help="add a same-model LLM judge (low trust)")
    ap.add_argument("--label", default="qwen2.5-3b")
    ap.add_argument("--instructions", default="Help callers book dental appointments.")
    ap.add_argument("--only", default="", help="comma-separated scenario ids (smoke runs)")
    ap.add_argument("--turn-timeout", type=float, default=45.0)
    args = ap.parse_args()

    scenarios = load_scenarios(ROOT / "scenarios" / "receptionist") + load_scenarios(
        ROOT / "scenarios" / "robustness"
    )
    if args.only:
        keep = set(args.only.split(","))
        scenarios = [s for s in scenarios if s.id in keep]
    baseline = benchmark_config(args.instructions)

    def agent_llm(cfg: AgentConfig) -> Any:
        return make_llm(
            args.base_url, args.model, cfg.pipeline.llm.temperature if cfg.pipeline.llm else None
        )

    judge = (
        LLMJudge(make_llm(args.base_url, args.model, 0.0), model_label=f"{args.model} (same model)")
        if args.judge
        else None
    )
    runner = BenchmarkRunner(
        agent_llm_factory=agent_llm, judge=judge, concurrency=1, turn_timeout_s=args.turn_timeout
    )
    started = time.time()
    base = await runner.run(baseline, scenarios, repetitions=args.reps, seed=args.seed)
    base_s = time.time() - started
    print("baseline", json.dumps({k: base.summary()[k] for k in ("pass_rate", "pass_rate_ci95")}))

    # the judge is reported, never used for gating: optimize with deterministic checks only
    opt_runner = BenchmarkRunner(
        agent_llm_factory=agent_llm, judge=None, concurrency=1, turn_timeout_s=args.turn_timeout
    )
    opt = Optimizer(
        opt_runner,
        [HeuristicProposer()],
        thresholds=GateThresholds(),
        repetitions=args.reps,
        seed=args.seed,
        max_iterations=args.iterations,
    )
    if judge is not None:
        base_for_opt = await opt_runner.run(
            baseline, scenarios, repetitions=args.reps, seed=args.seed
        )
    else:
        base_for_opt = base
    outcome = await opt.run(baseline, scenarios, baseline_result=base_for_opt)

    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "model": {
            "provider": "openai-compatible (Ollama)",
            "model": args.model,
            "base_url": args.base_url,
            "temperature": 0.2,
        },
        "scenarios": len(scenarios),
        "repetitions": args.reps,
        "seed": args.seed,
        "baseline": {
            "config_hash": base.config_hash,
            "instructions": baseline.instructions,
            "summary": base.summary(),
            "per_scenario": _per_scenario(base),
            "usage": _usage(base),
            "wall_time_s": round(base_s, 1),
            "judge": (
                {
                    "label": judge.model_label,
                    "trust": "low: same model as the agent; not used for gating",
                    "agreement_with_deterministic": round(
                        sum(
                            (r.score.judge.get("overall", 0) >= 3) == r.score.passed
                            for r in base.records
                        )
                        / len(base.records),
                        3,
                    ),
                }
                if judge is not None
                else None
            ),
        },
        "optimization": outcome.report(),
    }
    if outcome.best_result is not None and outcome.best_config is not None:
        report["best_candidate"] = {
            "config_hash": outcome.best_result.config_hash,
            "instructions": outcome.best_config.instructions,
            "summary": outcome.best_result.summary(),
            "per_scenario": _per_scenario(outcome.best_result),
        }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / f"benchmark_{args.label}.json").write_text(
        json.dumps(report, indent=2, default=str)
    )
    (EVIDENCE / f"benchmark_{args.label}.md").write_text(outcome.markdown())
    print("decision", outcome.decision, "->", EVIDENCE / f"benchmark_{args.label}.json")


if __name__ == "__main__":
    asyncio.run(main())
