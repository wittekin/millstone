"""Built-in pipeline stages — thin adapters wrapping existing orchestrator methods."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from millstone.loops.pipeline.stage import HandoffKind, StageItem, StageResult

if TYPE_CHECKING:
    from millstone.runtime.orchestrator import Orchestrator


class AnalyzeStage:
    """Wraps ``orchestrator.run_analyze()``.  Entry point — no inputs.

    When analysis is staged (MCP + approval gates), reads opportunities from
    the staging file and includes it in ``pending_mcp_syncs``.
    """

    name = "analyze"
    input_kind = None
    output_kind = HandoffKind.OPPORTUNITY

    def __init__(
        self,
        orchestrator: Orchestrator,
        issues_file: str | None = None,
    ) -> None:
        self._orch = orchestrator
        self._issues_file = issues_file

    def execute(self, inputs: list[StageItem]) -> StageResult:
        result = self._orch.run_analyze(issues_file=self._issues_file)
        if not result.get("success"):
            return StageResult(success=False, error="Analysis failed")

        opportunities = self._load_opportunities(result)
        items = [
            StageItem(
                kind=HandoffKind.OPPORTUNITY,
                artifact=opp,
                artifact_id=opp.opportunity_id,
                source_stage=self.name,
            )
            for opp in opportunities
        ]

        # Build pending MCP syncs if analysis was staged
        pending_syncs: list[dict] = []
        if result.get("staged") and result.get("staging_file"):
            pending_syncs.append(
                {
                    "type": "opportunities",
                    "staging_file": result["staging_file"],
                    "last_synced_index": 0,
                    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )

        return StageResult(
            success=True,
            outputs=items,
            checkpoint_data={"analyze_result": result},
            pending_mcp_syncs=pending_syncs,
        )

    def _load_opportunities(self, result: dict) -> list:
        """Load opportunities from staging file or provider."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import OpportunityStatus

        if result.get("staged") and result.get("staging_file"):
            prov = FileOpportunityProvider(Path(result["staging_file"]))
            opps = prov.list_opportunities()
        else:
            olm = self._orch._outer_loop_manager  # noqa: SLF001
            opps = olm.opportunity_provider.list_opportunities()

        return [o for o in opps if o.status == OpportunityStatus.identified]


class DesignStage:
    """Wraps ``orchestrator.run_design()``.  Batch: loops over N opportunities."""

    name = "design"
    input_kind = HandoffKind.OPPORTUNITY
    output_kind = HandoffKind.DESIGN

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orch = orchestrator

    def execute(self, inputs: list[StageItem]) -> StageResult:
        outputs: list[StageItem] = []
        pending_syncs: list[dict] = []

        for item in inputs:
            opp = item.artifact
            # Distinguish real Opportunity models from raw text injections.
            # Raw text (from --design "text" or roadmap goals) must NOT pass
            # an opportunity_id — that matches the existing no-ID path in
            # run_design() and avoids polluting reference-integrity checks.
            is_model = hasattr(opp, "opportunity_id") and hasattr(opp, "title")
            title = opp.title if is_model else str(opp)
            opp_id = opp.opportunity_id if is_model else None

            kwargs: dict = {"opportunity": title}
            if opp_id:
                kwargs["opportunity_id"] = opp_id

            result = self._orch.run_design(**kwargs)
            if not result.get("success"):
                return StageResult(
                    success=False,
                    outputs=outputs,
                    error=f"Design failed for opportunity: {title}",
                    pending_mcp_syncs=pending_syncs,
                )

            design_ref = result.get("design_file") or result.get("design_id")
            if not design_ref:
                return StageResult(
                    success=False,
                    outputs=outputs,
                    error="No design reference returned",
                    pending_mcp_syncs=pending_syncs,
                )

            if result.get("staged") and result.get("staging_file"):
                pending_syncs.append(
                    {
                        "type": "designs",
                        "staging_file": result["staging_file"],
                        "last_synced_index": 0,
                        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                )

            outputs.append(
                StageItem(
                    kind=HandoffKind.DESIGN,
                    artifact=design_ref,
                    artifact_id=str(design_ref),
                    source_stage=self.name,
                    parent_id=item.artifact_id,
                    metadata={"design_result": result},
                )
            )

        return StageResult(
            success=True,
            outputs=outputs,
            pending_mcp_syncs=pending_syncs,
        )


class ReviewDesignStage:
    """Wraps ``orchestrator.review_design()``.  Pass-through filter."""

    name = "review_design"
    input_kind = HandoffKind.DESIGN
    output_kind = HandoffKind.DESIGN

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orch = orchestrator

    def execute(self, inputs: list[StageItem]) -> StageResult:
        outputs: list[StageItem] = []
        for item in inputs:
            result = self._orch.review_design(str(item.artifact_id))
            if not result.get("approved"):
                return StageResult(
                    success=False,
                    outputs=outputs,
                    error=f"Design review failed: {item.artifact_id}",
                )
            outputs.append(item)
        return StageResult(success=True, outputs=outputs)


class PlanStage:
    """Wraps ``orchestrator.run_plan()``.  Emits WORKLIST signal per design."""

    name = "plan"
    input_kind = HandoffKind.DESIGN
    output_kind = HandoffKind.WORKLIST

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orch = orchestrator

    def execute(self, inputs: list[StageItem]) -> StageResult:
        outputs: list[StageItem] = []
        pending_syncs: list[dict] = []

        for item in inputs:
            result = self._orch.run_plan(design_path=str(item.artifact_id))
            if not result.get("success"):
                return StageResult(
                    success=False,
                    outputs=outputs,
                    error=f"Planning failed for design: {item.artifact_id}",
                    pending_mcp_syncs=pending_syncs,
                )

            tasks_added = result.get("tasks_added", 0)
            if not tasks_added:
                from millstone.utils import progress

                progress("No tasks were created by the planning agent.")
                # Zero tasks is not a failure — return success with current
                # outputs so the pipeline stops cleanly (no WORKLIST items
                # means ExecuteStage won't run).
                continue

            if result.get("staged") and result.get("staging_file"):
                pending_syncs.append(
                    {
                        "type": "tasks",
                        "staging_file": result["staging_file"],
                        "last_synced_index": 0,
                        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                )

            outputs.append(
                StageItem(
                    kind=HandoffKind.WORKLIST,
                    artifact=result,
                    artifact_id=f"plan-{item.artifact_id}",
                    source_stage=self.name,
                    parent_id=item.artifact_id,
                    metadata={
                        "tasks_added": tasks_added,
                        "design_path": str(item.artifact_id),
                    },
                )
            )

        return StageResult(
            success=True,
            outputs=outputs,
            pending_mcp_syncs=pending_syncs,
        )


class ExecuteStage:
    """Wraps ``orchestrator.run()`` (inner loop).  Terminal stage.

    Takes a WORKLIST signal — the actual task data lives in the tasklist
    provider, not in the StageItem payloads.
    """

    name = "execute"
    input_kind = HandoffKind.WORKLIST
    output_kind = None

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orch = orchestrator

    def execute(self, inputs: list[StageItem]) -> StageResult:
        exit_code = self._orch.run()
        return StageResult(
            success=(exit_code == 0),
            error=f"Inner loop exited with code {exit_code}" if exit_code != 0 else None,
        )
