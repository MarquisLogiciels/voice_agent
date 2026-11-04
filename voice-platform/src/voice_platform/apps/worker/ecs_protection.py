"""ECS task scale-in protection while calls are in progress.

The worker drains gracefully on SIGTERM (waits for active calls, ``drain_timeout``), but
Fargate kills a container at most 120 s after SIGTERM — so a deployment or scale-in would cut
every call that lasts longer. ECS lets a task protect itself: protected tasks are not stopped
by service deployments or scale-in until protection is removed or expires. The worker turns
protection on while it has active jobs and off when idle.

Runs only when ``ECS_AGENT_URI`` is set (ECS/Fargate platform ≥ 1.4); the task role needs
``ecs:UpdateTaskProtection``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable
from typing import Any

import aiohttp

from ...shared.logging import get_logger

logger = get_logger(__name__)


class TaskProtection:
    def __init__(
        self,
        active_jobs: Callable[[], int],
        *,
        agent_uri: str | None = None,
        interval_s: float = 15.0,
        expires_minutes: int = 60,
        renew_every_s: float = 600.0,
    ) -> None:
        self.active_jobs = active_jobs
        self.agent_uri = (agent_uri or os.environ.get("ECS_AGENT_URI") or "").rstrip("/")
        self.interval_s = interval_s
        self.expires_minutes = expires_minutes
        self.renew_every_s = renew_every_s
        self.protected = False
        self._last_set = 0.0
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.agent_uri)

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.run())

    async def _set(self, session: aiohttp.ClientSession, on: bool) -> None:
        body: dict[str, Any] = {"ProtectionEnabled": on}
        if on:
            body["ExpiresInMinutes"] = self.expires_minutes
        async with session.put(f"{self.agent_uri}/task-protection/v1/state", json=body) as r:
            if r.status >= 300:
                raise RuntimeError(f"task protection update failed: {r.status} {await r.text()}")
        self.protected, self._last_set = on, time.monotonic()
        logger.info("ECS task protection updated", extra={"protected": on})

    async def tick(self, session: aiohttp.ClientSession) -> None:
        want = self.active_jobs() > 0
        stale = time.monotonic() - self._last_set > self.renew_every_s
        if want != self.protected or (want and stale):
            await self._set(session, want)

    async def run(self) -> None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            while True:
                try:
                    await self.tick(session)
                except Exception:
                    logger.warning("ECS task protection update failed", exc_info=True)
                await asyncio.sleep(self.interval_s)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
