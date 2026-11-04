"""Tool execution ledger: idempotency + audit trail for every tool call."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from ..core.db import Database, DbScope
from ..core.models import ToolExecution
from ..shared.logging import get_logger, redact_value

logger = get_logger(__name__)


@dataclass
class LedgerEntry:
    key: str
    status: str  # pending | succeeded | failed
    response: dict[str, Any]


class ToolLedger(Protocol):
    async def begin(self, key: str, tool: str, request: dict[str, Any]) -> LedgerEntry | None:
        """Reserve ``key``. Returns None if new (caller proceeds) or the existing entry."""

    async def finish(self, key: str, *, status: str, response: dict[str, Any],
                     error_kind: str | None, error_message: str | None,
                     duration_ms: float) -> None: ...


class InMemoryLedger:
    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def begin(self, key: str, tool: str, request: dict[str, Any]) -> LedgerEntry | None:
        async with self._lock:
            existing = self.entries.get(key)
            if existing is not None and existing["status"] != "failed":
                return LedgerEntry(key, existing["status"], existing["response"])
            self.entries[key] = {"tool": tool, "request": request, "status": "pending",
                                 "response": {}, "attempts": (existing or {}).get("attempts", 0) + 1}
            return None

    async def finish(self, key: str, *, status: str, response: dict[str, Any],
                     error_kind: str | None, error_message: str | None,
                     duration_ms: float) -> None:
        async with self._lock:
            entry = self.entries.setdefault(key, {})
            entry.update(status=status, response=response, error_kind=error_kind,
                         error_message=error_message, duration_ms=duration_ms)


class PostgresLedger:
    """Durable ledger in ``tool_executions``; survives worker restarts mid-call."""

    def __init__(self, db: Database, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID | None,
                 agent_id: uuid.UUID | None) -> None:
        self._db = db
        self._scope = DbScope.for_tenant(tenant_id)
        self._tenant_id = tenant_id
        self._conversation_id = conversation_id
        self._agent_id = agent_id

    async def begin(self, key: str, tool: str, request: dict[str, Any]) -> LedgerEntry | None:
        async with self._db.session(self._scope) as s:
            stmt = (
                insert(ToolExecution)
                .values(tenant_id=self._tenant_id, conversation_id=self._conversation_id,
                        agent_id=self._agent_id, tool_name=tool, idempotency_key=key,
                        status="pending", request=redact_value(request))
                .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
                .returning(ToolExecution.id)
            )
            inserted = (await s.execute(stmt)).scalar()
            if inserted is not None:
                return None
            row = (await s.execute(
                sa.select(ToolExecution).where(ToolExecution.tenant_id == self._tenant_id,
                                               ToolExecution.idempotency_key == key)
                .with_for_update())).scalar_one()
            if row.status == "failed":
                row.status = "pending"
                row.attempts += 1
                row.started_at = datetime.now(UTC)
                return None
            return LedgerEntry(key, row.status, dict(row.response or {}))

    async def finish(self, key: str, *, status: str, response: dict[str, Any],
                     error_kind: str | None, error_message: str | None,
                     duration_ms: float) -> None:
        try:
            async with self._db.session(self._scope) as s:
                await s.execute(
                    sa.update(ToolExecution)
                    .where(ToolExecution.tenant_id == self._tenant_id,
                           ToolExecution.idempotency_key == key)
                    .values(status=status, response=redact_value(response),
                            error_kind=error_kind, error_message=error_message,
                            finished_at=datetime.now(UTC), duration_ms=duration_ms)
                )
        except Exception:
            # The external operation already happened; losing the audit row must not fail
            # the conversation. The pending row remains as evidence for reconciliation.
            logger.exception("failed to finalize tool ledger entry", extra={"tool_key": key})
