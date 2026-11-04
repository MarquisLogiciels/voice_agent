"""Deterministic LLM double for reproducible simulations and offline tests.

``ScriptedLLM`` implements LiveKit's ``llm.LLM`` and decides each reply with ordered rules
evaluated against the chat context (latest user message or latest tool result). It is used to
test the platform machinery (runtime, tools, evaluation, optimization) without network access.
It is **not** a quality benchmark of any real model.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, llm
from livekit.agents.llm import (
    ChatChunk,
    ChatContext,
    ChatMessage,
    ChoiceDelta,
    FunctionCallOutput,
    FunctionToolCall,
)

_ids = itertools.count(1)


@dataclass
class Turn:
    """What the scripted model sees for the current decision."""

    user_text: str | None
    tool_name: str | None
    tool_output: dict[str, Any] | None
    instructions: str
    history: list[Any]
    tools: list[str]

    def instruction_has(self, needle: str) -> bool:
        return needle.lower() in self.instructions.lower()


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


Decide = Callable[[Turn], Reply | None]


@dataclass
class Rule:
    decide: Decide
    name: str = ""


def when_user(pattern: str, reply: Callable[[re.Match[str], Turn], Reply] | Reply) -> Rule:
    rx = re.compile(pattern, re.I)

    def decide(t: Turn) -> Reply | None:
        if t.user_text is None or t.tool_name is not None:
            return None
        m = rx.search(t.user_text)
        if not m:
            return None
        return reply(m, t) if callable(reply) else reply

    return Rule(decide, f"user:{pattern}")


def after_tool(tool: str, reply: Callable[[dict[str, Any], Turn], Reply] | Reply) -> Rule:
    def decide(t: Turn) -> Reply | None:
        if t.tool_name != tool or t.tool_output is None:
            return None
        return reply(t.tool_output, t) if callable(reply) else reply

    return Rule(decide, f"tool:{tool}")


class ScriptedLLM(llm.LLM[Any]):
    def __init__(
        self,
        rules: list[Rule],
        *,
        fallback: str = "Could you say that again?",
        ttft_s: float = 0.0,
        model: str = "scripted",
    ) -> None:
        super().__init__()
        self.rules = rules
        self.fallback = fallback
        self.ttft_s = ttft_s
        self._model = model
        self.decisions: list[str] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "scripted"

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools: list[Any] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **_: Any,
    ) -> llm.LLMStream:
        return _ScriptedStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )

    def decide(self, chat_ctx: ChatContext, tool_names: list[str]) -> Reply:
        items = chat_ctx.items
        instructions = "\n".join(
            i.text_content or ""
            for i in items
            if isinstance(i, ChatMessage) and i.role in ("system", "developer")
        )
        last = items[-1] if items else None
        user_text = tool_name = None
        tool_output: dict[str, Any] | None = None
        if isinstance(last, FunctionCallOutput):
            tool_name = last.name
            try:
                tool_output = json.loads(last.output)
            except (TypeError, ValueError):
                tool_output = {"raw": last.output}
        else:
            for it in reversed(items):
                if isinstance(it, ChatMessage) and it.role == "user":
                    user_text = it.text_content or ""
                    break
        turn = Turn(
            user_text=user_text,
            tool_name=tool_name,
            tool_output=tool_output,
            instructions=instructions,
            history=list(items),
            tools=tool_names,
        )
        for rule in self.rules:
            reply = rule.decide(turn)
            if reply is not None:
                self.decisions.append(rule.name)
                return reply
        self.decisions.append("fallback")
        return Reply(text=self.fallback)


class _ScriptedStream(llm.LLMStream):
    def __init__(self, scripted: ScriptedLLM, **kw: Any) -> None:
        super().__init__(scripted, **kw)
        self._scripted = scripted

    async def _run(self) -> None:
        names = [getattr(getattr(t, "info", None), "name", None) for t in self._tools]
        reply = self._scripted.decide(self.chat_ctx, [n for n in names if n])
        if self._scripted.ttft_s:
            await asyncio.sleep(self._scripted.ttft_s)
        rid = f"scripted-{next(_ids)}"
        if reply.text:
            for i in range(0, len(reply.text), 24):
                self._event_ch.send_nowait(
                    ChatChunk(
                        id=rid, delta=ChoiceDelta(role="assistant", content=reply.text[i : i + 24])
                    )
                )
        if reply.tool_calls:
            calls = [
                FunctionToolCall(name=n, arguments=json.dumps(a), call_id=f"call_{next(_ids)}")
                for n, a in reply.tool_calls
            ]
            self._event_ch.send_nowait(
                ChatChunk(id=rid, delta=ChoiceDelta(role="assistant", tool_calls=calls))
            )
