from __future__ import annotations

import asyncio
import ipaddress
import json
import logging

import httpx
import pytest

from voice_platform.shared.crypto import (
    CredentialCipher,
    generate_api_key,
    generate_fernet_key,
    hash_password,
    parse_api_key_prefix,
    verify_password,
)
from voice_platform.shared.errors import ConfigurationError, ValidationFailedError
from voice_platform.shared.http import (
    EgressBlockedError,
    EgressPolicy,
    ResponseTooLargeError,
    build_guarded_client,
    read_limited,
)
from voice_platform.shared.logging import (
    JsonFormatter,
    RedactionFilter,
    log_context,
    mask_phone,
    redact_text,
    redact_value,
    secret_registry,
)
from voice_platform.shared.phone import is_allowed_destination, normalize_e164
from voice_platform.shared.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    RetryPolicy,
    retry_async,
)


# --- redaction -------------------------------------------------------------------------------
def test_redacts_patterns_and_registered_secrets() -> None:
    secret_registry.register("super-secret-value-123")
    text = (
        "Authorization: Bearer abc.def.ghi api_key=XYZ12345 token: t0k3n "
        "key sk-ABCDEFGHIJKLMNOPQRSTUV and super-secret-value-123"
    )
    out = redact_text(text)
    for leaked in ("abc.def.ghi", "XYZ12345", "t0k3n", "sk-ABCDEFGHIJKLMNOPQRSTUV",
                   "super-secret-value-123"):
        assert leaked not in out


def test_redact_value_masks_sensitive_keys_recursively() -> None:
    data = {"headers": {"Authorization": "Bearer x", "X-Other": "ok"},
            "nested": [{"password": "p", "name": "n"}]}
    out = redact_value(data)
    assert out["headers"]["Authorization"] == "[REDACTED]"
    assert out["headers"]["X-Other"] == "ok"
    assert out["nested"][0]["password"] == "[REDACTED]"
    assert out["nested"][0]["name"] == "n"


def test_json_formatter_includes_context_and_redacts() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1,
                               "calling with token=%s", ("abcdef123456",), None)
    record.headers = {"authorization": "Bearer zzz"}
    RedactionFilter().filter(record)
    with log_context(tenant_id="t1", conversation_id="c1"):
        payload = json.loads(JsonFormatter("svc").format(record))
    assert "abcdef123456" not in payload["msg"]
    assert payload["headers"]["authorization"] == "[REDACTED]"
    assert payload["tenant_id"] == "t1" and payload["conversation_id"] == "c1"


def test_mask_phone() -> None:
    assert mask_phone("+1 (514) 555-1234") == "***1234"
    assert mask_phone(None) is None


# --- crypto ----------------------------------------------------------------------------------
def test_password_hashing() -> None:
    h = hash_password("correct horse battery")
    assert verify_password(h, "correct horse battery")
    assert not verify_password(h, "wrong password!!")
    with pytest.raises(ValueError):
        hash_password("short")


def test_cipher_roundtrip_and_rotation() -> None:
    old = generate_fernet_key()
    c1 = CredentialCipher([old])
    token = c1.encrypt_json({"api_key": "k" * 20})
    new = generate_fernet_key()
    c2 = CredentialCipher([new, old])
    assert c2.decrypt_json(token) == {"api_key": "k" * 20}
    rotated = c2.rotate(token)
    assert CredentialCipher([new]).decrypt_json(rotated)["api_key"] == "k" * 20
    with pytest.raises(ConfigurationError):
        CredentialCipher([generate_fernet_key()]).decrypt_json(token)
    with pytest.raises(ConfigurationError):
        CredentialCipher([])


def test_api_key_format() -> None:
    key = generate_api_key()
    assert parse_api_key_prefix(key.plaintext) == key.prefix
    assert parse_api_key_prefix("vpk_short") is None
    assert parse_api_key_prefix("other") is None


# --- phone -----------------------------------------------------------------------------------
def test_phone_normalization() -> None:
    assert normalize_e164("(514) 555-0199") == "+15145550199"
    assert normalize_e164("tel:+33 1 23 45 67 89") == "+33123456789"
    assert normalize_e164("sip:+15145550199@trunk.example.com") == "+15145550199"
    with pytest.raises(ValidationFailedError):
        normalize_e164("12")
    assert is_allowed_destination("+15145550199", ["+1514"])
    assert not is_allowed_destination("+15145550199", ["+1418"])
    assert is_allowed_destination("+15145550199", [])


