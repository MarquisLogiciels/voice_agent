"""Fixed-window rate limiting backed by Redis, with an in-process fallback.

If Redis is not configured or unavailable, limits are enforced per process (each replica
enforces the limit independently). This degrades gracefully: the API keeps serving rather
than failing closed because a cache is down, while still bounding abuse per replica.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    remaining: int
    reset_in_s: float


class _LocalWindow:
    def __init__(self, max_keys: int = 50_000) -> None:
        self._counts: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._max = max_keys
        self._lock = asyncio.Lock()

    async def hit(self, key: str, window_s: int) -> tuple[int, float]:
        now = time.monotonic()
        async with self._lock:
            count, start = self._counts.get(key, (0, now))
            if now - start >= window_s:
                count, start = 0, now
            count += 1
            self._counts[key] = (count, start)
            self._counts.move_to_end(key)
            while len(self._counts) > self._max:
                self._counts.popitem(last=False)
            return count, window_s - (now - start)


class RateLimiter:
    def __init__(self, redis: Any | None = None) -> None:
        self._redis = redis
        self._local = _LocalWindow()
        self._redis_failed_at: float | None = None

    async def hit(self, key: str, *, limit: int, window_s: int = 60) -> RateDecision:
        count, reset = await self._hit(key, window_s)
        return RateDecision(
            allowed=count <= limit, remaining=max(0, limit - count), reset_in_s=reset
        )

    async def _hit(self, key: str, window_s: int) -> tuple[int, float]:
        if self._redis is not None and (
            self._redis_failed_at is None or time.monotonic() - self._redis_failed_at > 30
        ):
            bucket = int(time.time() // window_s)
            rkey = f"vp:rl:{key}:{bucket}"
            try:
                pipe = self._redis.pipeline()
                pipe.incr(rkey)
                pipe.expire(rkey, window_s + 1)
                count, _ = await pipe.execute()
                self._redis_failed_at = None
                return int(count), window_s - (time.time() % window_s)
            except Exception:
                logger.warning("redis rate limiter unavailable; using local limits", exc_info=True)
                self._redis_failed_at = time.monotonic()
        return await self._local.hit(key, window_s)
