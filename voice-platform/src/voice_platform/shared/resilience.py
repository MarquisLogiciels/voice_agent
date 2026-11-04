"""Retry with backoff + jitter, deadlines, and a circuit breaker."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.25
    max_delay_s: float = 4.0
    # Total wall-clock budget across attempts. Prevents retries from blowing a call's
    # latency budget even when individual attempts time out.
    deadline_s: float | None = None

    def backoff(self, attempt: int, *, retry_after_s: float | None = None) -> float:
        """Full-jitter exponential backoff (AWS architecture blog), honoring Retry-After."""
        cap = min(self.max_delay_s, self.base_delay_s * (2 ** (attempt - 1)))
        delay = random.uniform(0, cap)
        if retry_after_s is not None:
            delay = max(delay, min(retry_after_s, self.max_delay_s * 4))
        return delay


class RetryableError(Exception):
    """Marker interface: errors that may succeed when retried."""

    retry_after_s: float | None = None


async def retry_async(
    fn: Callable[[int], Awaitable[T]],
    *,
    policy: RetryPolicy,
    is_retryable: Callable[[BaseException], bool],
    retry_after: Callable[[BaseException], float | None] = lambda _e: None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Call ``fn(attempt)`` until it succeeds, a non-retryable error occurs or the budget ends."""
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            return await fn(attempt)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if attempt >= policy.max_attempts or not is_retryable(e):
                raise
            delay = policy.backoff(attempt, retry_after_s=retry_after(e))
            if policy.deadline_s is not None:
                remaining = policy.deadline_s - (time.monotonic() - started)
                if remaining <= delay:
                    raise
            if on_retry is not None:
                on_retry(attempt, e, delay)
            await asyncio.sleep(delay)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    def __init__(self, name: str, retry_in_s: float) -> None:
        super().__init__(f"circuit '{name}' is open; retry in {retry_in_s:.1f}s")
        self.name = name
        self.retry_in_s = retry_in_s


class CircuitBreaker:
    """Consecutive-failure circuit breaker.

    closed → (``failure_threshold`` consecutive failures) → open → (``recovery_timeout_s``)
    → half-open (one trial allowed) → closed on success / open on failure.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_in_flight = False

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        if self._clock() - self._opened_at >= self.recovery_timeout_s:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def allow(self) -> bool:
        state = self.state
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.HALF_OPEN and not self._half_open_in_flight:
            self._half_open_in_flight = True
            return True
        return False

    def check(self) -> None:
        if not self.allow():
            assert self._opened_at is not None
            remaining = self.recovery_timeout_s - (self._clock() - self._opened_at)
            raise CircuitOpenError(self.name, max(0.0, remaining))

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._half_open_in_flight = False

    def record_failure(self) -> None:
        self._half_open_in_flight = False
        self._failures += 1
        if self._opened_at is not None or self._failures >= self.failure_threshold:
            self._opened_at = self._clock()
