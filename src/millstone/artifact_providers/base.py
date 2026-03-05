"""Abstract base contracts for artifact providers.

Provider placeholder keys for ``get_prompt_placeholders()``:

Each provider subclass may return any of these keys to supply backend-specific
instructions that are substituted into prompt templates before agent dispatch.

Tasklist keys (used by TasklistProviderBase subclasses):
  TASKLIST_READ_INSTRUCTIONS    — how to read/list tasks from backend storage
  TASKLIST_COMPLETE_INSTRUCTIONS — how to mark exactly ONE task as done/complete
                                   (used in builder loop; NOT a bulk write)
  TASKLIST_REWRITE_INSTRUCTIONS — how to write the ENTIRE tasklist content back
                                   (used only in compaction; bulk replacement,
                                   not single-task update)
  TASKLIST_APPEND_INSTRUCTIONS  — how to append new tasks to the existing list
  TASKLIST_UPDATE_INSTRUCTIONS  — how to edit/update existing tasks in place

Opportunity keys (used by OpportunityProviderBase subclasses):
  OPPORTUNITY_WRITE_INSTRUCTIONS — how to write/create an opportunity record
  OPPORTUNITY_READ_INSTRUCTIONS  — how to read/list opportunities

Design keys (used by DesignProviderBase subclasses):
  DESIGN_WRITE_INSTRUCTIONS — how to write/create a design document (and edit
                               in place when revising from feedback)
  DESIGN_READ_INSTRUCTIONS  — how to read a design document
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from millstone.artifacts.models import (
    Design,
    DesignStatus,
    Opportunity,
    OpportunityStatus,
    TasklistItem,
    TaskStatus,
)


class OpportunityProviderBase(ABC):
    """Base contract for opportunity artifact providers."""

    @classmethod
    @abstractmethod
    def from_config(cls, options: dict[str, Any]) -> OpportunityProviderBase:
        """Construct provider from backend options."""

    @abstractmethod
    def list_opportunities(self) -> list[Opportunity]:
        """List all opportunities."""

    @abstractmethod
    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        """Get one opportunity by id."""

    @abstractmethod
    def write_opportunity(self, opportunity: Opportunity) -> None:
        """Create or persist an opportunity."""

    @abstractmethod
    def update_opportunity_status(self, opportunity_id: str, status: OpportunityStatus) -> None:
        """Update opportunity status by id."""

    def get_prompt_placeholders(self) -> dict[str, str]:
        """Return backend-specific prompt placeholder substitutions.

        Subclasses may override to supply values for OPPORTUNITY_WRITE_INSTRUCTIONS
        and OPPORTUNITY_READ_INSTRUCTIONS. Default returns an empty dict (no
        substitutions).
        """
        return {}


class DesignProviderBase(ABC):
    """Base contract for design artifact providers."""

    @classmethod
    @abstractmethod
    def from_config(cls, options: dict[str, Any]) -> DesignProviderBase:
        """Construct provider from backend options."""

    @abstractmethod
    def list_designs(self) -> list[Design]:
        """List all designs."""

    @abstractmethod
    def get_design(self, design_id: str) -> Design | None:
        """Get one design by id."""

    @abstractmethod
    def write_design(self, design: Design) -> None:
        """Create or persist a design."""

    @abstractmethod
    def update_design_status(self, design_id: str, status: DesignStatus) -> None:
        """Update design status by id."""

    def get_prompt_placeholders(self) -> dict[str, str]:
        """Return backend-specific prompt placeholder substitutions.

        Subclasses may override to supply values for DESIGN_WRITE_INSTRUCTIONS
        and DESIGN_READ_INSTRUCTIONS. Default returns an empty dict (no
        substitutions).
        """
        return {}


class TasklistProviderBase(ABC):
    """Base contract for tasklist artifact providers."""

    @classmethod
    @abstractmethod
    def from_config(cls, options: dict[str, Any]) -> TasklistProviderBase:
        """Construct provider from backend options."""

    @abstractmethod
    def list_tasks(self) -> list[TasklistItem]:
        """List all tasks."""

    @abstractmethod
    def get_task(self, task_id: str) -> TasklistItem | None:
        """Get one task by id."""

    @abstractmethod
    def append_tasks(self, tasks: list[TasklistItem]) -> None:
        """Append tasks."""

    @abstractmethod
    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        """Update task status by id."""

    @abstractmethod
    def get_snapshot(self) -> str:
        """Get serializable provider snapshot."""

    @abstractmethod
    def restore_snapshot(self, content: str) -> None:
        """Restore provider state from snapshot content."""

    def get_prompt_placeholders(self) -> dict[str, str]:
        """Return backend-specific prompt placeholder substitutions.

        Subclasses may override to supply values for TASKLIST_READ_INSTRUCTIONS,
        TASKLIST_COMPLETE_INSTRUCTIONS, TASKLIST_REWRITE_INSTRUCTIONS,
        TASKLIST_APPEND_INSTRUCTIONS, and TASKLIST_UPDATE_INSTRUCTIONS.
        Default returns an empty dict (no substitutions).
        """
        return {}
