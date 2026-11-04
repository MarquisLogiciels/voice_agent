"""Tracing setup for processes (apps only): OTLP exporter + LiveKit telemetry hook.

By default conversation content (PII) is stripped from spans before export
(``VP_OTEL_ALLOW_PII=false``); only timings, ids and outcomes leave the process.
"""

from __future__ import annotations

import os

from opentelemetry import trace

from ..shared.logging import get_logger

logger = get_logger(__name__)
_configured = False


def configure_tracing(
    *, service: str, endpoint: str | None, environment: str, allow_pii: bool | None = None
) -> None:
    global _configured
    if _configured or not endpoint:
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create({"service.name": service, "deployment.environment": environment})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    try:
        from livekit.agents.telemetry import set_tracer_provider

        if allow_pii is None:
            allow_pii = os.environ.get("VP_OTEL_ALLOW_PII", "false").lower() == "true"
        set_tracer_provider(provider, metadata={"service": service}, allow_pii=allow_pii)
    except Exception:
        logger.warning("could not attach LiveKit telemetry to the tracer provider", exc_info=True)
    _configured = True
    logger.info("tracing configured", extra={"otlp_endpoint": endpoint})
