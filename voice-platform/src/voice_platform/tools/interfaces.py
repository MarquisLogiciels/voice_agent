"""Interfaces the tools layer depends on (implemented by telephony and knowledge)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from ..config.schema import AgentConfig, TransferDestination
from ..integrations.base import TimeSlot
from ..integrations.registry import IntegrationSet
from ..knowledge.types import KnowledgeSearch
from .ledger import ToolLedger


@dataclass
class TransferResult:
    ok: bool
    status: str  # completed | failed | busy | no_answer | unavailable | rejected | dry_run
    destination: str | None
    mode: str | None
    message: str
    attempts: list[dict[str, Any]] = field(default_factory=list)


class TransferController(Protocol):
    def destinations(self) -> list[TransferDestination]: ...

    async def transfer(
        self, *, reason: str, destination: str | None, summary: str
    ) -> TransferResult: ...


@dataclass
class ConversationState:
    tool_calls: int = 0
    consecutive_failures: int = 0
    total_failures: int = 0
    sms_sent: int = 0
    offered_slots: list[TimeSlot] = field(default_factory=list)
    identified_contact_id: str | None = None
    transfer_attempted: bool = False
    transfer_result: TransferResult | None = None


@dataclass
class ToolEnv:
    tenant_id: uuid.UUID
    agent_id: uuid.UUID | None
    conversation_id: str
    config: AgentConfig
    integrations: IntegrationSet
    ledger: ToolLedger
    caller_number: str | None = None
    transfer: TransferController | None = None
    knowledge: KnowledgeSearch | None = None
    state: ConversationState = field(default_factory=ConversationState)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    on_event: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
    sms_from_number: str | None = None
