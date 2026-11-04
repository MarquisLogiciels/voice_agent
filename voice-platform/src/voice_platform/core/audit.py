"""Append-only audit log (UPDATE/DELETE blocked by a trigger and revoked grants)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..shared.logging import current_log_context, get_logger, redact_value
from .models import AuditLog
from .rbac import Principal

logger = get_logger(__name__)


async def record_audit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    actor: Principal | None,
    action: str,
    resource_type: str,
    resource_id: str | uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    ctx = current_log_context()
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_type=actor.kind if actor else "system",
            actor_id=actor.id if actor else None,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            details=redact_value(details or {}),
            ip=ip or ctx.get("client_ip"),
            request_id=ctx.get("request_id"),
        )
    )
    logger.info(
        "audit",
        extra={"audit_action": action, "resource_type": resource_type,
               "resource_id": str(resource_id) if resource_id else None},
    )
