"""Role-based access control.

Roles are per tenant and ordered. Permissions map to the minimum role that holds them.
Platform admins hold every permission in every tenant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from ..shared.errors import PermissionDeniedError


class Role(str, Enum):
    VIEWER = "viewer"
    EDITOR = "editor"
    ADMIN = "admin"
    OWNER = "owner"

    @property
    def rank(self) -> int:
        return _RANK[self]

    def at_least(self, other: Role) -> bool:
        return self.rank >= other.rank


_RANK = {Role.VIEWER: 1, Role.EDITOR: 2, Role.ADMIN: 3, Role.OWNER: 4}


class Permission(str, Enum):
    TENANT_READ = "tenant:read"
    TENANT_UPDATE = "tenant:update"
    MEMBERS_MANAGE = "members:manage"
    API_KEYS_MANAGE = "api_keys:manage"
    AGENTS_READ = "agents:read"
    AGENTS_WRITE = "agents:write"
    AGENTS_PUBLISH = "agents:publish"
    DEPLOYMENTS_WRITE = "deployments:write"
    CREDENTIALS_READ = "credentials:read"
    CREDENTIALS_WRITE = "credentials:write"
    INTEGRATIONS_READ = "integrations:read"
    INTEGRATIONS_WRITE = "integrations:write"
    PHONE_NUMBERS_READ = "phone_numbers:read"
    PHONE_NUMBERS_WRITE = "phone_numbers:write"
    CALLS_OUTBOUND = "calls:outbound"
    CONVERSATIONS_READ = "conversations:read"
    RECORDINGS_READ = "recordings:read"
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_WRITE = "knowledge:write"
    EVALUATIONS_READ = "evaluations:read"
    EVALUATIONS_RUN = "evaluations:run"
    OPTIMIZATIONS_RUN = "optimizations:run"
    OPTIMIZATIONS_APPROVE = "optimizations:approve"
    AUDIT_READ = "audit:read"


MIN_ROLE: dict[Permission, Role] = {
    Permission.TENANT_READ: Role.VIEWER,
    Permission.TENANT_UPDATE: Role.ADMIN,
    Permission.MEMBERS_MANAGE: Role.ADMIN,
    Permission.API_KEYS_MANAGE: Role.ADMIN,
    Permission.AGENTS_READ: Role.VIEWER,
    Permission.AGENTS_WRITE: Role.EDITOR,
    Permission.AGENTS_PUBLISH: Role.ADMIN,
    Permission.DEPLOYMENTS_WRITE: Role.ADMIN,
    Permission.CREDENTIALS_READ: Role.ADMIN,
    Permission.CREDENTIALS_WRITE: Role.ADMIN,
    Permission.INTEGRATIONS_READ: Role.VIEWER,
    Permission.INTEGRATIONS_WRITE: Role.ADMIN,
    Permission.PHONE_NUMBERS_READ: Role.VIEWER,
    Permission.PHONE_NUMBERS_WRITE: Role.ADMIN,
    Permission.CALLS_OUTBOUND: Role.EDITOR,
    Permission.CONVERSATIONS_READ: Role.VIEWER,
    Permission.RECORDINGS_READ: Role.ADMIN,
    Permission.KNOWLEDGE_READ: Role.VIEWER,
    Permission.KNOWLEDGE_WRITE: Role.EDITOR,
    Permission.EVALUATIONS_READ: Role.VIEWER,
    Permission.EVALUATIONS_RUN: Role.EDITOR,
    Permission.OPTIMIZATIONS_RUN: Role.EDITOR,
    Permission.OPTIMIZATIONS_APPROVE: Role.ADMIN,
    Permission.AUDIT_READ: Role.ADMIN,
}

# API keys can never be owners and never manage members/keys (no privilege escalation by key).
API_KEY_FORBIDDEN = {Permission.MEMBERS_MANAGE, Permission.API_KEYS_MANAGE}


@dataclass(frozen=True)
class Principal:
    kind: Literal["user", "api_key", "system"]
    id: str
    user_id: uuid.UUID | None = None
    is_platform_admin: bool = False
    memberships: dict[uuid.UUID, Role] = field(default_factory=dict)

    def role_in(self, tenant_id: uuid.UUID) -> Role | None:
        if self.is_platform_admin:
            return Role.OWNER
        return self.memberships.get(tenant_id)

    def can(self, tenant_id: uuid.UUID, permission: Permission) -> bool:
        if self.kind == "api_key" and permission in API_KEY_FORBIDDEN:
            return False
        role = self.role_in(tenant_id)
        return role is not None and role.at_least(MIN_ROLE[permission])

    def require(self, tenant_id: uuid.UUID, permission: Permission) -> Role:
        role = self.role_in(tenant_id)
        if role is None:
            # Do not reveal whether the tenant exists.
            raise PermissionDeniedError("not a member of this tenant")
        if not self.can(tenant_id, permission):
            raise PermissionDeniedError(f"missing permission {permission.value}")
        return role

    def require_platform_admin(self) -> None:
        if not self.is_platform_admin:
            raise PermissionDeniedError("platform administrator required")

    @property
    def actor_label(self) -> str:
        return f"{self.kind}:{self.id}"


SYSTEM_PRINCIPAL = Principal(kind="system", id="system", is_platform_admin=True)
