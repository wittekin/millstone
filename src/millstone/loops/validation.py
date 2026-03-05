"""Validation helpers for canonical loop contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ValidationSeverity(Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationError:
    category: str
    entity_id: str
    field: str
    message: str
    referenced_id: str
    severity: ValidationSeverity = ValidationSeverity.ERROR


def validate_role_references() -> list[ValidationError]:
    """Ensure state actions only reference declared role IDs."""
    from millstone.loops.registry import LOOP_REGISTRY

    errors: list[ValidationError] = []
    for loop_id, loop in LOOP_REGISTRY.items():
        role_ids = {role.id for role in loop.roles}
        for action in loop.state_actions:
            if action.role_id not in role_ids:
                errors.append(
                    ValidationError(
                        category="role_reference",
                        entity_id=loop_id,
                        field="state_actions.role_id",
                        message=(
                            f"State action for state '{action.state}' references unknown role "
                            f"'{action.role_id}'"
                        ),
                        referenced_id=action.role_id,
                    )
                )
    return errors


def validate_model() -> list[ValidationError]:
    """Backwards-compatible model validation entry point."""
    return validate_role_references()


def validate_model_strict() -> None:
    """Raise on validation errors."""
    errors = validate_model()
    if errors:
        rendered = "\n".join(
            f"{e.entity_id}:{e.field}: {e.message} ({e.referenced_id})" for e in errors
        )
        raise ValueError(f"Loop model validation failed:\n{rendered}")


__all__ = [
    "ValidationError",
    "ValidationSeverity",
    "validate_model",
    "validate_model_strict",
    "validate_role_references",
]
