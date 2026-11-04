"""Authentication: password login → JWT access tokens, and tenant API keys."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt
import sqlalchemy as sa

from ..shared.crypto import (
    constant_time_equals,
    hash_api_key,
    parse_api_key_prefix,
    verify_password,
    verify_password_dummy,
)
from ..shared.errors import AuthenticationError
from ..shared.settings import Settings
from .db import Database, DbScope
from .rbac import Principal, Role


@dataclass(frozen=True)
class IssuedToken:
    access_token: str
    expires_at: int
    token_type: str = "bearer"


class Authenticator:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    # --- passwords ---------------------------------------------------------------------------
    async def login(self, email: str, password: str) -> IssuedToken:
        async with self._db.session(DbScope()) as s:
            row = (
                await s.execute(sa.text("select * from vp_auth_find_user(:e)"), {"e": email})
            ).first()
            if row is None:
                verify_password_dummy(password)  # equalize timing
                raise AuthenticationError("invalid credentials")
            if row.disabled or not verify_password(row.password_hash, password):
                raise AuthenticationError("invalid credentials")
            await s.execute(
                sa.text(
                    "select set_config('app.user_id', :u, true);"
                ),
                {"u": str(row.id)},
            )
            await s.execute(
                sa.text("update users set last_login_at = now() where id = :u"), {"u": row.id}
            )
        return self.issue_token(row.id)

    def issue_token(self, user_id: uuid.UUID) -> IssuedToken:
        now = int(time.time())
        exp = now + self._settings.jwt_ttl_s
        token = jwt.encode(
            {
                "sub": str(user_id),
                "iat": now,
                "nbf": now,
                "exp": exp,
                "iss": self._settings.jwt_issuer,
                "jti": uuid.uuid4().hex,
                "typ": "access",
            },
            self._settings.jwt_secret.get_secret_value(),
            algorithm="HS256",
        )
        return IssuedToken(access_token=token, expires_at=exp)

    def _decode(self, token: str) -> uuid.UUID:
        try:
            claims = jwt.decode(
                token,
                self._settings.jwt_secret.get_secret_value(),
                algorithms=["HS256"],
                issuer=self._settings.jwt_issuer,
                options={"require": ["exp", "iat", "sub", "iss"]},
            )
        except jwt.PyJWTError as e:
            raise AuthenticationError("invalid or expired token") from e
        if claims.get("typ") != "access":
            raise AuthenticationError("invalid token type")
        try:
            return uuid.UUID(claims["sub"])
        except ValueError as e:
            raise AuthenticationError("invalid token subject") from e

    # --- request authentication -------------------------------------------------------------
    async def authenticate(self, bearer: str) -> Principal:
        if bearer.startswith("vpk_"):
            return await self._authenticate_api_key(bearer)
        user_id = self._decode(bearer)
        async with self._db.session(DbScope(user_id=user_id)) as s:
            user = (
                await s.execute(
                    sa.text("select disabled, is_platform_admin from users where id = :u"),
                    {"u": user_id},
                )
            ).first()
            if user is None or user.disabled:
                raise AuthenticationError("user disabled or not found")
            rows = (
                await s.execute(sa.text("select * from vp_user_memberships(:u)"), {"u": user_id})
            ).all()
        memberships = {r.tenant_id: Role(r.role) for r in rows if r.status == "active"}
        return Principal(
            kind="user",
            id=str(user_id),
            user_id=user_id,
            is_platform_admin=bool(user.is_platform_admin),
            memberships=memberships,
        )

    async def _authenticate_api_key(self, plaintext: str) -> Principal:
        prefix = parse_api_key_prefix(plaintext)
        if prefix is None:
            raise AuthenticationError("malformed API key")
        async with self._db.session(DbScope()) as s:
            row = (
                await s.execute(sa.text("select * from vp_auth_find_api_key(:p)"), {"p": prefix})
            ).first()
            if row is None or not constant_time_equals(row.key_hash, hash_api_key(plaintext)):
                raise AuthenticationError("invalid API key")
            now = datetime.now(UTC)
            if row.revoked_at is not None or (row.expires_at and row.expires_at <= now):
                raise AuthenticationError("API key revoked or expired")
        async with self._db.session(DbScope.for_tenant(row.tenant_id)) as s:
            await s.execute(
                sa.text(
                    "update api_keys set last_used_at = now() where id = :i and"
                    " (last_used_at is null or last_used_at < now() - interval '1 minute')"
                ),
                {"i": row.id},
            )
        role = Role(row.role) if row.role in Role._value2member_map_ else Role.VIEWER
        return Principal(kind="api_key", id=str(row.id), memberships={row.tenant_id: role})
