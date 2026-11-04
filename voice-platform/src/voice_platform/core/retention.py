"""Data-retention enforcement (conversations, transcripts, recordings, tool payloads).

Each tenant's ``retention_days`` bounds how long conversation data is kept; recordings use the
shorter of that and the agent's ``recording.retention_days`` (stored on the conversation as
``meta.recording_retention_days`` when available). Deletion cascades to events and nulls tool
execution links; recording objects are removed from object storage first.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from ..shared.blobstore import BlobStore
from ..shared.logging import get_logger
from .db import Database, DbScope
from .models import Conversation, Tenant, ToolExecution

logger = get_logger(__name__)
RETENTION_JOB = "retention.sweep"


async def sweep(db: Database, blobs: BlobStore, *, batch: int = 500) -> dict[str, Any]:
    now = datetime.now(UTC)
    report: dict[str, Any] = {
        "tenants": 0,
        "conversations_deleted": 0,
        "recordings_deleted": 0,
        "tool_payloads_scrubbed": 0,
    }
    async with db.session(DbScope.system()) as s:
        tenants = (await s.execute(sa.select(Tenant.id, Tenant.retention_days))).all()
    for tenant_id, days in tenants:
        report["tenants"] += 1
        cutoff = now - timedelta(days=days)
        async with db.session(DbScope.for_tenant(tenant_id)) as s:
            # recordings past their own (possibly shorter) retention
            rec_rows = (
                await s.execute(
                    sa.select(
                        Conversation.id,
                        Conversation.recording_uri,
                        Conversation.started_at,
                        Conversation.meta,
                    )
                    .where(
                        Conversation.tenant_id == tenant_id, Conversation.recording_uri.is_not(None)
                    )
                    .limit(batch)
                )
            ).all()
            for conv_id, uri, started, meta in rec_rows:
                rec_days = int((meta or {}).get("recording_retention_days") or days)
                if started < now - timedelta(days=min(rec_days, days)):
                    try:
                        await blobs.delete(uri)
                    except Exception:
                        logger.warning(
                            "recording delete failed",
                            extra={"conversation": str(conv_id)},
                            exc_info=True,
                        )
                        continue
                    await s.execute(
                        sa.update(Conversation)
                        .where(Conversation.id == conv_id)
                        .values(recording_uri=None, recording_status="deleted")
                    )
                    report["recordings_deleted"] += 1
            old = (
                await s.scalars(
                    sa.select(Conversation.id)
                    .where(Conversation.tenant_id == tenant_id, Conversation.started_at < cutoff)
                    .limit(batch)
                )
            ).all()
            if old:
                # tool payloads may contain customer data: scrub before unlinking
                res = await s.execute(
                    sa.update(ToolExecution)
                    .where(ToolExecution.conversation_id.in_(old))
                    .values(request={}, response={})
                )
                report["tool_payloads_scrubbed"] += getattr(res, "rowcount", 0) or 0
                await s.execute(sa.delete(Conversation).where(Conversation.id.in_(old)))
                report["conversations_deleted"] += len(old)
    logger.info("retention sweep finished", extra=report)
    return report
