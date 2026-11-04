"""Postgres-backed job queue (``FOR UPDATE SKIP LOCKED``) with leases and retries."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Job


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


async def enqueue(
    session: AsyncSession,
    *,
    kind: str,
    payload: dict[str, Any],
    tenant_id: uuid.UUID | None,
    priority: int = 100,
    max_attempts: int = 3,
    delay_s: float = 0.0,
) -> uuid.UUID:
    job = Job(
        tenant_id=tenant_id,
        kind=kind,
        payload=payload,
        priority=priority,
        max_attempts=max_attempts,
        run_after=datetime.now(UTC) + timedelta(seconds=delay_s),
    )
    session.add(job)
    await session.flush()
    return job.id


async def claim(
    session: AsyncSession, *, worker_id: str, kinds: list[str], lease_s: int = 300
) -> ClaimedJob | None:
    """Claim one ready job. Expired leases (crashed workers) are reclaimed."""
    row = (
        await session.execute(
            sa.text(
                """
                UPDATE jobs SET status = 'running', locked_by = :worker,
                    locked_until = now() + make_interval(secs => :lease),
                    attempts = attempts + 1, updated_at = now()
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE kind = ANY(:kinds) AND (
                        (status = 'queued' AND run_after <= now())
                        OR (status = 'running' AND locked_until < now())
                    )
                    ORDER BY priority, run_after
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id, tenant_id, kind, payload, attempts, max_attempts
                """
            ),
            {"worker": worker_id, "lease": lease_s, "kinds": kinds},
        )
    ).first()
    if row is None:
        return None
    return ClaimedJob(
        id=row.id,
        tenant_id=row.tenant_id,
        kind=row.kind,
        payload=row.payload,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
    )


async def extend_lease(
    session: AsyncSession, job_id: uuid.UUID, worker_id: str, lease_s: int
) -> None:
    await session.execute(
        sa.text(
            "UPDATE jobs SET locked_until = now() + make_interval(secs => :lease)"
            " WHERE id = :id AND locked_by = :worker AND status = 'running'"
        ),
        {"id": job_id, "worker": worker_id, "lease": lease_s},
    )


async def complete(session: AsyncSession, job_id: uuid.UUID, result: dict[str, Any]) -> None:
    await session.execute(
        sa.update(Job)
        .where(Job.id == job_id)
        .values(status="succeeded", result=result, locked_by=None, locked_until=None)
    )


async def fail(
    session: AsyncSession, job: ClaimedJob, error: str, *, retryable: bool = True
) -> str:
    """Record a failure; requeue with exponential backoff or mark dead."""
    if retryable and job.attempts < job.max_attempts:
        delay = min(600, 15 * (2 ** (job.attempts - 1)))
        status = "queued"
        run_after = datetime.now(UTC) + timedelta(seconds=delay)
    else:
        status = "dead" if retryable else "failed"
        run_after = datetime.now(UTC)
    await session.execute(
        sa.update(Job)
        .where(Job.id == job.id)
        .values(
            status=status,
            last_error=error[:4000],
            run_after=run_after,
            locked_by=None,
            locked_until=None,
        )
    )
    return status
