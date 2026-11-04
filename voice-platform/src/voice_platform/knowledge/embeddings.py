"""Embedding providers. All produce L2-normalized vectors of ``EMBEDDING_DIM`` dimensions."""

from __future__ import annotations

import hashlib
import math
import os
import re
import unicodedata
from typing import Protocol

import httpx

from ..core.models import EMBEDDING_DIM
from ..shared.errors import ConfigurationError, DependencyUnavailableError
from ..shared.resilience import RetryPolicy, retry_async


class Embedder(Protocol):
    name: str
    model: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _normalize(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "for", "on", "at",
         "with", "do", "does", "you", "your", "we", "our", "i", "it", "be", "can", "what",
         "le", "la", "les", "des", "un", "une", "et", "ou", "de", "du", "en", "est", "pour",
         "au", "aux", "vous", "nous", "je", "il", "elle", "que", "qui", "quoi", "est-ce"}


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(fold(text)) if t not in _STOP and len(t) > 1]


class HashingEmbedder:
    """Deterministic, offline lexical embedder (feature hashing of unigrams + bigrams +
    character trigrams). Useful for development, CI and as an outage fallback; it captures word
    overlap, not meaning, so production tenants should use a semantic embedding model."""

    name = "hashing"
    model = "hashing-v1"
    dim = EMBEDDING_DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        toks = tokens(text)
        feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:], strict=False)]
        for t in toks:
            if len(t) >= 4:
                feats.append("p4:" + t[:4])  # crude stemming: park / parking / parked
            if len(t) > 4:
                feats += [t[i:i + 3] for i in range(len(t) - 2)]
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            idx = h % self.dim
            vec[idx] += 1.0 if (h >> 63) & 1 else -1.0
        return _normalize(vec)


class OpenAIEmbedder:
    name = "openai"
    dim = EMBEDDING_DIM

    def __init__(self, model: str = "text-embedding-3-small", api_key: str | None = None,
                 base_url: str = "https://api.openai.com/v1", timeout_s: float = 15.0) -> None:
        self.model = model
        self._key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self._key:
            raise ConfigurationError("OPENAI_API_KEY is required for OpenAI embeddings")
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 96):
            batch = [t[:8000] for t in texts[i:i + 96]]

            async def call(_attempt: int, batch: list[str] = batch) -> list[list[float]]:
                resp = await self._client.post(
                    "/embeddings", headers={"Authorization": f"Bearer {self._key}"},
                    json={"model": self.model, "input": batch, "dimensions": self.dim})
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise DependencyUnavailableError(f"embeddings HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    raise ConfigurationError(f"embeddings rejected: HTTP {resp.status_code}")
                data = resp.json()["data"]
                return [_normalize(d["embedding"]) for d in sorted(data, key=lambda d: d["index"])]

            out.extend(await retry_async(
                call, policy=RetryPolicy(max_attempts=4, base_delay_s=0.5, max_delay_s=8),
                is_retryable=lambda e: isinstance(e, DependencyUnavailableError | httpx.TransportError)))
        return out

    async def aclose(self) -> None:
        await self._client.aclose()


def build_embedder(provider: str, model: str) -> Embedder:
    if provider == "hashing":
        return HashingEmbedder()
    if provider == "openai":
        return OpenAIEmbedder(model=model)
    raise ConfigurationError(f"unknown embedding provider '{provider}'")
