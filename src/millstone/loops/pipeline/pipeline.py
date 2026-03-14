"""Pipeline definition, selection strategies, and approval gates."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from millstone.loops.pipeline.stage import Stage, StageItem, StageResult


@dataclass
class SelectionStrategy:
    """Controls which items flow from one stage to the next.

    Attributes:
        mode: Selection mode — "all", "top_n", or "filter".
        n: Number of items to keep when mode is "top_n".
        sort_key: Attribute name on the wrapped artifact for sorting (e.g. "roi_score").
        sort_reverse: Sort descending (default True).
        predicate: Custom filter function when mode is "filter".
        on_select: Side-effect callback fired after selection with the chosen
            items and the originating StageResult.  Used for adoption status
            transitions (marking an opportunity as adopted after top-1 pick).
    """

    mode: str = "all"
    n: int | None = None
    sort_key: str | None = None
    sort_reverse: bool = True
    predicate: Callable[[StageItem], bool] | None = None
    on_select: Callable[[list[StageItem], StageResult], None] | None = None

    def apply(
        self,
        items: list[StageItem],
        result: StageResult | None = None,
    ) -> list[StageItem]:
        """Apply selection logic and fire on_select callback."""
        selected = list(items)

        if self.mode == "filter" and self.predicate is not None:
            selected = [i for i in selected if self.predicate(i)]

        if self.sort_key:
            selected = sorted(
                selected,
                key=lambda i: getattr(i.artifact, self.sort_key or "", 0) or 0,
                reverse=self.sort_reverse,
            )

        if self.mode == "top_n" and self.n is not None:
            selected = selected[: self.n]

        if self.on_select is not None and result is not None:
            self.on_select(selected, result)

        return selected


@dataclass
class ApprovalGate:
    """An approval gate that can halt the pipeline between stages.

    Attributes:
        after_stage: Name of the stage this gate follows.
        gate_name: Human-readable name shown in the halt message.
        enabled: Whether the gate is active.
    """

    after_stage: str
    gate_name: str
    enabled: bool = True


@dataclass
class PreflightCheck:
    """Validation run before the pipeline starts.

    Attributes:
        check: Callable that raises on failure.
        description: Human-readable description of the check.
    """

    check: Callable[[], None]
    description: str


@dataclass
class PipelineDefinition:
    """A composable sequence of stages with optional gates and selection.

    Attributes:
        stages: Ordered list of stages to execute.
        gates: Approval gates keyed by the stage name they follow.
        selections: Selection strategies keyed by the stage name they follow.
        preflights: Checks run before pipeline execution begins.
    """

    stages: Sequence[Stage]
    gates: dict[str, ApprovalGate] = field(default_factory=dict)
    selections: dict[str, SelectionStrategy] = field(default_factory=dict)
    preflights: list[PreflightCheck] = field(default_factory=list)

    def validate(self) -> list[str]:
        """Check that adjacent stages have compatible handoff kinds.

        Returns a list of error strings (empty if valid).
        """
        errors: list[str] = []
        if not self.stages:
            errors.append("Pipeline has no stages")
            return errors

        for i in range(1, len(self.stages)):
            prev = self.stages[i - 1]
            curr = self.stages[i]
            if prev.output_kind != curr.input_kind:
                errors.append(
                    f"Stage '{prev.name}' outputs {prev.output_kind!r} "
                    f"but stage '{curr.name}' expects {curr.input_kind!r}"
                )

        # Validate gate references
        stage_names = {s.name for s in self.stages}
        for gate_key in self.gates:
            if gate_key not in stage_names:
                errors.append(f"Gate references unknown stage '{gate_key}'")

        # Validate selection references
        for sel_key in self.selections:
            if sel_key not in stage_names:
                errors.append(f"Selection references unknown stage '{sel_key}'")

        return errors
