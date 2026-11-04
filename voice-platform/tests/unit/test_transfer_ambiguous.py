"""LiveKit SIP can fail the transfer API call while the REFER actually succeeds (the
transferee's NOTIFY overtook the 202 — seen in the SIP lab). The platform must not tell a
caller who has already been transferred that nobody is available."""

from __future__ import annotations

from typing import Any

from voice_platform.config.schema import TransferConfig
from voice_platform.telephony.sip import SipCallError
from voice_platform.telephony.transfer import TransferManager


class RacyTransport:
    def __init__(self, *, caller_leaves: bool) -> None:
        self.caller_leaves = caller_leaves
        self.refers = 0

    async def blind_transfer(self, **kw: Any) -> None:
        self.refers += 1
        raise SipCallError("no_answer", "SIP transfer failed: transaction failed to complete")

    async def participant_present(self, *, room_name: str, identity: str) -> bool:
        return not self.caller_leaves

    async def dial(self, **kw: Any) -> Any: ...
    async def hangup(self, **kw: Any) -> None: ...


def _manager(transport: RacyTransport) -> TransferManager:
    cfg = TransferConfig.model_validate(
        {
            "enabled": True,
            "destinations": [
                {"name": "desk", "phone_number": "+15145550177", "fallback": ["backup"]},
                {"name": "backup", "phone_number": "+15145550178"},
            ],
        }
    )
    tm = TransferManager(
        config=cfg,
        timezone="America/Toronto",
        transport=transport,
        room_name="call-x",
        caller_identity="sip_caller",
    )
    return tm


async def test_ambiguous_failure_with_caller_gone_is_a_completed_transfer() -> None:
    t = RacyTransport(caller_leaves=True)
    res = await _manager(t).transfer(reason="asked", destination=None, summary="wants a human")
    assert res.ok and res.status == "completed" and res.destination == "desk"
    assert t.refers == 1  # no second REFER to the fallback for an already-transferred caller


async def test_ambiguous_failure_with_caller_still_here_stays_a_failure() -> None:
    t = RacyTransport(caller_leaves=False)
    tm = _manager(t)
    tm._caller_left = lambda wait_s=0.0, interval_s=0.0: _false()  # type: ignore[method-assign]
    res = await tm.transfer(reason="asked", destination=None, summary="wants a human")
    assert not res.ok and t.refers == 2  # fallback tried


async def _false() -> bool:
    return False
