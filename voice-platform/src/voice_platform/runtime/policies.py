"""Conversation policies layered on AgentSession events: silence and maximum duration."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from ..config.schema import ConversationConfig
from ..shared.logging import get_logger

logger = get_logger(__name__)

EndCall = Callable[[str], Awaitable[None]]


class SilencePolicy:
    """Reprompts when the caller goes silent (LiveKit ``user_state_changed → away``) and ends
    the call politely after ``max_reprompts``."""

    def __init__(self, session: Any, cfg: ConversationConfig, end_call: EndCall) -> None:
        self.session = session
        self.cfg = cfg.silence
        self.end_call = end_call
        self.reprompts = 0
        self._task: asyncio.Task[None] | None = None
        self.ended = False

    def attach(self) -> None:
        self.session.on("user_state_changed", self._on_state)

    def _on_state(self, ev: Any) -> None:
        if ev.new_state == "speaking":
            self.reprompts = 0
            return
        if ev.new_state != "away" or self.ended:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._handle_away())

    async def _handle_away(self) -> None:
        try:
            if self.reprompts < self.cfg.max_reprompts and self.cfg.reprompt_messages:
                msg = self.cfg.reprompt_messages[
                    min(self.reprompts, len(self.cfg.reprompt_messages) - 1)
                ]
                self.reprompts += 1
                self.session.say(msg, allow_interruptions=True)
                return
            self.ended = True
            handle = self.session.say(self.cfg.goodbye_message, allow_interruptions=False)
            with contextlib.suppress(Exception):
                await handle.wait_for_playout()
            await self.end_call("silence_timeout")
        except Exception:
            logger.exception("silence policy failed")


class DurationPolicy:
    def __init__(self, session: Any, cfg: ConversationConfig, end_call: EndCall) -> None:
        self.session = session
        self.cfg = cfg
        self.end_call = end_call
        self._task: asyncio.Task[None] | None = None
        self.fired = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await asyncio.sleep(self.cfg.max_duration_s)
            self.fired = True
            logger.info("max call duration reached")
            with contextlib.suppress(Exception):
                self.session.interrupt()
            handle = self.session.say(self.cfg.wrap_up_message, allow_interruptions=False)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(handle.wait_for_playout(), timeout=20)
            await self.end_call("max_duration")
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("duration policy failed")

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
