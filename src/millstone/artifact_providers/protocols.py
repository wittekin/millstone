"""Artifact provider Protocol interfaces for millstone.

Defines runtime_checkable Protocols for the three canonical artifact contracts:
OpportunityProvider, DesignProvider, TasklistProvider.

These are structural contracts used for runtime validation and typing.
Concrete implementations are encouraged to inherit from the explicit ABC
contracts in ``millstone.artifact_providers.base``.
"""

from typing import Protocol, runtime_checkable

from millstone.artifacts.models import (
    DependencyKind,
    Design,
    DesignStatus,
    Opportunity,
    OpportunityStatus,
    TasklistItem,
    TaskStatus,
)


@runtime_checkable
class OpportunityProvider(Protocol):
    def list_opportunities(self) -> list[Opportunity]: ...
    def get_opportunity(self, opportunity_id: str) -> Opportunity | None: ...
    def write_opportunity(self, opportunity: Opportunity) -> None: ...
    def update_opportunity_status(self, opportunity_id: str, status: OpportunityStatus) -> None: ...
    def get_prompt_placeholders(self) -> dict[str, str]: ...


@runtime_checkable
class DesignProvider(Protocol):
    def list_designs(self) -> list[Design]: ...
    def get_design(self, design_id: str) -> Design | None: ...
    def write_design(self, design: Design) -> None: ...
    def update_design_status(self, design_id: str, status: DesignStatus) -> None: ...
    def get_prompt_placeholders(self) -> dict[str, str]: ...


@runtime_checkable
class TasklistProvider(Protocol):
    def list_tasks(self) -> list[TasklistItem]: ...
    def get_task(self, task_id: str) -> TasklistItem | None: ...
    def append_tasks(self, tasks: list[TasklistItem]) -> None: ...
    def update_task(self, task: TasklistItem) -> None: ...
    def update_task_status(self, task_id: str, status: TaskStatus) -> None: ...
    def get_snapshot(self) -> str: ...
    def restore_snapshot(self, content: str) -> None: ...
    def get_prompt_placeholders(self) -> dict[str, str]: ...


@runtime_checkable
class ReadyAwareTasklistProvider(Protocol):
    """Optional capability: provider can return only unblocked / ready-to-work tasks.

    Backends that natively model dependencies (e.g. beads) can answer this
    cheaply. Callers should use ``isinstance(provider, ReadyAwareTasklistProvider)``
    to feature-detect; for providers that do not implement it, fall back to
    ``list_tasks()``.
    """

    def list_ready_tasks(self) -> list[TasklistItem]: ...


@runtime_checkable
class DependencyLinker(Protocol):
    """Optional capability: provider can record typed links between two artifact ids.

    Implemented by providers whose backend stores a dependency graph (e.g. beads
    via ``bd dep add``). The semantics of ``from_id`` / ``to_id`` follow the
    given ``DependencyKind`` (e.g. for ``blocks``, ``from_id`` blocks ``to_id``).
    """

    def link(self, from_id: str, to_id: str, kind: DependencyKind) -> None: ...
