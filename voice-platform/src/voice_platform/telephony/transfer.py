"""Human transfers: destination rules, fallback chains, blind (SIP REFER) and warm transfers.

The manager never reports success unless the telephony operation succeeded:

* blind: LiveKit returns after the SIP REFER is accepted by the trunk (the caller is then
  bridged by the carrier; the agent leaves the call);
* warm: the human answered, received the briefing and confirmed via ``connect_to_caller``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..config.schema import TransferConfig, TransferDestination, TransferMode
from ..observability.metrics import TRANSFERS
from ..shared.logging import get_logger, mask_phone
from ..shared.phone import is_allowed_destination, try_normalize_e164
from ..tools.interfaces import TransferResult
from ..tools.scheduling import is_within_hours
from .sip import SipCallError, TelephonyTransport, to_sip_uri

logger = get_logger(__name__)

# LiveKit SIP error when the REFER transaction ended without the response it expected
_AMBIGUOUS_REFER = "transaction failed to complete"

WarmRunner = Callable[[TransferDestination, str], Awaitable[None]]
"""Runs LiveKit's WarmTransferTask for a destination with the handoff summary; raises on
failure (decline, voicemail, no answer)."""


class TransferManager:
    def __init__(
        self,
        *,
        config: TransferConfig,
        timezone: str,
        transport: TelephonyTransport | None,
        room_name: str,
        caller_identity: str | None,
        announce: Callable[[str], Awaitable[None]] | None = None,
        warm_runner: WarmRunner | None = None,
        dry_run: bool = False,
        allowed_prefixes: list[str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.cfg = config
        self.tz = ZoneInfo(timezone)
        self.transport = transport
        self.room_name = room_name
        self.caller_identity = caller_identity
        self.announce = announce
        self.warm_runner = warm_runner
        self.dry_run = dry_run
        self.allowed_prefixes = allowed_prefixes or []
        self.clock = clock
        self.on_event = on_event
        self.completed: TransferResult | None = None

    def destinations(self) -> list[TransferDestination]:
        return list(self.cfg.destinations)

    def _by_name(self, name: str) -> TransferDestination | None:
        return next((d for d in self.cfg.destinations if d.name == name), None)

    def _open_now(self, d: TransferDestination) -> bool:
        if d.hours is None or not d.hours.schedule:
            return True
        now = self.clock()
        return is_within_hours(d.hours, now, now, self.tz)

    def plan(self, requested: str | None) -> list[TransferDestination]:
        """Ordered attempt list: requested/default first, then its fallback chain (depth-first,
        de-duplicated), skipping destinations closed at this time."""
        start = self._by_name(requested) if requested else None
        if start is None and self.cfg.default_destination:
            start = self._by_name(self.cfg.default_destination)
        if start is None and self.cfg.destinations:
            start = self.cfg.destinations[0]
        out: list[TransferDestination] = []
        seen: set[str] = set()

        def visit(d: TransferDestination) -> None:
            if d.name in seen:
                return
            seen.add(d.name)
            if self._open_now(d):
                out.append(d)
            for f in d.fallback:
                nxt = self._by_name(f)
                if nxt is not None:
                    visit(nxt)

        if start is not None:
            visit(start)
        return out

    async def transfer(
        self, *, reason: str, destination: str | None, summary: str
    ) -> TransferResult:
        if self.completed is not None and self.completed.ok:
            return self.completed
        attempts: list[dict[str, Any]] = []
        plan = self.plan(destination)
        if not plan:
            TRANSFERS.labels("none", "unavailable").inc()
            return TransferResult(
                ok=False,
                status="unavailable",
                destination=None,
                mode=None,
                message=self.cfg.failure_message,
                attempts=attempts,
            )
        if self.announce is not None:
            try:
                await self.announce(self.cfg.announce_message)
            except Exception:
                logger.warning("transfer announcement failed", exc_info=True)

        last_status = "failed"
        for dest in plan:
            started = time.monotonic()
            status, error = await self._attempt(dest, summary)
            attempt = {
                "destination": dest.name,
                "mode": dest.mode.value,
                "status": status,
                "error": error,
                "duration_s": round(time.monotonic() - started, 2),
            }
            attempts.append(attempt)
            await self._emit({"type": "transfer_attempt", "reason": reason, **attempt})
            TRANSFERS.labels(dest.mode.value, status).inc()
            if status in ("completed", "dry_run"):
                result = TransferResult(
                    ok=True,
                    status=status,
                    destination=dest.name,
                    mode=dest.mode.value,
                    message="transferred",
                    attempts=attempts,
                )
                self.completed = result
                return result
            last_status = status
        return TransferResult(
            ok=False,
            status=last_status,
            destination=None,
            mode=None,
            message=self.cfg.failure_message,
            attempts=attempts,
        )

    async def _caller_left(self, wait_s: float = 6.0, interval_s: float = 0.5) -> bool:
        present = getattr(self.transport, "participant_present", None)
        if present is None or not self.caller_identity:
            return False
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if not await present(room_name=self.room_name, identity=self.caller_identity):
                return True
            await asyncio.sleep(interval_s)
        return False

    async def _attempt(self, dest: TransferDestination, summary: str) -> tuple[str, str | None]:
        target = dest.phone_number or dest.sip_uri or ""
        if dest.phone_number:
            e164 = try_normalize_e164(dest.phone_number)
            if not e164:
                return "failed", "invalid destination number"
            if not is_allowed_destination(e164, self.allowed_prefixes):
                return "rejected", "destination not allowed by outbound policy"
            target = e164
        if self.dry_run:
            logger.info(
                "dry-run transfer", extra={"destination": dest.name, "target": mask_phone(target)}
            )
            return "dry_run", None
        try:
            if dest.mode is TransferMode.WARM:
                if self.warm_runner is None:
                    return "failed", "warm transfer not supported in this session"
                await self.warm_runner(dest, summary)
            else:
                if self.transport is None or not self.caller_identity:
                    return "failed", "no telephony transport for this call"
                await self.transport.blind_transfer(
                    room_name=self.room_name,
                    participant_identity=self.caller_identity,
                    to_uri=to_sip_uri(target),
                    ringing_timeout_s=dest.ringing_timeout_s,
                )
            return "completed", None
        except SipCallError as e:
            if _AMBIGUOUS_REFER in e.message and await self._caller_left():
                # LiveKit SIP failed the API call because the transferee's NOTIFY overtook
                # the 202 (lab, LiveKit SIP v1.23.0), yet the REFER went through: the caller is
                # gone, so the transfer happened. Do not tell them nobody is available.
                logger.warning(
                    "transfer reported failed but the caller left: treating as completed",
                    extra={"destination": dest.name},
                )
                return "completed", None
            logger.warning(
                "transfer attempt failed",
                extra={"destination": dest.name, "status": e.status, "sip_code": e.sip_code},
            )
            return e.status, e.message
        except Exception as e:  # warm transfer raises ToolError on decline/voicemail
            text = str(e).lower()
            status = (
                "no_answer"
                if "voicemail" in text or "answer" in text
                else "rejected"
                if "declin" in text
                else "failed"
            )
            logger.warning(
                "transfer attempt failed", extra={"destination": dest.name, "error": str(e)[:200]}
            )
            return status, str(e)[:300]

    async def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                await self.on_event(event)
            except Exception:
                logger.warning("transfer event sink failed", exc_info=True)
