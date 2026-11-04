"""A deterministic OpenAI-compatible chat endpoint for lab tests that need a specific model
behaviour (e.g. "the model calls transfer_to_human") without depending on a small model's
choices. Streams Server-Sent Events like the real API."""

from __future__ import annotations

import json
import uuid
from typing import Any

from aiohttp import web


def _chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
    body = {
        "id": "chatcmpl-lab",
        "object": "chat.completion.chunk",
        "model": "lab-scripted",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body)}\n\n".encode()


class FakeLLM:
    """Calls ``transfer_to_human`` when the caller asks for a person; otherwise short text."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.runner: web.AppRunner | None = None
        self.port = 0

    def decide(self, body: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
        msgs = body.get("messages", [])
        tools = {t["function"]["name"] for t in body.get("tools", [])}
        last = msgs[-1] if msgs else {}
        if last.get("role") == "tool":
            return "Okay.", None
        users = [m for m in msgs if m.get("role") == "user"]
        said = str(users[-1].get("content") if users else "").lower()
        if "transfer_to_human" in tools and ("person" in said or "someone" in said):
            return None, {
                "name": "transfer_to_human",
                "arguments": json.dumps(
                    {"reason": "caller asked for a person", "summary": "Caller wants a human."}
                ),
            }
        return "Sure, how can I help you?", None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(body)
        text, call = self.decide(body)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(_chunk({"role": "assistant", "content": ""}))
        if call is not None:
            tc = {"index": 0, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                  "function": call}  # fmt: skip
            await resp.write(_chunk({"tool_calls": [tc]}))
            await resp.write(_chunk({}, "tool_calls"))
        else:
            await resp.write(_chunk({"content": text}))
            await resp.write(_chunk({}, "stop"))
        await resp.write(b"data: [DONE]\n\n")
        return resp

    async def start(self, port: int) -> str:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", port)
        await site.start()
        self.port = port
        return f"http://127.0.0.1:{port}/v1"

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
