"""Retrieval result types shared by the knowledge service and agent tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class KnowledgeHit:
    content: str
    document_title: str
    document_id: str
    chunk_id: str
    score: float
    source_uri: str | None = None


class KnowledgeSearch(Protocol):
    async def search(self, query: str, *, top_k: int) -> list[KnowledgeHit]: ...
