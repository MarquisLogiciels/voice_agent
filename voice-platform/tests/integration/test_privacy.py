"""Privacy controls verified end to end: erasure covers every recording version and survives a
backup restore; transcripts and phone numbers stay out of logs."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from voice_platform.core.audit import record_audit
from voice_platform.core.db import DbScope
from voice_platform.core.models import Conversation, ToolExecution
from voice_platform.core.privacy import (
    _hmac_keys,
    erase_subject_data,
    reapply_erasures,
    subject_ref,
)
from voice_platform.shared.blobstore import LocalBlobStore
from voice_platform.shared.logging import JsonFormatter, RedactionFilter, install_redaction

CALLER = "+15145550199"
KEYS = "R4n4fW3Yr6t6qQdC7bJ0m3kq9vJb8h0pZp5S6yT0pXk="


def test_s3_purge_removes_every_version_and_keeps_bucket_kms_default() -> None:
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    from voice_platform.shared.blobstore import S3BlobStore
    from voice_platform.shared.settings import Settings

    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="ca-central-1")
        bucket, region = "vp-lab-bucket", "ca-central-1"
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        store = S3BlobStore(Settings(environment="test", s3_bucket=bucket, s3_region=region))
        import asyncio

        key = "tenants/t1/recordings/c1.ogg"
        asyncio.run(store.put(key, b"v1", "audio/ogg"))
        asyncio.run(store.put(key, b"v2", "audio/ogg"))
        asyncio.run(store.delete(key))  # what erasure used to do
        left = s3.list_object_versions(Bucket="vp-lab-bucket", Prefix=key)
        assert len(left.get("Versions", [])) == 2  # both still recoverable
        asyncio.run(store.purge(key))
        left = s3.list_object_versions(Bucket="vp-lab-bucket", Prefix=key)
        assert not left.get("Versions") and not left.get("DeleteMarkers")


def test_put_does_not_override_bucket_encryption() -> None:
    from voice_platform.shared import blobstore

    src = Path(blobstore.__file__).read_text()
    assert 'ServerSideEncryption="AES256"' not in src  # would bypass the bucket's KMS CMK


async def test_erasure_and_reapplication_after_restore(
    db: Any, tenant: Any, tmp_path: Path
) -> None:
    blobs = LocalBlobStore(tmp_path, signing_secret="s", public_base_url="http://x")
    rec = f"tenants/{tenant.id}/recordings/c.ogg"
    await blobs.put(rec, b"audio", "audio/ogg")

    async def seed() -> uuid.UUID:
        async with db.session(DbScope.for_tenant(tenant.id)) as s:
            c = Conversation(tenant_id=tenant.id, caller_number=CALLER, recording_uri=rec)
            s.add(c)
            await s.flush()
            s.add(
                ToolExecution(
                    tenant_id=tenant.id,
                    conversation_id=c.id,
                    tool_name="create_lead",
                    idempotency_key=f"k-{uuid.uuid4().hex}",
                    status="failed",
                    request={"first_name": "Ana"},
                    response={"error": "x"},
                    error_message="HubSpot rejected Ana Roy +15145550199",
                )
            )
            return c.id

    conv_id = await seed()
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        res = await erase_subject_data(s, blobs, tenant.id, CALLER)
        await record_audit(
            s,
            tenant_id=tenant.id,
            actor=None,
            action="data_subject.erase",
            resource_type="data_subject",
            resource_id=CALLER[-4:],
            details={"subject_ref": subject_ref(CALLER, _hmac_keys(KEYS)[0])},
        )
    assert res.conversations == 1 and res.recordings == 1
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        assert await s.get(Conversation, conv_id) is None
        te = (await s.execute(sa.select(ToolExecution).where(
            ToolExecution.tool_name == "create_lead"))).scalars().all()  # fmt: skip
        assert all(t.request == {} and t.response == {} and t.error_message is None for t in te)
        q = "select details::text from audit_logs where action='data_subject.erase'"
        audit = (await s.execute(sa.text(q))).scalar()
        assert CALLER not in audit and "5145550199" not in audit  # only a keyed hash

    # a point-in-time restore brings the caller back…
    restored = await seed()
    totals = await reapply_erasures(db, blobs, KEYS)
    assert totals["conversations"] >= 1
    async with db.session(DbScope.for_tenant(tenant.id)) as s:
        assert await s.get(Conversation, restored) is None  # …and the replay removes it again


def test_logs_drop_livekit_pii_fields_and_mask_phone_numbers(caplog: Any) -> None:
    record = logging.LogRecord(
        "livekit.agents", logging.INFO, __file__, 1, "received user transcript", None, None
    )
    record.__dict__.update(
        {
            "lk.pii.user_transcript": "my card number is 4111 1111 1111 1111",
            "lk.pii.participant_identity": "sip_+15145550199",
            "room": "call-_+15145550199_9YhTqNpDm2Bs",
        }
    )
    RedactionFilter().filter(record)
    out = JsonFormatter("t").format(record)
    assert "4111" not in out and "5145550199" not in out
    assert "call-_***0199_9YhTqNpDm2Bs" in out


def test_redaction_reaches_handlers_installed_by_livekit() -> None:
    root = logging.getLogger()
    foreign = logging.StreamHandler()  # like LiveKit's CLI handler in the worker process
    root.addHandler(foreign)
    try:
        assert install_redaction() >= 1
        assert any(isinstance(f, RedactionFilter) for f in foreign.filters)
        assert install_redaction() == 0  # idempotent
    finally:
        root.removeHandler(foreign)
