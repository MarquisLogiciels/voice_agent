"""Bounded conversation context: summarize older turns, keep recent ones verbatim.

Compaction is planned on a snapshot, summarized asynchronously, then *applied to the current*
context by item id, so items appended while the summary was being produced are never lost.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..config.schema import ContextConfig
from ..shared.logging import get_logger

logger = get_logger(__name__)

_SUMMARY_PROMPT = (
    "Summarize the earlier part of this phone conversation for the agent's own memory. Keep: "
    "caller identity and contact details they gave, their requests, decisions, commitments, "
    "tool results (bookings, ids), and open questions. Be factual and under 150 words."
)


def _item_text(item: Any) -> str | None:
    kind = getattr(item, "type", None)
    if kind == "message" and item.role in ("user", "assistant"):
        text = item.text_content or ""
        return f"{item.role}: {text}" if text else None
    if kind == "function_call":
        return f"tool call {item.name}({item.arguments[:300]})"
    if kind == "function_call_output":
        return f"tool result: {str(item.output)[:300]}"
    return None


@dataclass
class CompactionPlan:
    drop_ids: set[str]
    transcript: str


class ContextCompactor:
    def __init__(self, cfg: ContextConfig, *, timeout_s: float = 4.0) -> None:
        self.cfg = cfg
        self.timeout_s = timeout_s
        self.summaries = 0

    def needs_compaction(self, chat_ctx: Any) -> bool:
        return len(chat_ctx.items) > self.cfg.summarize_when_items_exceed

    def plan(self, chat_ctx: Any) -> CompactionPlan | None:
        items = list(chat_ctx.items)
        if len(items) <= self.cfg.summarize_when_items_exceed:
            return None
        budget = self.cfg.keep_last_turns * 2
        split = 0
        seen = 0
        for idx in range(len(items) - 1, -1, -1):
            it = items[idx]
            if getattr(it, "type", None) == "message" and it.role in ("user", "assistant"):
                seen += 1
                if seen >= budget:
                    split = idx
                    break
        if split <= 0:
            return None
        # keep the tail from starting with orphaned tool items
        while split < len(items) and getattr(items[split], "type", None) in (
            "function_call",
            "function_call_output",
        ):
            split += 1
        older = [
            it
            for it in items[:split]
            if not (getattr(it, "type", None) == "message" and it.role in ("system", "developer"))
        ]
        if not older:
            return None
        transcript = "\n".join(t for t in (_item_text(i) for i in older) if t)
        return CompactionPlan(drop_ids={it.id for it in older}, transcript=transcript)

    async def summarize(self, plan: CompactionPlan, llm: Any | None) -> str | None:
        if llm is None or not plan.transcript:
            return None
        try:
            return await asyncio.wait_for(self._summarize(llm, plan.transcript), self.timeout_s)
        except Exception:
            logger.warning("context summarization failed; truncating instead", exc_info=True)
            return None

    def apply(self, current: Any, plan: CompactionPlan, summary: str | None) -> Any:
        from livekit.agents.llm import ChatContext, ChatMessage

        new_ctx = ChatContext()
        inserted = False
        for it in current.items:
            if it.id in plan.drop_ids:
                if not inserted and summary:
                    new_ctx.items.append(
                        ChatMessage(
                            role="assistant",
                            content=[f"[Summary of the earlier conversation] {summary}"],
                        )
                    )
                    inserted = True
                continue
            new_ctx.items.append(it)
        if summary:
            self.summaries += 1
        if len(new_ctx.items) > self.cfg.max_items:
            new_ctx.truncate(max_items=self.cfg.max_items)
        return new_ctx

    async def compact(self, chat_ctx: Any, llm: Any | None) -> Any:
        plan = self.plan(chat_ctx)
        if plan is None:
            return chat_ctx
        summary = await self.summarize(plan, llm)
        return self.apply(chat_ctx, plan, summary)

    async def _summarize(self, llm: Any, transcript: str) -> str:
        from livekit.agents.llm import ChatContext

        ctx = ChatContext()
        ctx.add_message(role="system", content=_SUMMARY_PROMPT)
        ctx.add_message(role="user", content=transcript[-12_000:])
        out: list[str] = []
        async with llm.chat(chat_ctx=ctx) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    out.append(chunk.delta.content)
        return "".join(out).strip()
