"""LLM-assisted scenario generation. Output is untrusted: every scenario is schema-validated,
forced to the sandbox, tagged ``generated`` and should be reviewed before it gates releases."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from ..config.schema import AgentConfig
from ..shared.logging import get_logger
from .scenario import Scenario

logger = get_logger(__name__)

DEFAULT_FOCUS = [
    "caller with incomplete information",
    "caller who changes their mind",
    "angry caller who wants a human",
    "caller asking an unrelated question",
    "caller speaking French (Québec)",
    "existing customer calling back",
]

_PROMPT = """You write test scenarios for an AI phone agent. Business: {business}.
Agent instructions: {instructions}
Enabled tools: {tools}. Transfer destinations: {destinations}.
Write {count} diverse scenarios focusing on: {focus}.
Each scenario is a JSON object with: id (kebab-case), name, tags (list), language ("en" or
"fr"), persona {{name, description, mood (neutral|friendly|angry|confused|hurried|anxious)}},
script (list of {{"say": "..."}} caller turns, 1-5 turns, realistic spoken language), and
expectations {{tools: [{{name, min_calls}}], forbidden_tools: [...], transfer: true|false|null,
agent_must_not_mention: [regex...]}}. Only use tools from the enabled list.
Respond with JSON only: {{"scenarios": [ ... ]}}"""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "generated"


async def generate_scenarios(
    llm: Any,
    config: AgentConfig,
    *,
    count: int = 5,
    focus: list[str] | None = None,
    prefix: str = "gen",
) -> tuple[list[Scenario], list[str]]:
    """Returns (valid scenarios, rejection reasons)."""
    from json_repair import repair_json
    from livekit.agents.llm import ChatContext

    prompt = _PROMPT.format(
        business=config.business.name,
        instructions=config.instructions[:3000],
        tools=", ".join(config.tools.enabled) or "none",
        destinations=", ".join(d.name for d in config.transfer.destinations) or "none",
        count=count,
        focus="; ".join(focus or DEFAULT_FOCUS),
    )
    ctx = ChatContext()
    ctx.add_message(role="user", content=prompt)
    out: list[str] = []
    async with llm.chat(chat_ctx=ctx) as stream:
        async for chunk in stream:
            if chunk.delta and chunk.delta.content:
                out.append(chunk.delta.content)
    try:
        data = json.loads(repair_json("".join(out)))
    except ValueError:
        return [], ["generator returned invalid JSON"]
    raw = data.get("scenarios", []) if isinstance(data, dict) else []
    valid: list[Scenario] = []
    rejected: list[str] = []
    enabled = set(config.tools.enabled)
    seen: set[str] = set()
    for item in raw[: count * 2]:
        if not isinstance(item, dict):
            continue
        item = {k: v for k, v in item.items() if k in Scenario.model_fields}
        item["id"] = f"{prefix}-{_slug(str(item.get('id') or item.get('name') or ''))}"[:100]
        item["source"] = "generated"
        item["tags"] = sorted({*(item.get("tags") or []), "generated"})
        item.pop("sandbox", None)  # generated scenarios never configure integrations/faults
        item.pop("simulated", None)
        try:
            sc = Scenario.model_validate(item)
        except ValidationError as e:
            rejected.append(f"{item.get('id')}: {e.errors()[0]['msg']}")
            continue
        unknown = {t.name for t in sc.expectations.tools} - enabled
        if unknown:
            rejected.append(f"{sc.id}: expects disabled tools {sorted(unknown)}")
            continue
        if sc.id in seen:
            continue
        seen.add(sc.id)
        valid.append(sc)
        if len(valid) >= count:
            break
    return valid, rejected
