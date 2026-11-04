"""Small statistics helpers (percentiles, Wilson intervals) without numpy dependencies."""

from __future__ import annotations

import math
from collections.abc import Sequence


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, q in [0, 100]."""
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    k = (len(data) - 1) * (q / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(data[int(k)])
    return float(data[lo] + (data[hi] - data[lo]) * (k - lo))


def summarize(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p90": None, "p95": None, "max": None}
    return {
        "count": len(values),
        "mean": round(sum(values) / len(values), 4),
        "p50": round(percentile(values, 50) or 0, 4),
        "p90": round(percentile(values, 90) or 0, 4),
        "p95": round(percentile(values, 95) or 0, 4),
        "max": round(max(values), 4),
    }


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))
