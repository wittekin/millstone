"""Pipeline executor — runs a PipelineDefinition, managing gates and checkpoints.

This is the single place where halting, checkpoint saving, and resume logic
lives, replacing all duplicated chaining in main() and run_cycle().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from millstone.loops.pipeline.pipeline import PipelineDefinition
from millstone.loops.pipeline.stage import HandoffKind, StageItem, StageResult
from millstone.utils import progress

if TYPE_CHECKING:
    from millstone.runtime.orchestrator import Orchestrator


@dataclass
class PipelineCheckpoint:
    """Serializable checkpoint for pipeline resume.

    Attributes:
        completed_stage: Name of last completed stage (or "<name>_partial" for
            mid-batch failures).
        stage_index: Index into pipeline.stages.  For partial failures this is
            the index of the *failed* stage (resume re-enters it).
        items: Serialized StageItems pending for the next stage (or remaining
            items for partial resume).
        stage_data: Opaque stage-specific data for resume context.
        pending_mcp_syncs: Full MCP sync contract entries with ``type``,
            ``staging_file``, ``last_synced_index``, ``created_at``.
        completed_item_ids: *Input* artifact IDs already processed before a
            mid-batch failure.  On resume these are filtered from the stage's
            inputs so they are not reprocessed.
        pipeline_stages: Ordered list of stage names from the original pipeline.
            Persisted so that resume reconstructs the same pipeline shape
            instead of inferring one from a canonical stage order.
    """

    completed_stage: str
    stage_index: int
    items: list[dict[str, Any]] = field(default_factory=list)
    stage_data: dict[str, Any] = field(default_factory=dict)
    pending_mcp_syncs: list[dict[str, Any]] = field(default_factory=list)
    completed_item_ids: list[str] = field(default_factory=list)
    pipeline_stages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed_stage": self.completed_stage,
            "stage_index": self.stage_index,
            "items": self.items,
            "stage_data": self.stage_data,
            "pending_mcp_syncs": self.pending_mcp_syncs,
            "completed_item_ids": self.completed_item_ids,
            "pipeline_stages": self.pipeline_stages,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PipelineCheckpoint:
        return cls(
            completed_stage=d["completed_stage"],
            stage_index=d["stage_index"],
            items=d.get("items", []),
            stage_data=d.get("stage_data", {}),
            pending_mcp_syncs=d.get("pending_mcp_syncs", []),
            completed_item_ids=d.get("completed_item_ids", []),
            pipeline_stages=d.get("pipeline_stages", []),
        )


class PipelineExecutor:
    """Executes a PipelineDefinition, managing gates and checkpoints."""

    def __init__(
        self,
        pipeline: PipelineDefinition,
        orchestrator: Orchestrator,
        enforce_gates: bool = True,
    ) -> None:
        self.pipeline = pipeline
        self.orchestrator = orchestrator
        self.enforce_gates = enforce_gates

    def run(
        self,
        *,
        initial_items: list[StageItem] | None = None,
        resume_from: PipelineCheckpoint | None = None,
    ) -> int:
        """Execute the pipeline.  Returns exit code (0=success, 1=failure/halt)."""
        # Capture original pipeline shape for checkpoints
        self._stage_names = [s.name for s in self.pipeline.stages]

        # Validate pipeline
        errors = self.pipeline.validate()
        if errors:
            for e in errors:
                progress(f"Pipeline validation error: {e}")
            return 1

        # Run preflights
        for pf in self.pipeline.preflights:
            try:
                pf.check()
            except Exception as exc:
                progress(f"Preflight failed ({pf.description}): {exc}")
                return 1

        # Determine starting point
        if resume_from:
            # Sync any pending MCP writes from the checkpoint
            if resume_from.pending_mcp_syncs:
                self.orchestrator._sync_pending_mcp_writes(  # noqa: SLF001
                    resume_from.pending_mcp_syncs
                )

            is_partial = resume_from.completed_stage.endswith("_partial")
            start_index = resume_from.stage_index if is_partial else resume_from.stage_index + 1

            current_items = self._deserialize_items(resume_from.items)

            # For partial resume, filter out already-completed items
            if is_partial and resume_from.completed_item_ids:
                completed_set = set(resume_from.completed_item_ids)
                current_items = [
                    item for item in current_items if item.artifact_id not in completed_set
                ]
        else:
            start_index = 0
            current_items = initial_items or []

        for i in range(start_index, len(self.pipeline.stages)):
            stage = self.pipeline.stages[i]

            # Skip stages that require inputs when none are available.
            # This handles the zero-tasks case: PlanStage succeeds but
            # produces no WORKLIST items, so ExecuteStage is skipped.
            if stage.input_kind is not None and not current_items:
                progress(f"Pipeline stage: {stage.name} — skipped (no items)")
                continue

            progress(f"Pipeline stage: {stage.name}")

            # Execute stage
            result = stage.execute(current_items)

            if not result.success:
                # Always save a failure checkpoint so --continue can resume
                # with preserved inputs, pipeline shape, and injected text.
                # Track which *input* IDs were consumed (not output IDs).
                # Stages process inputs sequentially — N outputs means the
                # first N inputs were successfully processed.
                n_completed = len(result.outputs)
                consumed_input_ids = [item.artifact_id for item in current_items[:n_completed]]
                checkpoint = PipelineCheckpoint(
                    completed_stage=f"{stage.name}_partial",
                    stage_index=i,
                    items=self._serialize_items(current_items),
                    completed_item_ids=consumed_input_ids,
                    pending_mcp_syncs=result.pending_mcp_syncs,
                    pipeline_stages=self._stage_names,
                )
                self._save_checkpoint(checkpoint)
                if n_completed > 0:
                    progress(
                        f"Partial progress saved ({n_completed} of "
                        f"{len(current_items)} items). "
                        "Resume with: millstone --continue"
                    )
                else:
                    progress(
                        f"Stage '{stage.name}' failed: {result.error}  "
                        "Resume with: millstone --continue"
                    )
                return 1

            # Apply selection strategy for outputs of this stage
            selection = self.pipeline.selections.get(stage.name)
            current_items = selection.apply(result.outputs, result) if selection else result.outputs

            # Check for approval gate after this stage
            gate = self.pipeline.gates.get(stage.name)
            if gate and gate.enabled and self.enforce_gates:
                return self._halt_at_gate(gate, stage.name, i, result, current_items)

        return 0

    # -- Checkpoint helpers --------------------------------------------------

    def _halt_at_gate(
        self,
        gate: Any,
        stage_name: str,
        stage_index: int,
        result: StageResult,
        selected_items: list[StageItem],
    ) -> int:
        """Halt the pipeline at an approval gate and save checkpoint."""
        progress("")
        progress("=" * 60)
        progress(f"APPROVAL GATE: {gate.gate_name}")
        progress("=" * 60)
        progress("")
        progress("Re-run with: millstone --continue")
        progress("Or run with --no-approve for fully autonomous operation.")

        checkpoint = PipelineCheckpoint(
            completed_stage=stage_name,
            stage_index=stage_index,
            items=self._serialize_items(selected_items),
            stage_data=result.checkpoint_data,
            pending_mcp_syncs=result.pending_mcp_syncs,
            pipeline_stages=self._stage_names,
        )
        self._save_checkpoint(checkpoint)
        return 0

    def _save_checkpoint(self, checkpoint: PipelineCheckpoint) -> None:
        """Persist checkpoint via orchestrator's state mechanism."""
        self.orchestrator.save_outer_loop_checkpoint(
            checkpoint.completed_stage,
            pipeline_checkpoint=checkpoint.to_dict(),
        )

    # -- Serialization -------------------------------------------------------

    def _serialize_items(self, items: list[StageItem]) -> list[dict[str, Any]]:
        """Serialize StageItems for checkpoint persistence.

        For injected items (raw text), the ``original_text`` in metadata
        preserves the full content for faithful resume.  For provider-backed
        artifacts, the artifact_id is sufficient to re-load from the provider.
        """
        serialized = []
        for item in items:
            entry: dict[str, Any] = {
                "kind": item.kind.value,
                "artifact_id": item.artifact_id,
                "source_stage": item.source_stage,
                "parent_id": item.parent_id,
                "metadata": item.metadata,
            }
            # Persist raw artifact content for strings (injected text, design refs)
            if isinstance(item.artifact, str):
                entry["artifact_text"] = item.artifact
            serialized.append(entry)
        return serialized

    def _deserialize_items(self, data: list[dict[str, Any]]) -> list[StageItem]:
        """Reconstruct StageItems from checkpoint data.

        Injected items are restored from persisted ``artifact_text`` or
        ``metadata.original_text``.  Provider-backed items are re-loaded
        from the appropriate provider using kind + ID.
        """
        items = []
        for d in data:
            kind = HandoffKind(d["kind"])
            metadata = d.get("metadata", {})

            # Prefer persisted text, then metadata original_text, then provider
            artifact_text = d.get("artifact_text") or metadata.get("original_text")
            artifact = artifact_text or self._load_artifact(kind, d["artifact_id"])

            items.append(
                StageItem(
                    kind=kind,
                    artifact=artifact,
                    artifact_id=d["artifact_id"],
                    source_stage=d.get("source_stage"),
                    parent_id=d.get("parent_id"),
                    metadata=metadata,
                )
            )
        return items

    def _load_artifact(self, kind: HandoffKind, artifact_id: str) -> Any:
        """Load a provider-backed artifact by kind and ID."""
        olm = self.orchestrator._outer_loop_manager  # noqa: SLF001
        if kind == HandoffKind.OPPORTUNITY:
            opp = olm.opportunity_provider.get_opportunity(artifact_id)
            return opp if opp is not None else artifact_id
        elif kind == HandoffKind.DESIGN:
            design = olm.design_provider.get_design(artifact_id)
            return design if design is not None else artifact_id
        elif kind == HandoffKind.WORKLIST:
            return artifact_id
        return artifact_id
