"""Composable pipeline for chaining outer-loop stages."""

from millstone.loops.pipeline.stage import HandoffKind, Stage, StageItem, StageResult

__all__ = [
    "HandoffKind",
    "Stage",
    "StageItem",
    "StageResult",
]
