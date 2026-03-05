"""Adapter helpers for reading loop contracts from the canonical registry."""

from __future__ import annotations

from collections.abc import Mapping

from millstone.loops.registry.loops import LOOP_REGISTRY
from millstone.loops.types.core import ArtifactType
from millstone.loops.types.loops import AgentRole, LoopDefinition, MechanicalCheck

ARTIFACT_TYPE_TO_MODEL: dict[ArtifactType, str] = {
    ArtifactType.TASKLIST: "TasklistItem",
    ArtifactType.DESIGN: "Design",
}


class LoopRegistryAdapter:
    def __init__(
        self,
        registry: Mapping[str, LoopDefinition] = LOOP_REGISTRY,
    ) -> None:
        self._registry = registry

    def get_loop(self, loop_id: str) -> LoopDefinition:
        try:
            return self._registry[loop_id]
        except KeyError as exc:
            raise KeyError(f"Unknown loop_id: {loop_id}") from exc

    def get_role(self, loop_id: str, role_id: str) -> AgentRole | None:
        loop = self.get_loop(loop_id)
        for role in loop.roles:
            if role.id == role_id:
                return role
        return None

    def get_checks(self, loop_id: str) -> list[MechanicalCheck]:
        return list(self.get_loop(loop_id).checks)

    def validate_role_id(self, loop_id: str, role_id: str) -> bool:
        loop = self._registry.get(loop_id)
        if loop is None:
            return False
        return any(role.id == role_id for role in loop.roles)

    def get_capability_tier(self, loop_id: str) -> str | None:
        loop = self._registry.get(loop_id)
        if loop is None:
            return None
        return loop.capability_tier
