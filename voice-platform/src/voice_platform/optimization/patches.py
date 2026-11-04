"""Allowlisted configuration patches — the only changes the optimizer may make."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from ..config.schema import AgentConfig


@dataclass(frozen=True)
class PathRule:
    pattern: str
    kind: Literal["text", "number", "choice", "bool"]
    min: float | None = None
    max: float | None = None
    choices: tuple[Any, ...] = ()
    max_len: int = 4000


DEFAULT_RULES: tuple[PathRule, ...] = (
    PathRule(r"^instructions$", "text", max_len=20_000),
    PathRule(r"^persona\.(personality|speaking_style)$", "text", max_len=1000),
    PathRule(r"^greeting\.text$", "text", max_len=500),
    PathRule(r"^tools\.description_overrides\.[a-z_.]+$", "text", max_len=1500),
    PathRule(r"^pipeline\.llm\.temperature$", "number", 0.0, 1.0),
    PathRule(r"^turn_handling\.endpointing\.min_delay_s$", "number", 0.2, 1.5),
    PathRule(r"^turn_handling\.endpointing\.max_delay_s$", "number", 1.0, 6.0),
    PathRule(r"^turn_handling\.interruption\.min_duration_s$", "number", 0.2, 1.5),
    PathRule(r"^turn_handling\.interruption\.min_words$", "number", 0, 4),
    PathRule(r"^turn_handling\.preemptive_generation$", "bool"),
    PathRule(r"^knowledge\.top_k$", "number", 1, 10),
    PathRule(r"^knowledge\.mode$", "choice", choices=("tool", "auto")),
    PathRule(r"^knowledge\.retrieval$", "choice", choices=("hybrid", "vector", "keyword")),
    PathRule(r"^conversation\.max_tool_steps$", "number", 2, 8),
)


@dataclass(frozen=True)
class Patch:
    op: Literal["set", "append_text"]
    path: str
    value: Any
    rationale: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"op": self.op, "path": self.path, "value": self.value, "rationale": self.rationale}


class PatchRejected(Exception):
    pass


@dataclass
class PatchPolicy:
    rules: tuple[PathRule, ...] = DEFAULT_RULES
    allowed_models: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """provider → models the optimizer may switch the LLM to (empty = model changes off)."""

    def rule_for(self, path: str) -> PathRule | None:
        if path == "pipeline.llm.model" and self.allowed_models:
            return PathRule(path, "choice")
        return next((r for r in self.rules if re.match(r.pattern, path)), None)

    def check(self, patch: Patch, config: dict[str, Any]) -> None:
        rule = self.rule_for(patch.path)
        if rule is None:
            raise PatchRejected(f"path '{patch.path}' is not optimizable")
        v = patch.value
        if patch.op == "append_text":
            if rule.kind != "text" or not isinstance(v, str):
                raise PatchRejected("append_text only applies to text fields")
        if rule.kind == "text":
            if not isinstance(v, str) or len(v) > rule.max_len:
                raise PatchRejected(f"text too long or not a string for {patch.path}")
        elif rule.kind == "number":
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise PatchRejected(f"{patch.path} must be a number")
            if (rule.min is not None and v < rule.min) or (rule.max is not None and v > rule.max):
                raise PatchRejected(f"{patch.path}={v} outside [{rule.min}, {rule.max}]")
        elif rule.kind == "bool" and not isinstance(v, bool):
            raise PatchRejected(f"{patch.path} must be boolean")
        elif rule.kind == "choice":
            if patch.path == "pipeline.llm.model":
                provider = str((config.get("pipeline", {}).get("llm") or {}).get("provider"))
                if v not in self.allowed_models.get(provider, ()):
                    raise PatchRejected(f"model '{v}' not in the approved list for {provider}")
            elif v not in rule.choices:
                raise PatchRejected(f"{patch.path} must be one of {rule.choices}")


def _set(data: dict[str, Any], path: str, value: Any, *, append: bool) -> None:
    parts = path.split(".")
    if parts[:2] == ["tools", "description_overrides"]:
        parts = ["tools", "description_overrides", ".".join(parts[2:])]
    cur = data
    for p in parts[:-1]:
        nxt = cur.get(p)
        if nxt is None:
            nxt = {}
            cur[p] = nxt
        if not isinstance(nxt, dict):
            raise PatchRejected(f"cannot descend into {p}")
        cur = nxt
    leaf = parts[-1]
    if append:
        existing = cur.get(leaf) or ""
        if value.strip() in existing:
            raise PatchRejected("text already present")
        cur[leaf] = (existing.rstrip() + "\n" + value.strip()).strip()
    else:
        cur[leaf] = value


def apply_patches(
    config: AgentConfig, patches: list[Patch], policy: PatchPolicy
) -> tuple[AgentConfig, list[Patch], list[tuple[Patch, str]]]:
    """Apply valid patches; returns (new config, applied, rejected-with-reason)."""
    data = copy.deepcopy(config.model_dump(mode="json"))
    applied: list[Patch] = []
    rejected: list[tuple[Patch, str]] = []
    for p in patches:
        try:
            policy.check(p, data)
            trial = copy.deepcopy(data)
            _set(trial, p.path, p.value, append=p.op == "append_text")
            AgentConfig.model_validate(trial)  # each patch must keep the config valid
            data = trial
            applied.append(p)
        except (PatchRejected, ValidationError, ValueError) as e:
            rejected.append((p, str(e)[:300]))
    return AgentConfig.model_validate(data), applied, rejected
