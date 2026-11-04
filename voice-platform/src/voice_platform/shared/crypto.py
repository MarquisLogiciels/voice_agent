"""Encryption, password hashing and API-key primitives."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .errors import ConfigurationError

_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _password_hasher.check_needs_rehash(password_hash)


# A real argon2 hash used to equalize timing when the user does not exist.
_DUMMY_HASH = _password_hasher.hash("dummy-password-for-timing")


def verify_password_dummy(password: str) -> None:
    verify_password(_DUMMY_HASH, password)


class CredentialCipher:
    """Envelope encryption for credential payloads (JSON dicts).

    ``keys`` is a list of urlsafe-base64 32-byte Fernet keys. The first key encrypts; every key
    can decrypt, which enables rotation: prepend a new key, then call :meth:`rotate` on stored
    ciphertexts.
    """

    def __init__(self, keys: list[str]) -> None:
        cleaned = [k.strip() for k in keys if k.strip()]
        if not cleaned:
            raise ConfigurationError("no encryption keys configured (VP_ENCRYPTION_KEYS)")
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in cleaned])
        except (ValueError, TypeError) as e:
            raise ConfigurationError(f"invalid encryption key: {e}") from e
        self.key_id = hashlib.sha256(cleaned[0].encode()).hexdigest()[:12]

    @classmethod
    def from_setting(cls, raw: str) -> CredentialCipher:
        return cls(raw.split(","))

    def encrypt_json(self, payload: dict[str, Any]) -> bytes:
        return self._fernet.encrypt(json.dumps(payload, separators=(",", ":")).encode())

    def decrypt_json(self, token: bytes) -> dict[str, Any]:
        try:
            data = json.loads(self._fernet.decrypt(token))
        except InvalidToken as e:
            raise ConfigurationError("credential could not be decrypted with current keys") from e
        if not isinstance(data, dict):
            raise ConfigurationError("credential payload is not an object")
        return data

    def rotate(self, token: bytes) -> bytes:
        return self._fernet.rotate(token)


def generate_fernet_key() -> str:
    return Fernet.generate_key().decode()


@dataclass(frozen=True)
class GeneratedApiKey:
    plaintext: str
    prefix: str
    digest: str


API_KEY_PREFIX = "vpk_"


def generate_api_key() -> GeneratedApiKey:
    prefix = secrets.token_hex(4)
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    plaintext = f"{API_KEY_PREFIX}{prefix}_{secret}"
    return GeneratedApiKey(plaintext=plaintext, prefix=prefix, digest=hash_api_key(plaintext))


def parse_api_key_prefix(plaintext: str) -> str | None:
    if not plaintext.startswith(API_KEY_PREFIX):
        return None
    rest = plaintext[len(API_KEY_PREFIX) :]
    prefix, sep, secret = rest.partition("_")
    if not sep or len(prefix) != 8 or len(secret) < 20:
        return None
    return prefix


def hash_api_key(plaintext: str) -> str:
    # API keys carry 256 bits of entropy, so a fast hash is appropriate (no brute force risk).
    return hashlib.sha256(plaintext.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def stable_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode())
        h.update(b"\x1f")
    return h.hexdigest()
