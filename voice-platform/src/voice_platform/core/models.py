"""Relational model. Every tenant-owned table carries ``tenant_id`` and is protected by
row-level security (see migrations). Large binary content (recordings, documents) lives in
object storage; rows only hold keys.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 768

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING)
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}
    # Fetch server-generated columns (timestamps, identities) via RETURNING so they are never
    # lazy-loaded later (lazy IO is not allowed under asyncio).
    __mapper_args__ = {"eager_defaults": True}


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


def _tenant_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def _jsonb(default: Any = None) -> Mapped[Any]:
    server_default = sa.text("'[]'::jsonb") if isinstance(default, list) else sa.text("'{}'::jsonb")
    return mapped_column(JSONB, nullable=False, server_default=server_default)


# Tables that get the standard tenant RLS policy. Kept here so the migration and the
# isolation tests use the same list.
TENANT_TABLES: tuple[str, ...] = (
    "memberships",
    "api_keys",
    "credentials",
    "integrations",
    "agents",
    "agent_versions",
    "deployments",
    "deployment_events",
    "phone_numbers",
    "conversations",
    "conversation_events",
    "tool_executions",
    "knowledge_bases",
    "knowledge_documents",
    "knowledge_chunks",
    "eval_scenarios",
    "eval_runs",
    "eval_results",
    "optimization_runs",
)


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(sa.String(63), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default="active")
    settings: Mapped[dict[str, Any]] = _jsonb()
    data_region: Mapped[str] = mapped_column(sa.String(32), nullable=False, server_default="ca")
    retention_days: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="365")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (sa.CheckConstraint("status in ('active','suspended')", name="status"),)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(sa.String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    full_name: Mapped[str] = mapped_column(sa.String(200), nullable=False, server_default="")
    is_platform_admin: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    disabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Membership(Base):
    __tablename__ = "memberships"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "user_id"),
        sa.CheckConstraint("role in ('owner','admin','editor','viewer')", name="role"),
    )


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    prefix: Mapped[str] = mapped_column(sa.String(16), unique=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    __table_args__ = (
        sa.CheckConstraint("role in ('admin','editor','viewer','runtime')", name="role"),
    )


class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    meta: Mapped[dict[str, Any]] = _jsonb()
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    __table_args__ = (sa.UniqueConstraint("tenant_id", "name"),)


class Integration(Base):
    __tablename__ = "integrations"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    config: Mapped[dict[str, Any]] = _jsonb()
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("credentials.id", ondelete="SET NULL")
    )
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    __table_args__ = (sa.UniqueConstraint("tenant_id", "name"),)


class AgentRow(Base):
    __tablename__ = "agents"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    slug: Mapped[str] = mapped_column(sa.String(63), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    __table_args__ = (sa.UniqueConstraint("tenant_id", "slug"),)


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    config: Mapped[dict[str, Any]] = _jsonb()
    config_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("agent_versions.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default="manual")
    notes: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    published_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created()
    __table_args__ = (
        sa.UniqueConstraint("agent_id", "version"),
        sa.CheckConstraint(
            "status in ('draft','candidate','published','rejected','archived')", name="status"
        ),
        sa.Index("ix_agent_versions_agent_status", "agent_id", "status"),
    )


class Deployment(Base):
    __tablename__ = "deployments"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    environment: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False
    )
    canary_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("agent_versions.id", ondelete="SET NULL")
    )
    canary_percent: Mapped[float] = mapped_column(sa.Float, nullable=False, server_default="0")
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    __table_args__ = (
        sa.UniqueConstraint("agent_id", "environment"),
        sa.CheckConstraint("canary_percent >= 0 and canary_percent <= 100", name="canary"),
    )


class DeploymentEvent(Base):
    __tablename__ = "deployment_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    from_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    to_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    details: Mapped[dict[str, Any]] = _jsonb()
    created_at: Mapped[datetime] = _created()


class PhoneNumber(Base):
    __tablename__ = "phone_numbers"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    e164: Mapped[str] = mapped_column(sa.String(20), unique=True, nullable=False)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="SET NULL")
    )
    environment: Mapped[str] = mapped_column(sa.String(32), nullable=False, server_default="prod")
    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False, server_default="twilio")
    provider_ref: Mapped[str | None] = mapped_column(sa.String(100))
    livekit_inbound_trunk_id: Mapped[str | None] = mapped_column(sa.String(100))
    livekit_dispatch_rule_id: Mapped[str | None] = mapped_column(sa.String(100))
    livekit_outbound_trunk_id: Mapped[str | None] = mapped_column(sa.String(100))
    capabilities: Mapped[dict[str, Any]] = _jsonb()
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default="active")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="SET NULL")
    )
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("agent_versions.id", ondelete="SET NULL")
    )
    environment: Mapped[str] = mapped_column(sa.String(32), nullable=False, server_default="prod")
    channel: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default="phone")
    direction: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default="inbound")
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default="initiated")
    room_name: Mapped[str | None] = mapped_column(sa.String(200), index=True)
    job_id: Mapped[str | None] = mapped_column(sa.String(100))
    caller_number: Mapped[str | None] = mapped_column(sa.String(32))
    callee_number: Mapped[str | None] = mapped_column(sa.String(32))
    started_at: Mapped[datetime] = _created()
    answered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(sa.String(64))
    duration_s: Mapped[float | None] = mapped_column(sa.Float)
    outcome: Mapped[str | None] = mapped_column(sa.String(64))
    summary: Mapped[str | None] = mapped_column(sa.Text)
    recording_status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, server_default="disabled"
    )
    recording_uri: Mapped[str | None] = mapped_column(sa.Text)
    recording_consent: Mapped[bool | None] = mapped_column(sa.Boolean)
    metrics: Mapped[dict[str, Any]] = _jsonb()
    cost: Mapped[dict[str, Any]] = _jsonb()
    transfer: Mapped[dict[str, Any]] = _jsonb()
    meta: Mapped[dict[str, Any]] = _jsonb()
    __table_args__ = (
        sa.Index("ix_conversations_tenant_started", "tenant_id", sa.text("started_at DESC")),
        sa.Index("ix_conversations_agent", "tenant_id", "agent_id"),
    )


class ConversationEvent(Base):
    __tablename__ = "conversation_events"
    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = _jsonb()
    created_at: Mapped[datetime] = _created()
    __table_args__ = (sa.Index("ix_conversation_events_conv_seq", "conversation_id", "seq"),)


class ToolExecution(Base):
    __tablename__ = "tool_executions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL")
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tool_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    request: Mapped[dict[str, Any]] = _jsonb()
    response: Mapped[dict[str, Any]] = _jsonb()
    error_kind: Mapped[str | None] = mapped_column(sa.String(32))
    error_message: Mapped[str | None] = mapped_column(sa.Text)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="1")
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(sa.Float)
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "idempotency_key"),
        sa.CheckConstraint("status in ('pending','succeeded','failed')", name="status"),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    actor_type: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(sa.String(100))
    action: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(sa.String(100))
    details: Mapped[dict[str, Any]] = _jsonb()
    ip: Mapped[str | None] = mapped_column(sa.String(64))
    request_id: Mapped[str | None] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = _created()


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    embedding_provider: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    embedding_model: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    chunk_size: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="900")
    chunk_overlap: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="150")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    __table_args__ = (sa.UniqueConstraint("tenant_id", "name"),)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    source_type: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    source_uri: Mapped[str | None] = mapped_column(sa.Text)
    blob_key: Mapped[str | None] = mapped_column(sa.Text)
    mime_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    sha256: Mapped[str | None] = mapped_column(sa.String(64))
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default="pending")
    error: Mapped[str | None] = mapped_column(sa.Text)
    chunk_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="1")
    meta: Mapped[dict[str, Any]] = _jsonb()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    token_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        sa.Computed(
            "to_tsvector('simple', vp_unaccent(content)) || "
            "to_tsvector('english', vp_unaccent(content)) || "
            "to_tsvector('french', vp_unaccent(content))",
            persisted=True,
        ),
    )
    meta: Mapped[dict[str, Any]] = _jsonb()
    created_at: Mapped[datetime] = _created()
    __table_args__ = (
        sa.Index("ix_knowledge_chunks_kb", "tenant_id", "knowledge_base_id"),
        sa.Index("ix_knowledge_chunks_tsv", "tsv", postgresql_using="gin"),
        sa.Index(
            "ix_knowledge_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class EvalScenario(Base):
    __tablename__ = "eval_scenarios"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE")
    )
    slug: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    tags: Mapped[list[Any]] = _jsonb(default=[])
    spec: Mapped[dict[str, Any]] = _jsonb()
    source: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default="manual")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    __table_args__ = (sa.UniqueConstraint("tenant_id", "slug"),)


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False
    )
    suite: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default="queued")
    seed: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    repetitions: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="1")
    settings: Mapped[dict[str, Any]] = _jsonb()
    summary: Mapped[dict[str, Any]] = _jsonb()
    error: Mapped[str | None] = mapped_column(sa.Text)
    optimization_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class EvalResult(Base):
    __tablename__ = "eval_results"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("eval_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scenario_slug: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    repetition: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    passed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    category_scores: Mapped[dict[str, Any]] = _jsonb()
    failures: Mapped[list[Any]] = _jsonb(default=[])
    transcript: Mapped[list[Any]] = _jsonb(default=[])
    tool_calls: Mapped[list[Any]] = _jsonb(default=[])
    metrics: Mapped[dict[str, Any]] = _jsonb()
    judge: Mapped[dict[str, Any]] = _jsonb()
    created_at: Mapped[datetime] = _created()


class OptimizationRun(Base):
    __tablename__ = "optimization_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    baseline_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(sa.String(24), nullable=False, server_default="queued")
    settings: Mapped[dict[str, Any]] = _jsonb()
    iterations: Mapped[list[Any]] = _jsonb(default=[])
    best_candidate_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decision: Mapped[str | None] = mapped_column(sa.String(24))
    report: Mapped[dict[str, Any]] = _jsonb()
    report_markdown: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = _jsonb()
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default="queued")
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="100")
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="3")
    run_after: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    locked_by: Mapped[str | None] = mapped_column(sa.String(100))
    locked_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    result: Mapped[dict[str, Any]] = _jsonb()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    __table_args__ = (
        sa.Index("ix_jobs_ready", "status", "priority", "run_after"),
        sa.CheckConstraint(
            "status in ('queued','running','succeeded','failed','dead')", name="status"
        ),
    )
