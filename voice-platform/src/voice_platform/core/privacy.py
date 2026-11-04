"""Data-subject erasure, including re-application after a database restore.

Erasure removes a caller's conversations (events cascade), the content of their tool
executions (request, response, error text) and every *version* of their recordings. The audit
log records only a keyed hash of the number (``subject_ref``) — never the number itself — so
that after a point-in-time restore the same erasures can be re-applied
(``vp reapply-erasures``): backups taken before an erasure would otherwise resurrect the data.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..shared.blobstore import BlobStore
from ..shared.logging import get_logger
from .db import Database, DbScope
from .models import AuditLog, Conversation, ToolExecution

logger = get_logger(__name__)
ERASE_ACTION = "data_subject.erase"


def _hmac_keys(encryption_keys: str) -> list[bytes]:
    keys = [k.strip() for k in encryption_keys.split(",") if k.strip()]
    return [hashlib.sha256(b"vp-erasure-ref:" + k.encode()).digest() for k in keys]


def subject_ref(e164: str, key: bytes) -> str:
    return hmac.new(key, e164.encode(), hashlib.sha256).hexdigest()[:40]


def subject_refs(e164: str, encryption_keys: str) -> set[str]:
    """One ref per configured key, so refs stay matchable across key rotation."""
    return {subject_ref(e164, k) for k in _hmac_keys(encryption_keys)}


@dataclass
class ErasureResult:
    conversations: int
    recordings: int


async def erase_subject_data(
    session: AsyncSession, blobs: BlobStore, tenant_id: uuid.UUID, e164: str
) -> ErasureResult:
    convs = (
        await session.scalars(
            sa.select(Conversation).where(
                Conversation.tenant_id == tenant_id,
                sa.or_(Conversation.caller_number == e164, Conversation.callee_number == e164),
            )
        )
    ).all()
    ids = [c.id for c in convs]
    recordings = 0
    for c in convs:
        if c.recording_uri:
            await blobs.purge(c.recording_uri)  # every version, not just a delete marker
            recordings += 1
    if ids:
        await session.execute(
            sa.update(ToolExecution)
            .where(ToolExecution.conversation_id.in_(ids))
            .values(request={}, response={}, error_message=None)
        )
        await session.execute(sa.delete(Conversation).where(Conversation.id.in_(ids)))
    return ErasureResult(conversations=len(ids), recordings=recordings)


async def reapply_erasures(db: Database, blobs: BlobStore, encryption_keys: str) -> dict[str, Any]:
    """Re-run every recorded erasure (run after restoring a backup)."""
    keys = _hmac_keys(encryption_keys)
    async with db.session(DbScope.system()) as s:
        rows = (
            await s.execute(
                sa.select(AuditLog.tenant_id, AuditLog.details).where(
                    AuditLog.action == ERASE_ACTION
                )
            )
        ).all()
    wanted: dict[uuid.UUID, set[str]] = {}
    for tenant_id, details in rows:
        ref = (details or {}).get("subject_ref")
        if tenant_id is not None and ref:
            wanted.setdefault(tenant_id, set()).add(ref)
    totals = {"tenants": len(wanted), "conversations": 0, "recordings": 0, "subjects": 0}
    for tenant_id, refs in wanted.items():
        async with db.session(DbScope.for_tenant(tenant_id)) as s:
            numbers = {
                n
                for row in (
                    await s.execute(
                        sa.select(Conversation.caller_number, Conversation.callee_number)
                    )
                ).all()
                for n in row
                if n
            }
            for number in numbers:
                if {subject_ref(number, k) for k in keys} & refs:
                    res = await erase_subject_data(s, blobs, tenant_id, number)
                    totals["subjects"] += 1
                    totals["conversations"] += res.conversations
                    totals["recordings"] += res.recordings
    logger.info("erasures re-applied", extra=totals)
    return totals
