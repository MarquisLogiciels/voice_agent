"""Auth, tenants, members, API keys, audit log, provider catalogue."""

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field

from ...core.audit import record_audit
from ...core.auth import Authenticator
from ...core.db import DbScope
from ...core.models import ApiKey, AuditLog, Membership, Tenant, User
from ...core.rbac import Permission as P
from ...core.rbac import Role
from ...providers.health import global_tracker, run_health_checks
from ...providers.types import ProviderKind
from ...shared.crypto import generate_api_key, hash_password
from ...shared.errors import ConflictError, NotFoundError, PermissionDeniedError, RateLimitedError
from .deps import PrincipalDep, ServicesDep, TenantCtx, tenant_ctx

router = APIRouter(prefix="/v1")


# --- auth -------------------------------------------------------------------------------------
class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, services: ServicesDep) -> dict[str, Any]:
    ip = request.client.host if request.client else "unknown"
    for key in (f"login:ip:{ip}", f"login:email:{body.email.lower()}"):
        decision = await services.rate_limiter.hit(
            key, limit=services.settings.login_rate_limit_per_minute)
        if not decision.allowed:
            raise RateLimitedError("too many login attempts", retry_after_s=decision.reset_in_s)
    token = await Authenticator(services.db, services.settings).login(body.email, body.password)
    return {"access_token": token.access_token, "token_type": "bearer",
            "expires_at": token.expires_at}


@router.get("/me")
async def me(principal: PrincipalDep, services: ServicesDep) -> dict[str, Any]:
    tenants: list[dict[str, Any]] = []
    if principal.is_platform_admin:
        async with services.db.session(DbScope.system()) as s:
            for t in (await s.scalars(sa.select(Tenant).order_by(Tenant.name))).all():
                tenants.append({"id": str(t.id), "slug": t.slug, "name": t.name,
                                "role": Role.OWNER.value})
    elif principal.kind == "user" and principal.user_id is not None:
        # narrow lookup function: only the caller's own memberships cross the RLS boundary
        async with services.db.session(DbScope(user_id=principal.user_id)) as s:
            rows = (await s.execute(sa.text("select * from vp_user_memberships(:u)"),
                                    {"u": principal.user_id})).all()
        tenants = [{"id": str(r.tenant_id), "slug": r.slug, "name": r.name, "role": r.role}
                   for r in sorted(rows, key=lambda r: r.name) if r.status == "active"]
    else:
        tenants = [{"id": str(t), "role": r.value} for t, r in principal.memberships.items()]
    return {"kind": principal.kind, "id": principal.id,
            "is_platform_admin": principal.is_platform_admin, "tenants": tenants}


# --- tenants ----------------------------------------------------------------------------------
class TenantIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,62}$")
    name: str = Field(min_length=1, max_length=200)
    retention_days: int = Field(default=365, ge=1, le=3650)
    data_region: str = "ca"
    owner_email: EmailStr | None = None
    owner_password: str | None = Field(default=None, min_length=12, max_length=200)


def _tenant_out(t: Tenant) -> dict[str, Any]:
    return {"id": str(t.id), "slug": t.slug, "name": t.name, "status": t.status,
            "retention_days": t.retention_days, "data_region": t.data_region,
            "settings": t.settings, "created_at": t.created_at.isoformat()}


@router.post("/tenants", status_code=201)
async def create_tenant(body: TenantIn, principal: PrincipalDep, services: ServicesDep
                        ) -> dict[str, Any]:
    principal.require_platform_admin()
    async with services.db.session(DbScope.system()) as s:
        if await s.scalar(sa.select(Tenant.id).where(Tenant.slug == body.slug)):
            raise ConflictError("tenant slug already exists")
        t = Tenant(slug=body.slug, name=body.name, retention_days=body.retention_days,
                   data_region=body.data_region)
        s.add(t)
        await s.flush()
        if body.owner_email and body.owner_password:
            user = await s.scalar(sa.select(User).where(User.email == body.owner_email.lower()))
            if user is None:
                user = User(email=body.owner_email.lower(),
                            password_hash=hash_password(body.owner_password))
                s.add(user)
                await s.flush()
            s.add(Membership(tenant_id=t.id, user_id=user.id, role=Role.OWNER.value))
        await record_audit(s, tenant_id=t.id, actor=principal, action="tenant.create",
                           resource_type="tenant", resource_id=t.id)
        return _tenant_out(t)


