"""Core types for the pipeline stage abstraction.

HandoffKind describes the edges between outer-loop stages, mapping to the
ontology's three canonical artifact contracts (opportunity, design, worklist).
These are distinct from ``ArtifactType`` in ``loops/types/core.py`` which
describes artifacts *within* the inner loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class HandoffKind(str, Enum):
    """Kind of artifact flowing between pipeline stages."""

    OPPORTUNITY = "opportunity"  # maps to ontology: opportunity artifact contract
    DESIGN = "design"  # maps to ontology: design artifact contract
    WORKLIST = "worklist"  # maps to ontology: "selected, executable subset"


@dataclass
class StageItem:
    """A single item flowing through the pipeline.

    Wraps the underlying model object (Opportunity, Design, design-ref string,
    plan result dict) with pipeline metadata for tracking provenance.
    """

    kind: HandoffKind
    artifact: Any  # Opportunity | Design | str (design ref) | dict
    artifact_id: str
    source_stage: str | None = None
    parent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageResult:
    """Result of executing a stage on a batch of inputs."""

    success: bool
    outputs: list[StageItem] = field(default_factory=list)
    error: str | None = None
    checkpoint_data: dict[str, Any] = field(default_factory=dict)
    # Full MCP staging contract entries.
    # Each entry: {"type": str, "staging_file": str, "last_synced_index": int, "created_at": str}
    pending_mcp_syncs: list[dict[str, Any]] = field(default_factory=list)


@runtime_checkable
class Stage(Protocol):
    """Protocol for a pipeline stage.

    Stages are thin adapters that wrap existing orchestrator methods.
    They must NOT handle approval gates or checkpointing — those are
    pipeline executor responsibilities.
    """

    @property
    def name(self) -> str:
        """Unique name for this stage (e.g., 'analyze', 'design')."""
        ...

    @property
    def input_kind(self) -> HandoffKind | None:
        """Kind of artifact this stage consumes. None for entry points."""
        ...

    @property
    def output_kind(self) -> HandoffKind | None:
        """Kind of artifact this stage produces. None for terminals."""
        ...

    def execute(self, inputs: list[StageItem]) -> StageResult:
        """Execute the stage on the given inputs.

        For entry-point stages (input_kind is None), *inputs* is empty.
        For batch stages, one input may produce multiple output StageItems.
        """
        ...
