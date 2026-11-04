"""Outbound HTTP with SSRF protection.

Two layers:

1. :class:`EgressPolicy.check_url` — static checks before a request: scheme, userinfo, port,
   host allowlist, IP-literal hosts.
2. :class:`SSRFGuardBackend` — an httpcore network backend that resolves the hostname itself
   and refuses to connect if *any* resolved address is non-public. Because validation happens
   at connect time on the exact address used, DNS rebinding between "check" and "connect" is
   not possible.

Redirects are never followed automatically by guarded clients.
"""

from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import socket
import ssl
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import certifi
import httpcore
import httpx

from .errors import PermissionDeniedError, ValidationFailedError


class EgressBlockedError(PermissionDeniedError):
    code = "egress_blocked"


class ResponseTooLargeError(ValidationFailedError):
    code = "response_too_large"


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _unwrap(ip: IPAddress) -> IPAddress:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


@dataclass(frozen=True)
class EgressPolicy:
    """Which destinations an outbound integration may reach."""

    allowed_hosts: tuple[str, ...] = ()
    """Exact hosts or ``*.example.com`` wildcards. Empty = any public host."""
    allowed_ports: tuple[int, ...] = (443,)
    allow_http: bool = False
    allow_private_networks: bool = False
    """Dev/test only: permits loopback/private destinations (e.g. a local mock server)."""
    blocked_networks: tuple[str, ...] = field(
        default=(
            "169.254.169.254/32",  # cloud metadata (also link-local, kept explicit)
            "fd00:ec2::254/128",
        )
    )

    def host_allowed(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        if not self.allowed_hosts:
            return True
        return any(
            fnmatch.fnmatchcase(host, pattern.lower())
            if "*" in pattern
            else host == pattern.lower()
            for pattern in self.allowed_hosts
        )

    def ip_allowed(self, ip: IPAddress) -> bool:
        ip = _unwrap(ip)
        for net in self.blocked_networks:
            if ip in ipaddress.ip_network(net):
                return False
        if self.allow_private_networks:
            return not ip.is_multicast and not ip.is_unspecified
        return ip.is_global and not ip.is_multicast

    def check_url(self, url: str | httpx.URL) -> None:
        parsed = urlsplit(str(url))
        scheme = parsed.scheme.lower()
        if scheme not in ("https", "http") or (scheme == "http" and not self.allow_http):
            raise EgressBlockedError(f"scheme '{scheme}' is not allowed")
        if parsed.username or parsed.password:
            raise EgressBlockedError("credentials in URL are not allowed")
        host = parsed.hostname
        if not host:
            raise EgressBlockedError("URL has no host")
        port = parsed.port or (443 if scheme == "https" else 80)
        allowed_ports = set(self.allowed_ports) | ({80} if self.allow_http else set())
        if port not in allowed_ports:
            raise EgressBlockedError(f"port {port} is not allowed")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not self.ip_allowed(literal):
            raise EgressBlockedError("destination address is not allowed")
        if not self.host_allowed(host):
            raise EgressBlockedError(f"host '{host}' is not in the allowlist")


async def _resolve(host: str, port: int) -> list[IPAddress]:
    try:
        literal = ipaddress.ip_address(host)
        return [literal]
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise httpcore.ConnectError(f"DNS resolution failed for {host}: {e}") from e
    addrs: list[IPAddress] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        addr = ipaddress.ip_address(sockaddr[0])
        if addr not in addrs:
            addrs.append(addr)
    return addrs


class SSRFGuardBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, policy: EgressPolicy, inner: httpcore.AsyncNetworkBackend | None = None):
        self._policy = policy
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await _resolve(host, port)
        if not addresses:
            raise httpcore.ConnectError(f"no addresses for {host}")
        # Refuse if ANY address is disallowed: an attacker-controlled DNS name may mix public
        # and private records hoping the client picks the private one.
        if any(not self._policy.ip_allowed(a) for a in addresses):
            raise EgressBlockedError(f"destination '{host}' resolves to a non-public address")
        last_exc: Exception | None = None
        for addr in addresses:
            try:
                return await self._inner.connect_tcp(
                    str(addr),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                last_exc = e
        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.AsyncNetworkStream:
        raise EgressBlockedError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connection pool uses :class:`SSRFGuardBackend`."""

    def __init__(self, policy: EgressPolicy, *, max_connections: int = 20) -> None:
        super().__init__(trust_env=False)
        ctx = ssl.create_default_context(cafile=certifi.where())
        # Replace the pool with one using the guarded backend. ``_pool`` is the documented
        # extension point used by httpx's own transport implementation.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ctx,
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
            keepalive_expiry=30.0,
            network_backend=SSRFGuardBackend(policy),
        )
        self._policy = policy

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._policy.check_url(request.url)
        return await super().handle_async_request(request)


def build_guarded_client(
    policy: EgressPolicy,
    *,
    timeout_s: float = 10.0,
    base_url: str = "",
    headers: dict[str, str] | None = None,
    max_connections: int = 20,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=httpx.Timeout(timeout_s, connect=min(5.0, timeout_s)),
        transport=_GuardedTransport(policy, max_connections=max_connections),
        follow_redirects=False,
        trust_env=False,
    )


async def read_limited(response: httpx.Response, max_bytes: int) -> bytes:
    """Read a streamed response body, aborting once it exceeds ``max_bytes``."""
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        await response.aclose()
        raise ResponseTooLargeError(f"response exceeds {max_bytes} bytes")
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            await response.aclose()
            raise ResponseTooLargeError(f"response exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
