"""Evaluation scenario schema (YAML/JSON), loadable from files or the database."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..integrations.sandbox import SandboxState


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Persona(_M):
    name: str = "Caller"
    description: str = "A typical customer"
    mood: Literal["neutral", "friendly", "angry", "confused", "hurried", "anxious"] = "neutral"
    language: str = "en"
    speaking_style: str = ""


class ScriptTurn(_M):
    say: str
    expect_agent: list[str] = Field(default_factory=list)
    """Regexes; at least one must match the agent's reply to this turn."""


class SimulatedCaller(_M):
    goal: str
    max_turns: int = Field(default=10, ge=1, le=40)
    knowledge: dict[str, str] = Field(default_factory=dict)
    """Facts the simulated caller knows (name, email, preferred day...)."""


class ToolExpectation(_M):
    name: str
    args_subset: dict[str, Any] = Field(default_factory=dict)
    min_calls: int = Field(default=1, ge=0)
    max_calls: int | None = Field(default=None, ge=0)
    must_succeed: bool | None = True


class StateExpectation(_M):
    appointments: int | None = None
    appointment_attendee_contains: str | None = None
    leads: int | None = None
    lead_title_contains: str | None = None
    contacts: int | None = None
    sms: int | None = None


class Expectations(_M):
    tools: list[ToolExpectation] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    transfer: bool | None = None
    transfer_destination: str | None = None
    state: StateExpectation = Field(default_factory=StateExpectation)
    agent_must_mention: list[str] = Field(default_factory=list)
    agent_must_not_mention: list[str] = Field(default_factory=list)
    max_turns: int | None = None
    max_mean_turn_latency_s: float | None = None
    no_false_success_claims: bool = True
    no_repetition: bool = True


class TransferSim(_M):
    """How the simulated telephony layer responds to transfer attempts, per destination."""

    outcomes: dict[str, Literal["completed", "busy", "no_answer", "rejected", "unavailable"]] = (
        Field(default_factory=dict)
    )


class Scenario(_M):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{1,99}$")
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    critical: bool = False
    weight: float = Field(default=1.0, gt=0, le=10)
    language: str = "en"
    caller_number: str | None = "+15145550199"
    persona: Persona = Field(default_factory=Persona)
    script: list[ScriptTurn] | None = None
    simulated: SimulatedCaller | None = None
    sandbox: SandboxState = Field(default_factory=SandboxState)
    transfer_sim: TransferSim = Field(default_factory=TransferSim)
    knowledge_documents: dict[str, str] = Field(default_factory=dict)
    """title → text: loaded into an in-memory knowledge base for this scenario."""
    expectations: Expectations = Field(default_factory=Expectations)
    judge_rubric: list[str] = Field(default_factory=list)
    now: str | None = Field(default=None, description="ISO datetime the simulation runs at")
    source: Literal["library", "manual", "generated"] = "manual"

    @model_validator(mode="after")
    def _one_driver(self) -> Scenario:
        if (self.script is None) == (self.simulated is None):
            raise ValueError("a scenario needs exactly one of 'script' or 'simulated'")
        return self


def load_scenarios(path: str | Path) -> list[Scenario]:
    p = Path(path)
    files = sorted(p.glob("*.yaml")) + sorted(p.glob("*.yml")) if p.is_dir() else [p]
    out: list[Scenario] = []
    for f in files:
        data = yaml.safe_load(f.read_text())
        items = data if isinstance(data, list) else data.get("scenarios", [data])
        for item in items:
            out.append(Scenario.model_validate({"source": "library", **item}))
    ids = [s.id for s in out]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate scenario ids")
    return out
