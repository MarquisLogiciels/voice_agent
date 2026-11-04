"""Cost estimation from usage × approximate pricing (estimates, not billing)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..providers.pricing import load_pricing, pricing_as_of


@dataclass
class Usage:
    stt_audio_s: dict[str, float] = field(default_factory=dict)
    llm_input_tokens: dict[str, int] = field(default_factory=dict)
    llm_output_tokens: dict[str, int] = field(default_factory=dict)
    tts_chars: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def _add(d: dict[str, Any], key: str, value: float) -> None:
        d[key] = d.get(key, 0) + value

    def add_stt(self, provider_key: str, seconds: float) -> None:
        self._add(self.stt_audio_s, provider_key, seconds)

    def add_llm(self, provider_key: str, prompt: int, completion: int) -> None:
        self._add(self.llm_input_tokens, provider_key, prompt)
        self._add(self.llm_output_tokens, provider_key, completion)

    def add_tts(self, provider_key: str, chars: int) -> None:
        self._add(self.tts_chars, provider_key, chars)

    def as_dict(self) -> dict[str, Any]:
        return {"stt_audio_s": self.stt_audio_s, "llm_input_tokens": self.llm_input_tokens,
                "llm_output_tokens": self.llm_output_tokens, "tts_chars": self.tts_chars}


def _price(provider_key: str) -> Any:
    # provider_key format: "<kind>:<provider>:<model>"
    kind, provider, model = (provider_key.split(":") + ["-", "-"])[:3]
    table = load_pricing().get(f"{kind}/{provider}", {})
    return table.get(model) or table.get("*")


def estimate_cost(usage: Usage) -> dict[str, Any]:
    lines: dict[str, float] = {}
    unknown: list[str] = []
    for key, secs in usage.stt_audio_s.items():
        p = _price(key)
        if p and p.usd_per_audio_minute is not None:
            lines[key] = lines.get(key, 0) + secs / 60 * p.usd_per_audio_minute
        else:
            unknown.append(key)
    for key in set(usage.llm_input_tokens) | set(usage.llm_output_tokens):
        p = _price(key)
        if p and p.usd_per_1m_input_tokens is not None:
            lines[key] = lines.get(key, 0) + (
                usage.llm_input_tokens.get(key, 0) * p.usd_per_1m_input_tokens
                + usage.llm_output_tokens.get(key, 0) * (p.usd_per_1m_output_tokens or 0)) / 1e6
        else:
            unknown.append(key)
    for key, chars in usage.tts_chars.items():
        p = _price(key)
        if p and p.usd_per_1k_chars is not None:
            lines[key] = lines.get(key, 0) + chars / 1000 * p.usd_per_1k_chars
        else:
            unknown.append(key)
    return {
        "total_usd": round(sum(lines.values()), 6),
        "lines": {k: round(v, 6) for k, v in lines.items()},
        "unpriced": sorted(set(unknown)),
        "pricing_as_of": pricing_as_of(),
        "estimate": True,
    }
