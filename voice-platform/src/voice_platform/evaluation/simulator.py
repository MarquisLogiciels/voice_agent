"""Runs a scenario against the real PlatformAgent in a sandbox (text mode).

Never touches production systems: integrations are the in-memory sandbox seeded from the
scenario, telephony is a simulated transport, persistence goes to memory.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from livekit.agents.llm import ChatContext

from ..config.schema import AgentConfig, PipelineMode
from ..config.service import ResolvedVersion
from ..integrations.registry import IntegrationSet
from ..integrations.sandbox import SandboxSuite
from ..knowledge.chunking import chunk_text
from ..knowledge.embeddings import HashingEmbedder
from ..providers.builder import BuiltPipeline, PipelineBuilder
from ..providers.registry import default_registry
from ..runtime.recorder import MemorySink
from ..runtime.session import CallInfo, RuntimeDeps, build_runtime_session
from ..shared.logging import get_logger
from ..telephony.sip import SipCallError
from ..telephony.transfer import TransferManager
from ..tools.interfaces import KnowledgeHit
from .scenario import Scenario

logger = get_logger(__name__)


@dataclass
class TurnRecord:
    user: str
    agent: str
    latency_s: float
    tool_calls: list[dict[str, Any]]


@dataclass
class SimulationResult:
    scenario: Scenario
    turns: list[TurnRecord]
    tool_events: list[dict[str, Any]]
    sandbox: SandboxSuite
    transfer_attempts: list[dict[str, Any]]
    transfer_ok: bool
    error: str | None = None
    llm_decisions: list[str] = field(default_factory=list)

    def transcript(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for t in self.turns:
            out.append({"role": "user", "text": t.user})
            if t.agent:
                out.append({"role": "assistant", "text": t.agent})
        return out


class SimulatedTransport:
    def __init__(self, outcomes: dict[str, str], destinations: dict[str, str]) -> None:
        self.outcomes = outcomes
        self.by_target = {v: k for k, v in destinations.items()}
        self.calls: list[dict[str, Any]] = []

    async def blind_transfer(self, *, to_uri: str, **kw: Any) -> None:
        target = to_uri.removeprefix("tel:")
        name = self.by_target.get(target, target)
        self.calls.append({"destination": name, "to": target})
        outcome = self.outcomes.get(name, "completed")
        if outcome != "completed":
            raise SipCallError(outcome, f"simulated {outcome}")

    async def dial(self, **kw: Any) -> Any:
        raise SipCallError("rejected", "simulations never dial out")

    async def hangup(self, **kw: Any) -> None:
        return None


class InMemoryKnowledge:
    """Scenario-scoped knowledge base using the offline hashing embedder."""

    def __init__(self, documents: dict[str, str]) -> None:
        self.chunks: list[tuple[str, str, list[float]]] = []
        self._embedder = HashingEmbedder()
        self._docs = documents

    async def build(self) -> None:
        texts: list[tuple[str, str]] = []
        for title, text in self._docs.items():
            for c in chunk_text(text, size=600, overlap=100):
                texts.append((title, c.content))
        vecs = await self._embedder.embed([t for _, t in texts])
        self.chunks = [(title, text, v) for (title, text), v in zip(texts, vecs, strict=True)]

    async def search(self, query: str, *, top_k: int) -> list[KnowledgeHit]:
        q = (await self._embedder.embed([query]))[0]
        scored = sorted(((sum(a * b for a, b in zip(q, v, strict=True)), title, text)
                         for title, text, v in self.chunks), reverse=True)[:top_k]
        return [KnowledgeHit(content=text, document_title=title, document_id=title,
                             chunk_id=f"{title}:{i}", score=max(0.0, s))
                for i, (s, title, text) in enumerate(scored)]


class CallerDriver:
    """Produces the next caller utterance (None = hang up)."""

    async def next(self, agent_reply: str | None, turn: int) -> str | None:
        raise NotImplementedError


class ScriptedCaller(CallerDriver):
    def __init__(self, scenario: Scenario) -> None:
        assert scenario.script is not None
        self.turns = scenario.script

    async def next(self, agent_reply: str | None, turn: int) -> str | None:
        return self.turns[turn].say if turn < len(self.turns) else None


class LLMCaller(CallerDriver):
    """Role-plays the caller with a *separate* LLM (never the agent's own instance)."""

    END = "[END_CALL]"

    def __init__(self, scenario: Scenario, llm: Any) -> None:
        assert scenario.simulated is not None
        self.scenario = scenario
        self.sim = scenario.simulated
        self.llm = llm
        self.history: list[tuple[str, str]] = []
        self._ended = False

    def _system(self) -> str:
        p = self.scenario.persona
        facts = "; ".join(f"{k}: {v}" for k, v in self.sim.knowledge.items()) or "none"
        return (
            f"You are role-playing a phone caller named {p.name}. {p.description}. Mood: {p.mood}."
            f" Speak {p.language}. {p.speaking_style} Your goal: {self.sim.goal}. Facts you know:"
            f" {facts}. Reply with ONE short spoken utterance only, no stage directions. When "
            f"your goal is achieved or clearly impossible, say goodbye and append {self.END}."
        )

    async def next(self, agent_reply: str | None, turn: int) -> str | None:
        if turn >= self.sim.max_turns:
            return None
        if agent_reply is not None:
            self.history.append(("agent", agent_reply))
        ctx = ChatContext()
        ctx.add_message(role="system", content=self._system())
        for who, text in self.history:
            # from the caller-LLM's perspective the agent is the "user"
            ctx.add_message(role="user" if who == "agent" else "assistant", content=text)
        if not self.history:
            ctx.add_message(role="user", content="(the phone is answered)")
        out: list[str] = []
        async with self.llm.chat(chat_ctx=ctx) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    out.append(chunk.delta.content)
        text = "".join(out).strip()
        ended = self.END in text
        text = text.replace(self.END, "").strip()
        if not text:
            return None
        self.history.append(("caller", text))
        self._ended = ended
        return text


def _parse_now(s: Scenario) -> datetime:
    if s.now:
        dt = datetime.fromisoformat(s.now)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return datetime.now(UTC)


async def run_scenario(
    scenario: Scenario,
    config: AgentConfig,
    *,
    agent_llm: Any,
    caller_llm: Any | None = None,
    turn_timeout_s: float = 45.0,
) -> SimulationResult:
    """Drive one conversation. ``agent_llm`` is the model under test (an ``llm.LLM``)."""
    cfg = config.model_copy(deep=True)
    # Bind every capability to the sandbox; nothing in a simulation reaches real systems.
    cfg.integrations.crm = "sandbox" if cfg.integrations.crm else None
    cfg.integrations.calendar = "sandbox" if cfg.integrations.calendar else None
    cfg.integrations.messaging = "sandbox" if cfg.integrations.messaging else None
    suite = SandboxSuite(scenario.sandbox.model_copy(deep=True), name="sandbox")
    now = _parse_now(scenario)
    knowledge = None
    if scenario.knowledge_documents:
        knowledge = InMemoryKnowledge(scenario.knowledge_documents)
        await knowledge.build()
    destinations = {d.name: d.phone_number or d.sip_uri or "" for d in cfg.transfer.destinations}
    transport = SimulatedTransport(dict(scenario.transfer_sim.outcomes), destinations)
    managers: list[TransferManager] = []

    def transfer_factory(session: Any, env: Any) -> TransferManager:
        tm = TransferManager(config=cfg.transfer, timezone=cfg.business.timezone,
                             transport=transport, room_name="sim", caller_identity="caller",
                             clock=lambda: now)
        managers.append(tm)
        return tm

    resolved = ResolvedVersion(tenant_id=uuid.uuid4(), agent_id=uuid.uuid4(),
                               version_id=uuid.uuid4(), version=0, environment="simulation",
                               config=cfg, is_canary=False)
    deps = RuntimeDeps(
        builder=PipelineBuilder(default_registry()),
        integrations=IntegrationSet(crm=suite if cfg.integrations.crm else None,
                                    calendar=suite if cfg.integrations.calendar else None,
                                    messaging=suite if cfg.integrations.messaging else None),
        sink=MemorySink(), knowledge=knowledge, transfer_factory=transfer_factory,
        clock=lambda: now, enable_policies=False,
        pipeline_override=BuiltPipeline(mode=PipelineMode.CASCADED, llm=agent_llm,
                                        turn_detection="manual"),
    )
    cfg.conversation.end_call_enabled = False
    rs = build_runtime_session(resolved, CallInfo(
        conversation_id=f"sim-{scenario.id}-{uuid.uuid4().hex[:6]}", direction="simulation",
        caller_number=scenario.caller_number), deps)
    driver: CallerDriver = (ScriptedCaller(scenario) if scenario.script is not None
                            else LLMCaller(scenario, caller_llm))
    turns: list[TurnRecord] = []
    error: str | None = None
    try:
        async with rs.session as session:
            await session.start(rs.agent)
            agent_reply: str | None = None
            for i in range(60):
                utterance = await driver.next(agent_reply, i)
                if utterance is None:
                    break
                started = time.perf_counter()
                n_tools = len(rs.recorder.tool_events)
                result = await asyncio.wait_for(session.run(user_input=utterance),
                                                timeout=turn_timeout_s)
                texts = [ev.item.text_content or "" for ev in result.events
                         if ev.type == "message" and ev.item.role == "assistant"]
                agent_reply = " ".join(t for t in texts if t).strip()
                turns.append(TurnRecord(user=utterance, agent=agent_reply,
                                        latency_s=time.perf_counter() - started,
                                        tool_calls=rs.recorder.tool_events[n_tools:]))
                if getattr(driver, "_ended", False):
                    break
                if managers and managers[0].completed is not None and managers[0].completed.ok:
                    break  # caller has been handed to a human
    except Exception as e:  # recorded as a failed simulation, not a crash of the benchmark
        logger.warning("simulation error", extra={"scenario": scenario.id}, exc_info=True)
        error = f"{type(e).__name__}: {e}"[:500]
    finally:
        await rs.close(status="completed")
    transfer = managers[0].completed if managers else None
    decisions = list(getattr(agent_llm, "decisions", []))
    return SimulationResult(
        scenario=scenario, turns=turns, tool_events=list(rs.recorder.tool_events),
        sandbox=suite, transfer_attempts=transport.calls,
        transfer_ok=bool(transfer and transfer.ok), error=error, llm_decisions=decisions)


def tool_calls_summary(result: SimulationResult) -> list[dict[str, Any]]:
    return [{"tool": e["tool"], "ok": e["ok"], "error": e.get("error"),
             "args": e.get("args"), "replayed": e.get("replayed", False)}
            for e in result.tool_events]


def dumps(result: SimulationResult) -> str:
    return json.dumps({"transcript": result.transcript(),
                       "tools": tool_calls_summary(result)}, default=str)


AgentLLMFactory = Callable[[AgentConfig], Any]
