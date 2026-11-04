from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from voice_platform.core.db import DbScope
from voice_platform.core.models import KnowledgeChunk, KnowledgeDocument
from voice_platform.knowledge.chunking import chunk_text
from voice_platform.knowledge.extract import extract_text, html_to_text, sniff_mime
from voice_platform.knowledge.service import (
    KnowledgeService,
    RetrievalCase,
    TenantKnowledge,
    evaluate_retrieval,
)
from voice_platform.shared.blobstore import LocalBlobStore
from voice_platform.shared.errors import ValidationFailedError

FAQ = """## Hours
We are open Monday to Friday from 9 am to 5 pm. We are closed on statutory holidays.

## Services and prices
A routine cleaning costs 120 dollars. Teeth whitening costs 350 dollars.
Emergency visits are available the same day when booked before noon.

## Insurance
We accept Sun Life, Manulife and Canada Life. Direct billing is available.

## Parking
Free parking is available behind the building on Rue Saint-Denis.
"""

FR = """## Tarifs
Le détartrage coûte 120 $. Le blanchiment des dents coûte 350 $.

## Heures d'ouverture
Nous sommes ouverts du lundi au vendredi, de 9 h à 17 h.
"""


def _make_pdf(text: str) -> bytes:
    """Build a minimal valid single-page PDF (with xref) containing ``text``."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj" % i + obj + b"endobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (len(objects) + 1, xref)
    return bytes(out)


PDF = _make_pdf("Cancellation policy: 24 hours notice")


@pytest.fixture
def service(db: Any, tmp_path: Path) -> KnowledgeService:
    blobs = LocalBlobStore(tmp_path / "blobs", signing_secret="s" * 32, public_base_url="http://x")
    return KnowledgeService(db, blobs)


async def _kb_with_docs(
    db: Any, service: KnowledgeService, tenant_id: uuid.UUID, docs: list[tuple[str, str, bytes]]
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    async with db.session(DbScope.for_tenant(tenant_id)) as s:
        kb = await service.create_kb(
            s,
            tenant_id,
            name=f"kb-{uuid.uuid4().hex[:6]}",
            embedding_provider="hashing",
            embedding_model="hashing-v1",
        )
        ids = []
        for title, filename, data in docs:
            doc = await service.add_document(
                s, tenant_id, kb.id, title=title, filename=filename, data=data, declared_mime=None
            )
            ids.append(doc.id)
        kb_id = kb.id
    for doc_id in ids:
        await service.process(tenant_id, doc_id)
    return kb_id, ids


async def test_ingest_and_hybrid_retrieval(
    db: Any, service: KnowledgeService, tenant: Any, other_tenant: Any
) -> None:
    kb_id, ids = await _kb_with_docs(
        db,
        service,
        tenant.id,
        [
            ("Clinic FAQ", "faq.md", FAQ.encode()),
            ("Tarifs FR", "tarifs.txt", FR.encode()),
            ("Policies", "policy.pdf", PDF),
        ],
    )
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        docs = (
            await s.scalars(sa.select(KnowledgeDocument).where(KnowledgeDocument.id.in_(ids)))
        ).all()
        assert {d.status for d in docs} == {"ready"}
        assert all(d.chunk_count >= 1 for d in docs)
    kn = TenantKnowledge(service, tenant.id, [kb_id])
    hits = await kn.search("How much does a cleaning cost?", top_k=3)
    assert hits and "120 dollars" in hits[0].content and hits[0].document_title == "Clinic FAQ"
    assert 0 < hits[0].score <= 1
    # accent-insensitive keyword match (STT often drops accents)
    fr = await kn.search("combien coute le detartrage", top_k=2)
    assert fr and fr[0].document_title == "Tarifs FR"
    pdf_hits = await kn.search("cancellation notice", top_k=2)
    assert pdf_hits and pdf_hits[0].document_title == "Policies"
    # tenant isolation: another tenant cannot read this KB even by id
    assert (
        await TenantKnowledge(service, other_tenant.id, [kb_id]).search("cleaning", top_k=3) == []
    )
    report = await evaluate_retrieval(
        kn,
        [
            RetrievalCase("Do you take Manulife insurance?", expected_text="Manulife"),
            RetrievalCase("Where can I park?", expected_text="parking"),
            RetrievalCase("Quand êtes-vous ouverts?", expected_document="Tarifs FR"),
        ],
        k=3,
    )
    assert report["hit_rate"] == 1.0 and report["mrr"] > 0.6


async def test_replace_and_delete_document(db: Any, service: KnowledgeService, tenant: Any) -> None:
    kb_id, (doc_id,) = await _kb_with_docs(
        db, service, tenant.id, [("FAQ", "faq.md", FAQ.encode())]
    )
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        await service.replace_document(
            s,
            tenant.id,
            doc_id,
            filename="faq.md",
            data=b"## Hours\nNow open Saturdays from 10 to 2.",
            declared_mime=None,
        )
    await service.process(tenant.id, doc_id)
    kn = TenantKnowledge(service, tenant.id, [kb_id])
    hits = await kn.search(
        "cleaning cost",
        top_k=3,
    )
    assert all("120 dollars" not in h.content for h in hits)  # old revision gone
    assert (await kn.search("open Saturdays", top_k=1))[0].document_id == str(doc_id)
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        await service.delete_document(s, tenant.id, doc_id)
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        n = await s.scalar(
            sa.select(sa.func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == doc_id)
        )
        assert n == 0


async def test_failed_extraction_marks_document_failed(
    db: Any, service: KnowledgeService, tenant: Any
) -> None:
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        kb = await service.create_kb(
            s, tenant.id, name="kbx", embedding_provider="hashing", embedding_model="hashing-v1"
        )
        doc = await service.add_document(
            s,
            tenant.id,
            kb.id,
            title="bad",
            filename="x.pdf",
            data=b"%PDF-1.4 garbage",
            declared_mime=None,
        )
    with pytest.raises(ValidationFailedError):
        await service.process(tenant.id, doc.id)
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        row = await s.get(KnowledgeDocument, doc.id)
        assert row is not None and row.status == "failed" and row.error


def test_extractors_and_chunking() -> None:
    import docx

    d = docx.Document()
    d.add_heading("Services", level=1)
    d.add_paragraph("We offer root canals and implants.")
    buf = io.BytesIO()
    d.save(buf)
    data = buf.getvalue()
    mime = sniff_mime("services.docx", None, data)
    text = extract_text(data, mime)
    assert "## Services" in text and "root canals" in text
    html = "<html><script>alert(1)</script><nav>menu</nav><h1>Prices</h1><p>Cleaning $120</p>"
    out = html_to_text(html)
    assert "alert" not in out and "menu" not in out and "Cleaning $120" in out
    with pytest.raises(ValidationFailedError):
        sniff_mime("evil.exe", "application/octet-stream", b"MZ")
    long = "## Section\n\n" + " ".join(f"Sentence number {i} is here." for i in range(300))
    chunks = chunk_text(long, size=400, overlap=80)
    assert len(chunks) > 5 and all(len(c.content) <= 400 + len("Section\n") for c in chunks)
    assert all(c.heading == "Section" for c in chunks)
