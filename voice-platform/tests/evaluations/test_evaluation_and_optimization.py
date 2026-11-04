"""Evaluation engine + optimization loop, end to end, offline and deterministic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.conftest import sample_config
from tests.support.models import CHECK_FIRST, receptionist_model
from voice_platform.config.schema import AgentConfig
from voice_platform.evaluation.runner import BenchmarkRunner
from voice_platform.evaluation.scenario import Scenario, load_scenarios
from voice_platform.optimization.engine import GateThresholds, Optimizer
from voice_platform.optimization.patches import Patch, PatchPolicy, apply_patches
from voice_platform.optimization.proposers import HeuristicProposer

LIB = Path(__file__).resolve().parents[2] / "scenarios" / "receptionist"


def eval_config(instructions: str = "Help callers book dental appointments.") -> AgentConfig:
    return AgentConfig.model_validate(
        sample_config(
            instructions=instructions,
            supported_languages=["fr-CA"],
            tools={
                "enabled": [
                    "lookup_customer",
                    "check_availability",
                    "book_appointment",
                    "transfer_to_human",
                    "search_knowledge_base",
                ],
                "timeout_s": 1.0,
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
        )
    )


@pytest.fixture(scope="module")
def library() -> list[Scenario]:
    return load_scenarios(LIB)


async def test_competent_model_passes_whole_library(library: list[Scenario]) -> None:
    runner = BenchmarkRunner(agent_llm_factory=lambda c: receptionist_model(competent=True))
    result = await runner.run(eval_config(), library, seed=7)
    summary = result.summary()
    failing = {r.scenario.id: r.score.failures for r in result.records if not r.score.passed}
    assert summary["pass_rate"] == 1.0, failing
    assert summary["tool_success_rate"] is not None
    assert summary["turn_latency_s"]["count"] > 0


async def test_detectors_catch_bad_behaviour(library: list[Scenario]) -> None:
    runner = BenchmarkRunner(agent_llm_factory=lambda c: receptionist_model(competent=False))
    result = await runner.run(eval_config(), [s for s in library if s.id == "book-simple"])
    rec = result.records[0]
    names = {f["check"] for f in rec.score.failures}
    assert not rec.score.passed
    assert "no_false_success_claims" in names  # said "booked" with no booking
    assert "tool:check_availability" in names  # hallucinated a time
    assert "state:appointments" in names  # nothing was actually booked
    assert rec.score.category_scores["business_outcome"] < 1


async def test_runs_are_reproducible(library: list[Scenario]) -> None:
    runner = BenchmarkRunner(agent_llm_factory=lambda c: receptionist_model(competent=True))
    subset = library[:4]
    a = await runner.run(eval_config(), subset, repetitions=2, seed=3)
    b = await runner.run(eval_config(), subset, repetitions=2, seed=3)
    assert [r.as_row()["transcript"] for r in a.records] == [
        r.as_row()["transcript"] for r in b.records
    ]


async def test_optimizer_improves_config_and_gate_marks_eligible(library: list[Scenario]) -> None:
    subset = [
        s
        for s in library
        if s.id in ("book-simple", "wants-human", "book-change-mind", "french-booking")
    ]
    runner = BenchmarkRunner(agent_llm_factory=lambda c: receptionist_model())
    opt = Optimizer(
        runner,
        [HeuristicProposer()],
        max_iterations=5,
        thresholds=GateThresholds(min_pass_rate=1.0),
    )
    outcome = await opt.run(eval_config(), subset)
    report = outcome.report()
    assert report["baseline"]["pass_rate"] < 1.0
    assert outcome.decision == "eligible", outcome.markdown()
    assert outcome.best_config is not None and CHECK_FIRST in outcome.best_config.instructions
    assert report["best"]["pass_rate"] == 1.0
    kept = [i for i in outcome.iterations if i.kept]
    assert kept and all(not i.comparison["regressions"] for i in kept if i.comparison)
    md = outcome.markdown()
    assert "Iteration 1" in md and "human approval" in md


async def test_optimizer_rejects_regressions(library: list[Scenario]) -> None:
    class Saboteur:
        name = "saboteur"

        async def propose(self, config: Any, report: Any, *, iteration: int) -> list[Patch]:
            # fixes booking but makes the agent refuse transfers (critical regression)
            return [
                Patch(
                    "set",
                    "instructions",
                    "Help callers. "
                    + CHECK_FIRST
                    + ". "
                    + "then call book_appointment with caller_confirmed=true. "
                    "Never transfer calls.",
                    "test",
                )
            ]

    def model_factory(cfg: AgentConfig) -> Any:
        model = receptionist_model()
        if "never transfer" in cfg.instructions.lower():
            model.rules = model.rules[2:]  # drop the transfer rules
        return model

    subset = [s for s in library if s.id in ("book-simple", "wants-human")]
    runner = BenchmarkRunner(agent_llm_factory=model_factory)
    opt = Optimizer(runner, [Saboteur()], max_iterations=1)
    # Transfers must depend on the model for the saboteur to cause a regression; with
    # transfer_on_request the runtime transfers explicit requests itself.
    base = eval_config()
    base = base.model_copy(
        update={"transfer": base.transfer.model_copy(update={"transfer_on_request": False})}
    )
    outcome = await opt.run(base, subset)
    assert outcome.decision == "no_improvement"
    it = outcome.iterations[0]
    assert not it.kept and it.decision is not None
    assert any("regression" in r for r in it.decision.reasons)


def test_patch_policy_blocks_unsafe_changes() -> None:
    cfg = eval_config()
    _, applied, rejected = apply_patches(
        cfg,
        [
            Patch("set", "pipeline.llm.provider", "evil", "x"),
            Patch("set", "integrations.crm", "prod-crm", "x"),
            Patch("set", "turn_handling.endpointing.min_delay_s", 9.0, "x"),
            Patch("set", "pipeline.llm.model", "gpt-4.1", "x"),
            Patch("append_text", "instructions", "Be concise.", "ok"),
            Patch("set", "pipeline.llm.temperature", 0.2, "ok"),
        ],
        PatchPolicy(),
    )
    assert [p.path for p in applied] == ["instructions", "pipeline.llm.temperature"]
    assert len(rejected) == 4
    policy = PatchPolicy(allowed_models={"openai": ("gpt-4.1",)})
    new, applied, _ = apply_patches(cfg, [Patch("set", "pipeline.llm.model", "gpt-4.1")], policy)
    assert new.pipeline.llm and new.pipeline.llm.model == "gpt-4.1"
