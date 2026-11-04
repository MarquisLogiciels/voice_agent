"""Agents, immutable versions, deployments, canaries, rollback and per-call resolution."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import record_audit
from ..core.models import AgentRow, AgentVersion, Deployment, DeploymentEvent
from ..core.rbac import Principal
from ..shared.errors import ConflictError, NotFoundError, ValidationFailedError
from .diff import Change, diff_configs
from .schema import AgentConfig, config_hash

ENVIRONMENTS = ("dev", "staging", "prod")


def parse_config(data: dict[str, Any]) -> AgentConfig:
    try:
        return AgentConfig.model_validate(data)
    except ValidationError as e:
        raise ValidationFailedError(
            "invalid agent configuration",
            details={
                "errors": [
                    {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
                    for err in e.errors()
                ]
            },
        ) from e


@dataclass(frozen=True)
class ResolvedVersion:
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    version_id: uuid.UUID
    version: int
    environment: str
    config: AgentConfig
    is_canary: bool


def canary_bucket(call_id: str) -> float:
    """Stable value in [0, 100) derived from the call id."""
    digest = hashlib.sha256(call_id.encode()).digest()
    return (int.from_bytes(digest[:8], "big") % 10_000) / 100.0


class AgentService:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID, actor: Principal | None):
        self.s = session
        self.tenant_id = tenant_id
        self.actor = actor

    @property
    def _actor_id(self) -> uuid.UUID | None:
        return self.actor.user_id if self.actor else None

    # --- agents ------------------------------------------------------------------------------
    async def create_agent(
        self, *, slug: str, name: str, description: str, config: dict[str, Any]
    ) -> tuple[AgentRow, AgentVersion]:
        parsed = parse_config(config)
        exists = await self.s.scalar(
            sa.select(AgentRow.id).where(AgentRow.tenant_id == self.tenant_id, AgentRow.slug == slug)
        )
        if exists:
            raise ConflictError(f"agent '{slug}' already exists")
        agent = AgentRow(tenant_id=self.tenant_id, slug=slug, name=name, description=description)
        self.s.add(agent)
        await self.s.flush()
        version = await self._new_version(agent.id, parsed, status="draft", source="manual",
                                          parent=None, notes="initial draft")
        await record_audit(self.s, tenant_id=self.tenant_id, actor=self.actor,
                           action="agent.create", resource_type="agent", resource_id=agent.id)
        return agent, version

    async def get_agent(self, agent_id: uuid.UUID) -> AgentRow:
        agent = await self.s.scalar(
            sa.select(AgentRow).where(AgentRow.id == agent_id, AgentRow.tenant_id == self.tenant_id)
        )
        if agent is None:
            raise NotFoundError("agent not found")
        return agent

    async def list_agents(self) -> list[AgentRow]:
        return list(
            (
                await self.s.scalars(
                    sa.select(AgentRow)
                    .where(AgentRow.tenant_id == self.tenant_id, AgentRow.archived_at.is_(None))
                    .order_by(AgentRow.created_at)
                )
            ).all()
        )

    async def archive_agent(self, agent_id: uuid.UUID) -> None:
        agent = await self.get_agent(agent_id)
        agent.archived_at = datetime.now(UTC)
        await record_audit(self.s, tenant_id=self.tenant_id, actor=self.actor,
                           action="agent.archive", resource_type="agent", resource_id=agent_id)

    # --- versions ----------------------------------------------------------------------------
    async def _next_number(self, agent_id: uuid.UUID) -> int:
        # Serialize version numbering per agent.
        await self.s.execute(
            sa.text("select pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"agent-version:{agent_id}"},
        )
        current = await self.s.scalar(
            sa.select(sa.func.max(AgentVersion.version)).where(AgentVersion.agent_id == agent_id)
        )
        return int(current or 0) + 1

    async def _new_version(
        self, agent_id: uuid.UUID, config: AgentConfig, *, status: str, source: str,
        parent: uuid.UUID | None, notes: str,
    ) -> AgentVersion:
        data = config.model_dump(mode="json")
        version = AgentVersion(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            version=await self._next_number(agent_id),
            status=status,
            config=data,
            config_hash=config_hash(data),
            parent_version_id=parent,
            source=source,
            notes=notes,
            created_by=self._actor_id,
        )
        self.s.add(version)
        await self.s.flush()
        return version

    async def get_version(self, version_id: uuid.UUID) -> AgentVersion:
        v = await self.s.scalar(
            sa.select(AgentVersion).where(
                AgentVersion.id == version_id, AgentVersion.tenant_id == self.tenant_id
            )
        )
        if v is None:
            raise NotFoundError("version not found")
        return v

    async def list_versions(self, agent_id: uuid.UUID) -> list[AgentVersion]:
        await self.get_agent(agent_id)
        return list(
            (
                await self.s.scalars(
                    sa.select(AgentVersion)
                    .where(AgentVersion.agent_id == agent_id)
                    .order_by(AgentVersion.version.desc())
                )
            ).all()
        )

    async def create_draft(
        self, agent_id: uuid.UUID, config: dict[str, Any], *, notes: str = "",
        parent_version_id: uuid.UUID | None = None, source: str = "manual",
        status: str = "draft",
    ) -> AgentVersion:
        await self.get_agent(agent_id)
        parsed = parse_config(config)
        if parent_version_id is not None:
            parent = await self.get_version(parent_version_id)
            if parent.agent_id != agent_id:
                raise ValidationFailedError("parent version belongs to another agent")
        version = await self._new_version(agent_id, parsed, status=status, source=source,
                                          parent=parent_version_id, notes=notes)
        await record_audit(self.s, tenant_id=self.tenant_id, actor=self.actor,
                           action=f"agent_version.create_{status}", resource_type="agent_version",
                           resource_id=version.id, details={"version": version.version})
        return version

    async def update_draft(self, version_id: uuid.UUID, config: dict[str, Any], notes: str | None
                           ) -> AgentVersion:
        version = await self.get_version(version_id)
        if version.status != "draft":
            raise ConflictError("only drafts can be edited; create a new draft instead")
        parsed = parse_config(config)
        data = parsed.model_dump(mode="json")
        version.config = data
        version.config_hash = config_hash(data)
        if notes is not None:
            version.notes = notes
        await self.s.flush()
        return version

    async def publish(self, version_id: uuid.UUID) -> AgentVersion:
        version = await self.get_version(version_id)
        if version.status == "published":
            return version
        if version.status not in ("draft", "candidate"):
            raise ConflictError(f"cannot publish a {version.status} version")
        parse_config(version.config)  # re-validate against the current schema
        version.status = "published"
        version.published_at = datetime.now(UTC)
        version.published_by = self._actor_id
        await self.s.flush()
        await record_audit(self.s, tenant_id=self.tenant_id, actor=self.actor,
                           action="agent_version.publish", resource_type="agent_version",
                           resource_id=version.id, details={"version": version.version})
        return version

    async def reject(self, version_id: uuid.UUID, reason: str) -> AgentVersion:
        version = await self.get_version(version_id)
        if version.status not in ("draft", "candidate"):
            raise ConflictError("only drafts/candidates can be rejected")
        version.status = "rejected"
        version.notes = (version.notes + "\nrejected: " + reason).strip()
        await self.s.flush()
        return version

    async def diff(self, from_id: uuid.UUID, to_id: uuid.UUID) -> list[Change]:
        a = await self.get_version(from_id)
        b = await self.get_version(to_id)
        return diff_configs(a.config, b.config)

    # --- deployments -------------------------------------------------------------------------
    async def get_deployment(self, agent_id: uuid.UUID, environment: str) -> Deployment | None:
        return await self.s.scalar(
            sa.select(Deployment).where(
                Deployment.agent_id == agent_id, Deployment.environment == environment
            )
        )

    async def list_deployments(self, agent_id: uuid.UUID) -> list[Deployment]:
        return list(
            (await self.s.scalars(sa.select(Deployment).where(Deployment.agent_id == agent_id))).all()
        )

    def _check_env(self, environment: str) -> None:
        if environment not in ENVIRONMENTS:
            raise ValidationFailedError(f"environment must be one of {ENVIRONMENTS}")

    async def deploy(
        self, agent_id: uuid.UUID, environment: str, version_id: uuid.UUID, *,
        action: str = "deploy",
    ) -> Deployment:
        self._check_env(environment)
        version = await self.get_version(version_id)
        if version.agent_id != agent_id:
            raise ValidationFailedError("version belongs to another agent")
        if version.status != "published":
            raise ConflictError("only published versions can be deployed")
        dep = await self.get_deployment(agent_id, environment)
        previous = dep.version_id if dep else None
        if dep is None:
            dep = Deployment(tenant_id=self.tenant_id, agent_id=agent_id,
                             environment=environment, version_id=version_id)
            self.s.add(dep)
        else:
            dep.version_id = version_id
            dep.canary_version_id = None
            dep.canary_percent = 0
        dep.updated_by = self._actor_id
        await self.s.flush()
        self.s.add(DeploymentEvent(tenant_id=self.tenant_id, deployment_id=dep.id, action=action,
                                   from_version_id=previous, to_version_id=version_id,
                                   actor_id=self._actor_id))
        await record_audit(self.s, tenant_id=self.tenant_id, actor=self.actor,
                           action=f"deployment.{action}", resource_type="deployment",
                           resource_id=dep.id,
                           details={"environment": environment, "version_id": str(version_id),
                                    "previous_version_id": str(previous) if previous else None})
        return dep

    async def set_canary(
        self, agent_id: uuid.UUID, environment: str, version_id: uuid.UUID | None, percent: float
    ) -> Deployment:
        self._check_env(environment)
        dep = await self.get_deployment(agent_id, environment)
        if dep is None:
            raise ConflictError("deploy a stable version before configuring a canary")
        if version_id is not None:
            version = await self.get_version(version_id)
            if version.agent_id != agent_id or version.status != "published":
                raise ConflictError("canary must be a published version of this agent")
            if version_id == dep.version_id:
                raise ValidationFailedError("canary version equals the stable version")
        if not 0 <= percent <= 100:
            raise ValidationFailedError("percent must be within [0, 100]")
        dep.canary_version_id = version_id
        dep.canary_percent = percent if version_id else 0
        dep.updated_by = self._actor_id
        await self.s.flush()
        self.s.add(DeploymentEvent(tenant_id=self.tenant_id, deployment_id=dep.id, action="canary",
                                   from_version_id=dep.version_id, to_version_id=version_id,
                                   actor_id=self._actor_id, details={"percent": percent}))
        await record_audit(self.s, tenant_id=self.tenant_id, actor=self.actor,
                           action="deployment.canary", resource_type="deployment",
                           resource_id=dep.id, details={"percent": percent,
                                                        "version_id": str(version_id)})
        return dep

    async def promote_canary(self, agent_id: uuid.UUID, environment: str) -> Deployment:
        dep = await self.get_deployment(agent_id, environment)
        if dep is None or dep.canary_version_id is None:
            raise ConflictError("no canary to promote")
        return await self.deploy(agent_id, environment, dep.canary_version_id, action="promote")

    async def rollback(
        self, agent_id: uuid.UUID, environment: str, to_version_id: uuid.UUID | None = None
    ) -> Deployment:
        dep = await self.get_deployment(agent_id, environment)
        if dep is None:
            raise NotFoundError("no deployment for this environment")
        target = to_version_id
        if target is None:
            # previous version this deployment pointed to
            target = await self.s.scalar(
                sa.select(DeploymentEvent.from_version_id)
                .where(
                    DeploymentEvent.deployment_id == dep.id,
                    DeploymentEvent.to_version_id == dep.version_id,
                    DeploymentEvent.from_version_id.is_not(None),
                    DeploymentEvent.action.in_(("deploy", "promote", "rollback")),
                )
                .order_by(DeploymentEvent.created_at.desc())
                .limit(1)
            )
            if target is None:
                raise ConflictError("no previous version to roll back to")
        return await self.deploy(agent_id, environment, target, action="rollback")

    # --- runtime resolution ------------------------------------------------------------------
    async def resolve_for_call(
        self, agent_id: uuid.UUID, environment: str, call_id: str
    ) -> ResolvedVersion:
        dep = await self.get_deployment(agent_id, environment)
        if dep is None:
            raise NotFoundError(f"agent has no deployment in '{environment}'")
        chosen = dep.version_id
        is_canary = False
        if dep.canary_version_id and dep.canary_percent > 0:
            if canary_bucket(call_id) < dep.canary_percent:
                chosen, is_canary = dep.canary_version_id, True
        version = await self.get_version(chosen)
        config = parse_config(version.config).materialize(environment)
        return ResolvedVersion(
            tenant_id=self.tenant_id, agent_id=agent_id, version_id=version.id,
            version=version.version, environment=environment, config=config,
            is_canary=is_canary,
        )
