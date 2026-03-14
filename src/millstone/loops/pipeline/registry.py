"""Stage registry for extensibility.

Built-in stages are auto-registered at import.  Third-party stages can be
registered via ``register_stage()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from millstone.loops.pipeline.stage import Stage

if TYPE_CHECKING:
    from millstone.runtime.orchestrator import Orchestrator

# Global registry: stage name → class
_STAGE_FACTORIES: dict[str, type] = {}


def register_stage(name: str, stage_cls: type) -> None:
    """Register a stage class by name."""
    _STAGE_FACTORIES[name] = stage_cls


def get_stage(name: str, orchestrator: Orchestrator, **kwargs: Any) -> Stage:
    """Instantiate a registered stage."""
    if name not in _STAGE_FACTORIES:
        available = sorted(_STAGE_FACTORIES)
        raise ValueError(f"Unknown pipeline stage: {name!r}. Available: {available}")
    return _STAGE_FACTORIES[name](orchestrator=orchestrator, **kwargs)


def list_stages() -> list[str]:
    """Return sorted list of registered stage names."""
    return sorted(_STAGE_FACTORIES)


def _register_builtins() -> None:
    from millstone.loops.pipeline.stages import (
        AnalyzeStage,
        DesignStage,
        ExecuteStage,
        PlanStage,
        ReviewDesignStage,
    )

    register_stage("analyze", AnalyzeStage)
    register_stage("design", DesignStage)
    register_stage("review_design", ReviewDesignStage)
    register_stage("plan", PlanStage)
    register_stage("execute", ExecuteStage)


_register_builtins()
