"""CallRecorder: turns AgentSession events into a durable, analyzable conversation record.

Writes are queued and flushed in the background so database latency never sits on the audio
path. If the database is unavailable, events are buffered (bounded) and retried; on overflow
the oldest *non-transcript* events are dropped first.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa

from ..core.db import Database, DbScope
from ..core.models import Conversation, ConversationEvent
from ..observability import metrics as m
from ..observability.stats import summarize
from ..providers.cost import Usage, estimate_cost
from ..shared.logging import get_logger

logger = get_logger(__name__)


class ConversationSink(Protocol):
    async def write(self, events: list[dict[str, Any]]) -> None: ...
    async def finalize(self, fields: dict[str, Any]) -> None: ...


class MemorySink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.final: dict[str, Any] | None = None

    async def write(self, events: list[dict[str, Any]]) -> None:
        self.events.extend(events)

    async def finalize(self, fields: dict[str, Any]) -> None:
        self.final = fields


class PostgresSink:
    def __init__(self, db: Database, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
        self._db = db
        self._tenant = tenant_id
        self._conv = conversation_id

    async def write(self, events: list[dict[str, Any]]) -> None:
        async with self._db.session(DbScope.for_tenant(self._tenant)) as s:
            s.add_all(
                [
                    ConversationEvent(
                        tenant_id=self._tenant,
                        conversation_id=self._conv,
                        seq=e["seq"],
                        type=e["type"],
                        payload={k: v for k, v in e.items() if k not in ("seq", "type")},
                    )
                    for e in events
                ]
            )

    async def finalize(self, fields: dict[str, Any]) -> None:
        async with self._db.session(DbScope.for_tenant(self._tenant)) as s:
            await s.execute(
                sa.update(Conversation).where(Conversation.id == self._conv).values(**fields)
            )


class CallRecorder:
    def __init__(
        self,
        sink: ConversationSink,
        *,
        agent_label: str,
        providers: dict[str, list[str]],
        max_buffer: int = 5000,
    ) -> None:
        self.sink = sink
        self.agent_label = agent_label
        self.providers = providers
        self._queue: deque[dict[str, Any]] = deque()
        self._max_buffer = max_buffer
        self._seq = 0
        self._flusher: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._closed = False
        self.started_at = time.time()
        self.usage = Usage()
        self.transcript: list[dict[str, Any]] = []
        self.tool_events: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.interruptions = 0
        self._pending_eou: float | None = None
        self.latencies: dict[str, list[float]] = {
            "eou": [],
            "stt": [],
            "llm_ttft": [],
            "tts_ttfb": [],
            "e2e": [],
        }

    # --- wiring -----------------------------------------------------------------------------
    def attach(self, session: Any) -> None:
        session.on("conversation_item_added", self._on_item)
        session.on("session_usage_updated", self._on_usage)
        session.on("function_tools_executed", self._on_tools)
        session.on("agent_false_interruption", lambda ev: self.add({"type": "false_interruption"}))
        session.on("user_state_changed", self._on_user_state)
        session.on("error", self._on_error)
        self._flusher = asyncio.create_task(self._flush_loop())

    def add(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        self._seq += 1
        event = {"seq": self._seq, "ts": time.time(), **event}
        self._queue.append(event)
        if len(self._queue) > self._max_buffer:
            for i, e in enumerate(self._queue):
                if e["type"] not in ("transcript",):
                    del self._queue[i]
                    break
            else:
                self._queue.popleft()
        if len(self._queue) >= 25:
            self._wake.set()

    async def record_tool_event(self, event: dict[str, Any]) -> None:
        self.tool_events.append(event)
        self.add(event)

    # --- handlers ---------------------------------------------------------------------------
    def _on_item(self, ev: Any) -> None:
        item = ev.item
        if getattr(item, "type", None) != "message" or item.role not in ("user", "assistant"):
            return
        text = item.text_content or ""
        entry = {
            "role": item.role,
            "text": text,
            "interrupted": bool(item.interrupted),
            "ts": ev.created_at,
        }
        self.transcript.append(entry)
        if item.interrupted:
            self.interruptions += 1
        self.add({"type": "transcript", **entry})
        self._on_turn_metrics(item.role, dict(getattr(item, "metrics", None) or {}))

    def _on_tools(self, ev: Any) -> None:
        for call, out in ev.zipped():
            self.add(
                {
                    "type": "llm_tool_call",
                    "name": call.name,
                    "call_id": call.call_id,
                    "is_error": bool(out.is_error) if out is not None else None,
                }
            )

    def _on_user_state(self, ev: Any) -> None:
        if ev.new_state == "away":
            self.add({"type": "user_away"})

    def _on_error(self, ev: Any) -> None:
        err = ev.error
        info = {
            "type": "error",
            "source": type(ev.source).__name__ if ev.source else None,
            "recoverable": bool(getattr(err, "recoverable", False)),
            "error": str(getattr(err, "error", err))[:500],
        }
        self.errors.append(info)
        self.add(info)
        kind = getattr(err, "type", "unknown").split("_")[0]
        m.PROVIDER_ERRORS.labels(
            kind, getattr(err, "label", "unknown"), str(info["recoverable"]).lower()
        ).inc()

    def _label(self, kind: str) -> tuple[str, str]:
        keys = self.providers.get(kind) or [f"{kind}:unknown:-"]
        _, provider, model = [*keys[0].split(":"), "-", "-"][:3]
        return provider, model

    def _on_turn_metrics(self, role: str, mt: dict[str, Any]) -> None:
        """Per-turn latency from ``ChatMessage.metrics`` (LiveKit's per-turn report)."""
        if role == "user":
            if "end_of_turn_delay" in mt:
                self.latencies["eou"].append(mt["end_of_turn_delay"])
                m.EOU_DELAY.labels(self._label("stt")[0]).observe(mt["end_of_turn_delay"])
                self._pending_eou = mt["end_of_turn_delay"]
            if "transcription_delay" in mt:
                self.latencies["stt"].append(mt["transcription_delay"])
                m.STT_DELAY.labels(self._label("stt")[0]).observe(mt["transcription_delay"])
            return
        ttft = mt.get("llm_node_ttft")
        ttfb = mt.get("tts_node_ttfb")
        if ttft is not None:
            self.latencies["llm_ttft"].append(ttft)
            m.LLM_TTFT.labels(*self._label("llm")).observe(ttft)
        if ttfb is not None:
            self.latencies["tts_ttfb"].append(ttfb)
            m.TTS_TTFB.labels(*self._label("tts")).observe(ttfb)
        eou = self._pending_eou
        if eou is not None and ttft is not None and ttfb is not None:
            e2e = eou + ttft + ttfb
            self.latencies["e2e"].append(e2e)
            m.E2E_LATENCY.labels(self.agent_label).observe(e2e)
            self.add(
                {"type": "turn_latency", "eou": eou, "llm_ttft": ttft, "tts_ttfb": ttfb, "e2e": e2e}
            )
            self._pending_eou = None

    def _match_key(self, kind: str, provider: str, model: str) -> str:
        keys = self.providers.get(kind) or []
        p = (provider or "").lower()
        for k in keys:
            name = k.split(":")[1]
            if name and (name in p or p in name):
                return f"{kind}:{name}:{model or k.split(':')[2]}"
        return keys[0] if keys else f"{kind}:{p or 'unknown'}:{model or '-'}"

    def _on_usage(self, ev: Any) -> None:
        """``session_usage_updated`` carries cumulative per-model usage; keep the latest."""
        usage = Usage()
        for mu in getattr(ev.usage, "model_usage", []):
            t = getattr(mu, "type", "")
            if t == "llm_usage":
                usage.add_llm(
                    self._match_key("llm", mu.provider, mu.model), mu.input_tokens, mu.output_tokens
                )
            elif t == "tts_usage":
                usage.add_tts(self._match_key("tts", mu.provider, mu.model), mu.characters_count)
            elif t == "stt_usage":
                usage.add_stt(self._match_key("stt", mu.provider, mu.model), mu.audio_duration)
        self.usage = usage

    # --- flushing ---------------------------------------------------------------------------
    async def _flush_loop(self) -> None:
        backoff = 1.0
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            self._wake.clear()
            if not await self._flush_once():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15)
            else:
                backoff = 1.0
            if self._closed and not self._queue:
                return

    async def _flush_once(self) -> bool:
        if not self._queue:
            return True
        batch = list(self._queue)
        try:
            await self.sink.write(batch)
        except Exception:
            logger.warning(
                "conversation event flush failed; will retry",
                extra={"pending": len(batch)},
                exc_info=True,
            )
            return False
        for _ in batch:
            self._queue.popleft()
        return True

    def metrics_summary(self) -> dict[str, Any]:
        return {
            "latency": {k: summarize(v) for k, v in self.latencies.items()},
            "turns": sum(1 for t in self.transcript if t["role"] == "user"),
            "agent_turns": sum(1 for t in self.transcript if t["role"] == "assistant"),
            "interruptions": self.interruptions,
            "tool_calls": len(self.tool_events),
            "tool_failures": sum(1 for t in self.tool_events if not t.get("ok")),
            "errors": len(self.errors),
            "usage": self.usage.as_dict(),
        }

    async def close(self, *, final_fields: dict[str, Any]) -> dict[str, Any]:
        """Flush remaining events and write the final conversation fields."""
        self._closed = True
        self._wake.set()
        if self._flusher is not None:
            try:
                await asyncio.wait_for(self._flusher, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._flusher.cancel()
                logger.error(
                    "conversation events could not be fully flushed",
                    extra={"pending": len(self._queue)},
                )
        summary = self.metrics_summary()
        fields = {
            "metrics": summary,
            "cost": estimate_cost(self.usage),
            "ended_at": datetime.now(UTC),
            "duration_s": round(time.time() - self.started_at, 2),
            **final_fields,
        }
        for attempt in range(3):
            try:
                await self.sink.finalize(fields)
                break
            except Exception:
                logger.warning("finalize failed", extra={"attempt": attempt + 1}, exc_info=True)
                await asyncio.sleep(0.5 * (attempt + 1))
        return fields
