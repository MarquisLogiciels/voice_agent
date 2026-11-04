"""Tenant isolation (RLS) and configuration versioning against a real PostgreSQL."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, ProgrammingError

from tests.conftest import sample_config
from voice_platform.config.service import AgentService
from voice_platform.core import jobs
from voice_platform.core.credentials import CredentialVault
from voice_platform.core.db import DbScope
from voice_platform.core.models import TENANT_TABLES, AgentRow, AuditLog, Credential, Job
from voice_platform.shared.crypto import CredentialCipher, generate_fernet_key
from voice_platform.shared.errors import ConflictError, NotFoundError, ValidationFailedError
from voice_platform.shared.logging import redact_text


async def _make_agent(db: Any, tenant_id: uuid.UUID, slug: str = "front-desk") -> tuple[Any, Any]:
    async with db.session(DbScope.for_tenant(tenant_id)) as s:
        svc = AgentService(s, tenant_id, None)
        return await svc.create_agent(slug=slug, name="Front", description="",
                                      config=sample_config())


async def test_rls_hides_other_tenants_even_without_where(db: Any, tenant: Any,
                                                          other_tenant: Any) -> None:
    await _make_agent(db, tenant.id)
    await _make_agent(db, other_tenant.id)
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        rows = (await s.execute(sa.text("select tenant_id from agents"))).all()
        assert rows and all(r.tenant_id == tenant.id for r in rows)
        tenants = (await s.execute(sa.text("select id from tenants"))).all()
        assert [t.id for t in tenants] == [tenant.id]


async def test_rls_blocks_cross_tenant_writes(db: Any, tenant: Any, other_tenant: Any) -> None:
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with db.session(DbScope.for_tenant(tenant.id)) as s:
            s.add(AgentRow(tenant_id=other_tenant.id, slug="x", name="x"))
            await s.flush()


async def test_no_tenant_context_sees_nothing(db: Any, tenant: Any) -> None:
    await _make_agent(db, tenant.id, slug="hidden")
    async with db.session(DbScope()) as s:
        for table in TENANT_TABLES:
            n = await s.scalar(sa.text(f"select count(*) from {table}"))
            assert n == 0, table


async def test_cross_tenant_update_is_noop(db: Any, tenant: Any, other_tenant: Any) -> None:
    agent, _ = await _make_agent(db, other_tenant.id, slug="victim")
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        res = await s.execute(sa.text("update agents set name='pwned' where id=:i"),
                              {"i": agent.id})
        assert res.rowcount == 0
    async with db.session(DbScope.for_tenant(other_tenant.id)) as s:
        assert await s.scalar(sa.select(AgentRow.name).where(AgentRow.id == agent.id)) == "Front"


async def test_audit_log_is_append_only(db: Any, tenant: Any) -> None:
    await _make_agent(db, tenant.id, slug="audited")
    with pytest.raises(DBAPIError):
        async with db.session(DbScope.for_tenant(tenant.id)) as s:
            await s.execute(sa.text("delete from audit_logs"))
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        n = await s.scalar(sa.select(sa.func.count()).select_from(AuditLog))
        assert n and n >= 1


async def test_version_lifecycle_publish_deploy_canary_rollback(db: Any, tenant: Any) -> None:
    agent, v1 = await _make_agent(db, tenant.id, slug="lifecycle")
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        svc = AgentService(s, tenant.id, None)
        with pytest.raises(ConflictError):
            await svc.deploy(agent.id, "prod", v1.id)  # drafts can't deploy
        await svc.publish(v1.id)
        await svc.deploy(agent.id, "prod", v1.id)
        with pytest.raises(ConflictError):
            await svc.update_draft(v1.id, sample_config(), None)  # published is immutable
        v2 = await svc.create_draft(agent.id, sample_config(instructions="v2 instructions"),
                                    parent_version_id=v1.id)
        assert v2.version == 2
        await svc.publish(v2.id)
        await svc.set_canary(agent.id, "prod", v2.id, 30)
        picks = [
            (await svc.resolve_for_call(agent.id, "prod", f"call-{i}")).is_canary
            for i in range(300)
        ]
        assert 0.2 < sum(picks) / len(picks) < 0.4
        again = [
            (await svc.resolve_for_call(agent.id, "prod", f"call-{i}")).is_canary
            for i in range(300)
        ]
        assert picks == again  # pinned deterministically per call id
        await svc.promote_canary(agent.id, "prod")
        dep = await svc.get_deployment(agent.id, "prod")
        assert dep and dep.version_id == v2.id and dep.canary_version_id is None
        await svc.rollback(agent.id, "prod")
        dep = await svc.get_deployment(agent.id, "prod")
        assert dep and dep.version_id == v1.id
        resolved = await svc.resolve_for_call(agent.id, "prod", "call-x")
        assert resolved.version == 1
        changes = await svc.diff(v1.id, v2.id)
        assert [c.path for c in changes] == ["instructions"]


async def test_invalid_config_rejected_with_details(db: Any, tenant: Any) -> None:
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        svc = AgentService(s, tenant.id, None)
        bad = sample_config()
        bad["pipeline"]["llm"] = None
        with pytest.raises(ValidationFailedError) as ei:
            await svc.create_agent(slug="bad", name="b", description="", config=bad)
        assert ei.value.details["errors"]


async def test_resolve_unknown_environment(db: Any, tenant: Any) -> None:
    agent, _ = await _make_agent(db, tenant.id, slug="nodeploy")
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        with pytest.raises(NotFoundError):
            await AgentService(s, tenant.id, None).resolve_for_call(agent.id, "prod", "c")


async def test_credentials_encrypted_and_isolated(db: Any, tenant: Any, other_tenant: Any) -> None:
    vault = CredentialVault(CredentialCipher([generate_fernet_key()]))
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        cred = await vault.create(s, tenant_id=tenant.id, name="hubspot-main", kind="hubspot",
                                  payload={"access_token": "pat-na1-very-secret-token"})
        raw = await s.scalar(sa.select(Credential.ciphertext).where(Credential.id == cred.id))
        assert b"very-secret" not in raw
        with pytest.raises(ValidationFailedError):
            await vault.create(s, tenant_id=tenant.id, name="bad", kind="twilio",
                               payload={"account_sid": "nope", "auth_token": "x"})
    async with db.session(DbScope.for_tenant(other_tenant.id)) as s:
        with pytest.raises(NotFoundError):
            await vault.resolve(s, tenant_id=other_tenant.id, credential_id=cred.id)
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        payload = await vault.resolve(s, tenant_id=tenant.id, credential_id=cred.id)
        assert payload["access_token"] == "pat-na1-very-secret-token"
    # decrypted secrets are registered for log redaction
    assert "pat-na1-very-secret-token" not in redact_text("token pat-na1-very-secret-token used")


async def test_job_queue_claim_retry_and_lease_expiry(db: Any, tenant: Any) -> None:
    kind = f"test-{uuid.uuid4().hex[:6]}"
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        job_id = await jobs.enqueue(s, kind=kind, payload={"x": 1}, tenant_id=tenant.id,
                                    max_attempts=2)
    async with db.session(DbScope.system()) as s:
        claimed = await jobs.claim(s, worker_id="w1", kinds=[kind], lease_s=60)
        assert claimed and claimed.id == job_id and claimed.attempts == 1
    async with db.session(DbScope.system()) as s:
        assert await jobs.claim(s, worker_id="w2", kinds=[kind]) is None  # leased
    async with db.session(DbScope.system()) as s:
        status = await jobs.fail(s, claimed, "boom")
        assert status == "queued"
        await s.execute(sa.update(Job).where(Job.id == job_id).values(run_after=sa.func.now()))
    async with db.session(DbScope.system()) as s:
        again = await jobs.claim(s, worker_id="w2", kinds=[kind], lease_s=1)
        assert again and again.attempts == 2
        # simulate a crashed worker: lease expires and the job is reclaimable
        await s.execute(sa.update(Job).where(Job.id == job_id)
                        .values(locked_until=sa.func.now() - sa.text("interval '1 second'")))
    async with db.session(DbScope.system()) as s:
        third = await jobs.claim(s, worker_id="w3", kinds=[kind])
        assert third and third.attempts == 3
        assert await jobs.fail(s, third, "boom") == "dead"
