"""Unit tests for the pipeline module."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from millstone.loops.pipeline.executor import PipelineCheckpoint, PipelineExecutor
from millstone.loops.pipeline.injection import (
    inject_design,
    inject_opportunity,
    inject_worklist,
)
from millstone.loops.pipeline.pipeline import (
    ApprovalGate,
    PipelineDefinition,
    PreflightCheck,
    SelectionStrategy,
)
from millstone.loops.pipeline.registry import get_stage, list_stages, register_stage
from millstone.loops.pipeline.stage import HandoffKind, StageItem, StageResult

# ---------------------------------------------------------------------------
# Test fixtures: mock stages
# ---------------------------------------------------------------------------


class MockEntryStage:
    """Stage that produces opportunities from nothing."""

    name = "mock_entry"
    input_kind = None
    output_kind = HandoffKind.OPPORTUNITY

    def __init__(self, items: list[StageItem] | None = None, success: bool = True):
        self._items = items or []
        self._success = success

    def execute(self, inputs: list[StageItem]) -> StageResult:
        return StageResult(success=self._success, outputs=self._items)


class MockTransformStage:
    """Stage that transforms opportunities to designs."""

    name = "mock_transform"
    input_kind = HandoffKind.OPPORTUNITY
    output_kind = HandoffKind.DESIGN

    def __init__(self, success: bool = True, fail_on_item: int | None = None):
        self._success = success
        self._fail_on_item = fail_on_item
        self.received_inputs: list[StageItem] = []

    def execute(self, inputs: list[StageItem]) -> StageResult:
        self.received_inputs = list(inputs)
        outputs = []
        for i, item in enumerate(inputs):
            if self._fail_on_item is not None and i == self._fail_on_item:
                return StageResult(
                    success=False,
                    outputs=outputs,
                    error=f"Failed on item {i}",
                )
            outputs.append(
                StageItem(
                    kind=HandoffKind.DESIGN,
                    artifact=f"design-for-{item.artifact_id}",
                    artifact_id=f"design-{item.artifact_id}",
                    source_stage=self.name,
                    parent_id=item.artifact_id,
                )
            )
        if not self._success:
            return StageResult(success=False, error="generic failure")
        return StageResult(success=True, outputs=outputs)


class MockTerminalStage:
    """Terminal stage that consumes designs."""

    name = "mock_terminal"
    input_kind = HandoffKind.DESIGN
    output_kind = None

    def __init__(self, success: bool = True):
        self._success = success
        self.received_inputs: list[StageItem] = []

    def execute(self, inputs: list[StageItem]) -> StageResult:
        self.received_inputs = list(inputs)
        return StageResult(success=self._success)


def _make_opp_items(n: int, roi_scores: list[float] | None = None) -> list[StageItem]:
    """Create N opportunity StageItems with optional ROI scores."""

    @dataclass
    class FakeOpp:
        opportunity_id: str
        title: str
        roi_score: float

    items = []
    for i in range(n):
        roi = roi_scores[i] if roi_scores else float(i)
        opp = FakeOpp(
            opportunity_id=f"opp-{i}",
            title=f"Opportunity {i}",
            roi_score=roi,
        )
        items.append(
            StageItem(
                kind=HandoffKind.OPPORTUNITY,
                artifact=opp,
                artifact_id=f"opp-{i}",
                source_stage="test",
            )
        )
    return items


# ---------------------------------------------------------------------------
# PipelineDefinition.validate()
# ---------------------------------------------------------------------------


class TestPipelineValidation:
    def test_valid_chain(self):
        stages = [MockEntryStage(), MockTransformStage(), MockTerminalStage()]
        pipeline = PipelineDefinition(stages=stages)
        assert pipeline.validate() == []

    def test_empty_pipeline(self):
        pipeline = PipelineDefinition(stages=[])
        errors = pipeline.validate()
        assert len(errors) == 1
        assert "no stages" in errors[0].lower()

    def test_mismatched_kinds(self):
        # Entry produces OPPORTUNITY, terminal expects DESIGN — skip transform
        stages = [MockEntryStage(), MockTerminalStage()]
        pipeline = PipelineDefinition(stages=stages)
        errors = pipeline.validate()
        assert len(errors) == 1
        assert "mock_entry" in errors[0]
        assert "mock_terminal" in errors[0]

    def test_none_input_kind_for_entry(self):
        """Entry stage with input_kind=None is valid at position 0."""
        pipeline = PipelineDefinition(stages=[MockEntryStage()])
        assert pipeline.validate() == []

    def test_gate_references_unknown_stage(self):
        stages = [MockEntryStage()]
        gates = {"nonexistent": ApprovalGate(after_stage="nonexistent", gate_name="Test")}
        pipeline = PipelineDefinition(stages=stages, gates=gates)
        errors = pipeline.validate()
        assert any("nonexistent" in e for e in errors)

    def test_selection_references_unknown_stage(self):
        stages = [MockEntryStage()]
        selections = {"nonexistent": SelectionStrategy()}
        pipeline = PipelineDefinition(stages=stages, selections=selections)
        errors = pipeline.validate()
        assert any("nonexistent" in e for e in errors)


# ---------------------------------------------------------------------------
# SelectionStrategy
# ---------------------------------------------------------------------------


class TestSelectionStrategy:
    def test_all_mode_returns_everything(self):
        items = _make_opp_items(3)
        strategy = SelectionStrategy(mode="all")
        assert len(strategy.apply(items)) == 3

    def test_top_n_sorts_by_roi_score(self):
        items = _make_opp_items(5, roi_scores=[1.0, 5.0, 3.0, 2.0, 4.0])
        strategy = SelectionStrategy(mode="top_n", n=2, sort_key="roi_score")
        result = strategy.apply(items)
        assert len(result) == 2
        assert result[0].artifact.roi_score == 5.0
        assert result[1].artifact.roi_score == 4.0

    def test_top_1_returns_highest(self):
        items = _make_opp_items(3, roi_scores=[1.0, 3.0, 2.0])
        strategy = SelectionStrategy(mode="top_n", n=1, sort_key="roi_score")
        result = strategy.apply(items)
        assert len(result) == 1
        assert result[0].artifact_id == "opp-1"

    def test_filter_mode(self):
        items = _make_opp_items(3)
        strategy = SelectionStrategy(
            mode="filter",
            predicate=lambda i: i.artifact_id != "opp-1",
        )
        result = strategy.apply(items)
        assert len(result) == 2
        assert all(i.artifact_id != "opp-1" for i in result)

    def test_on_select_fires(self):
        items = _make_opp_items(3, roi_scores=[1.0, 3.0, 2.0])
        callback = MagicMock()
        strategy = SelectionStrategy(mode="top_n", n=1, sort_key="roi_score", on_select=callback)
        mock_result = StageResult(success=True)
        result = strategy.apply(items, mock_result)
        callback.assert_called_once_with(result, mock_result)

    def test_on_select_not_fired_without_result(self):
        items = _make_opp_items(1)
        callback = MagicMock()
        strategy = SelectionStrategy(on_select=callback)
        strategy.apply(items)  # no result arg
        callback.assert_not_called()


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------


class TestInjection:
    def test_inject_opportunity(self):
        item = inject_opportunity("Add caching layer")
        assert item.kind == HandoffKind.OPPORTUNITY
        assert item.artifact_id == "add-caching-layer"
        # Injected opportunities carry raw text, NOT Opportunity models
        assert item.artifact == "Add caching layer"
        assert isinstance(item.artifact, str)
        assert item.source_stage == "injection"
        assert item.metadata["original_text"] == "Add caching layer"

    def test_inject_opportunity_no_opportunity_id(self):
        """Injected opportunities have no opportunity_id attribute (raw text)."""
        item = inject_opportunity("Something")
        assert not hasattr(item.artifact, "opportunity_id")

    def test_inject_design(self):
        item = inject_design(".millstone/designs/my-feature.md")
        assert item.kind == HandoffKind.DESIGN
        assert item.artifact_id == ".millstone/designs/my-feature.md"
        assert item.source_stage == "injection"

    def test_inject_worklist(self):
        item = inject_worklist()
        assert item.kind == HandoffKind.WORKLIST
        assert item.artifact_id == "tasklist"
        assert item.source_stage == "injection"


# ---------------------------------------------------------------------------
# PipelineCheckpoint serialization
# ---------------------------------------------------------------------------


class TestPipelineCheckpoint:
    def test_round_trip(self):
        cp = PipelineCheckpoint(
            completed_stage="design",
            stage_index=2,
            items=[{"kind": "design", "artifact_id": "my-feature"}],
            stage_data={"key": "value"},
            pending_mcp_syncs=[
                {
                    "type": "designs",
                    "staging_file": "/tmp/staged.md",
                    "last_synced_index": 3,
                    "created_at": "2026-03-14T10:00:00Z",
                }
            ],
            completed_item_ids=["opp-1", "opp-2"],
            pipeline_stages=["analyze", "design", "plan"],
        )
        d = cp.to_dict()
        restored = PipelineCheckpoint.from_dict(d)

        assert restored.completed_stage == "design"
        assert restored.stage_index == 2
        assert len(restored.items) == 1
        assert restored.stage_data == {"key": "value"}
        assert len(restored.pending_mcp_syncs) == 1
        assert restored.pending_mcp_syncs[0]["last_synced_index"] == 3
        assert restored.completed_item_ids == ["opp-1", "opp-2"]
        assert restored.pipeline_stages == ["analyze", "design", "plan"]

    def test_from_dict_defaults(self):
        cp = PipelineCheckpoint.from_dict({"completed_stage": "analyze", "stage_index": 0})
        assert cp.items == []
        assert cp.stage_data == {}
        assert cp.pending_mcp_syncs == []
        assert cp.completed_item_ids == []
        assert cp.pipeline_stages == []


# ---------------------------------------------------------------------------
# PipelineExecutor
# ---------------------------------------------------------------------------


class TestPipelineExecutor:
    def _mock_orchestrator(self):
        orch = MagicMock()
        orch._outer_loop_manager = MagicMock()
        return orch

    def test_simple_pipeline(self):
        items = _make_opp_items(2)
        entry = MockEntryStage(items=items)
        transform = MockTransformStage()
        terminal = MockTerminalStage()

        pipeline = PipelineDefinition(stages=[entry, transform, terminal])
        executor = PipelineExecutor(pipeline, self._mock_orchestrator())
        result = executor.run()

        assert result == 0
        assert len(transform.received_inputs) == 2
        assert len(terminal.received_inputs) == 2

    def test_entry_stage_no_initial_items(self):
        items = _make_opp_items(1)
        entry = MockEntryStage(items=items)
        transform = MockTransformStage()

        pipeline = PipelineDefinition(stages=[entry, transform])
        executor = PipelineExecutor(pipeline, self._mock_orchestrator())
        result = executor.run()

        assert result == 0
        assert len(transform.received_inputs) == 1

    def test_initial_items(self):
        items = _make_opp_items(3)
        transform = MockTransformStage()
        terminal = MockTerminalStage()

        pipeline = PipelineDefinition(stages=[transform, terminal])
        executor = PipelineExecutor(pipeline, self._mock_orchestrator())
        result = executor.run(initial_items=items)

        assert result == 0
        assert len(transform.received_inputs) == 3

    def test_stage_failure_returns_1(self):
        entry = MockEntryStage(items=_make_opp_items(1))
        transform = MockTransformStage(success=False)

        pipeline = PipelineDefinition(stages=[entry, transform])
        executor = PipelineExecutor(pipeline, self._mock_orchestrator())
        result = executor.run()

        assert result == 1

    def test_validation_failure_returns_1(self):
        # Mismatched kinds
        entry = MockEntryStage()
        terminal = MockTerminalStage()
        pipeline = PipelineDefinition(stages=[entry, terminal])
        executor = PipelineExecutor(pipeline, self._mock_orchestrator())
        result = executor.run()

        assert result == 1

    def test_preflight_failure_returns_1(self):
        entry = MockEntryStage(items=[])
        pipeline = PipelineDefinition(
            stages=[entry],
            preflights=[
                PreflightCheck(
                    check=lambda: (_ for _ in ()).throw(RuntimeError("nope")),
                    description="always fails",
                )
            ],
        )
        executor = PipelineExecutor(pipeline, self._mock_orchestrator())
        result = executor.run()
        assert result == 1

    def test_gate_halts_pipeline(self):
        items = _make_opp_items(1)
        entry = MockEntryStage(items=items)
        transform = MockTransformStage()
        terminal = MockTerminalStage()

        gates = {"mock_entry": ApprovalGate(after_stage="mock_entry", gate_name="Test Gate")}
        pipeline = PipelineDefinition(stages=[entry, transform, terminal], gates=gates)

        orch = self._mock_orchestrator()
        executor = PipelineExecutor(pipeline, orch, enforce_gates=True)
        result = executor.run()

        assert result == 0  # halted successfully
        orch.save_outer_loop_checkpoint.assert_called_once()
        # Transform should NOT have been called
        assert len(transform.received_inputs) == 0

    def test_gate_skipped_when_not_enforced(self):
        items = _make_opp_items(1)
        entry = MockEntryStage(items=items)
        transform = MockTransformStage()
        terminal = MockTerminalStage()

        gates = {"mock_entry": ApprovalGate(after_stage="mock_entry", gate_name="Test Gate")}
        pipeline = PipelineDefinition(stages=[entry, transform, terminal], gates=gates)

        executor = PipelineExecutor(pipeline, self._mock_orchestrator(), enforce_gates=False)
        result = executor.run()

        assert result == 0
        # Transform SHOULD have been called
        assert len(transform.received_inputs) == 1

    def test_disabled_gate_does_not_halt(self):
        items = _make_opp_items(1)
        entry = MockEntryStage(items=items)
        transform = MockTransformStage()

        gates = {
            "mock_entry": ApprovalGate(after_stage="mock_entry", gate_name="Test", enabled=False)
        }
        pipeline = PipelineDefinition(stages=[entry, transform], gates=gates)

        executor = PipelineExecutor(pipeline, self._mock_orchestrator(), enforce_gates=True)
        result = executor.run()

        assert result == 0
        assert len(transform.received_inputs) == 1

    def test_selection_strategy_applied(self):
        items = _make_opp_items(3, roi_scores=[1.0, 3.0, 2.0])
        entry = MockEntryStage(items=items)
        transform = MockTransformStage()

        selections = {"mock_entry": SelectionStrategy(mode="top_n", n=1, sort_key="roi_score")}
        pipeline = PipelineDefinition(stages=[entry, transform], selections=selections)

        executor = PipelineExecutor(pipeline, self._mock_orchestrator())
        result = executor.run()

        assert result == 0
        assert len(transform.received_inputs) == 1
        assert transform.received_inputs[0].artifact_id == "opp-1"  # highest ROI

    def test_partial_batch_failure_saves_checkpoint(self):
        """When a batch stage fails after processing some items, save checkpoint."""
        items = _make_opp_items(3)
        entry = MockEntryStage(items=items)
        transform = MockTransformStage(fail_on_item=2)  # fails on 3rd item

        pipeline = PipelineDefinition(stages=[entry, transform])
        orch = self._mock_orchestrator()
        executor = PipelineExecutor(pipeline, orch)
        result = executor.run()

        assert result == 1
        # Should have saved checkpoint with partial progress
        orch.save_outer_loop_checkpoint.assert_called_once()
        call_kwargs = orch.save_outer_loop_checkpoint.call_args
        assert "mock_transform_partial" in str(call_kwargs)

    def test_partial_batch_saves_input_ids_not_output_ids(self):
        """Checkpoint must track consumed input IDs, not produced output IDs."""
        items = _make_opp_items(3)
        entry = MockEntryStage(items=items)
        transform = MockTransformStage(fail_on_item=2)  # processes items 0,1 ok

        pipeline = PipelineDefinition(stages=[entry, transform])
        orch = self._mock_orchestrator()
        executor = PipelineExecutor(pipeline, orch)
        executor.run()

        # Extract the pipeline_checkpoint from the save call
        call_args = orch.save_outer_loop_checkpoint.call_args
        cp_dict = call_args[1]["pipeline_checkpoint"]
        cp = PipelineCheckpoint.from_dict(cp_dict)

        # completed_item_ids should be INPUT IDs (opp-0, opp-1), not output IDs
        assert cp.completed_item_ids == ["opp-0", "opp-1"]

    def test_gate_checkpoint_includes_pipeline_stages(self):
        """Gate checkpoints must persist the original pipeline stage names."""
        items = _make_opp_items(1)
        entry = MockEntryStage(items=items)
        transform = MockTransformStage()
        terminal = MockTerminalStage()

        gates = {"mock_entry": ApprovalGate(after_stage="mock_entry", gate_name="Test")}
        pipeline = PipelineDefinition(stages=[entry, transform, terminal], gates=gates)

        orch = self._mock_orchestrator()
        executor = PipelineExecutor(pipeline, orch, enforce_gates=True)
        executor.run()

        call_args = orch.save_outer_loop_checkpoint.call_args
        cp_dict = call_args[1]["pipeline_checkpoint"]
        cp = PipelineCheckpoint.from_dict(cp_dict)
        assert cp.pipeline_stages == ["mock_entry", "mock_transform", "mock_terminal"]

    def test_serialization_preserves_injected_text(self):
        """Injected items must round-trip their original text through checkpoints."""
        item = StageItem(
            kind=HandoffKind.OPPORTUNITY,
            artifact="Add caching layer for API responses",
            artifact_id="add-caching-layer-for-api-respon",
            source_stage="injection",
            metadata={"original_text": "Add caching layer for API responses"},
        )

        orch = self._mock_orchestrator()
        pipeline = PipelineDefinition(stages=[MockEntryStage()])
        executor = PipelineExecutor(pipeline, orch)

        serialized = executor._serialize_items([item])
        assert serialized[0]["artifact_text"] == "Add caching layer for API responses"

        restored = executor._deserialize_items(serialized)
        assert restored[0].artifact == "Add caching layer for API responses"
        assert restored[0].artifact != "add-caching-layer-for-api-respon"

    def test_zero_output_failure_still_saves_checkpoint(self):
        """Failure on the first item must still save a checkpoint for --continue."""
        items = _make_opp_items(2)
        entry = MockEntryStage(items=items)
        transform = MockTransformStage(fail_on_item=0)  # fails on 1st item

        pipeline = PipelineDefinition(stages=[entry, transform])
        orch = self._mock_orchestrator()
        executor = PipelineExecutor(pipeline, orch)
        result = executor.run()

        assert result == 1
        # Checkpoint must be saved even with zero completed outputs
        orch.save_outer_loop_checkpoint.assert_called_once()
        call_args = orch.save_outer_loop_checkpoint.call_args
        cp_dict = call_args[1]["pipeline_checkpoint"]
        cp = PipelineCheckpoint.from_dict(cp_dict)
        assert cp.completed_stage == "mock_transform_partial"
        assert cp.completed_item_ids == []  # nothing completed
        # But the inputs are preserved for resume
        assert len(cp.items) == 2
        assert cp.pipeline_stages == ["mock_entry", "mock_transform"]

    def test_zero_output_failure_preserves_injected_text(self):
        """Even with zero outputs, injected text must survive in checkpoint."""
        injected = StageItem(
            kind=HandoffKind.OPPORTUNITY,
            artifact="Build a caching layer",
            artifact_id="build-a-caching-layer",
            source_stage="injection",
            metadata={"original_text": "Build a caching layer"},
        )
        transform = MockTransformStage(fail_on_item=0)

        pipeline = PipelineDefinition(stages=[transform])
        orch = self._mock_orchestrator()
        executor = PipelineExecutor(pipeline, orch)
        executor.run(initial_items=[injected])

        cp_dict = orch.save_outer_loop_checkpoint.call_args[1]["pipeline_checkpoint"]
        cp = PipelineCheckpoint.from_dict(cp_dict)
        assert cp.items[0]["artifact_text"] == "Build a caching layer"


# ---------------------------------------------------------------------------
# Resume pipeline
# ---------------------------------------------------------------------------


class TestBuildResumePipeline:
    def test_resume_filters_gates_to_remaining_stages(self):
        """Gates referencing stages not in the resumed pipeline must be dropped."""
        from millstone.loops.pipeline.cli import build_resume_pipeline

        cp = PipelineCheckpoint(
            completed_stage="analyze",
            stage_index=0,
            pipeline_stages=["analyze", "design", "plan"],
        )
        config = {
            "approve_opportunities": True,  # gate after "analyze" — not in remaining
            "approve_plans": True,  # gate after "plan" — IS in remaining
            "review_designs": False,
        }
        orch = MagicMock()
        pipeline = build_resume_pipeline(cp, orch, config)

        # "analyze" gate should be filtered out; "plan" gate should remain
        assert "analyze" not in pipeline.gates
        assert "plan" in pipeline.gates
        # Pipeline should only have design, plan (remaining after analyze)
        stage_names = [s.name for s in pipeline.stages]
        assert stage_names == ["design", "plan"]
        # validate() should pass (no dangling gate references)
        assert pipeline.validate() == []


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestStageRegistry:
    def test_builtin_stages_registered(self):
        stages = list_stages()
        assert "analyze" in stages
        assert "design" in stages
        assert "review_design" in stages
        assert "plan" in stages
        assert "execute" in stages

    def test_custom_stage_registration(self):
        class CustomStage:
            name = "custom"
            input_kind = None
            output_kind = None

            def __init__(self, orchestrator=None):
                pass

            def execute(self, inputs):
                return StageResult(success=True)

        register_stage("custom_test", CustomStage)
        assert "custom_test" in list_stages()

        orch = MagicMock()
        stage = get_stage("custom_test", orch)
        assert stage.name == "custom"

    def test_unknown_stage_raises(self):
        orch = MagicMock()
        with pytest.raises(ValueError, match="Unknown pipeline stage"):
            get_stage("nonexistent_stage_xyz", orch)


# ---------------------------------------------------------------------------
# HandoffKind
# ---------------------------------------------------------------------------


class TestHandoffKind:
    def test_values(self):
        assert HandoffKind.OPPORTUNITY.value == "opportunity"
        assert HandoffKind.DESIGN.value == "design"
        assert HandoffKind.WORKLIST.value == "worklist"

    def test_from_string(self):
        assert HandoffKind("opportunity") == HandoffKind.OPPORTUNITY
        assert HandoffKind("design") == HandoffKind.DESIGN
        assert HandoffKind("worklist") == HandoffKind.WORKLIST
