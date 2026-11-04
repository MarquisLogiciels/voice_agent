"""Span helpers for platform code. Process setup lives in ``tracing_setup`` (apps only)."""

from __future__ import annotations

from typing import Any

from opentelemetry import trace

tracer = trace.get_tracer("voice_platform")


def span_attrs(**attrs: Any) -> dict[str, Any]:
    return {
        k: v for k, v in attrs.items() if v is not None and isinstance(v, str | int | float | bool)
    }
