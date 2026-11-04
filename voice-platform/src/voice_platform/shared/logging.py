"""Structured JSON logging with context enrichment and secret redaction.

Installed on the root logger so LiveKit's own loggers inherit the same formatting and
redaction. Context (tenant, agent, conversation, ...) is carried in a ``ContextVar`` so it
follows asyncio tasks without being passed explicitly.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_log_context: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "vp_log_context", default=_EMPTY
)

REDACTED = "[REDACTED]"

# Patterns that indicate a secret regardless of where it appears in a message.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|basic|token)?\s*[^\s,;\"']+"),
    re.compile(r"(?i)(bearer\s+)[a-z0-9\-._~+/]+=*"),
    re.compile(
        r"(?i)((?:api[_-]?key|apikey|secret|password|passwd|token|auth_token)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'&}]+"
    ),
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bvpk_[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"\bAC[0-9a-f]{32}\b"),  # Twilio account SID-like identifiers
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),  # JWTs
]

_SENSITIVE_KEYS = {
    "authorization",
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "client_secret",
    "private_key",
    "auth_token",
    "x-api-key",
    "cookie",
    "set-cookie",
}


class SecretRegistry:
    """Known secret values (decrypted credentials) that must never be logged verbatim."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()

    def register(self, *values: str | None) -> None:
        with self._lock:
            for v in values:
                if v and len(v) >= 6:
                    self._values.add(v)

    def snapshot(self) -> tuple[str, ...]:
        with self._lock:
            # longest first so overlapping secrets are fully masked
            return tuple(sorted(self._values, key=len, reverse=True))


secret_registry = SecretRegistry()


def redact_text(text: str) -> str:
    for value in secret_registry.snapshot():
        if value in text:
            text = text.replace(value, REDACTED)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 1:
            text = pattern.sub(lambda m: (m.group(1) or "") + REDACTED, text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def redact_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact secrets in log-bound structures."""
    if _depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if key.lower() in _SENSITIVE_KEYS:
                out[key] = REDACTED
            else:
                out[key] = redact_value(v, _depth=_depth + 1)
        return out
    if isinstance(value, list | tuple | set):
        return [redact_value(v, _depth=_depth + 1) for v in value]
    return value


def mask_phone(number: str | None) -> str | None:
    """Keep only the last 4 digits of a phone number for logs."""
    if not number:
        return number
    digits = re.sub(r"\D", "", number)
    if len(digits) <= 4:
        return "***"
    return "***" + digits[-4:]


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    current = dict(_log_context.get())
    current.update({k: v for k, v in fields.items() if v is not None})
    token = _log_context.set(current)
    try:
        yield
    finally:
        _log_context.reset(token)


def bind_log_context(**fields: Any) -> None:
    """Bind fields for the remainder of the current task (no automatic reset)."""
    current = dict(_log_context.get())
    current.update({k: v for k, v in fields.items() if v is not None})
    _log_context.set(current)


def current_log_context() -> Mapping[str, Any]:
    return _log_context.get()


_STD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed %-format — keep the raw message
            message = str(record.msg)
        record.msg = redact_text(message)
        record.args = None
        for key, value in list(record.__dict__.items()):
            if key in _STD_ATTRS:
                continue
            if key.lower() in _SENSITIVE_KEYS:
                record.__dict__[key] = REDACTED
            else:
                record.__dict__[key] = redact_value(value)
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "service": self._service,
        }
        payload.update(_log_context.get())
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


class _ContextTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ctx = _log_context.get()
        if ctx:
            base += " " + " ".join(f"{k}={v}" for k, v in ctx.items())
        return base


def configure_logging(*, level: str = "INFO", json_output: bool = True, service: str) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_vp_handler", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler._vp_handler = True  # type: ignore[attr-defined]
    handler.addFilter(RedactionFilter())
    if json_output:
        handler.setFormatter(JsonFormatter(service))
    else:
        handler.setFormatter(
            _ContextTextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(level.upper())
    # third-party noise
    for noisy in ("httpx", "httpcore", "botocore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
