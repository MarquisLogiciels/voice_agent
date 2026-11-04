"""Knowledge bases: ingestion pipeline and hybrid retrieval (pgvector + full-text, RRF)."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import jobs
from ..core.db import Database, DbScope
from ..core.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument
from ..shared.blobstore import BlobStore, tenant_key
from ..shared.errors import NotFoundError, ValidationFailedError
from ..shared.logging import get_logger
from .chunking import chunk_text
from .embeddings import Embedder, build_embedder, tokens
from .extract import extract_text_isolated, sniff_mime
from .types import KnowledgeHit

logger = get_logger(__name__)

RRF_K = 60
INGEST_JOB = "knowledge.ingest"


class KnowledgeService:
    def __init__(
        self, db: Database, blobs: BlobStore, embedder_factory: Any = build_embedder
    ) -> None:
        self.db = db
        self.blobs = blobs
        self._embedder_factory = embedder_factory
        self._embedders: dict[tuple[str, str], Embedder] = {}

    def embedder(self, kb: KnowledgeBase) -> Embedder:
        key = (kb.embedding_provider, kb.embedding_model)
        if key not in self._embedders:
            self._embedders[key] = self._embedder_factory(*key)
        return self._embedders[key]

    # --- management -------------------------------------------------------------------------
    async def create_kb(
        self,
        s: AsyncSession,
        tenant_id: uuid.UUID,
        *,
        name: str,
        description: str = "",
        embedding_provider: str = "openai",
        embedding_model: str = "text-embedding-3-small",
    ) -> KnowledgeBase:
        kb = KnowledgeBase(
            tenant_id=tenant_id,
            name=name,
            description=description,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
        )
        s.add(kb)
        await s.flush()
        return kb

    async def get_kb(
        self, s: AsyncSession, tenant_id: uuid.UUID, kb_id: uuid.UUID
    ) -> KnowledgeBase:
        kb = await s.scalar(
            sa.select(KnowledgeBase).where(
                KnowledgeBase.id == kb_id, KnowledgeBase.tenant_id == tenant_id
            )
        )
        if kb is None:
            raise NotFoundError("knowledge base not found")
        return kb

    async def add_document(
        self,
        s: AsyncSession,
        tenant_id: uuid.UUID,
        kb_id: uuid.UUID,
        *,
        title: str,
        filename: str,
        data: bytes,
        declared_mime: str | None,
        source_type: str = "upload",
        source_uri: str | None = None,
        max_bytes: int = 25 * 1024 * 1024,
    ) -> KnowledgeDocument:
        if not data:
            raise ValidationFailedError("empty document")
        if len(data) > max_bytes:
            raise ValidationFailedError("document too large")
        await self.get_kb(s, tenant_id, kb_id)
        mime = sniff_mime(filename, declared_mime, data)
        digest = hashlib.sha256(data).hexdigest()
        doc = KnowledgeDocument(
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            title=title[:300],
            source_type=source_type,
            source_uri=source_uri,
            mime_type=mime,
            size_bytes=len(data),
            sha256=digest,
            status="pending",
        )
        s.add(doc)
        await s.flush()
        doc.blob_key = tenant_key(tenant_id, "documents", str(doc.id), "original")
        await self.blobs.put(doc.blob_key, data, mime)
        await jobs.enqueue(
            s,
            kind=INGEST_JOB,
            tenant_id=tenant_id,
            payload={"document_id": str(doc.id)},
            max_attempts=3,
        )
        return doc

    async def replace_document(
        self,
        s: AsyncSession,
        tenant_id: uuid.UUID,
        doc_id: uuid.UUID,
        *,
        filename: str,
        data: bytes,
        declared_mime: str | None,
    ) -> KnowledgeDocument:
        doc = await self._doc(s, tenant_id, doc_id)
        mime = sniff_mime(filename, declared_mime, data)
        doc.revision += 1
        doc.mime_type = mime
        doc.size_bytes = len(data)
        doc.sha256 = hashlib.sha256(data).hexdigest()
        doc.status = "pending"
        doc.error = None
        doc.blob_key = tenant_key(tenant_id, "documents", str(doc.id), f"r{doc.revision}")
        await self.blobs.put(doc.blob_key, data, mime)
        await jobs.enqueue(
            s,
            kind=INGEST_JOB,
            tenant_id=tenant_id,
            payload={"document_id": str(doc.id), "revision": doc.revision},
        )
        return doc

    async def delete_document(
        self, s: AsyncSession, tenant_id: uuid.UUID, doc_id: uuid.UUID
    ) -> None:
        doc = await self._doc(s, tenant_id, doc_id)
        key = doc.blob_key
        await s.delete(doc)  # chunks cascade
        await s.flush()
        if key:
            try:
                await self.blobs.delete(key)
            except Exception:
                logger.warning("blob delete failed; orphan will be swept", exc_info=True)

    async def _doc(
        self, s: AsyncSession, tenant_id: uuid.UUID, doc_id: uuid.UUID
    ) -> KnowledgeDocument:
        doc = await s.scalar(
            sa.select(KnowledgeDocument).where(
                KnowledgeDocument.id == doc_id, KnowledgeDocument.tenant_id == tenant_id
            )
        )
        if doc is None:
            raise NotFoundError("document not found")
        return doc

    # --- ingestion (job) --------------------------------------------------------------------
    async def process(self, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> dict[str, Any]:
        scope = DbScope.for_tenant(tenant_id)
        async with self.db.session(scope) as s:
            doc = await self._doc(s, tenant_id, doc_id)
            kb = await self.get_kb(s, tenant_id, doc.knowledge_base_id)
            doc.status = "processing"
            blob_key, mime, revision = doc.blob_key, doc.mime_type, doc.revision
            chunk_size, overlap = kb.chunk_size, kb.chunk_overlap
            embedder = self.embedder(kb)
        assert blob_key is not None
        try:
            data = await self.blobs.get(blob_key)
            text = await extract_text_isolated(data, mime)
            chunks = chunk_text(text, size=chunk_size, overlap=overlap)
            vectors = await embedder.embed([c.content for c in chunks])
        except Exception as e:
            async with self.db.session(scope) as s:
                await s.execute(
                    sa.update(KnowledgeDocument)
                    .where(KnowledgeDocument.id == doc_id)
                    .values(status="failed", error=str(e)[:1000])
                )
            raise
        async with self.db.session(scope) as s:
            current = await self._doc(s, tenant_id, doc_id)
            if current.revision != revision:
                return {"skipped": "superseded by a newer revision"}
            # atomic swap: old chunks out, new chunks in, same transaction
            await s.execute(sa.delete(KnowledgeChunk).where(KnowledgeChunk.document_id == doc_id))
            s.add_all(
                [
                    KnowledgeChunk(
                        tenant_id=tenant_id,
                        knowledge_base_id=current.knowledge_base_id,
                        document_id=doc_id,
                        ordinal=c.ordinal,
                        content=c.content,
                        token_count=len(c.content) // 4,
                        embedding=v,
                        meta={"heading": c.heading, "embedder": embedder.model},
                    )
                    for c, v in zip(chunks, vectors, strict=True)
                ]
            )
            current.status = "ready"
            current.chunk_count = len(chunks)
            current.error = None
        return {"chunks": len(chunks), "chars": len(text)}

    # --- retrieval --------------------------------------------------------------------------
    async def search(
        self,
        tenant_id: uuid.UUID,
        kb_ids: list[uuid.UUID],
        query: str,
        *,
        top_k: int = 4,
        mode: Literal["hybrid", "vector", "keyword"] = "hybrid",
        candidates: int = 30,
    ) -> list[KnowledgeHit]:
        if not kb_ids or not query.strip():
            return []
        async with self.db.session(DbScope.for_tenant(tenant_id)) as s:
            kbs = (
                await s.scalars(
                    sa.select(KnowledgeBase).where(
                        KnowledgeBase.id.in_(kb_ids), KnowledgeBase.tenant_id == tenant_id
                    )
                )
            ).all()
            if not kbs:
                return []
            qvec = None
            if mode in ("hybrid", "vector"):
                qvec = (await self.embedder(kbs[0]).embed([query]))[0]
            terms = [re.sub(r"[^a-z0-9]", "", t) for t in tokens(query)]
            or_query = " | ".join(t for t in terms if len(t) >= 2)
            params: dict[str, Any] = {
                "t": tenant_id,
                "kbs": [k.id for k in kbs],
                "n": candidates,
                "k": top_k,
            }
            ctes = []
            if qvec is not None:
                params["qvec"] = str(qvec)
                ctes.append("""vec AS (
                    SELECT id, row_number() OVER (ORDER BY embedding <=> CAST(:qvec AS vector)) AS r
                    FROM knowledge_chunks
                    WHERE tenant_id = :t AND knowledge_base_id = ANY(:kbs) AND embedding IS NOT NULL
                    ORDER BY embedding <=> CAST(:qvec AS vector) LIMIT :n)""")
            if or_query and mode in ("hybrid", "keyword"):
                params["q"] = or_query
                ctes.append("""kw AS (
                    SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, qq.q) DESC) AS r
                    FROM knowledge_chunks,
                         (SELECT to_tsquery('simple', vp_unaccent(:q))
                                 || to_tsquery('english', vp_unaccent(:q))
                                 || to_tsquery('french', vp_unaccent(:q)) AS q) qq
                    WHERE tenant_id = :t AND knowledge_base_id = ANY(:kbs) AND tsv @@ qq.q
                    ORDER BY ts_rank_cd(tsv, qq.q) DESC LIMIT :n)""")
            if not ctes:
                return []
            names = [c.split()[0] for c in ctes]
            union = " UNION ".join(f"SELECT id FROM {n}" for n in names)  # noqa: S608
            score = " + ".join(f"coalesce(1.0 / ({RRF_K} + {n}.r), 0)" for n in names)
            joins = " ".join(f"LEFT JOIN {n} ON {n}.id = c.id" for n in names)
            # Only fixed CTE names/fragments are interpolated; user input is bound via params.
            sql = "\n".join(
                [
                    f"WITH {', '.join(ctes)}, ids AS ({union})",
                    "SELECT c.id, c.content, c.document_id, d.title, d.source_uri,"
                    f" {score} AS score",
                    "FROM ids JOIN knowledge_chunks c ON c.id = ids.id",
                    "JOIN knowledge_documents d ON d.id = c.document_id",
                    joins,
                    "ORDER BY score DESC LIMIT :k",
                ]
            )
            rows = (await s.execute(sa.text(sql), params)).all()
        max_score = len(names) / (RRF_K + 1)
        return [
            KnowledgeHit(
                content=r.content,
                document_title=r.title,
                document_id=str(r.document_id),
                chunk_id=str(r.id),
                score=float(r.score) / max_score,
                source_uri=r.source_uri,
            )
            for r in rows
        ]


@dataclass
class TenantKnowledge:
    """Adapter implementing ``tools.interfaces.KnowledgeSearch`` for one agent/call."""

    service: KnowledgeService
    tenant_id: uuid.UUID
    kb_ids: list[uuid.UUID]
    mode: Literal["hybrid", "vector", "keyword"] = "hybrid"

    async def search(self, query: str, *, top_k: int) -> list[KnowledgeHit]:
        return await self.service.search(
            self.tenant_id, self.kb_ids, query, top_k=top_k, mode=self.mode
        )


@dataclass
class RetrievalCase:
    question: str
    expected_document: str | None = None
    expected_text: str | None = None


async def evaluate_retrieval(
    search: Any, cases: list[RetrievalCase], *, k: int = 4
) -> dict[str, Any]:
    """Hit rate@k and MRR for labelled questions (expected doc title or text fragment)."""
    hits = 0
    rr_total = 0.0
    details = []
    for case in cases:
        results = await search.search(case.question, top_k=k)
        rank = None
        for i, h in enumerate(results, start=1):
            ok = (case.expected_document and h.document_title == case.expected_document) or (
                case.expected_text and case.expected_text.lower() in h.content.lower()
            )
            if ok:
                rank = i
                break
        if rank:
            hits += 1
            rr_total += 1 / rank
        details.append({"question": case.question, "rank": rank})
    n = max(1, len(cases))
    return {"hit_rate": hits / n, "mrr": rr_total / n, "k": k, "cases": details}
