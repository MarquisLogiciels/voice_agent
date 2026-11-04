"""Workers protect their ECS task from deployment/scale-in while calls are active (Fargate
kills containers 120 s after SIGTERM, which would cut longer calls)."""

from __future__ import annotations

from typing import Any

import aiohttp
from aiohttp import web

from voice_platform.apps.worker.ecs_protection import TaskProtection


async def test_protection_follows_active_calls() -> None:
    seen: list[dict[str, Any]] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append(await request.json())
        return web.json_response({"protection": {}})

    app = web.Application()
    app.router.add_put("/task-protection/v1/state", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    jobs = {"n": 0}
    p = TaskProtection(lambda: jobs["n"], agent_uri=f"http://127.0.0.1:{port}", renew_every_s=0)
    async with aiohttp.ClientSession() as s:
        await p.tick(s)  # idle, never protected: no call
        jobs["n"] = 2
        await p.tick(s)  # call started → protect
        await p.tick(s)  # still busy, renewal due (renew_every_s=0)
        jobs["n"] = 0
        await p.tick(s)  # idle → release
    await runner.cleanup()
    assert seen == [
        {"ProtectionEnabled": True, "ExpiresInMinutes": 60},
        {"ProtectionEnabled": True, "ExpiresInMinutes": 60},
        {"ProtectionEnabled": False},
    ]


def test_disabled_outside_ecs(monkeypatch: Any) -> None:
    monkeypatch.delenv("ECS_AGENT_URI", raising=False)
    assert not TaskProtection(lambda: 1).enabled
