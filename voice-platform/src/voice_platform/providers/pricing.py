"""Loads the approximate pricing table used for cost estimates."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .types import Pricing

_DEFAULT = Path(__file__).parent / "data" / "pricing.yaml"


@lru_cache(maxsize=1)
def load_pricing() -> dict[str, dict[str, Pricing]]:
    path = Path(os.environ.get("VP_PRICING_FILE") or _DEFAULT)
    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    out: dict[str, dict[str, Pricing]] = {}
    for key, models in (raw.get("prices") or {}).items():
        out[key] = {str(m): Pricing(**fields) for m, fields in models.items()}
    return out


def pricing_as_of() -> str:
    path = Path(os.environ.get("VP_PRICING_FILE") or _DEFAULT)
    raw = yaml.safe_load(path.read_text()) or {}
    return str(raw.get("as_of", "unknown"))
