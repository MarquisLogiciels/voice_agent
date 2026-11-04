"""Object storage for documents and recordings (never stored in Postgres)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import time
from pathlib import Path
from typing import Any, Protocol

from .errors import NotFoundError, ValidationFailedError
from .settings import Settings

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,500}$")


def validate_key(key: str) -> str:
    if not _KEY_RE.match(key) or ".." in key.split("/") or "//" in key:
        raise ValidationFailedError("invalid object key")
    return key


def tenant_key(tenant_id: Any, *parts: str) -> str:
    """Object keys are always prefixed by tenant so IAM policies can scope by prefix."""
    return validate_key("/".join([f"tenants/{tenant_id}", *parts]))


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def purge(self, key: str) -> None:
        """Irrecoverable delete (all versions) — for erasure and retention."""
        ...

    async def signed_url(self, key: str, ttl_s: int = 300) -> str: ...


class LocalBlobStore:
    """Filesystem store for development and tests."""

    def __init__(self, root: str | Path, *, signing_secret: str, public_base_url: str) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._secret = signing_secret.encode()
        self._base = public_base_url.rstrip("/")

    def _path(self, key: str) -> Path:
        p = (self._root / validate_key(key)).resolve()
        if self._root not in p.parents:
            raise ValidationFailedError("invalid object key")
        return p

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)

        await asyncio.to_thread(_write)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise NotFoundError("object not found")
        return await asyncio.to_thread(path.read_bytes)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))

    async def purge(self, key: str) -> None:
        await self.delete(key)

    def sign(self, key: str, expires: int) -> str:
        return hmac.new(self._secret, f"{key}:{expires}".encode(), hashlib.sha256).hexdigest()

    def verify(self, key: str, expires: int, signature: str) -> bool:
        return expires >= int(time.time()) and hmac.compare_digest(
            self.sign(key, expires), signature
        )

    async def signed_url(self, key: str, ttl_s: int = 300) -> str:
        expires = int(time.time()) + ttl_s
        return f"{self._base}/v1/blobs/{key}?expires={expires}&sig={self.sign(key, expires)}"


class S3BlobStore:
    """S3 / S3-compatible (MinIO, R2) store with server-side encryption."""

    def __init__(self, settings: Settings) -> None:
        import boto3

        kwargs: dict[str, Any] = {"region_name": settings.s3_region}
        if settings.s3_endpoint_url:
            kwargs["endpoint_url"] = settings.s3_endpoint_url
        if settings.s3_access_key_id and settings.s3_secret_access_key:
            kwargs["aws_access_key_id"] = settings.s3_access_key_id.get_secret_value()
            kwargs["aws_secret_access_key"] = settings.s3_secret_access_key.get_secret_value()
        self._client = boto3.client("s3", **kwargs)
        self._bucket = settings.s3_bucket
        if not self._bucket:
            raise ValidationFailedError("VP_S3_BUCKET is required for the s3 blob backend")

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=validate_key(key),
            Body=data,
            ContentType=content_type,
            # no per-object SSE header: the bucket default (KMS CMK in the Terraform stack)
            # applies; forcing AES256 here silently bypassed the customer-managed key
        )

    async def get(self, key: str) -> bytes:
        try:
            obj = await asyncio.to_thread(
                self._client.get_object, Bucket=self._bucket, Key=validate_key(key)
            )
        except self._client.exceptions.NoSuchKey as e:
            raise NotFoundError("object not found") from e
        body: bytes = await asyncio.to_thread(obj["Body"].read)
        return body

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(
            self._client.delete_object, Bucket=self._bucket, Key=validate_key(key)
        )

    async def purge(self, key: str) -> None:
        """Delete every version and delete marker of ``key``. On a versioned bucket a plain
        delete only adds a marker and the recording stays recoverable until lifecycle expiry."""
        k = validate_key(key)

        def run() -> None:
            pages = self._client.get_paginator("list_object_versions").paginate(
                Bucket=self._bucket, Prefix=k
            )
            for page in pages:
                items = [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
                objs: list[Any] = [
                    {"Key": v["Key"], "VersionId": v["VersionId"]} for v in items if v["Key"] == k
                ]
                if objs:
                    self._client.delete_objects(
                        Bucket=self._bucket, Delete={"Objects": objs, "Quiet": True}
                    )

        await asyncio.to_thread(run)

    async def signed_url(self, key: str, ttl_s: int = 300) -> str:
        url: str = await asyncio.to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self._bucket, "Key": validate_key(key)},
            ExpiresIn=ttl_s,
        )
        return url


def build_blob_store(settings: Settings) -> BlobStore:
    if settings.blob_backend == "s3":
        return S3BlobStore(settings)
    return LocalBlobStore(
        settings.blob_local_path,
        signing_secret=settings.jwt_secret.get_secret_value(),
        public_base_url=settings.public_base_url,
    )
