"""Agents, versions, validation, deployments."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...config.schema import AgentConfig
from ...config.service import ENVIRONMENTS, AgentService, parse_config
from ...core.models import AgentRow, AgentVersion, Deployment
from ...core.rbac import Permission as P
from ...providers.types import BuildContext
from .deps import TenantCtx, tenant_ctx

router = APIRouter(prefix="/v1/tenants/{tenant_id}")


def _agent(a: AgentRow) -> dict[str, Any]:
    return {"id": str(a.id), "slug": a.slug, "name": a.name, "description": a.description,
            "created_at": a.created_at.isoformat()}


def _version(v: AgentVersion, *, with_config: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(v.id), "agent_id": str(v.agent_id), "version": v.version, "status": v.status,
        "source": v.source, "notes": v.notes, "config_hash": v.config_hash,
        "parent_version_id": str(v.parent_version_id) if v.parent_version_id else None,
        "created_at": v.created_at.isoformat(),
        "published_at": v.published_at.isoformat() if v.published_at else None,
    }
    if with_config:
        out["config"] = v.config
    return out


def _deployment(d: Deployment) -> dict[str, Any]:
    return {"environment": d.environment, "version_id": str(d.version_id),
            "canary_version_id": str(d.canary_version_id) if d.canary_version_id else None,
            "canary_percent": d.canary_percent, "updated_at": d.updated_at.isoformat()}


class AgentIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,62}$")
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    config: dict[str, Any]


@router.post("/agents", status_code=201)
async def create_agent(body: AgentIn, ctx: TenantCtx = Depends(tenant_ctx(P.AGENTS_WRITE))
                       ) -> dict[str, Any]:
    agent, version = await AgentService(ctx.session, ctx.tenant_id, ctx.principal).create_agent(
        slug=body.slug, name=body.name, description=body.description, config=body.config)
    return {**_agent(agent), "draft": _version(version)}


@router.get("/agents")
async def list_agents(ctx: TenantCtx = Depends(tenant_ctx(P.AGENTS_READ))) -> list[dict[str, Any]]:
    svc = AgentService(ctx.session, ctx.tenant_id, ctx.principal)
    out = []
    for a in await svc.list_agents():
        deps = await svc.list_deployments(a.id)
        out.append({**_agent(a), "deployments": [_deployment(d) for d in deps]})
    return out


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: uuid.UUID, ctx: TenantCtx = Depends(tenant_ctx(P.AGENTS_READ))
                    ) -> dict[str, Any]:
    svc = AgentService(ctx.session, ctx.tenant_id, ctx.principal)
    a = await svc.get_agent(agent_id)
    versions = await svc.list_versions(agent_id)
    return {**_agent(a), "versions": [_version(v) for v in versions],
            "deployments": [_deployment(d) for d in await svc.list_deployments(agent_id)]}


@router.delete("/agents/{agent_id}", status_code=204)
async def archive_agent(agent_id: uuid.UUID,
                        ctx: TenantCtx = Depends(tenant_ctx(P.AGENTS_PUBLISH))) -> None:
    await AgentService(ctx.session, ctx.tenant_id, ctx.principal).archive_agent(agent_id)


class ConfigIn(BaseModel):
    config: dict[str, Any]
    notes: str = ""
    parent_version_id: uuid.UUID | None = None


@router.post("/agents/{agent_id}/versions", status_code=201)
async def create_version(agent_id: uuid.UUID, body: ConfigIn,
                         ctx: TenantCtx = Depends(tenant_ctx(P.AGENTS_WRITE))) -> dict[str, Any]:
    v = await AgentService(ctx.session, ctx.tenant_id, ctx.principal).create_draft(
        agent_id, body.config, notes=body.notes, parent_version_id=body.parent_version_id)
    return _version(v, with_config=True)


@router.get("/versions/{version_id}")
async def get_version(version_id: uuid.UUID,
                      ctx: TenantCtx = Depends(tenant_ctx(P.AGENTS_READ))) -> dict[str, Any]:
    v = await AgentService(ctx.session, ctx.tenant_id, ctx.principal).get_version(version_id)
    return _version(v, with_config=True)


class DraftPatch(BaseModel):
    config: dict[str, Any]
    notes: str | None = None


@router.put("/versions/{version_id}")
async def update_draft(version_id: uuid.UUID, body: DraftPatch,
                       ctx: TenantCtx = Depends(tenant_ctx(P.AGENTS_WRITE))) -> dict[str, Any]:
    v = await AgentService(ctx.session, ctx.tenant_id, ctx.principal).update_draft(
        version_id, body.config, body.notes)
    return _version(v, with_config=True)


@router.post("/versions/{version_id}/publish")
async def publish(version_id: uuid.UUID, ctx: TenantCtx = Depends(tenant_ctx(P.AGENTS_PUBLISH))
                  ) -> dict[str, Any]:
    v = await AgentService(ctx.session, ctx.tenant_id, ctx.principal).publish(version_id)
    return _version(v)


@router.get("/versions/{from_id}/diff/{to_id}")
async def diff(from_id: uuid.UUID, to_id: uuid.UUID,
               ctx: TenantCtx = Depends(tenant_ctx(P.AGENTS_READ))) -> list[dict[str, Any]]:
    changes = await AgentService(ctx.session, ctx.tenant_id, ctx.principal).diff(from_id, to_id)
    return [c.as_dict() for c in changes]


class ValidateIn(BaseModel):
    config: dict[str, Any]
    environment: str = "prod"


@router.post("/agents/validate")
async def validate(body: ValidateIn, ctx: TenantCtx = Depends(tenant_ctx(P.AGENTS_READ))
                   ) -> dict[str, Any]:
    """Schema validation + provider/capability checks without saving anything."""
    cfg: AgentConfig = parse_config(body.config).materialize(body.environment)
    problems = ctx.services.builder.validate(cfg.pipeline, BuildContext(), check_secrets=False)
    return {"valid": not [p for p in problems if p.severity == "error"],
            "problems": [p.as_dict() for p in problems]}


class DeployIn(BaseModel):
    version_id: uuid.UUID


class CanaryIn(BaseModel):
    version_id: uuid.UUID | None
    percent: float = Field(ge=0, le=100)


class RollbackIn(BaseModel):
    version_id: uuid.UUID | None = None


def _env(environment: str) -> str:
    from ...shared.errors import ValidationFailedError

    if environment not in ENVIRONMENTS:
        raise ValidationFailedError(f"environment must be one of {ENVIRONMENTS}")
    return environment


@router.put("/agents/{agent_id}/deployments/{environment}")
async def deploy(agent_id: uuid.UUID, environment: str, body: DeployIn,
                 ctx: TenantCtx = Depends(tenant_ctx(P.DEPLOYMENTS_WRITE))) -> dict[str, Any]:
    d = await AgentService(ctx.session, ctx.tenant_id, ctx.principal).deploy(
        agent_id, _env(environment), body.version_id)
    return _deployment(d)


@router.put("/agents/{agent_id}/deployments/{environment}/canary")
async def canary(agent_id: uuid.UUID, environment: str, body: CanaryIn,
                 ctx: TenantCtx = Depends(tenant_ctx(P.DEPLOYMENTS_WRITE))) -> dict[str, Any]:
    d = await AgentService(ctx.session, ctx.tenant_id, ctx.principal).set_canary(
        agent_id, _env(environment), body.version_id, body.percent)
    return _deployment(d)


@router.post("/agents/{agent_id}/deployments/{environment}/promote")
async def promote(agent_id: uuid.UUID, environment: str,
                  ctx: TenantCtx = Depends(tenant_ctx(P.DEPLOYMENTS_WRITE))) -> dict[str, Any]:
    d = await AgentService(ctx.session, ctx.tenant_id, ctx.principal).promote_canary(
        agent_id, _env(environment))
    return _deployment(d)


@router.post("/agents/{agent_id}/deployments/{environment}/rollback")
async def rollback(agent_id: uuid.UUID, environment: str, body: RollbackIn,
                   ctx: TenantCtx = Depends(tenant_ctx(P.DEPLOYMENTS_WRITE))) -> dict[str, Any]:
    d = await AgentService(ctx.session, ctx.tenant_id, ctx.principal).rollback(
        agent_id, _env(environment), body.version_id)
    return _deployment(d)
