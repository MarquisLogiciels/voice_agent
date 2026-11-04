"""Shared HTTP client for integrations: retries, circuit breaker, error normalization."""

from __future__ import annotations

import email.utils
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from ..observability.metrics import INTEGRATION_LATENCY, INTEGRATION_REQUESTS
from ..observability.tracing import span_attrs, tracer
from ..shared.http import EgressBlockedError, EgressPolicy, ResponseTooLargeError, read_limited
from ..shared.logging import get_logger, redact_value
from ..shared.resilience import CircuitBreaker, CircuitOpenError, RetryPolicy, retry_async
from .base import ErrorKind, IntegrationError

logger = get_logger(__name__)

AuthHook = Callable[[httpx.Request], Awaitable[None]]

IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            return max(0.0, parsed.timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def classify_status(status: int) -> ErrorKind:
    if status == 401:
        return ErrorKind.AUTH
    if status == 403:
        return ErrorKind.PERMISSION
    if status == 404:
        return ErrorKind.NOT_FOUND
    if status in (409, 412):
        return ErrorKind.CONFLICT
    if status in (400, 422):
        return ErrorKind.VALIDATION
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if status in (408, 504):
        return ErrorKind.TIMEOUT
    if status >= 500:
        return ErrorKind.UNAVAILABLE
    return ErrorKind.UNKNOWN


class IntegrationHttpClient:
    """One instance per (tenant, integration) per process. Thread-unsafe, asyncio-safe."""

    def __init__(
        self,
        *,
        integration: str,
        base_url: str,
        egress: EgressPolicy,
        auth: AuthHook | None = None,
        timeout_s: float = 8.0,
        retry: RetryPolicy | None = None,
        max_response_bytes: int = 2 * 1024 * 1024,
        max_request_bytes: int = 256 * 1024,
        breaker: CircuitBreaker | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        from ..shared.http import _GuardedTransport

        egress.check_url(base_url)
        self.integration = integration
        self._egress = egress
        self._auth = auth
        self._retry = retry or RetryPolicy(
            max_attempts=3, base_delay_s=0.3, max_delay_s=3.0, deadline_s=timeout_s * 1.5
        )
        self._max_response = max_response_bytes
        self._max_request = max_request_bytes
        self.breaker = breaker or CircuitBreaker(
            integration, failure_threshold=5, recovery_timeout_s=30
        )
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s, connect=min(4.0, timeout_s)),
            transport=transport or _GuardedTransport(egress),
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": "voice-platform/0.1",
                "Accept": "application/json",
                **(default_headers or {}),
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retry_unsafe: bool = False,
        expected: tuple[int, ...] = (),
        operation: str = "request",
    ) -> httpx.Response:
        """Send a request with normalization.

        Non-idempotent methods (POST/PATCH) are retried only for responses that guarantee the
        request was not processed (429), unless ``retry_unsafe`` (vendor idempotency key in use).
        A network failure on an unsafe request raises with ``ambiguous=True``.
        """
        method = method.upper()
        safe = method in IDEMPOTENT_METHODS or retry_unsafe
        body: bytes | None = None
        req_headers = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":"), default=str).encode()
            req_headers.setdefault("Content-Type", "application/json")
        if body is not None and len(body) > self._max_request:
            raise IntegrationError(
                ErrorKind.VALIDATION, "request body too large", integration=self.integration
            )

        async def attempt(n: int) -> httpx.Response:
            try:
                self.breaker.check()
            except CircuitOpenError as e:
                raise IntegrationError(
                    ErrorKind.UNAVAILABLE, str(e), integration=self.integration
                ) from e
            request = self._client.build_request(
                method, path, params=params, content=body, data=data, headers=req_headers
            )
            self._egress.check_url(request.url)
            if self._auth is not None:
                await self._auth(request)
            started = time.perf_counter()
            status_label = "error"
            try:
                resp = await self._client.send(request, stream=True)
                try:
                    content = await read_limited(resp, self._max_response)
                finally:
                    await resp.aclose()
                resp._content = content  # body is now fully read (bounded)
                status_label = str(resp.status_code)
            except EgressBlockedError as e:
                raise IntegrationError(
                    ErrorKind.CONFIGURATION, str(e), integration=self.integration
                ) from e
            except ResponseTooLargeError as e:
                raise IntegrationError(
                    ErrorKind.UNKNOWN, "response too large", integration=self.integration
                ) from e
            except httpx.TimeoutException as e:
                self.breaker.record_failure()
                raise IntegrationError(
                    ErrorKind.TIMEOUT,
                    f"{operation} timed out",
                    integration=self.integration,
                    ambiguous=not safe,
                ) from e
            except httpx.TransportError as e:
                self.breaker.record_failure()
                raise IntegrationError(
                    ErrorKind.UNAVAILABLE,
                    f"{operation} network error",
                    integration=self.integration,
                    ambiguous=not safe and not isinstance(e, httpx.ConnectError),
                ) from e
            finally:
                elapsed = time.perf_counter() - started
                INTEGRATION_LATENCY.labels(self.integration, operation).observe(elapsed)
                INTEGRATION_REQUESTS.labels(self.integration, operation, status_label).inc()

            if resp.status_code >= 500 or resp.status_code == 429:
                if resp.status_code >= 500:
                    self.breaker.record_failure()
            else:
                self.breaker.record_success()
            if resp.status_code < 400 or resp.status_code in expected:
                return resp
            kind = classify_status(resp.status_code)
            logger.warning(
                "integration request failed",
                extra={
                    "integration": self.integration,
                    "operation": operation,
                    "status": resp.status_code,
                    "body": redact_value(resp.text[:500]),
                },
            )
            raise IntegrationError(
                kind,
                f"{operation} failed with HTTP {resp.status_code}",
                status=resp.status_code,
                retry_after_s=_retry_after(resp),
                integration=self.integration,
                ambiguous=(not safe and resp.status_code >= 500),
            )

        def is_retryable(e: BaseException) -> bool:
            if not isinstance(e, IntegrationError):
                return False
            if e.kind is ErrorKind.RATE_LIMITED:
                return True  # request was rejected, never processed
            return safe and e.retryable

        with tracer.start_as_current_span(
            "vp.integration",
            attributes=span_attrs(integration=self.integration, operation=operation, method=method),
        ):
            return await self._retry_call(attempt, is_retryable, operation)

    async def _retry_call(self, attempt: Any, is_retryable: Any, operation: str) -> httpx.Response:
        return await retry_async(
            attempt,
            policy=self._retry,
            is_retryable=is_retryable,
            retry_after=lambda e: getattr(e, "retry_after_s", None),
            on_retry=lambda n, e, d: logger.info(
                "retrying integration request",
                extra={
                    "integration": self.integration,
                    "operation": operation,
                    "attempt": n,
                    "delay_s": round(d, 3),
                    "error": str(e),
                },
            ),
        )

    async def json(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self.request(method, path, **kw)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as e:
            raise IntegrationError(
                ErrorKind.UNKNOWN, "invalid JSON from integration", integration=self.integration
            ) from e

    async def paginate(
        self,
        method: str,
        path: str,
        *,
        next_page: Callable[[Any], tuple[str | None, dict[str, Any] | None]],
        items: Callable[[Any], list[Any]],
        max_items: int = 200,
        max_pages: int = 10,
        **kw: Any,
    ) -> AsyncIterator[Any]:
        """Generic cursor pagination with hard caps (bounded latency during calls)."""
        count = 0
        pages = 0
        current_path: str | None = path
        current_kw = dict(kw)
        while current_path and pages < max_pages:
            data = await self.json(method, current_path, **current_kw)
            pages += 1
            for item in items(data):
                yield item
                count += 1
                if count >= max_items:
                    return
            current_path, override = next_page(data)
            if override is not None:
                current_kw.update(override)
