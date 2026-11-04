"""Builds a ready-to-start AgentSession + PlatformAgent for one call from a pinned version.

The same factory serves production calls (worker), simulations (evaluation) and the console.
Everything environment-specific (telephony, persistence, ledger, integrations) is injected.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from livekit.agents import AgentSession

from ..config.schema import AgentConfig, PipelineMode
from ..config.service import ResolvedVersion
from ..integrations.registry import IntegrationSet
from ..knowledge.types import KnowledgeSearch
from ..providers.builder import BuiltPipeline, PipelineBuilder
from ..providers.types import BuildContext
from ..shared.logging import get_logger
from ..tools.executor import ToolExecutor
from ..tools.interfaces import ToolEnv, TransferController
from ..tools.ledger import InMemoryLedger, ToolLedger
from .agent import PlatformAgent
from .policies import DurationPolicy, SilencePolicy
from .prompt import build_instructions
from .recorder import CallRecorder, ConversationSink, MemorySink

logger = get_logger(__name__)


@dataclass
class CallInfo:
    conversation_id: str
    direction: str = "inbound"  # inbound | outbound | web | simulation
    caller_number: str | None = None  # the customer's number (both directions)
    business_number: str | None = None
    room_name: str = ""
    caller_identity: str | None = None  # LiveKit participant identity of the customer
    caller_context: str | None = None  # e.g. purpose of an outbound call


@dataclass
class RuntimeDeps:
    builder: PipelineBuilder
    integrations: IntegrationSet
    build_ctx: BuildContext = field(default_factory=BuildContext)
    ledger: ToolLedger = field(default_factory=InMemoryLedger)
    sink: ConversationSink = field(default_factory=MemorySink)
    knowledge: KnowledgeSearch | None = None
    transfer_factory: Callable[[AgentSession[Any], ToolEnv], TransferController | None] | None = (
        None
    )
    end_call: Callable[[str], Awaitable[None]] | None = None
    end_call_tool_factory: Callable[[], Any] | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    pipeline_override: BuiltPipeline | None = None
    """Pre-built components (simulation with scripted/fake providers)."""
    enable_policies: bool = True


@dataclass
class RuntimeSession:
    session: AgentSession[Any]
    agent: PlatformAgent
    recorder: CallRecorder
    env: ToolEnv
    executor: ToolExecutor
    pipeline: BuiltPipeline
    silence: SilencePolicy | None
    duration: DurationPolicy | None
    end_reason: str | None = None

    def start_policies(self) -> None:
        if self.silence is not None:
            self.silence.attach()
        if self.duration is not None:
            self.duration.start()

    async def close(self, *, status: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.duration is not None:
            self.duration.cancel()
        transfer = self.env.state.transfer_result
        fields: dict[str, Any] = {
            "status": status,
            "end_reason": self.end_reason,
            "transfer": (
                {
                    "ok": transfer.ok,
                    "status": transfer.status,
                    "destination": transfer.destination,
                    "mode": transfer.mode,
                    "attempts": transfer.attempts,
                }
                if transfer
                else {}
            ),
            **(extra or {}),
        }
        try:
            return await self.recorder.close(final_fields=fields)
        finally:
            await self.env.integrations.aclose()


def turn_handling_options(cfg: AgentConfig, built: BuiltPipeline) -> dict[str, Any]:
    th = cfg.turn_handling
    return {
        "turn_detection": built.turn_detection,
        "endpointing": {
            "mode": th.endpointing.mode,
            "min_delay": th.endpointing.min_delay_s,
            "max_delay": th.endpointing.max_delay_s,
        },
        "interruption": {
            "enabled": th.interruption.enabled,
            "mode": th.interruption.mode,
            "min_duration": th.interruption.min_duration_s,
            "min_words": th.interruption.min_words,
            "false_interruption_timeout": th.interruption.false_interruption_timeout_s,
            "resume_false_interruption": th.interruption.resume_false_interruption,
        },
        "preemptive_generation": {"enabled": th.preemptive_generation},
    }


def build_runtime_session(
    resolved: ResolvedVersion, call: CallInfo, deps: RuntimeDeps
) -> RuntimeSession:
    cfg = resolved.config
    deps.build_ctx.default_language = cfg.language
    built = deps.pipeline_override or deps.builder.build(cfg.pipeline, deps.build_ctx)
    for w in built.warnings:
        logger.warning("pipeline warning", extra={"path": w.path, "detail": w.message})

    recorder = CallRecorder(
        deps.sink, agent_label=str(resolved.agent_id), providers=built.providers
    )

    env = ToolEnv(
        tenant_id=resolved.tenant_id,
        agent_id=resolved.agent_id,
        conversation_id=call.conversation_id,
        config=cfg,
        integrations=deps.integrations,
        ledger=deps.ledger,
        caller_number=call.caller_number,
        knowledge=deps.knowledge,
        clock=deps.clock,
        on_event=recorder.record_tool_event,
        sms_from_number=call.business_number,
    )

    session_kwargs: dict[str, Any] = {
        "llm": built.llm,
        "vad": built.vad,
        "turn_handling": turn_handling_options(cfg, built),
        "max_tool_steps": cfg.conversation.max_tool_steps,
        "user_away_timeout": cfg.conversation.silence.user_away_timeout_s,
        "ivr_detection": call.direction == "outbound" and cfg.conversation.ivr_detection_outbound,
    }
    if built.conn_options is not None:
        session_kwargs["conn_options"] = built.conn_options
    if cfg.pipeline.mode is PipelineMode.CASCADED:
        session_kwargs.update(stt=built.stt, tts=built.tts)
    elif built.tts is not None:
        session_kwargs["tts"] = built.tts
    session: AgentSession[Any] = AgentSession(
        **{k: v for k, v in session_kwargs.items() if v is not None}
    )

    if deps.transfer_factory is not None and cfg.transfer.enabled:
        env.transfer = deps.transfer_factory(session, env)
    executor = ToolExecutor.for_agent(env)

    extra_tools: list[Any] = []
    if cfg.conversation.end_call_enabled and deps.end_call_tool_factory is not None:
        extra_tools.append(deps.end_call_tool_factory())

    instructions = build_instructions(
        cfg, now=deps.clock(), caller_number=call.caller_number, caller_context=call.caller_context
    )
    agent = PlatformAgent(
        config=cfg,
        instructions=instructions,
        executor=executor,
        extra_tools=extra_tools,
        knowledge=deps.knowledge,
        announcements=[cfg.recording.consent_announcement]
        if cfg.recording.enabled and cfg.recording.consent_required
        else [],
    )
    recorder.attach(session)
    recorder.add(
        {
            "type": "lifecycle",
            "event": "session_built",
            "direction": call.direction,
            "agent_version": resolved.version,
            "canary": resolved.is_canary,
            "providers": built.providers,
        }
    )

    rs = RuntimeSession(
        session=session,
        agent=agent,
        recorder=recorder,
        env=env,
        executor=executor,
        pipeline=built,
        silence=None,
        duration=None,
    )

    async def end_call(reason: str) -> None:
        rs.end_reason = rs.end_reason or reason
        if deps.end_call is not None:
            await deps.end_call(reason)
        else:
            session.shutdown()

    if deps.enable_policies:
        rs.silence = SilencePolicy(session, cfg.conversation, end_call)
        rs.duration = DurationPolicy(session, cfg.conversation, end_call)
    return rs


def new_conversation_id() -> str:
    return str(uuid.uuid4())