# --- resilience ------------------------------------------------------------------------------
async def test_retry_stops_on_non_retryable() -> None:
    calls = 0

    async def fn(attempt: int) -> int:
        nonlocal calls
        calls += 1
        raise ValueError("fatal")

    with pytest.raises(ValueError):
        await retry_async(fn, policy=RetryPolicy(max_attempts=5, base_delay_s=0.001),
                          is_retryable=lambda e: False)
    assert calls == 1


async def test_retry_succeeds_after_transient() -> None:
    attempts: list[int] = []

    async def fn(attempt: int) -> str:
        attempts.append(attempt)
        if attempt < 3:
            raise ConnectionError("transient")
        return "ok"

    out = await retry_async(fn, policy=RetryPolicy(max_attempts=3, base_delay_s=0.001),
                            is_retryable=lambda e: isinstance(e, ConnectionError))
    assert out == "ok" and attempts == [1, 2, 3]


async def test_retry_respects_deadline() -> None:
    async def fn(attempt: int) -> None:
        raise ConnectionError("down")

    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(ConnectionError):
        await retry_async(
            fn,
            policy=RetryPolicy(max_attempts=50, base_delay_s=0.2, max_delay_s=0.2, deadline_s=0.3),
            is_retryable=lambda e: True,
            retry_after=lambda e: 0.2,
        )
    assert loop.time() - start < 1.0


def test_circuit_breaker_transitions() -> None:
    now = [0.0]
    cb = CircuitBreaker("x", failure_threshold=2, recovery_timeout_s=10, clock=lambda: now[0])
    assert cb.state is CircuitState.CLOSED
    cb.record_failure()
    assert cb.allow()
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        cb.check()
    now[0] = 11
    assert cb.state is CircuitState.HALF_OPEN
    assert cb.allow()          # one trial
    assert not cb.allow()      # second concurrent trial refused
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    now[0] = 22
    assert cb.allow()
    cb.record_success()
    assert cb.state is CircuitState.CLOSED


# --- SSRF guard ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x",            # http not allowed
        "https://127.0.0.1/x",            # loopback literal
        "https://169.254.169.254/latest",  # metadata
        "https://10.0.0.5/",               # private
        "https://user:pw@example.com/",    # userinfo
        "https://example.com:8443/",       # port
        "file:///etc/passwd",
        "https://[::1]/",
    ],
)
def test_policy_blocks(url: str) -> None:
    with pytest.raises(EgressBlockedError):
        EgressPolicy().check_url(url)


def test_policy_allowlist() -> None:
    p = EgressPolicy(allowed_hosts=("api.example.com", "*.crm.example.com"))
    p.check_url("https://api.example.com/v1")
    p.check_url("https://eu.crm.example.com/v1")
    with pytest.raises(EgressBlockedError):
        p.check_url("https://evil.com/")
    with pytest.raises(EgressBlockedError):
        p.check_url("https://crm.example.com.evil.com/")


def test_ip_policy_ipv4_mapped() -> None:
    p = EgressPolicy()
    assert not p.ip_allowed(ipaddress.ip_address("::ffff:127.0.0.1"))
    assert not p.ip_allowed(ipaddress.ip_address("100.64.1.1"))  # CGNAT, not global
    assert p.ip_allowed(ipaddress.ip_address("8.8.8.8"))


async def test_guard_blocks_dns_names_resolving_to_loopback() -> None:
    # "localhost" passes the static check (allowlist empty) but resolves to 127.0.0.1;
    # the connect-time guard must refuse it.
    client = build_guarded_client(EgressPolicy())
    with pytest.raises(EgressBlockedError):
        await client.get("https://localhost/")
    await client.aclose()


async def test_read_limited_aborts() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 5000))
    async with httpx.AsyncClient(transport=transport) as c:
        async with c.stream("GET", "https://example.com") as resp:
            with pytest.raises(ResponseTooLargeError):
                await read_limited(resp, 1000)
