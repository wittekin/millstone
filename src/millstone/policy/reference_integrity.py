"""Cross-artifact reference integrity validation for millstone."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from millstone.artifacts.models import Design, Opportunity, TasklistItem


@runtime_checkable
class OpportunityLookup(Protocol):
    """Lookup protocol for resolving opportunities by ID."""

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None: ...


@runtime_checkable
class DesignLookup(Protocol):
    """Lookup protocol for resolving designs by ID."""

    def get_design(self, design_id: str) -> Design | None: ...


class ReferenceIntegrityError(ValueError):
    """Raised when one or more cross-artifact references fail to resolve."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__(
            "Reference integrity check failed:\n"
            + "\n".join(f"  - {violation}" for violation in violations)
        )


class ReferenceIntegrityChecker:
    """Validate cross-artifact references using injected provider lookups."""

    def __init__(
        self,
        opportunity_provider: OpportunityLookup | None = None,
        design_provider: DesignLookup | None = None,
    ) -> None:
        self._opportunity_provider = opportunity_provider
        self._design_provider = design_provider

    def check_design(self, design: Design) -> None:
        """Validate that design.opportunity_ref is present and resolves."""
        if self._opportunity_provider is None:
            raise ValueError("opportunity_provider required to check design.opportunity_ref")

        violations: list[str] = []
        opportunity_ref = (design.opportunity_ref or "").strip()
        if not opportunity_ref:
            violations.append("design.opportunity_ref is required but absent or empty")
        elif self._opportunity_provider.get_opportunity(opportunity_ref) is None:
            violations.append(
                f"design.opportunity_ref={opportunity_ref!r} does not resolve to a known opportunity"
            )

        if violations:
            raise ReferenceIntegrityError(violations)

    def check_opportunity(self, opportunity: Opportunity) -> None:
        """Validate opportunity.design_ref when present."""
        design_ref = (opportunity.design_ref or "").strip()
        if not design_ref:
            return

        if self._design_provider is None:
            raise ValueError("design_provider required to check opportunity.design_ref")

        if self._design_provider.get_design(design_ref) is None:
            raise ReferenceIntegrityError(
                [(f"opportunity.design_ref={design_ref!r} does not resolve to a known design")]
            )

    def check_task(self, task: TasklistItem) -> None:
        """Validate task.design_ref and task.opportunity_ref when present."""
        violations: list[str] = []

        design_ref = (task.design_ref or "").strip()
        if design_ref:
            if self._design_provider is None:
                raise ValueError("design_provider required to check task.design_ref")
            if self._design_provider.get_design(design_ref) is None:
                violations.append(
                    f"task.design_ref={design_ref!r} does not resolve to a known "
                    f"design (task_id={task.task_id!r})"
                )

        opportunity_ref = (task.opportunity_ref or "").strip()
        if opportunity_ref:
            if self._opportunity_provider is None:
                raise ValueError("opportunity_provider required to check task.opportunity_ref")
            if self._opportunity_provider.get_opportunity(opportunity_ref) is None:
                violations.append(
                    f"task.opportunity_ref={opportunity_ref!r} does not resolve "
                    f"to a known opportunity (task_id={task.task_id!r})"
                )

        if violations:
            raise ReferenceIntegrityError(violations)

    def check_tasks(self, tasks: list[TasklistItem]) -> None:
        """Validate multiple tasks and report all violations in one error."""
        all_violations: list[str] = []
        for task in tasks:
            try:
                self.check_task(task)
            except ReferenceIntegrityError as exc:
                all_violations.extend(exc.violations)

        if all_violations:
            raise ReferenceIntegrityError(all_violations)