@router.get("/tenants")
async def list_tenants(principal: PrincipalDep, services: ServicesDep) -> list[dict[str, Any]]:
    principal.require_platform_admin()
    async with services.db.session(DbScope.system()) as s:
        return [_tenant_out(t) for t in (await s.scalars(sa.select(Tenant)
                                                         .order_by(Tenant.created_at))).all()]


@router.get("/tenants/{tenant_id}")
async def get_tenant(ctx: TenantCtx = Depends(tenant_ctx(P.TENANT_READ))) -> dict[str, Any]:
    t = await ctx.session.get(Tenant, ctx.tenant_id)
    if t is None:
        raise NotFoundError("tenant not found")
    return _tenant_out(t)


class TenantPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    settings: dict[str, Any] | None = None


@router.patch("/tenants/{tenant_id}")
async def update_tenant(body: TenantPatch, ctx: TenantCtx = Depends(tenant_ctx(P.TENANT_UPDATE))
                        ) -> dict[str, Any]:
    # tenants table only allows writes in platform scope; the permission check is done above
    async with ctx.services.db.session(DbScope.system()) as s:
        t = await s.get(Tenant, ctx.tenant_id)
        if t is None:
            raise NotFoundError("tenant not found")
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(t, field, value)
        await record_audit(s, tenant_id=t.id, actor=ctx.principal, action="tenant.update",
                           resource_type="tenant", resource_id=t.id,
                           details=body.model_dump(exclude_none=True))
        return _tenant_out(t)


# --- members ----------------------------------------------------------------------------------
class MemberIn(BaseModel):
    email: EmailStr
    role: Role
    password: str | None = Field(default=None, min_length=12, max_length=200)
    full_name: str = ""


@router.get("/tenants/{tenant_id}/members")
async def list_members(ctx: TenantCtx = Depends(tenant_ctx(P.TENANT_READ))
                       ) -> list[dict[str, Any]]:
    rows = (await ctx.session.execute(
        sa.select(Membership, User).join(User, User.id == Membership.user_id)
        .where(Membership.tenant_id == ctx.tenant_id))).all()
    return [{"user_id": str(u.id), "email": u.email, "full_name": u.full_name, "role": m.role}
            for m, u in rows]


@router.post("/tenants/{tenant_id}/members", status_code=201)
async def add_member(body: MemberIn, ctx: TenantCtx = Depends(tenant_ctx(P.MEMBERS_MANAGE))
                     ) -> dict[str, Any]:
    if body.role is Role.OWNER and ctx.role is not Role.OWNER:
        raise PermissionDeniedError("only owners can add owners")
    async with ctx.services.db.session(DbScope.system()) as s:
        user = await s.scalar(sa.select(User).where(User.email == body.email.lower()))
        if user is None:
            if not body.password:
                raise NotFoundError("user does not exist; provide an initial password")
            user = User(email=body.email.lower(), password_hash=hash_password(body.password),
                        full_name=body.full_name)
            s.add(user)
            await s.flush()
        exists = await s.scalar(sa.select(Membership.id).where(
            Membership.tenant_id == ctx.tenant_id, Membership.user_id == user.id))
        if exists:
            raise ConflictError("already a member")
        s.add(Membership(tenant_id=ctx.tenant_id, user_id=user.id, role=body.role.value))
        await record_audit(s, tenant_id=ctx.tenant_id, actor=ctx.principal, action="member.add",
                           resource_type="user", resource_id=user.id,
                           details={"role": body.role.value})
        return {"user_id": str(user.id), "email": user.email, "role": body.role.value}


@router.delete("/tenants/{tenant_id}/members/{user_id}", status_code=204)
async def remove_member(user_id: uuid.UUID,
                        ctx: TenantCtx = Depends(tenant_ctx(P.MEMBERS_MANAGE))) -> None:
    m = await ctx.session.scalar(sa.select(Membership).where(
        Membership.tenant_id == ctx.tenant_id, Membership.user_id == user_id))
    if m is None:
        raise NotFoundError("membership not found")
    if m.role == Role.OWNER.value and ctx.role is not Role.OWNER:
        raise PermissionDeniedError("only owners can remove owners")
    await ctx.session.delete(m)
    await record_audit(ctx.session, tenant_id=ctx.tenant_id, actor=ctx.principal,
                       action="member.remove", resource_type="user", resource_id=user_id)


# --- API keys ---------------------------------------------------------------------------------
class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: Role = Role.EDITOR
    expires_in_days: int | None = Field(default=365, ge=1, le=3650)


