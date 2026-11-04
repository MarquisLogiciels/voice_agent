"""Phone-number provisioning on LiveKit SIP (inbound trunk + dispatch rule, outbound trunk).

LiveKit side, per number:
* inbound trunk accepting calls to the number (optionally restricted to carrier IPs);
* dispatch rule creating an individual room per call and dispatching the platform agent;
the worker then routes the call by the dialed number (``sip.trunkPhoneNumber``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..shared.logging import get_logger
from .sip import SipCallError, _wrap

logger = get_logger(__name__)



@dataclass(frozen=True)
class ProvisionedNumber:
    inbound_trunk_id: str
    dispatch_rule_id: str


class SipProvisioner:
    def __init__(self, lkapi: Any, *, agent_name: str) -> None:
        self._api = lkapi
        self._agent_name = agent_name

    async def provision_inbound(
        self, *, e164: str, label: str, allowed_addresses: list[str] | None = None,
        auth_username: str | None = None, auth_password: str | None = None,
        krisp: bool = True,
    ) -> ProvisionedNumber:
        from livekit import api

        trunk = api.SIPInboundTrunkInfo(name=f"vp:{label}:{e164}", numbers=[e164],
                                        krisp_enabled=krisp)
        if allowed_addresses:
            # e.g. the carrier's published SIP signaling CIDRs (keep in tenant/infra config;
            # carriers change them, so they are not hard-coded here)
            trunk.allowed_addresses.extend(allowed_addresses)
        if auth_username and auth_password:
            trunk.auth_username = auth_username
            trunk.auth_password = auth_password
        try:
            created = await self._api.sip.create_sip_inbound_trunk(
                api.CreateSIPInboundTrunkRequest(trunk=trunk))
        except Exception as e:
            raise _wrap(e, "create inbound trunk") from e
        rule = api.SIPDispatchRule(
            dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix="call-"))
        try:
            dr = await self._api.sip.create_sip_dispatch_rule(api.CreateSIPDispatchRuleRequest(
                dispatch_rule=api.SIPDispatchRuleInfo(
                    rule=rule, trunk_ids=[created.sip_trunk_id], name=f"vp:{label}:{e164}",
                    room_config=api.RoomConfiguration(
                        agents=[api.RoomAgentDispatch(agent_name=self._agent_name)]),
                )))
        except Exception as e:
            # don't leave a dangling trunk behind
            await self._safe_delete_trunk(created.sip_trunk_id)
            raise _wrap(e, "create dispatch rule") from e
        return ProvisionedNumber(inbound_trunk_id=created.sip_trunk_id,
                                 dispatch_rule_id=dr.sip_dispatch_rule_id)

    async def create_outbound_trunk(
        self, *, label: str, address: str, numbers: list[str], auth_username: str | None,
        auth_password: str | None,
    ) -> str:
        from livekit import api

        trunk = api.SIPOutboundTrunkInfo(name=f"vp:{label}", address=address, numbers=numbers)
        if auth_username and auth_password:
            trunk.auth_username = auth_username
            trunk.auth_password = auth_password
        try:
            created = await self._api.sip.create_sip_outbound_trunk(
                api.CreateSIPOutboundTrunkRequest(trunk=trunk))
        except Exception as e:
            raise _wrap(e, "create outbound trunk") from e
        return str(created.sip_trunk_id)

    async def deprovision(self, *, inbound_trunk_id: str | None, dispatch_rule_id: str | None
                          ) -> None:
        from livekit import api

        if dispatch_rule_id:
            try:
                await self._api.sip.delete_sip_dispatch_rule(
                    api.DeleteSIPDispatchRuleRequest(sip_dispatch_rule_id=dispatch_rule_id))
            except Exception as e:
                raise _wrap(e, "delete dispatch rule") from e
        if inbound_trunk_id:
            await self._safe_delete_trunk(inbound_trunk_id)

    async def _safe_delete_trunk(self, trunk_id: str) -> None:
        from livekit import api

        try:
            await self._api.sip.delete_sip_trunk(api.DeleteSIPTrunkRequest(sip_trunk_id=trunk_id))
        except Exception:
            logger.warning("failed to delete SIP trunk", extra={"trunk_id": trunk_id},
                           exc_info=True)


__all__ = ["ProvisionedNumber", "SipCallError", "SipProvisioner"]
