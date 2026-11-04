"""Platform error hierarchy. API layer maps these to HTTP responses."""

from __future__ import annotations

from typing import Any


class PlatformError(Exception):
    code = "platform_error"
    http_status = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(PlatformError):
    code = "not_found"
    http_status = 404


class ConflictError(PlatformError):
    code = "conflict"
    http_status = 409


class ValidationFailedError(PlatformError):
    code = "validation_failed"
    http_status = 422


class AuthenticationError(PlatformError):
    code = "unauthenticated"
    http_status = 401


class PermissionDeniedError(PlatformError):
    code = "permission_denied"
    http_status = 403


class RateLimitedError(PlatformError):
    code = "rate_limited"
    http_status = 429

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class DependencyUnavailableError(PlatformError):
    code = "dependency_unavailable"
    http_status = 503


class ConfigurationError(PlatformError):
    code = "configuration_error"
    http_status = 422
