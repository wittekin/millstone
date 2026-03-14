"""CLI flag → pipeline construction.

Maps argparse results to a ``PipelineDefinition`` plus initial ``StageItem``
list.  Replaces the ~400 lines of duplicated chaining logic in ``main()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from millstone.artifact_providers.file import FileOpportunityProvider
from millstone.artifacts.models import OpportunityStatus
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
from millstone.loops.pipeline.stage import StageItem, StageResult
from millstone.loops.pipeline.stages import (
    AnalyzeStage,
    DesignStage,
    ExecuteStage,
    PlanStage,
    ReviewDesignStage,
)

if TYPE_CHECKING:
    from millstone.runtime.orchestrator import Orchestrator

# Canonical stage ordering for --through resolution
STAGE_ORDER = ["analyze", "design", "review_design", "plan", "execute"]


def build_pipeline_from_args(
    args: Any,
    config: dict[str, Any],
    orchestrator: Orchestrator,
) -> tuple[PipelineDefinition, list[StageItem]]:
    """Map CLI flags to a PipelineDefinition + initial items.

    Raises ValueError if no pipeline-eligible CLI flags are found.
    """
    review_designs = config.get("review_designs", True)
    no_approve = getattr(args, "no_approve", False)

    # Resolve --through target (--complete is alias for --through execute)
    through = getattr(args, "through", None)
    if through is None and getattr(args, "complete", False):
        through = "execute"

    # Gate configuration
    gates = _build_gates(config, no_approve, review_designs)

    # --- --deliver ---
    if getattr(args, "deliver", None):
        return _build_deliver(args, config, orchestrator, review_designs)

    # --- --analyze ---
    if getattr(args, "analyze", False):
        return _build_from_analyze(args, config, orchestrator, review_designs, gates, through)

    # --- --design ---
    if getattr(args, "design", None):
        return _build_from_design(args, config, orchestrator, review_designs, gates, through)

    # --- --plan ---
    if getattr(args, "plan", None):
        return _build_from_plan(args, orchestrator, gates, through)

    raise ValueError("No pipeline-eligible CLI flags found")


def resolve_cycle_pipeline(
    orchestrator: Orchestrator,
    config: dict[str, Any],
    *,
    no_approve: bool = False,
    issues_file: str | None = None,
) -> tuple[PipelineDefinition, list[StageItem]]:
    """Triage logic for ``--cycle``.

    Checks pending tasks, roadmap, then falls through to analyze.
    Returns the appropriate pipeline + initial items.
    """

    from millstone.utils import progress

    review_designs = config.get("review_designs", True)
    gates = _build_gates(config, no_approve, review_designs)

    # 1. Sync any pending MCP writes from prior halted run
    state = orchestrator.load_state()
    if state:
        outer = state.get("outer_loop") or {}
        pending_syncs = outer.get("pending_mcp_syncs")
        if pending_syncs:
            orchestrator._sync_pending_mcp_writes(pending_syncs)  # noqa: SLF001
            # Resume from checkpoint if one exists
            stage = outer.get("stage")
            pipeline_cp = outer.get("pipeline_checkpoint")
            if pipeline_cp:
                # Pipeline-based resume
                from millstone.loops.pipeline.executor import PipelineCheckpoint

                checkpoint = PipelineCheckpoint.from_dict(pipeline_cp)
                remaining = build_resume_pipeline(checkpoint, orchestrator, config)
                items = _deserialize_items_from_checkpoint(checkpoint, orchestrator)
                return remaining, items
            elif stage:
                # Legacy checkpoint — fall through to let orchestrator handle
                pass

    # 2. Check pending tasks → short-circuit to execute
    if orchestrator.has_remaining_tasks():
        progress("Pending tasks found in tasklist. Executing tasks...")
        pipeline = PipelineDefinition(
            stages=[ExecuteStage(orchestrator)],
        )
        return pipeline, [inject_worklist()]

    # 3. Check roadmap goal
    olm = orchestrator._outer_loop_manager  # noqa: SLF001
    roadmap_goal = olm._get_next_roadmap_goal()  # noqa: SLF001
    if roadmap_goal:
        progress(f"Found goal in roadmap: {roadmap_goal[:50]}...")
        stages = _build_design_through_execute(orchestrator, review_designs)
        pipeline = PipelineDefinition(stages=stages, gates=_filter_gates_to_stages(gates, stages))
        return pipeline, [inject_opportunity(roadmap_goal)]

    # 4. Fall through → full cycle: analyze → design → plan → execute
    all_stages: list = [AnalyzeStage(orchestrator, issues_file=issues_file)]
    all_stages.extend(_build_design_through_execute(orchestrator, review_designs))

    selections = {
        "analyze": _build_analyze_selection(orchestrator),
    }

    pipeline = PipelineDefinition(
        stages=all_stages,
        gates=_filter_gates_to_stages(gates, all_stages),
        selections=selections,
    )
    return pipeline, []


def build_resume_pipeline(
    checkpoint: Any,
    orchestrator: Orchestrator,
    config: dict[str, Any],
) -> PipelineDefinition:
    """Build a pipeline containing only the stages remaining after the checkpoint.

    Uses the persisted ``pipeline_stages`` list to reconstruct the *original*
    pipeline shape.  Falls back to canonical stage order only when the
    checkpoint lacks a stage list (legacy checkpoints).
    """
    from millstone.loops.pipeline.registry import get_stage

    no_approve = config.get("no_approve", False)
    review_designs = config.get("review_designs", True)
    gates = _build_gates(config, no_approve, review_designs)

    completed = checkpoint.completed_stage.removesuffix("_partial")
    is_partial = checkpoint.completed_stage.endswith("_partial")

    # Use persisted pipeline shape if available; fall back to canonical order
    original_stages = checkpoint.pipeline_stages or STAGE_ORDER

    try:
        completed_idx = original_stages.index(completed)
    except ValueError:
        completed_idx = 0

    remaining_names = (
        original_stages[completed_idx:] if is_partial else original_stages[completed_idx + 1 :]
    )

    stages = [get_stage(name, orchestrator) for name in remaining_names]

    # Filter gates to only reference stages present in the resumed pipeline
    remaining_set = set(remaining_names)
    filtered_gates = {k: v for k, v in gates.items() if k in remaining_set}

    return PipelineDefinition(stages=stages, gates=filtered_gates)


# -- Private helpers ---------------------------------------------------------


def _filter_gates_to_stages(
    gates: dict[str, ApprovalGate],
    stages: list,
) -> dict[str, ApprovalGate]:
    """Filter gates to only reference stages present in the pipeline."""
    stage_names = {s.name for s in stages}
    return {k: v for k, v in gates.items() if k in stage_names}


def _build_gates(
    config: dict[str, Any],
    no_approve: bool,
    review_designs: bool,
) -> dict[str, ApprovalGate]:
    """Build approval gates from config."""
    if no_approve:
        return {}

    gates: dict[str, ApprovalGate] = {}
    if config.get("approve_opportunities", True):
        gates["analyze"] = ApprovalGate(
            after_stage="analyze",
            gate_name="Opportunities identified",
        )
    if config.get("approve_designs", True):
        gate_stage = "review_design" if review_designs else "design"
        gates[gate_stage] = ApprovalGate(
            after_stage=gate_stage,
            gate_name="Design created",
        )
    if config.get("approve_plans", True):
        gates["plan"] = ApprovalGate(
            after_stage="plan",
            gate_name="Tasks added to tasklist",
        )
    return gates


def _build_analyze_selection(
    orchestrator: Orchestrator,
) -> SelectionStrategy:
    """Build top-1 selection with adoption side effect."""

    def _adopt_selected(selected: list[StageItem], result: StageResult) -> None:
        """Mark selected opportunity as adopted."""
        if not selected:
            return
        item = selected[0]
        opp_id = item.artifact_id

        # Check if analysis was staged
        analyze_result = result.checkpoint_data.get("analyze_result", {})
        if analyze_result.get("staged") and analyze_result.get("staging_file"):
            prov = FileOpportunityProvider(Path(analyze_result["staging_file"]))
            prov.update_opportunity_status(opp_id, OpportunityStatus.adopted)
        else:
            olm = orchestrator._outer_loop_manager  # noqa: SLF001
            olm.opportunity_provider.update_opportunity_status(opp_id, OpportunityStatus.adopted)

    return SelectionStrategy(
        mode="top_n",
        n=1,
        sort_key="roi_score",
        on_select=_adopt_selected,
    )


def _build_design_through_execute(
    orchestrator: Orchestrator,
    review_designs: bool,
) -> list:
    """Build [Design, ReviewDesign?, Plan, Execute] stage list."""
    stages: list = [DesignStage(orchestrator)]
    if review_designs:
        stages.append(ReviewDesignStage(orchestrator))
    stages.append(PlanStage(orchestrator))
    stages.append(ExecuteStage(orchestrator))
    return stages


def _build_stages_through(
    start_stages: list,
    orchestrator: Orchestrator,
    review_designs: bool,
    through: str | None,
) -> list:
    """Extend stages from a starting set through the target stage."""
    if through is None:
        return start_stages

    # Map through target to stages needed after the start
    extensions: dict[str, list] = {
        "design": [DesignStage(orchestrator)]
        + ([ReviewDesignStage(orchestrator)] if review_designs else []),
        "plan": [DesignStage(orchestrator)]
        + ([ReviewDesignStage(orchestrator)] if review_designs else [])
        + [PlanStage(orchestrator)],
        "execute": _build_design_through_execute(orchestrator, review_designs),
    }

    ext = extensions.get(through)
    if ext is None:
        raise ValueError(f"Unknown --through target: {through!r}")

    return start_stages + ext


def _build_from_analyze(
    args: Any,
    config: dict[str, Any],
    orchestrator: Orchestrator,
    review_designs: bool,
    gates: dict[str, ApprovalGate],
    through: str | None,
) -> tuple[PipelineDefinition, list[StageItem]]:
    """Build pipeline starting from --analyze."""
    issues_file = getattr(args, "issues", None)
    start = [AnalyzeStage(orchestrator, issues_file=issues_file)]

    if through:
        stages = _build_stages_through(start, orchestrator, review_designs, through)
        selections = {"analyze": _build_analyze_selection(orchestrator)}
        pipeline = PipelineDefinition(
            stages=stages,
            gates=_filter_gates_to_stages(gates, stages),
            selections=selections,
        )
    else:
        pipeline = PipelineDefinition(stages=start)

    return pipeline, []


def _build_from_design(
    args: Any,
    config: dict[str, Any],
    orchestrator: Orchestrator,
    review_designs: bool,
    gates: dict[str, ApprovalGate],
    through: str | None,
) -> tuple[PipelineDefinition, list[StageItem]]:
    """Build pipeline starting from --design."""
    stages: list = [DesignStage(orchestrator)]
    if review_designs:
        stages.append(ReviewDesignStage(orchestrator))

    if through == "plan":
        stages.append(PlanStage(orchestrator))
    elif through == "execute":
        stages.append(PlanStage(orchestrator))
        stages.append(ExecuteStage(orchestrator))

    if through:
        pipeline = PipelineDefinition(stages=stages, gates=_filter_gates_to_stages(gates, stages))
    else:
        pipeline = PipelineDefinition(stages=stages)

    return pipeline, [inject_opportunity(args.design)]


def _build_from_plan(
    args: Any,
    orchestrator: Orchestrator,
    gates: dict[str, ApprovalGate],
    through: str | None,
) -> tuple[PipelineDefinition, list[StageItem]]:
    """Build pipeline starting from --plan."""
    stages: list = [PlanStage(orchestrator)]

    if through == "execute":
        stages.append(ExecuteStage(orchestrator))
        pipeline = PipelineDefinition(stages=stages, gates=_filter_gates_to_stages(gates, stages))
    else:
        pipeline = PipelineDefinition(stages=stages)

    return pipeline, [inject_design(args.plan)]


def _build_deliver(
    args: Any,
    config: dict[str, Any],
    orchestrator: Orchestrator,
    review_designs: bool,
) -> tuple[PipelineDefinition, list[StageItem]]:
    """Build pipeline for --deliver (design → plan → execute, no gates).

    Includes file-provider-only backlog-empty preflight.
    """
    stages = _build_design_through_execute(orchestrator, review_designs)

    preflights: list[PreflightCheck] = []
    using_remote = config.get("tasklist_provider", "file") != "file"
    if not using_remote:

        def _assert_empty_backlog() -> None:
            if orchestrator.has_remaining_tasks():
                raise RuntimeError(
                    "--deliver requires an empty pending tasklist so the new "
                    "objective does not mix with existing backlog tasks. "
                    "Run `millstone` to finish existing tasks first, or point "
                    "`--tasklist` to a fresh file for this objective."
                )

        preflights.append(
            PreflightCheck(
                check=_assert_empty_backlog,
                description="--deliver requires empty pending tasklist (file provider only)",
            )
        )

    pipeline = PipelineDefinition(
        stages=stages,
        preflights=preflights,
        # --deliver skips all gates
    )
    return pipeline, [inject_opportunity(args.deliver)]


def _deserialize_items_from_checkpoint(
    checkpoint: Any,
    orchestrator: Orchestrator,
) -> list[StageItem]:
    """Re-hydrate StageItems from a checkpoint's serialized items."""
    from millstone.loops.pipeline.executor import PipelineExecutor

    # Borrow the executor's deserialization logic
    dummy_pipeline = PipelineDefinition(stages=[])
    executor = PipelineExecutor(dummy_pipeline, orchestrator)
    return executor._deserialize_items(checkpoint.items)  # noqa: SLF001
