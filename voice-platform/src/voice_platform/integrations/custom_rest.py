"""Declarative custom REST integrations.

Tenants describe *fixed* actions (method + path template + typed parameters). The LLM can only
supply parameter values; it can never choose the host, path structure, headers or credentials.
Every request goes through the SSRF-guarded transport with the tenant's host allowlist.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator, model_validator

from ..shared.http import EgressPolicy
from .base import ErrorKind, IntegrationContext, IntegrationError
from .http import IntegrationHttpClient

_PARAM_NAME = r"^[a-z][a-z0-9_]{0,40}$"
_FORBIDDEN_HEADERS = {
    "authorization",
    "cookie",
    "host",
    "content-length",
    "transfer-encoding",
    "x-api-key",
    "proxy-authorization",
}


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=_PARAM_NAME)
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = Field(default="", max_length=300)
    required: bool = True
    location: Literal["query", "path", "body"] | None = None
    enum: list[str] | None = None
    max_length: int = Field(default=200, ge=1, le=2000)
    pattern: str | None = None


class ResponseMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict[str, str] = Field(default_factory=dict)
    """output name → dotted path into the JSON response (``items.0.name``)."""
    list_path: str | None = None
    """If set, the response is a list at this path; ``fields`` apply to each element."""
    max_items: int = Field(default=5, ge=1, le=50)


class ActionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=_PARAM_NAME)
    description: str = Field(min_length=5, max_length=1000)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str = Field(pattern=r"^/[A-Za-z0-9_\-./{}]*$", max_length=300)
    parameters: list[ParamSpec] = Field(default_factory=list, max_length=20)
    consequential: bool = False
    response: ResponseMapping = Field(default_factory=ResponseMapping)

    @model_validator(mode="after")
    def _check(self) -> ActionSpec:
        placeholders = set(re.findall(r"{([a-z][a-z0-9_]*)}", self.path))
        names = {p.name for p in self.parameters}
        if len(names) != len(self.parameters):
            raise ValueError("duplicate parameter names")
        missing = placeholders - names
        if missing:
            raise ValueError(f"path placeholders without parameters: {sorted(missing)}")
        for p in self.parameters:
            if p.name in placeholders:
                p.location = "path"
                p.required = True
            elif p.location == "path":
                raise ValueError(f"parameter {p.name} has location=path but is not in the path")
            elif p.location is None:
                p.location = "query" if self.method in ("GET", "DELETE") else "body"
        if self.method != "GET" and not self.consequential and self.method != "DELETE":
            # Writes are consequential by default; tenants must not silently downgrade them.
            self.consequential = True
        if self.method == "DELETE":
            self.consequential = True
        return self


class AuthSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["none", "bearer", "api_key_header", "basic"] = "none"


class CustomRestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(pattern=r"^https?://[^\s/]+(/[^\s]*)?$")
    allowed_hosts: list[str] = Field(default_factory=list)
    auth: AuthSpec = Field(default_factory=AuthSpec)
    default_headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = Field(default=6.0, gt=0, le=20)
    actions: list[ActionSpec] = Field(min_length=1, max_length=50)

    @field_validator("default_headers")
    @classmethod
    def _headers(cls, v: dict[str, str]) -> dict[str, str]:
        for k, val in v.items():
            if k.lower() in _FORBIDDEN_HEADERS:
                raise ValueError(f"header '{k}' cannot be set here (use the credential)")
            if "\r" in k or "\n" in k or "\r" in val or "\n" in val:
                raise ValueError("header values cannot contain newlines")
        return v

    @model_validator(mode="after")
    def _hosts(self) -> CustomRestConfig:
        host = urlsplit(self.base_url).hostname or ""
        if not self.allowed_hosts:
            self.allowed_hosts = [host]
        elif host not in self.allowed_hosts:
            raise ValueError("base_url host must be in allowed_hosts")
        names = [a.name for a in self.actions]
        if len(set(names)) != len(names):
            raise ValueError("duplicate action names")
        return self


_PY_TYPES: dict[str, type] = {"string": str, "integer": int, "number": float, "boolean": bool}


def action_input_model(action: ActionSpec) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for p in action.parameters:
        py: Any = _PY_TYPES[p.type]
        kwargs: dict[str, Any] = {"description": p.description}
        if p.type == "string":
            kwargs["max_length"] = p.max_length
            if p.pattern:
                kwargs["pattern"] = p.pattern
        if p.enum:
            py = Literal[tuple(p.enum)]  # type: ignore[valid-type]
        if p.required:
            fields[p.name] = (py, Field(**kwargs))
        else:
            fields[p.name] = (py | None, Field(default=None, **kwargs))
    return create_model(  # type: ignore[call-overload,no-any-return]
        f"Custom_{action.name}", __config__=ConfigDict(extra="forbid"), **fields
    )


def _extract(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            if not part.isdigit() or int(part) >= len(cur):
                return None
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _truncate(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "…"
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, list):
        return [_truncate(v, depth + 1) for v in value[:10]]
    if isinstance(value, dict):
        return {str(k)[:60]: _truncate(v, depth + 1) for k, v in list(value.items())[:30]}
    return value


class CustomRestIntegration:
    def __init__(
        self,
        ctx: IntegrationContext,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_insecure: bool = False,
    ) -> None:
        self.name = ctx.name
        self.cfg = CustomRestConfig.model_validate(ctx.config)
        self.actions = {a.name: a for a in self.cfg.actions}
        self.input_models = {a.name: action_input_model(a) for a in self.cfg.actions}
        egress = EgressPolicy(
            allowed_hosts=tuple(self.cfg.allowed_hosts),
            allowed_ports=(443, 80) if allow_insecure else (443,),
            allow_http=allow_insecure,
            allow_private_networks=allow_insecure,
        )
        creds = ctx.credentials or {}
        auth_type = self.cfg.auth.type

        async def auth(req: httpx.Request) -> None:
            if auth_type == "bearer":
                req.headers["Authorization"] = f"Bearer {creds['token']}"
            elif auth_type == "api_key_header":
                req.headers[creds.get("header", "X-API-Key")] = creds["value"]
            elif auth_type == "basic":
                raw = f"{creds['username']}:{creds['password']}".encode()
                req.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()

        if auth_type != "none" and not creds:
            raise IntegrationError(
                ErrorKind.CONFIGURATION, f"custom integration '{ctx.name}' needs a credential"
            )
        self.http = IntegrationHttpClient(
            integration=f"custom:{ctx.name}",
            base_url=self.cfg.base_url.rstrip("/"),
            egress=egress,
            auth=auth if auth_type != "none" else None,
            timeout_s=min(self.cfg.timeout_s, ctx.timeout_s),
            transport=transport,
            default_headers=self.cfg.default_headers,
            max_response_bytes=512 * 1024,
            max_request_bytes=64 * 1024,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    def tool_schema(self, action_name: str) -> dict[str, Any]:
        return self.input_models[action_name].model_json_schema()

    async def execute(self, action_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        action = self.actions.get(action_name)
        if action is None:
            raise IntegrationError(ErrorKind.CONFIGURATION, f"unknown action '{action_name}'")
        args = (
            self.input_models[action_name].model_validate(arguments).model_dump(exclude_none=True)
        )
        path = action.path
        query: dict[str, Any] = {}
        body: dict[str, Any] = {}
        for p in action.parameters:
            if p.name not in args:
                continue
            value = args[p.name]
            if p.location == "path":
                # encode everything, including '/', so values cannot alter the path
                path = path.replace("{" + p.name + "}", quote(str(value), safe=""))
            elif p.location == "query":
                query[p.name] = value
            else:
                body[p.name] = value
        resp = await self.http.request(
            action.method,
            path,
            params=query or None,
            json_body=body if action.method in ("POST", "PUT", "PATCH") else None,
            operation=action.name,
        )
        try:
            data = resp.json() if resp.content else None
        except ValueError:
            data = {"text": resp.text[:500]}
        return self._map(action, data)

    @staticmethod
    def _map(action: ActionSpec, data: Any) -> dict[str, Any]:
        mapping = action.response
        if mapping.list_path is not None:
            items = _extract(data, mapping.list_path) if mapping.list_path else data
            if not isinstance(items, list):
                items = []
            items = items[: mapping.max_items]
            if mapping.fields:
                items = [
                    {k: _truncate(_extract(i, p)) for k, p in mapping.fields.items()} for i in items
                ]
            return {"items": _truncate(items), "count": len(items)}
        if mapping.fields:
            return {k: _truncate(_extract(data, p)) for k, p in mapping.fields.items()}
        return {"result": _truncate(data)}