@router.post("/tenants/{tenant_id}/api-keys", status_code=201)
async def create_api_key(body: ApiKeyIn, ctx: TenantCtx = Depends(tenant_ctx(P.API_KEYS_MANAGE))
                         ) -> dict[str, Any]:
    if body.role is Role.OWNER:
        raise PermissionDeniedError("API keys cannot have the owner role")
    gen = generate_api_key()
    expires = None
    if body.expires_in_days:
        from datetime import timedelta

        expires = datetime.now(UTC) + timedelta(days=body.expires_in_days)
    key = ApiKey(tenant_id=ctx.tenant_id, name=body.name, prefix=gen.prefix,
                 key_hash=gen.digest, role=body.role.value, created_by=ctx.principal.user_id,
                 expires_at=expires)
    ctx.session.add(key)
    await ctx.session.flush()
    await record_audit(ctx.session, tenant_id=ctx.tenant_id, actor=ctx.principal,
                       action="api_key.create", resource_type="api_key", resource_id=key.id)
    return {"id": str(key.id), "name": key.name, "role": key.role, "key": gen.plaintext,
            "expires_at": expires.isoformat() if expires else None,
            "note": "Store this key now; it cannot be shown again."}


@router.get("/tenants/{tenant_id}/api-keys")
async def list_api_keys(ctx: TenantCtx = Depends(tenant_ctx(P.API_KEYS_MANAGE))
                        ) -> list[dict[str, Any]]:
    keys = (await ctx.session.scalars(sa.select(ApiKey).where(
        ApiKey.tenant_id == ctx.tenant_id).order_by(ApiKey.created_at))).all()
    return [{"id": str(k.id), "name": k.name, "role": k.role, "prefix": k.prefix,
             "revoked": k.revoked_at is not None,
             "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
             "expires_at": k.expires_at.isoformat() if k.expires_at else None} for k in keys]


@router.delete("/tenants/{tenant_id}/api-keys/{key_id}", status_code=204)
async def revoke_api_key(key_id: uuid.UUID,
                         ctx: TenantCtx = Depends(tenant_ctx(P.API_KEYS_MANAGE))) -> None:
    key = await ctx.session.scalar(sa.select(ApiKey).where(ApiKey.id == key_id,
                                                           ApiKey.tenant_id == ctx.tenant_id))
    if key is None:
        raise NotFoundError("API key not found")
    key.revoked_at = datetime.now(UTC)
    await record_audit(ctx.session, tenant_id=ctx.tenant_id, actor=ctx.principal,
                       action="api_key.revoke", resource_type="api_key", resource_id=key_id)


# --- audit ------------------------------------------------------------------------------------
@router.get("/tenants/{tenant_id}/audit")
async def audit_log(limit: int = 100, ctx: TenantCtx = Depends(tenant_ctx(P.AUDIT_READ))
                    ) -> list[dict[str, Any]]:
    rows = (await ctx.session.scalars(sa.select(AuditLog).where(
        AuditLog.tenant_id == ctx.tenant_id).order_by(AuditLog.id.desc())
        .limit(min(limit, 500)))).all()
    return [{"id": r.id, "at": r.created_at.isoformat(), "actor": f"{r.actor_type}:{r.actor_id}",
             "action": r.action, "resource": f"{r.resource_type}:{r.resource_id}",
             "details": r.details} for r in rows]


# --- providers --------------------------------------------------------------------------------
@router.get("/providers")
async def provider_catalog(principal: PrincipalDep, services: ServicesDep,
                           kind: ProviderKind | None = None) -> list[dict[str, Any]]:
    return [d.metadata() for d in services.registry.list(kind)]


@router.get("/providers/health")
async def provider_health(principal: PrincipalDep, services: ServicesDep,
                          probe: bool = False) -> dict[str, Any]:
    """Circuit state seen by this process; ``probe=true`` (platform admins) runs no-cost
    authenticated probes with the platform-level keys."""
    out: dict[str, Any] = {"circuits": [h.__dict__ for h in global_tracker().snapshot()]}
    if probe:
        principal.require_platform_admin()
        import os

        checks: dict[str, Any] = {}
        for d in services.registry.list():
            if d.health_check is None:
                continue
            key = next((os.environ[e] for e in d.secret_env if os.environ.get(e)), None)
            if key:
                checks[f"{d.kind.value}/{d.name}"] = (d.health_check, key)
        out["probes"] = await run_health_checks(checks)
    return out
