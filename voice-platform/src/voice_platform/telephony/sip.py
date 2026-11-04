"""LiveKit SIP/room/egress operations behind a small, mockable transport interface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from ..shared.errors import ConfigurationError
from ..shared.logging import get_logger, mask_phone
from ..shared.settings import Settings

logger = get_logger(__name__)


class SipCallError(Exception):
    """A SIP operation failed. ``status`` is normalized; ``sip_code`` is raw when known."""

    def __init__(self, status: str, message: str, *, sip_code: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # busy | no_answer | rejected | unavailable | failed
        self.message = message
        self.sip_code = sip_code


def map_sip_status(code: int | None) -> str:
    if code is None:
        return "failed"
    if code in (486, 600):
        return "busy"
    if code in (408, 480, 487):
        return "no_answer"
    if code in (403, 603, 606):
        return "rejected"
    if code in (404, 410, 484, 502, 503, 504):
        return "unavailable"
    return "failed"


def _sip_code(err: Exception) -> int | None:
    meta = getattr(err, "metadata", None) or {}
    for key in ("sip_status_code", "sip_status", "sipStatusCode"):
        raw = meta.get(key)
        if raw and str(raw).isdigit():
            return int(raw)
    status = getattr(err, "status", None)
    if isinstance(status, int) and 300 <= status < 700 and status not in (400, 401, 404, 500):
        return status
    return None


def _wrap(err: Exception, what: str) -> SipCallError:
    code = _sip_code(err)
    return SipCallError(
        map_sip_status(code), f"{what} failed: {getattr(err, 'message', err)}", sip_code=code
    )


@dataclass(frozen=True)
class DialResult:
    participant_identity: str
    sip_call_id: str | None


class TelephonyTransport(Protocol):
    async def blind_transfer(
        self,
        *,
        room_name: str,
        participant_identity: str,
        to_uri: str,
        ringing_timeout_s: float,
        play_dialtone: bool = True,
    ) -> None: ...

    async def dial(
        self,
        *,
        room_name: str,
        to_number: str,
        trunk_id: str,
        from_number: str | None,
        participant_identity: str,
        ringing_timeout_s: float,
        metadata: dict[str, Any] | None = None,
        wait_until_answered: bool = True,
    ) -> DialResult: ...

    async def hangup(self, *, room_name: str) -> None: ...


class LiveKitTransport:
    """Real implementation over ``livekit.api.LiveKitAPI``."""

    def __init__(self, lkapi: Any) -> None:
        self._api = lkapi

    @classmethod
    def from_settings(cls, settings: Settings) -> LiveKitTransport:
        return cls(livekit_api(settings))

    async def aclose(self) -> None:
        await self._api.aclose()

    async def blind_transfer(
        self,
        *,
        room_name: str,
        participant_identity: str,
        to_uri: str,
        ringing_timeout_s: float,
        play_dialtone: bool = True,
    ) -> None:
        from livekit import api

        req = api.TransferSIPParticipantRequest(
            room_name=room_name,
            participant_identity=participant_identity,
            transfer_to=to_uri,
            play_dialtone=play_dialtone,
        )
        req.ringing_timeout.FromNanoseconds(int(ringing_timeout_s * 1e9))
        try:
            await self._api.sip.transfer_sip_participant(req, timeout=ringing_timeout_s + 10)
        except Exception as e:
            raise _wrap(e, "SIP transfer") from e

    async def dial(
        self,
        *,
        room_name: str,
        to_number: str,
        trunk_id: str,
        from_number: str | None,
        participant_identity: str,
        ringing_timeout_s: float,
        metadata: dict[str, Any] | None = None,
        wait_until_answered: bool = True,
    ) -> DialResult:
        from livekit import api

        req = api.CreateSIPParticipantRequest(
            sip_trunk_id=trunk_id,
            sip_call_to=to_number,
            room_name=room_name,
            participant_identity=participant_identity,
            participant_name="Caller",
            participant_metadata=json.dumps(metadata or {}),
            wait_until_answered=wait_until_answered,
            play_dialtone=False,
            krisp_enabled=True,
        )
        if from_number:
            req.sip_number = from_number
        req.ringing_timeout.FromNanoseconds(int(ringing_timeout_s * 1e9))
        try:
            info = await self._api.sip.create_sip_participant(req)
        except Exception as e:
            raise _wrap(e, "outbound dial") from e
        logger.info("outbound call answered", extra={"to": mask_phone(to_number)})
        return DialResult(
            participant_identity=info.participant_identity or participant_identity,
            sip_call_id=getattr(info, "sip_call_id", None),
        )

    async def hangup(self, *, room_name: str) -> None:
        from livekit import api

        try:
            await self._api.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:
            logger.warning("delete_room failed", exc_info=True)


def livekit_api(settings: Settings) -> Any:
    from livekit import api

    if not (settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret):
        raise ConfigurationError(
            "VP_LIVEKIT_URL / VP_LIVEKIT_API_KEY / VP_LIVEKIT_API_SECRET are required for telephony"
        )
    return api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key.get_secret_value(),
        api_secret=settings.livekit_api_secret.get_secret_value(),
    )


def to_sip_uri(number_or_uri: str) -> str:
    if number_or_uri.startswith(("sip:", "sips:", "tel:")):
        return number_or_uri
    return f"tel:{number_or_uri}"
