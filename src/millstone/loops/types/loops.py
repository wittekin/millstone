"""Loop definition dataclasses used by the dev-review contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from millstone.loops.types.core import ArtifactType, DecisionType


class ContextMode(Enum):
    AMBIENT = "ambient"
    INJECTED = "injected"


class ContextType(Enum):
    CODEBASE = "codebase"
    REVIEW_GUIDELINES = "review_guidelines"
    PRIOR_FEEDBACK = "prior_feedback"


@dataclass
class ContextRequirement:
    context_type: ContextType
    mode: ContextMode
    source: str | None = None
    required: bool = True


class TransitionTrigger(Enum):
    VERDICT = "verdict"
    OUTCOME = "outcome"
    ALWAYS = "always"


@dataclass
class TransitionCondition:
    trigger: TransitionTrigger
    value: str | DecisionType | None = None

    @classmethod
    def verdict(cls, v: DecisionType) -> TransitionCondition:
        return cls(TransitionTrigger.VERDICT, v)

    @classmethod
    def outcome(cls, o: str) -> TransitionCondition:
        return cls(TransitionTrigger.OUTCOME, o)

    @classmethod
    def always(cls) -> TransitionCondition:
        return cls(TransitionTrigger.ALWAYS, None)


@dataclass
class Transition:
    from_state: str
    condition: TransitionCondition
    to_state: str
    max_iterations: int | None = None


class CheckType(Enum):
    LOC_THRESHOLD = "loc_threshold"
    PATTERN_MATCH = "pattern_match"


@dataclass
class MechanicalCheck:
    id: str
    name: str
    description: str
    check_type: CheckType | str
    threshold: int | float | None = None
    patterns: list[str] | None = None

    def get_check_type(self) -> CheckType:
        """Return check type as enum for compatibility with orchestrator checks."""
        if isinstance(self.check_type, CheckType):
            return self.check_type
        return CheckType(self.check_type)

    def get_threshold_value(self) -> int | float | None:
        """Return raw numeric threshold value."""
        return self.threshold


@dataclass
class AgentRole:
    id: str
    name: str
    input_artifacts: list[ArtifactType]
    input_context: list[str]
    guidance_prompt: str
    output_type: ArtifactType
    output_schema: str | None = None
    context_requirements: list[ContextRequirement] = field(default_factory=list)


@dataclass
class ArtifactSource:
    artifact_type: ArtifactType
    source: str
    filter: str | None = None


@dataclass
class ArtifactDisposition:
    artifact_type: ArtifactType
    action: str
    destination: str
    notify: list[str] | None = None


@dataclass
class QualityGate:
    gate_type: str
    criteria: dict[str, Any]


@dataclass
class StateAction:
    state: str
    role_id: str
    inputs: list[ArtifactType] = field(default_factory=list)
    outputs: list[ArtifactType] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)


@dataclass
class LoopDefinition:
    id: str
    name: str
    description: str
    function: str

    roles: list[AgentRole] = field(default_factory=list)
    checks: list[MechanicalCheck] = field(default_factory=list)

    initial_state: str = "start"
    transitions: list[Transition] = field(default_factory=list)
    terminal_states: set[str] = field(default_factory=lambda: {"done", "halted"})
    state_actions: list[StateAction] = field(default_factory=list)

    input_sources: list[ArtifactSource] = field(default_factory=list)
    output_dispositions: list[ArtifactDisposition] = field(default_factory=list)
    quality_gates: list[QualityGate] = field(default_factory=list)

    produces: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)

    capability_tier: str | None = None


__all__ = [
    "AgentRole",
    "ArtifactDisposition",
    "ArtifactSource",
    "CheckType",
    "ContextMode",
    "ContextRequirement",
    "ContextType",
    "LoopDefinition",
    "MechanicalCheck",
    "QualityGate",
    "StateAction",
    "Transition",
    "TransitionCondition",
    "TransitionTrigger",
]
