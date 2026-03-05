"""Artifact-provider registries and factory helpers.

This module keeps runtime-extensible registries for each artifact-provider
contract. Backends can be registered as provider classes (preferred) or
factory callables (legacy compatibility).
"""

from collections.abc import Callable
from typing import Any, TypeAlias, TypeVar

from millstone.artifact_providers.base import (
    DesignProviderBase,
    OpportunityProviderBase,
    TasklistProviderBase,
)
from millstone.artifact_providers.protocols import (
    DesignProvider,
    OpportunityProvider,
    TasklistProvider,
)

ProviderOptions: TypeAlias = dict[str, Any]
T = TypeVar("T")
ProviderFactory: TypeAlias = Callable[[ProviderOptions], T]
OpportunityProviderClass: TypeAlias = type[OpportunityProviderBase]
DesignProviderClass: TypeAlias = type[DesignProviderBase]
TasklistProviderClass: TypeAlias = type[TasklistProviderBase]


OPPORTUNITY_PROVIDERS: dict[str, ProviderFactory[OpportunityProvider]] = {}
DESIGN_PROVIDERS: dict[str, ProviderFactory[DesignProvider]] = {}
TASKLIST_PROVIDERS: dict[str, ProviderFactory[TasklistProvider]] = {}


def _format_available(backends: list[str]) -> str:
    return ", ".join(backends) if backends else "(none)"


def get_opportunity_provider(backend: str, options: ProviderOptions) -> OpportunityProvider:
    factory = OPPORTUNITY_PROVIDERS.get(backend)
    if factory is None:
        available = _format_available(list_opportunity_backends())
        raise ValueError(
            f"Unknown opportunity provider backend: {backend}. Available: {available}"
        )
    provider = factory(options)
    if not isinstance(provider, OpportunityProvider):
        raise TypeError(
            f"Registered opportunity provider '{backend}' returned {type(provider).__name__}, "
            "which does not implement OpportunityProvider."
        )
    return provider


def get_design_provider(backend: str, options: ProviderOptions) -> DesignProvider:
    factory = DESIGN_PROVIDERS.get(backend)
    if factory is None:
        available = _format_available(list_design_backends())
        raise ValueError(f"Unknown design provider backend: {backend}. Available: {available}")
    provider = factory(options)
    if not isinstance(provider, DesignProvider):
        raise TypeError(
            f"Registered design provider '{backend}' returned {type(provider).__name__}, "
            "which does not implement DesignProvider."
        )
    return provider


def get_tasklist_provider(backend: str, options: ProviderOptions) -> TasklistProvider:
    factory = TASKLIST_PROVIDERS.get(backend)
    if factory is None:
        available = _format_available(list_tasklist_backends())
        raise ValueError(f"Unknown tasklist provider backend: {backend}. Available: {available}")
    provider = factory(options)
    if not isinstance(provider, TasklistProvider):
        raise TypeError(
            f"Registered tasklist provider '{backend}' returned {type(provider).__name__}, "
            "which does not implement TasklistProvider."
        )
    return provider


def register_opportunity_provider(
    name: str, factory: ProviderFactory[OpportunityProvider]
) -> None:
    if not callable(factory):
        raise TypeError("Opportunity provider factory must be callable")
    OPPORTUNITY_PROVIDERS[name] = factory


def register_design_provider(name: str, factory: ProviderFactory[DesignProvider]) -> None:
    if not callable(factory):
        raise TypeError("Design provider factory must be callable")
    DESIGN_PROVIDERS[name] = factory


def register_tasklist_provider(name: str, factory: ProviderFactory[TasklistProvider]) -> None:
    if not callable(factory):
        raise TypeError("Tasklist provider factory must be callable")
    TASKLIST_PROVIDERS[name] = factory


def register_opportunity_provider_class(
    name: str, provider_cls: OpportunityProviderClass
) -> None:
    if not issubclass(provider_cls, OpportunityProviderBase):
        raise TypeError(
            f"Opportunity provider class {provider_cls.__name__} must inherit OpportunityProviderBase"
        )
    register_opportunity_provider(name, provider_cls.from_config)


def register_design_provider_class(name: str, provider_cls: DesignProviderClass) -> None:
    if not issubclass(provider_cls, DesignProviderBase):
        raise TypeError(f"Design provider class {provider_cls.__name__} must inherit DesignProviderBase")
    register_design_provider(name, provider_cls.from_config)


def register_tasklist_provider_class(name: str, provider_cls: TasklistProviderClass) -> None:
    if not issubclass(provider_cls, TasklistProviderBase):
        raise TypeError(
            f"Tasklist provider class {provider_cls.__name__} must inherit TasklistProviderBase"
        )
    register_tasklist_provider(name, provider_cls.from_config)


def list_opportunity_backends() -> list[str]:
    return sorted(OPPORTUNITY_PROVIDERS.keys())


def list_design_backends() -> list[str]:
    return sorted(DESIGN_PROVIDERS.keys())


def list_tasklist_backends() -> list[str]:
    return sorted(TASKLIST_PROVIDERS.keys())
