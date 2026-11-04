"""Credential vault: typed, validated, encrypted at rest, write-only through the API."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..shared.crypto import CredentialCipher
from ..shared.errors import ConflictError, NotFoundError, ValidationFailedError
from ..shared.logging import secret_registry
from .models import Credential


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BearerToken(_Strict):
    token: str = Field(min_length=8)


class BasicAuth(_Strict):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class ApiKeyHeader(_Strict):
    header: str = Field(default="X-API-Key", pattern=r"^[A-Za-z0-9\-]{1,64}$")
    value: str = Field(min_length=8)


class HubSpotCredential(_Strict):
    access_token: str = Field(min_length=10)  # private-app token or OAuth access token


class SalesforceCredential(_Strict):
    client_id: str
    client_secret: str
    login_url: str = "https://login.salesforce.com"
    # client-credentials flow needs a My Domain URL in login_url; refresh-token flow uses this:
    refresh_token: str | None = None


class GoogleServiceAccount(_Strict):
    client_email: str
    private_key: str
    token_uri: str = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint URL
    delegated_user: str | None = None  # domain-wide delegation subject


class GoogleOAuthRefresh(_Strict):
    client_id: str
    client_secret: str
    refresh_token: str


class TwilioCredential(_Strict):
    account_sid: str = Field(pattern=r"^AC[0-9a-fA-F]{32}$")
    auth_token: str = Field(min_length=16)


class ProviderApiKey(_Strict):
    api_key: str = Field(min_length=8)
    base_url: str | None = None


CREDENTIAL_SCHEMAS: dict[str, type[BaseModel]] = {
    "bearer": BearerToken,
    "basic": BasicAuth,
    "api_key_header": ApiKeyHeader,
    "hubspot": HubSpotCredential,
    "salesforce": SalesforceCredential,
    "google_service_account": GoogleServiceAccount,
    "google_oauth": GoogleOAuthRefresh,
    "twilio": TwilioCredential,
    "provider_api_key": ProviderApiKey,
}


def validate_credential_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    schema = CREDENTIAL_SCHEMAS.get(kind)
    if schema is None:
        raise ValidationFailedError(
            f"unknown credential kind '{kind}'", details={"kinds": sorted(CREDENTIAL_SCHEMAS)}
        )
    try:
        return schema.model_validate(payload).model_dump(exclude_none=True)
    except ValidationError as e:
        # Never echo the values back; only field locations and messages.
        raise ValidationFailedError(
            "invalid credential payload",
            details={"errors": [{"loc": err["loc"], "msg": err["msg"]} for err in e.errors()]},
        ) from e


def _register_secrets(payload: dict[str, Any]) -> None:
    for value in payload.values():
        if isinstance(value, str):
            secret_registry.register(value)


class CredentialVault:
    def __init__(self, cipher: CredentialCipher) -> None:
        self._cipher = cipher

    async def create(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        name: str,
        kind: str,
        payload: dict[str, Any],
        meta: dict[str, Any] | None = None,
        created_by: uuid.UUID | None = None,
    ) -> Credential:
        clean = validate_credential_payload(kind, payload)
        exists = await session.scalar(
            sa.select(Credential.id).where(
                Credential.tenant_id == tenant_id, Credential.name == name
            )
        )
        if exists:
            raise ConflictError(f"credential '{name}' already exists")
        cred = Credential(
            tenant_id=tenant_id,
            name=name,
            kind=kind,
            ciphertext=self._cipher.encrypt_json(clean),
            key_id=self._cipher.key_id,
            meta=meta or {},
            created_by=created_by,
        )
        session.add(cred)
        await session.flush()
        return cred

    async def update(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        credential_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> Credential:
        cred = await self._get(session, tenant_id, credential_id)
        clean = validate_credential_payload(cred.kind, payload)
        cred.ciphertext = self._cipher.encrypt_json(clean)
        cred.key_id = self._cipher.key_id
        await session.flush()
        return cred

    async def resolve(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, credential_id: uuid.UUID
    ) -> dict[str, Any]:
        cred = await self._get(session, tenant_id, credential_id)
        payload = self._cipher.decrypt_json(cred.ciphertext)
        _register_secrets(payload)
        return payload

    async def resolve_by_name(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, name: str
    ) -> tuple[str, dict[str, Any]]:
        cred = await session.scalar(
            sa.select(Credential).where(Credential.tenant_id == tenant_id, Credential.name == name)
        )
        if cred is None:
            raise NotFoundError(f"credential '{name}' not found")
        payload = self._cipher.decrypt_json(cred.ciphertext)
        _register_secrets(payload)
        return cred.kind, payload

    async def rotate_all(self, session: AsyncSession) -> int:
        """Re-encrypt every credential with the current primary key (platform scope)."""
        rows = (await session.scalars(sa.select(Credential))).all()
        n = 0
        for cred in rows:
            if cred.key_id != self._cipher.key_id:
                cred.ciphertext = self._cipher.rotate(cred.ciphertext)
                cred.key_id = self._cipher.key_id
                n += 1
        await session.flush()
        return n

    async def _get(
        self, session: AsyncSession, tenant_id: uuid.UUID, credential_id: uuid.UUID
    ) -> Credential:
        cred = await session.scalar(
            sa.select(Credential).where(
                Credential.id == credential_id, Credential.tenant_id == tenant_id
            )
        )
        if cred is None:
            raise NotFoundError("credential not found")
        return cred
