"""ToolExecutor: the only path from an LLM tool call to an external side effect.

Order of guards: enabled → call budget → argument validation → capability binding →
confirmation → idempotency → timeout → structured outcome → ledger/audit → metrics.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from ..integrations.base import ErrorKind, IntegrationError
from ..observability.metrics import TOOL_CALLS, TOOL_LATENCY
from ..observability.tracing import span_attrs, tracer
from ..shared.crypto import stable_hash
from ..shared.logging import get_logger, redact_value
from .definitions import CATALOG, ToolRefusal, ToolSpec, _Args
from .interfaces import ToolEnv

logger = get_logger(__name__)

_LLM_NAME = re.compile(r"[^a-zA-Z0-9_\-]")


class ToolOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    message: str = ""
    replayed: bool = False

    def to_llm(self) -> str:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.data is not None:
            payload["data"] = self.data
        if self.error:
            payload["error"] = self.error
        if self.message:
            payload["message"] = self.message
        return json.dumps(payload, ensure_ascii=False, default=str)


@dataclass
class RegisteredTool:
    spec: ToolSpec
    llm_name: str
    description: str
    require_confirmation: bool
    custom_target: tuple[str, str] | None = None  # (integration, action) for custom REST
    custom_schema: dict[str, Any] | None = None

    def schema(self) -> dict[str, Any]:
        if self.custom_schema is not None:
            return {
                "name": self.llm_name,
                "description": self.description,
                "parameters": self.custom_schema,
            }
        return self.spec.llm_schema(
            self.description, require_confirmation=self.require_confirmation
        )


class _CustomArgs(_Args):
    model_config = ConfigDict(extra="allow")


async def _custom_handler(env: ToolEnv, a: Any) -> dict[str, Any]:  # pragma: no cover - replaced
    raise NotImplementedError


@dataclass
class ToolExecutor:
    env: ToolEnv
    tools: dict[str, RegisteredTool] = field(default_factory=dict)

    @classmethod
    def for_agent(cls, env: ToolEnv) -> ToolExecutor:
        cfg = env.config.tools
        tools: dict[str, RegisteredTool] = {}
        for name in cfg.enabled:
            if name == "end_call":
                continue  # provided by LiveKit's EndCallTool in the runtime
            if name.startswith("custom."):
                _, integration, action = name.split(".")
                adapter = env.integrations.custom.get(integration)
                if adapter is None or action not in adapter.actions:
                    logger.warning("custom tool not available", extra={"tool": name})
                    continue
                spec_action = adapter.actions[action]
                llm_name = _LLM_NAME.sub("_", f"{integration}__{action}")[:64]
                schema = _custom_schema(adapter.tool_schema(action), spec_action.consequential)
                tools[llm_name] = RegisteredTool(
                    spec=ToolSpec(
                        name,
                        spec_action.description,
                        _CustomArgs,
                        _custom_handler,
                        "custom",
                        consequential=spec_action.consequential,
                        dedupe=spec_action.consequential,
                    ),
                    llm_name=llm_name,
                    description=cfg.description_overrides.get(name, spec_action.description),
                    require_confirmation=spec_action.consequential,
                    custom_target=(integration, action),
                    custom_schema=schema,
                )
                continue
            spec = CATALOG[name]
            tools[name] = RegisteredTool(
                spec=spec,
                llm_name=name,
                description=cfg.description_overrides.get(name, spec.description),
                require_confirmation=name in cfg.require_confirmation,
            )
        return cls(env=env, tools=tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    async def execute(self, llm_name: str, raw_args: dict[str, Any]) -> ToolOutcome:
        started = time.perf_counter()
        tool = self.tools.get(llm_name)
        name = tool.spec.name if tool else llm_name
        with tracer.start_as_current_span(
            "vp.tool",
            attributes=span_attrs(
                tool=name,
                conversation_id=self.env.conversation_id,
                tenant_id=str(self.env.tenant_id),
            ),
        ) as span:
            outcome = await self._execute(tool, raw_args)
            span.set_attributes(
                span_attrs(ok=outcome.ok, error=outcome.error, replayed=outcome.replayed)
            )
        duration_ms = (time.perf_counter() - started) * 1000
        status = "ok" if outcome.ok else (outcome.error or "error")
        TOOL_CALLS.labels(name if tool else "unknown", "ok" if outcome.ok else "error").inc()
        TOOL_LATENCY.labels(name if tool else "unknown").observe(duration_ms / 1000)
        st = self.env.state
        if outcome.ok:
            st.consecutive_failures = 0
        elif outcome.error not in ("confirmation_required",):
            st.consecutive_failures += 1
            st.total_failures += 1
            tcfg = self.env.config.transfer
            if (
                tcfg.enabled
                and tcfg.transfer_on_failure
                and self.env.transfer is not None
                and st.consecutive_failures >= tcfg.max_failed_tool_calls_before_transfer
                and not st.transfer_attempted
                and name != "transfer_to_human"
            ):
                outcome.message += (
                    " Several system operations have failed. Apologize and offer "
                    "to transfer the caller to a team member."
                )
        logger.info(
            "tool executed",
            extra={
                "tool": name,
                "status": status,
                "duration_ms": round(duration_ms, 1),
                "replayed": outcome.replayed,
            },
        )
        if self.env.on_event is not None:
            event = {
                "type": "tool",
                "tool": name,
                "ok": outcome.ok,
                "error": outcome.error,
                "replayed": outcome.replayed,
                "duration_ms": round(duration_ms, 1),
                "args": redact_value(raw_args),
                "data": outcome.data,
            }
            res = self.env.on_event(event)
            if inspect.isawaitable(res):
                await res
        return outcome

    async def _execute(self, tool: RegisteredTool | None, raw_args: dict[str, Any]) -> ToolOutcome:
        env = self.env
        if tool is None:
            return ToolOutcome(
                ok=False, error="not_permitted", message="This tool is not available to you."
            )
        st = env.state
        if st.tool_calls >= env.config.tools.max_calls_per_conversation:
            return ToolOutcome(
                ok=False,
                error="limit_reached",
                message="Tool call limit for this conversation reached.",
            )
        st.tool_calls += 1
        spec = tool.spec

        # 1. validate
        if tool.custom_target is None:
            args_model: type[BaseModel] = spec.args
        else:
            adapter = env.integrations.custom[tool.custom_target[0]]
            args_model = adapter.input_models[tool.custom_target[1]]
        payload = dict(raw_args or {})
        confirmed = bool(payload.pop("caller_confirmed", False)) if tool.custom_target else None
        try:
            args = args_model.model_validate(payload)
        except ValidationError as e:
            fields = ", ".join(
                ".".join(str(p) for p in err["loc"]) + f": {err['msg']}" for err in e.errors()[:5]
            )
            return ToolOutcome(
                ok=False,
                error="invalid_arguments",
                message=f"Invalid arguments ({fields}). Ask the caller for the "
                "missing or corrected information.",
            )

        # 2. confirmation
        if tool.require_confirmation:
            is_confirmed = (
                confirmed if confirmed is not None else getattr(args, "caller_confirmed", False)
            )
            if not is_confirmed:
                return ToolOutcome(
                    ok=False,
                    error="confirmation_required",
                    message="Before doing this, read the details back to the "
                    "caller and ask them to confirm. Then call again with "
                    "caller_confirmed=true.",
                )

        # 3. idempotency
        canonical = args.model_dump(
            mode="json",
            exclude={"caller_confirmed"} if hasattr(args, "caller_confirmed") else set(),
        )
        key = None
        if spec.dedupe:
            key = "tool:" + stable_hash(
                env.conversation_id, spec.name, json.dumps(canonical, sort_keys=True, default=str)
            )
            try:
                existing = await env.ledger.begin(key, spec.name, canonical)
            except Exception:
                # Without a ledger entry we cannot guarantee idempotency: refuse the side effect.
                logger.exception("tool ledger unavailable", extra={"tool": spec.name})
                return ToolOutcome(
                    ok=False,
                    error="unavailable",
                    message="The system is temporarily unable to complete this. Do not claim "
                    "success; offer to take a message or transfer.",
                )
            if existing is not None:
                if existing.status == "succeeded":
                    replay = ToolOutcome.model_validate(existing.response)
                    replay.replayed = True
                    replay.message = (
                        replay.message + " (This was already done earlier in the "
                        "call; do not repeat it to the caller as new.)"
                    ).strip()
                    return replay
                return ToolOutcome(
                    ok=False, error="in_progress", message="This operation is already in progress."
                )

        # 4. run with timeout
        started = time.perf_counter()
        try:
            coro: Awaitable[dict[str, Any]]
            if tool.custom_target is not None:
                adapter = env.integrations.custom[tool.custom_target[0]]
                coro = adapter.execute(tool.custom_target[1], canonical)
            else:
                coro = spec.handler(env, args)
            data = await asyncio.wait_for(coro, timeout=env.config.tools.timeout_s)
            outcome = ToolOutcome(ok=True, data=data, message=_success_hint(spec))
        except ToolRefusal as e:
            outcome = ToolOutcome(ok=False, error=e.code, message=e.message)
        except IntegrationError as e:
            outcome = ToolOutcome(ok=False, error=e.kind.value, message=_failure_hint(e))
        except TimeoutError:
            outcome = ToolOutcome(
                ok=False,
                error="timeout",
                message="The business system did not respond in time. Do not "
                "tell the caller it succeeded. Offer to try again or take a "
                "message.",
            )
        except Exception:
            logger.exception("tool handler crashed", extra={"tool": spec.name})
            outcome = ToolOutcome(
                ok=False,
                error="internal_error",
                message="An internal error occurred. Do not claim success.",
            )

        if key is not None:
            await env.ledger.finish(
                key,
                status="succeeded" if outcome.ok else "failed",
                response=outcome.model_dump(mode="json"),
                error_kind=outcome.error,
                error_message=outcome.message if not outcome.ok else None,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        return outcome


def _success_hint(spec: ToolSpec) -> str:
    if spec.consequential:
        return "Done — the business system confirmed this. You may tell the caller it is done."
    return ""


def _failure_hint(e: IntegrationError) -> str:
    base = e.safe_message()
    if e.ambiguous:
        return (
            base + " It is unclear whether the change was saved. Do not tell the caller it "
            "succeeded; say a team member will confirm."
        )
    if e.kind is ErrorKind.CONFLICT:
        return base + " Offer alternatives."
    if e.retryable:
        return base + " You may try once more; if it fails again, offer to take a message."
    return base + " Do not claim success. Offer to take a message or transfer to staff."


def _custom_schema(schema: dict[str, Any], consequential: bool) -> dict[str, Any]:
    from .definitions import _simplify

    out = _simplify(schema)
    if consequential:
        out["properties"]["caller_confirmed"] = {
            "type": "boolean",
            "description": "True only after the caller explicitly confirmed this action.",
        }
    return out
