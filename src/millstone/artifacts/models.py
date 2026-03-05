"""Canonical artifact model dataclasses and status enums for millstone.

These are pure data definitions with no imports from other millstone modules.
"""

import re
from dataclasses import dataclass, field
from enum import Enum


class ArtifactValidationError(ValueError):
    """Raised when an artifact model fails contract validation."""

    def __init__(self, artifact_type: str, violations: list[str]) -> None:
        self.artifact_type = artifact_type
        self.violations = violations
        message = f"{artifact_type} validation failed:\n" + "\n".join(
            f"  - {violation}" for violation in violations
        )
        super().__init__(message)


class OpportunityStatus(str, Enum):
    identified = "identified"
    adopted = "adopted"
    rejected = "rejected"


class DesignStatus(str, Enum):
    draft = "draft"
    reviewed = "reviewed"
    approved = "approved"
    superseded = "superseded"


class TaskStatus(str, Enum):
    todo = "todo"
    in_progress = "in_progress"
    done = "done"
    blocked = "blocked"


@dataclass
class Opportunity:
    opportunity_id: str  # canonical identity (explicit ID field, else title slug)
    title: str
    status: OpportunityStatus
    description: str
    requires_design: bool | None = None
    design_ref: str | None = None
    source_ref: str | None = None
    priority: str | None = None
    roi_score: float | None = None
    raw: str | None = None  # preserved raw markdown block for round-trip fidelity

    def validate(self) -> None:
        violations = []

        if not isinstance(self.opportunity_id, str) or not self.opportunity_id.strip():
            violations.append("opportunity_id is required and must not be empty")
        elif not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", self.opportunity_id):
            violations.append(
                "opportunity_id must match slug pattern [a-z0-9]([a-z0-9-]*[a-z0-9])?"
            )

        if not isinstance(self.title, str) or not self.title.strip():
            violations.append("title is required and must not be empty")

        if not isinstance(self.status, OpportunityStatus):
            violations.append("status must be an OpportunityStatus value")

        if not isinstance(self.description, str) or not self.description.strip():
            violations.append("description is required and must not be empty")

        if violations:
            raise ArtifactValidationError("Opportunity", violations)


@dataclass
class Design:
    design_id: str  # canonical identity (slug-like)
    title: str
    status: DesignStatus
    body: str  # full document body (markdown, excluding metadata header)
    opportunity_ref: str | None = None  # None only for legacy records parsed from disk;
    # write paths enforce presence via validate().
    tasklist_ref: str | None = None
    review_summary: str | None = None

    def validate(self) -> None:
        violations = []

        if not isinstance(self.design_id, str) or not self.design_id.strip():
            violations.append("design_id is required and must not be empty")
        elif not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", self.design_id):
            violations.append("design_id must match slug pattern [a-z0-9]([a-z0-9-]*[a-z0-9])?")

        if not isinstance(self.title, str) or not self.title.strip():
            violations.append("title is required and must not be empty")

        if not isinstance(self.status, DesignStatus):
            violations.append("status must be a DesignStatus value")

        if not isinstance(self.opportunity_ref, str) or not self.opportunity_ref.strip():
            violations.append("opportunity_ref is required and must not be empty")

        if not isinstance(self.body, str) or not self.body.strip():
            violations.append("body is required and must not be empty")

        if violations:
            raise ArtifactValidationError("Design", violations)


@dataclass
class TasklistItem:
    task_id: str  # stable item id
    title: str
    status: TaskStatus
    design_ref: str | None = None
    opportunity_ref: str | None = None
    risk: str | None = None
    tests: str | None = None
    criteria: str | None = None
    context: str | None = None
    raw: str | None = None  # preserved raw markdown block

    def validate(self) -> None:
        violations = []

        if not isinstance(self.task_id, str) or not self.task_id.strip():
            violations.append("task_id is required and must not be empty")
        elif not re.fullmatch(r"[a-z0-9_-]{1,40}", self.task_id):
            violations.append("task_id must match pattern [a-z0-9_-]{1,40}")

        if not isinstance(self.title, str) or not self.title.strip():
            violations.append("title is required and must not be empty")

        if not isinstance(self.status, TaskStatus):
            violations.append("status must be a TaskStatus value")

        if violations:
            raise ArtifactValidationError("TasklistItem", violations)


class EvidenceKind(str, Enum):
    """Classification of an evidence record by the loop boundary that produced it.

    review:        Outcome of the reviewer agent evaluating a task implementation.
    eval:          Outcome of running the test/eval suite.
    design_review: Outcome of the design review agent evaluating a design artifact.
    sanity_check:  Outcome of a sanity check (implementation or review).
    merge:         Outcome of the merge/integration pipeline gate.
    effect:        Outcome of applying or observing a remote effect (EffectRecord link).
    """

    review = "review"
    eval = "eval"
    design_review = "design_review"
    sanity_check = "sanity_check"
    merge = "merge"
    effect = "effect"


@dataclass
class EvidenceRecord:
    """Normalized evidence artifact produced at a loop gate boundary.

    Fields:
        evidence_id:     Stable identifier, format "<timestamp>-<kind>[-<slug>]".
        kind:            Which loop boundary produced this record.
        timestamp:       ISO 8601 UTC timestamp of emission.
        outcome:         Human-readable outcome token.
                         Canonical values by kind:
                           review        -> "approved" | "request_changes"
                           eval          -> "passed"   | "failed"
                           design_review -> "approved" | "needs_revision"
                           sanity_check  -> "ok"       | "halt"
                           merge         -> "merged"   | "conflict" | "safety_fail" | "eval_fail"
                           effect        -> "applied"  | "skipped"  | "failed"      | "denied"
        work_item_id:    Canonical identity of the work item this evidence covers.
                         For tasks: task_id or task text slug.
                         For designs: design_id.
                         None when not tied to a specific work item.
        work_item_kind:  "task" | "design" | "opportunity" | None.
        capability_tier: The active profile's capability tier at emission time.
        detail:          Kind-specific payload dictionary.
    """

    evidence_id: str
    kind: EvidenceKind
    timestamp: str
    outcome: str
    work_item_id: str | None = None
    work_item_kind: str | None = None
    capability_tier: str | None = None
    detail: dict = field(default_factory=dict)
