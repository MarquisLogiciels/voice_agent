"""Builds LiveKit pipeline components from a validated :class:`PipelineConfig`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config.schema import (
    PipelineConfig,
    PipelineMode,
    ProviderRef,
    ProviderSpec,
    TurnDetectionMode,
)
from ..shared.logging import get_logger
from .health import ProviderHealthTracker, global_tracker
from .registry import Problem, ProviderError, ProviderRegistry, provider_key
from .types import BuildContext, ProviderKind

logger = get_logger(__name__)


@dataclass
class BuiltPipeline:
    mode: PipelineMode
    stt: Any | None = None
    llm: Any | None = None
    tts: Any | None = None
    vad: Any | None = None
    turn_detection: Any | None = None
    conn_options: Any | None = None
    providers: dict[str, list[str]] = field(default_factory=dict)
    """kind → provider keys in effective fallback order (for metrics/cost attribution)."""
    warnings: list[Problem] = field(default_factory=list)


class PipelineBuilder:
    def __init__(
        self, registry: ProviderRegistry, *, tracker: ProviderHealthTracker | None = None
    ) -> None:
        self.registry = registry
        self.tracker = tracker or global_tracker()

    # ------------------------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------------------------
    def validate(
        self,
        pipeline: PipelineConfig,
        ctx: BuildContext | None = None,
        *,
        check_secrets: bool = True,
    ) -> list[Problem]:
        problems: list[Problem] = []
        reg = self.registry

        def chain(kind: ProviderKind, spec: ProviderSpec | None, path: str) -> None:
            if spec is None:
                return
            for i, ref in enumerate(spec.chain()):
                p = path if i == 0 else f"{path}.fallbacks[{i - 1}]"
                problems.extend(reg.validate_ref(kind, ref, p, ctx, check_secrets=check_secrets))

        if pipeline.vad is not None:
            problems.extend(
                reg.validate_ref(
                    ProviderKind.VAD, pipeline.vad, "pipeline.vad", ctx, check_secrets=check_secrets
                )
            )
        if pipeline.mode is PipelineMode.CASCADED:
            chain(ProviderKind.STT, pipeline.stt, "pipeline.stt")
            chain(ProviderKind.LLM, pipeline.llm, "pipeline.llm")
            chain(ProviderKind.TTS, pipeline.tts, "pipeline.tts")
            assert pipeline.stt is not None
            for i, ref in enumerate(pipeline.stt.chain()):
                if not reg.has(ProviderKind.STT, ref.provider):
                    continue
                caps = reg.get(ProviderKind.STT, ref.provider).capabilities
                path = "pipeline.stt" if i == 0 else f"pipeline.stt.fallbacks[{i - 1}]"
                if not caps.streaming and pipeline.vad is None:
                    problems.append(
                        Problem(
                            path,
                            f"{ref.provider} STT is not streaming and needs a VAD (pipeline.vad)",
                        )
                    )
                if (
                    pipeline.turn_detection.mode is TurnDetectionMode.STT
                    and not caps.emits_end_of_speech
                ):
                    problems.append(
                        Problem(
                            path,
                            f"{ref.provider} STT does not emit end-of-speech;"
                            " turn_detection mode 'stt' will not work",
                        )
                    )
        else:
            chain(ProviderKind.REALTIME, pipeline.realtime, "pipeline.realtime")
            chain(ProviderKind.TTS, pipeline.realtime_tts, "pipeline.realtime_tts")

        td = pipeline.turn_detection
        if td.mode is TurnDetectionMode.MODEL:
            if pipeline.vad is None:
                problems.append(
                    Problem("pipeline.turn_detection", "model-based turn detection requires a VAD")
                )
            ref = ProviderRef(provider=td.provider or "livekit", model=td.model, options=td.options)
            problems.extend(
                reg.validate_ref(
                    ProviderKind.TURN_DETECTION,
                    ref,
                    "pipeline.turn_detection",
                    ctx,
                    check_secrets=check_secrets,
                )
            )
        return problems

    # ------------------------------------------------------------------------------------
    # build
    # ------------------------------------------------------------------------------------
    def build(self, pipeline: PipelineConfig, ctx: BuildContext) -> BuiltPipeline:
        problems = self.validate(pipeline, ctx)
        errors = [p for p in problems if p.severity == "error"]
        # Missing secrets on *fallback* entries are tolerated (entry skipped with a warning);
        # anything else is fatal.
        fatal = [p for p in errors if ".fallbacks[" not in p.path]
        if fatal:
            raise ProviderError("; ".join(f"{p.path}: {p.message}" for p in fatal))
        out = BuiltPipeline(mode=pipeline.mode, warnings=[p for p in problems if p not in fatal])

        vad = ctx.vad
        if vad is None and pipeline.vad is not None:
            vad = self.registry.create(ProviderKind.VAD, pipeline.vad, ctx)
        out.vad = vad
        ctx.vad = vad

        if pipeline.mode is PipelineMode.CASCADED:
            assert pipeline.stt and pipeline.llm and pipeline.tts
            out.stt = self._build_chain(ProviderKind.STT, pipeline.stt, ctx, out)
            out.llm = self._build_chain(ProviderKind.LLM, pipeline.llm, ctx, out)
            out.tts = self._build_chain(ProviderKind.TTS, pipeline.tts, ctx, out)
        else:
            assert pipeline.realtime is not None
            out.llm = self._build_chain(ProviderKind.REALTIME, pipeline.realtime, ctx, out)
            if pipeline.realtime_tts is not None:
                out.tts = self._build_chain(ProviderKind.TTS, pipeline.realtime_tts, ctx, out)

        out.turn_detection = self._turn_detection(pipeline, ctx)
        out.conn_options = self.session_conn_options(pipeline)
        return out

    def _turn_detection(self, pipeline: PipelineConfig, ctx: BuildContext) -> Any:
        td = pipeline.turn_detection
        if td.mode is TurnDetectionMode.MODEL:
            ref = ProviderRef(provider=td.provider or "livekit", model=td.model, options=td.options)
            return self.registry.create(ProviderKind.TURN_DETECTION, ref, ctx)
        return td.mode.value  # "vad" | "stt" | "realtime_llm" | "manual"

    def session_conn_options(self, pipeline: PipelineConfig) -> Any:
        """LiveKit SessionConnectOptions from the config's per-provider ConnectionPolicy."""
        from livekit.agents import APIConnectOptions, SessionConnectOptions

        def opts(spec: ProviderSpec | None) -> Any:
            if spec is None:
                return APIConnectOptions()
            c = spec.connection
            return APIConnectOptions(
                max_retry=c.max_retries, retry_interval=c.retry_interval_s, timeout=c.timeout_s
            )

        llm_spec = pipeline.llm if pipeline.mode is PipelineMode.CASCADED else pipeline.realtime
        return SessionConnectOptions(
            stt_conn_options=opts(pipeline.stt),
            llm_conn_options=opts(llm_spec),
            tts_conn_options=opts(pipeline.tts or pipeline.realtime_tts),
        )

    def _build_chain(
        self, kind: ProviderKind, spec: ProviderSpec, ctx: BuildContext, out: BuiltPipeline
    ) -> Any:
        ordered = self.tracker.order(kind.value, spec.chain())
        instances: list[Any] = []
        keys: list[str] = []
        for ref in ordered:
            key = provider_key(kind, ref)
            try:
                inst = self.registry.create(kind, ref, ctx)
            except (ProviderError, ValueError) as e:
                if ref is ordered[0] and len(ordered) == 1:
                    raise ProviderError(f"{kind.value}: {e}") from e
                out.warnings.append(
                    Problem(f"pipeline.{kind.value}", f"skipped {key}: {e}", "warning")
                )
                continue
            if kind is ProviderKind.STT and not inst.capabilities.streaming:
                from livekit.agents import stt as lk_stt

                if ctx.vad is None:
                    raise ProviderError(f"{key} is not streaming and no VAD is configured")
                inst = lk_stt.StreamAdapter(stt=inst, vad=ctx.vad)
            self._attach_health(inst, key)
            instances.append(inst)
            keys.append(key)
        if not instances:
            raise ProviderError(f"no usable {kind.value} provider in the configured chain")
        out.providers[kind.value] = keys
        if len(instances) == 1:
            return instances[0]
        return self._fallback(kind, instances, keys, spec, ctx)

    def _fallback(
        self,
        kind: ProviderKind,
        instances: list[Any],
        keys: list[str],
        spec: ProviderSpec,
        ctx: BuildContext,
    ) -> Any:
        c = spec.connection
        if kind is ProviderKind.STT:
            from livekit.agents import stt as lk_stt

            adapter: Any = lk_stt.FallbackAdapter(
                instances,
                vad=ctx.vad,
                attempt_timeout=c.timeout_s,
                max_retry_per_stt=c.max_retries,
                retry_interval=max(c.retry_interval_s, 0.5),
            )
            event = "stt_availability_changed"
        elif kind is ProviderKind.LLM:
            from livekit.agents import llm as lk_llm

            adapter = lk_llm.FallbackAdapter(
                instances,
                attempt_timeout=c.timeout_s,
                max_retry_per_llm=c.max_retries,
                retry_interval=c.retry_interval_s,
            )
            event = "llm_availability_changed"
        elif kind is ProviderKind.TTS:
            from livekit.agents import tts as lk_tts

            adapter = lk_tts.FallbackAdapter(instances, max_retry_per_tts=c.max_retries)
            event = "tts_availability_changed"
        else:
            from livekit.agents import llm as lk_llm

            return lk_llm.RealtimeModelFallbackAdapter(instances)

        by_id = {id(inst): key for inst, key in zip(instances, keys, strict=True)}

        def on_availability(ev: Any) -> None:
            inst = getattr(ev, kind.value, None)
            key = by_id.get(id(inst))
            if key is None:
                return
            if ev.available:
                self.tracker.record_success(key)
            else:
                self.tracker.record_failure(key)
            logger.warning(
                "provider availability changed",
                extra={"provider_key": key, "available": ev.available},
            )

        adapter.on(event, on_availability)
        return adapter

    def _attach_health(self, inst: Any, key: str) -> None:
        on = getattr(inst, "on", None)
        if on is None:
            return

        def on_error(ev: Any) -> None:
            # Recoverable errors are being retried by LiveKit; only final failures count.
            if not getattr(ev, "recoverable", False):
                self.tracker.record_failure(key)

        try:
            on("error", on_error)
            on("metrics_collected", lambda _ev: self.tracker.record_success(key))
        except ValueError:
            # Some components (e.g. realtime models) declare a closed event set.
            pass
