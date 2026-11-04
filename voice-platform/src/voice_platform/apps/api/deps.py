"""FastAPI dependencies: services, authentication, tenant-scoped transactions, RBAC."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.auth import Authenticator
from ...core.db import DbScope
from ...core.rbac import Permission, Principal, Role
from ...shared.errors import AuthenticationError, RateLimitedError
from ...shared.logging import bind_log_context
from ..services import Services


def get_services(request: Request) -> Services:
    return request.app.state.services  # type: ignore[no-any-return]


ServicesDep = Annotated[Services, Depends(get_services)]


async def get_principal(
    request: Request,
    services: ServicesDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("missing bearer token")
    token = authorization[7:].strip()
    principal = await Authenticator(services.db, services.settings).authenticate(token)
    decision = await services.rate_limiter.hit(
        f"api:{principal.actor_label}", limit=services.settings.api_rate_limit_per_minute
    )
    if not decision.allowed:
        raise RateLimitedError("rate limit exceeded", retry_after_s=decision.reset_in_s)
    bind_log_context(actor=principal.actor_label)
    request.state.principal = principal
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]


@dataclass
class TenantCtx:
    tenant_id: uuid.UUID
    principal: Principal
    role: Role
    session: AsyncSession
    services: Services


def tenant_ctx(permission: Permission) -> Callable[..., Any]:
    """Dependency: authenticated principal with ``permission`` in the path tenant, and one
    database transaction bound to that tenant (RLS)."""

    async def dep(
        services: ServicesDep,
        principal: PrincipalDep,
        tenant_id: Annotated[uuid.UUID, Path()],
    ) -> AsyncIterator[TenantCtx]:
        role = principal.require(tenant_id, permission)
        bind_log_context(tenant_id=str(tenant_id))
        async with services.db.session(DbScope.for_tenant(tenant_id, principal.user_id)) as s:
            yield TenantCtx(
                tenant_id=tenant_id, principal=principal, role=role, session=s, services=services
            )

    return dep


def T(permission: Permission) -> Any:
    return Annotated[TenantCtx, Depends(tenant_ctx(permission))]
