"""Provider health: authenticated no-cost probes and a process-wide circuit breaker.

Probes call a free metadata endpoint with the configured key (list models, account info). They
verify credentials and reachability; they do not exercise streaming paths.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from ..config.schema import ProviderRef
from ..shared.logging import get_logger
from ..shared.resilience import CircuitBreaker, CircuitState

logger = get_logger(__name__)

_TIMEOUT = httpx.Timeout(5.0)


class HealthCheckError(Exception):
    pass


async def _probe(url: str, headers: dict[str, str]) -> None:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code in (401, 403):
        raise HealthCheckError(f"authentication rejected ({resp.status_code})")
    if resp.status_code >= 400:
        raise HealthCheckError(f"unexpected status {resp.status_code}")


async def openai(key: str) -> None:
    await _probe("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})


async def anthropic(key: str) -> None:
    await _probe(
        "https://api.anthropic.com/v1/models", {"x-api-key": key, "anthropic-version": "2023-06-01"}
    )


async def google_genai(key: str) -> None:
    await _probe("https://generativelanguage.googleapis.com/v1beta/models", {"x-goog-api-key": key})


async def deepgram(key: str) -> None:
    await _probe("https://api.deepgram.com/v1/projects", {"Authorization": f"Token {key}"})


async def assemblyai(key: str) -> None:
    await _probe("https://api.assemblyai.com/v2/transcript?limit=1", {"Authorization": key})


async def elevenlabs(key: str) -> None:
    await _probe("https://api.elevenlabs.io/v1/user", {"xi-api-key": key})


async def cartesia(key: str) -> None:
    await _probe(
        "https://api.cartesia.ai/voices", {"X-API-Key": key, "Cartesia-Version": "2024-11-13"}
    )


@dataclass
class ProviderHealth:
    key: str
    state: str
    consecutive_failures: int
    last_failure_at: float | None
    last_success_at: float | None


class ProviderHealthTracker:
    """Shares provider failure state across all sessions in a worker process.

    LiveKit FallbackAdapters keep availability per instance; since each call builds new
    instances, without this tracker every call would first retry a provider that is known to
    be down. The builder uses :meth:`order` to put open-circuit providers last.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_timeout_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._recovery = recovery_timeout_s
        self._clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}
        self._last_failure: dict[str, float] = {}
        self._last_success: dict[str, float] = {}

    def _breaker(self, key: str) -> CircuitBreaker:
        br = self._breakers.get(key)
        if br is None:
            br = CircuitBreaker(
                key,
                failure_threshold=self._threshold,
                recovery_timeout_s=self._recovery,
                clock=self._clock,
            )
            self._breakers[key] = br
        return br

    def record_failure(self, key: str) -> None:
        self._breaker(key).record_failure()
        self._last_failure[key] = time.time()
        logger.warning(
            "provider failure recorded",
            extra={"provider_key": key, "state": self._breaker(key).state.value},
        )

    def record_success(self, key: str) -> None:
        br = self._breaker(key)
        if br.state is not CircuitState.CLOSED or br.consecutive_failures:
            logger.info("provider recovered", extra={"provider_key": key})
        br.record_success()
        self._last_success[key] = time.time()

    def is_available(self, key: str) -> bool:
        return self._breaker(key).state is not CircuitState.OPEN

    def order(self, kind: str, chain: list[ProviderRef]) -> list[ProviderRef]:
        """Stable partition: available providers first, open-circuit providers last."""
        from .registry import provider_key

        healthy = [r for r in chain if self.is_available(provider_key(kind, r))]
        degraded = [r for r in chain if not self.is_available(provider_key(kind, r))]
        return healthy + degraded

    def snapshot(self) -> list[ProviderHealth]:
        return [
            ProviderHealth(
                key=k,
                state=b.state.value,
                consecutive_failures=b.consecutive_failures,
                last_failure_at=self._last_failure.get(k),
                last_success_at=self._last_success.get(k),
            )
            for k, b in sorted(self._breakers.items())
        ]


_tracker = ProviderHealthTracker()


def global_tracker() -> ProviderHealthTracker:
    return _tracker


async def run_health_checks(
    checks: dict[str, tuple[Callable[[str], object], str]], timeout_s: float = 6.0
) -> dict[str, dict[str, object]]:
    """Run ``{label: (check_fn, key)}`` concurrently; returns status per label."""

    async def one(fn: Callable[[str], object], key: str) -> dict[str, object]:
        started = time.perf_counter()
        try:
            await asyncio.wait_for(fn(key), timeout_s)  # type: ignore[arg-type]
            return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
        except Exception as e:  # report, never raise
            return {"ok": False, "error": type(e).__name__ + ": " + str(e)[:200]}

    labels = list(checks)
    results = await asyncio.gather(*(one(*checks[label]) for label in labels))
    return dict(zip(labels, results, strict=True))
