"""Central provider registry: registration, validation, lazy instantiation, metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from ..config.schema import ProviderRef
from .types import BuildContext, MissingSecretError, ProviderDescriptor, ProviderKind


class ProviderError(Exception):
    """Raised when a provider cannot be validated or instantiated."""


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    severity: str = "error"  # "error" | "warning"

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message, "severity": self.severity}


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[tuple[ProviderKind, str], ProviderDescriptor] = {}

    def register(self, descriptor: ProviderDescriptor, *, replace: bool = False) -> None:
        key = (descriptor.kind, descriptor.name)
        if key in self._providers and not replace:
            raise ProviderError(
                f"provider {descriptor.kind.value}/{descriptor.name} already registered"
            )
        self._providers[key] = descriptor

    def get(self, kind: ProviderKind, name: str) -> ProviderDescriptor:
        try:
            return self._providers[(kind, name)]
        except KeyError:
            available = sorted(n for k, n in self._providers if k is kind)
            raise ProviderError(
                f"unknown {kind.value} provider '{name}' (available: {', '.join(available)})"
            ) from None

    def has(self, kind: ProviderKind, name: str) -> bool:
        return (kind, name) in self._providers

    def descriptors(self, kind: ProviderKind | None = None) -> list[ProviderDescriptor]:
        return sorted(
            (d for (k, _), d in self._providers.items() if kind is None or k is kind),
            key=lambda d: (d.kind.value, d.name),
        )

    def validate_ref(
        self,
        kind: ProviderKind,
        ref: ProviderRef,
        path: str,
        ctx: BuildContext | None = None,
        *,
        check_secrets: bool = True,
    ) -> list[Problem]:
        problems: list[Problem] = []
        if not self.has(kind, ref.provider):
            try:
                self.get(kind, ref.provider)
            except ProviderError as e:
                return [Problem(f"{path}.provider", str(e))]
        desc = self.get(kind, ref.provider)
        if desc.options_model is not None:
            try:
                desc.options_model.model_validate(ref.options)
            except ValidationError as e:
                for err in e.errors():
                    loc = ".".join(str(p) for p in err["loc"])
                    problems.append(Problem(f"{path}.options.{loc}", err["msg"]))
        elif ref.options:
            problems.append(Problem(f"{path}.options", f"{desc.name} accepts no options"))
        if desc.capabilities.requires_voice and not ref.voice:
            problems.append(Problem(f"{path}.voice", f"{desc.name} requires a voice"))
        if desc.known_models and ref.model and ref.model not in desc.known_models:
            problems.append(
                Problem(
                    f"{path}.model",
                    f"model '{ref.model}' is not in the known list for "
                    f"{desc.name}; it will be passed through unchanged",
                    "warning",
                )
            )
        langs = desc.capabilities.languages
        if langs and ref.language and ref.language.split("-")[0] not in langs:
            problems.append(
                Problem(
                    f"{path}.language",
                    f"{desc.name} does not list language '{ref.language}'",
                    "warning",
                )
            )
        if check_secrets and desc.secret_env and ctx is not None:
            try:
                if ctx.secret(ref, desc.secret_env) is None:
                    problems.append(
                        Problem(
                            f"{path}",
                            f"no API key: set one of {', '.join(desc.secret_env)} or "
                            "reference a tenant credential",
                        )
                    )
            except MissingSecretError as e:
                problems.append(Problem(f"{path}.credential", str(e)))
        return problems

    def create(self, kind: ProviderKind, ref: ProviderRef, ctx: BuildContext) -> Any:
        desc = self.get(kind, ref.provider)
        problems = [
            p for p in self.validate_ref(kind, ref, kind.value, ctx) if p.severity == "error"
        ]
        if problems:
            raise ProviderError("; ".join(f"{p.path}: {p.message}" for p in problems))
        try:
            return desc.factory(ref, ctx)
        except ImportError as e:
            raise ProviderError(
                f"{desc.display_name} needs the '{desc.package}' package: {e}"
            ) from e
        except (MissingSecretError, ValueError) as e:
            raise ProviderError(f"{desc.kind.value}/{desc.name}: {e}") from e


def provider_key(kind: ProviderKind | str, ref: ProviderRef) -> str:
    k = kind.value if isinstance(kind, ProviderKind) else kind
    return f"{k}:{ref.provider}:{ref.model or '-'}"


_default_registry: ProviderRegistry | None = None


def default_registry() -> ProviderRegistry:
    """The registry pre-populated with the built-in catalogue (created on first use)."""
    global _default_registry
    if _default_registry is None:
        from .catalog import register_builtin_providers

        reg = ProviderRegistry()
        register_builtin_providers(reg)
        _default_registry = reg
    return _default_registry
