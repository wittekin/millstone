"""Shared decision-gate contract for non-interactive approval/resume flows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DECISION_GATE_EXIT_CODE = 2


@dataclass
class DecisionGate:
    """Serializable description of a pending human/supervisor decision."""

    gate_type: str
    title: str
    message: str
    resume_commands: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def halt_reason(self) -> str:
        return f"decision_gate:{self.gate_type}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_type": self.gate_type,
            "title": self.title,
            "message": self.message,
            "resume_commands": list(self.resume_commands),
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> DecisionGate | None:
        if not payload:
            return None
        return cls(
            gate_type=str(payload.get("gate_type", "")),
            title=str(payload.get("title", "")),
            message=str(payload.get("message", "")),
            resume_commands=[str(cmd) for cmd in payload.get("resume_commands", [])],
            details=dict(payload.get("details", {})),
        )


class DecisionGateHalt(RuntimeError):
    """Raised when execution must halt for an explicit supervisor decision."""

    def __init__(self, gate: DecisionGate):
        super().__init__(gate.message)
        self.gate = gate
