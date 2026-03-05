"""Core loop model types used by millstone's runtime loop contract."""

from __future__ import annotations

from enum import Enum


class ArtifactType(Enum):
    """Artifacts passed between loop states/roles."""

    TASKLIST = "tasklist"
    DIFF = "diff"
    FEEDBACK = "feedback"
    DECISION = "decision"
    COMMIT = "commit"
    DESIGN = "design"


class DecisionType(Enum):
    """Review verdicts that drive loop transitions."""

    APPROVED = "approved"
    REQUEST_CHANGES = "request_changes"
    REJECTED = "rejected"
    BLOCKED = "blocked"


__all__ = ["ArtifactType", "DecisionType"]
