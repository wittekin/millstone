"""Artifact-provider contracts, registries, and backend implementations."""

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
from millstone.artifact_providers.registry import (
    get_design_provider,
    get_opportunity_provider,
    get_tasklist_provider,
    list_design_backends,
    list_opportunity_backends,
    list_tasklist_backends,
    register_design_provider,
    register_design_provider_class,
    register_opportunity_provider,
    register_opportunity_provider_class,
    register_tasklist_provider,
    register_tasklist_provider_class,
)

__all__ = [
    "DesignProvider",
    "DesignProviderBase",
    "OpportunityProvider",
    "OpportunityProviderBase",
    "TasklistProvider",
    "TasklistProviderBase",
    "get_design_provider",
    "get_opportunity_provider",
    "get_tasklist_provider",
    "list_design_backends",
    "list_opportunity_backends",
    "list_tasklist_backends",
    "register_design_provider",
    "register_design_provider_class",
    "register_opportunity_provider",
    "register_opportunity_provider_class",
    "register_tasklist_provider",
    "register_tasklist_provider_class",
]
