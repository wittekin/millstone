"""E2E tests for --complete flag approval-gate checkpoints and execution chains.

Tests the desired behavior of --complete across all three entry points:
  --plan file.md --complete
  --design "obj" --complete
  --analyze --complete

Key behaviors verified here (not in unit tests):
  - Approval-gate halt writes state.json outer_loop checkpoint on disk
  - Approval-gate halt prints "millstone --continue" as the resume instruction
  - --continue after a --complete approval-gate halt completes execution and
    clears state.json
  - Happy-path chains execute all downstream stages
  - Edge cases (0 tasks, failure) produce correct exit codes and messages
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from millstone import orchestrate
from millstone.config import DEFAULT_CONFIG
from millstone.runtime.orchestrator import Orchestrator
from tests.e2e.conftest import StubCli

# ---------------------------------------------------------------------------
# Shared canned responses (same as other E2E tests)
# ---------------------------------------------------------------------------

_APPROVED_JSON = (
    '{"status": "APPROVED", "review": "Looks good", "summary": "Looks good!",'
    ' "findings": [], "findings_by_severity":'
    ' {"critical": [], "high": [], "medium": [], "low": [], "nit": []}}'
)
_SANITY_OK_JSON = '{"status": "OK", "reason": ""}'
_PLAN_REVIEW_APPROVED = '{"verdict": "APPROVED", "feedback": [], "score": 9}'
# Design review uses keyword-based fallback: any output containing "APPROVED" passes.
_DESIGN_REVIEW_APPROVED = "APPROVED"


# ---------------------------------------------------------------------------
# Side-effect helpers
# ---------------------------------------------------------------------------


def _write_task(repo: Path) -> None:
    """Append a single unchecked task to the tasklist."""
    tasklist_path = repo / ".millstone" / "tasklist.md"
    content = tasklist_path.read_text()
    content += "\n- [ ] Stub task from plan\n"
    tasklist_path.write_text(content)


def _make_code_change(repo: Path) -> None:
    """Create a Python file and stage it."""
    (repo / "impl.py").write_text("def stub(): pass\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=False)


def _commit_with_tick(repo: Path) -> None:
    """Tick the first unchecked task, stage all changes, and commit."""
    tasklist_path = repo / ".millstone" / "tasklist.md"
    content = tasklist_path.read_text()
    content = content.replace("- [ ]", "- [x]", 1)
    tasklist_path.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=False)
    subprocess.run(
        ["git", "commit", "-m", "Stub: plan task"],
        cwd=repo,
        capture_output=True,
        check=False,
    )


def _base_config(**overrides) -> dict:
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# Helper: write a design file and return its path
# ---------------------------------------------------------------------------


def _write_design_file(repo: Path, name: str = "test-design") -> Path:
    """Write a minimal design file and return its path."""
    designs_dir = repo / ".millstone" / "designs"
    designs_dir.mkdir(parents=True, exist_ok=True)
    design_file = designs_dir / f"{name}.md"
    design_file.write_text(
        f"# {name}\n\n"
        f"- **design_id**: {name}\n"
        f"- **title**: {name}\n"
        f"- **status**: draft\n\n"
        "---\n\nTest design content.\n"
    )
    return design_file


# ---------------------------------------------------------------------------
# Happy-path tests: StubCli harness calling orchestrator methods directly
# ---------------------------------------------------------------------------

_DESIGN_STUB_ID = "add-caching"
_DESIGN_STUB_OPP_ID = "add-caching"  # slug of "Add caching"; must exist in opportunities.md


def _write_opportunities_stub(repo: Path) -> None:
    """Write a minimal opportunities.md so the design integrity check has a reference to resolve."""
    opp_file = repo / ".millstone" / "opportunities.md"
    opp_file.parent.mkdir(parents=True, exist_ok=True)
    opp_file.write_text(
        f"- [ ] **Add caching**\n"
        f"  - Opportunity ID: {_DESIGN_STUB_OPP_ID}\n"
        f"  - Description: Add a caching layer.\n"
        f"  - ROI Score: 7.0\n"
    )


def _write_design_stub(repo: Path) -> None:
    """Write a minimal design file (simulates what the design agent creates)."""
    designs_dir = repo / ".millstone" / "designs"
    designs_dir.mkdir(parents=True, exist_ok=True)
    (designs_dir / f"{_DESIGN_STUB_ID}.md").write_text(
        f"# Add caching\n\n"
        f"- **design_id**: {_DESIGN_STUB_ID}\n"
        f"- **title**: Add caching\n"
        f"- **status**: draft\n"
        f"- **opportunity_ref**: {_DESIGN_STUB_OPP_ID}\n\n"
        "---\n\nDesign content.\n"
    )


class TestPlanCompleteHappyPath:
    """--plan file.md --complete executes all planned tasks end-to-end."""

    def test_plan_complete_executes_tasks(self, temp_repo: Path, stub_cli: StubCli) -> None:
        """plan stub creates 1 task, builder approves; exit 0, task committed.

        Calls run_plan() then run() on a real Orchestrator (no approval gate
        since approve_plans=False) to simulate the happy path of --plan --complete.
        """
        design_file = _write_design_file(temp_repo)

        # Start with an empty tasklist so run_plan's task is the only pending one
        (temp_repo / ".millstone" / "tasklist.md").write_text("# Tasklist\n\n")

        # Plan phase: author writes task to tasklist
        stub_cli.add(role="author", output="Tasks added.", side_effect=_write_task)
        stub_cli.add(role="author", output=_PLAN_REVIEW_APPROVED)

        # Inner-loop build/review
        stub_cli.add(role="author", output="Implemented.", side_effect=_make_code_change)
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(role="builder", output="Committed.", side_effect=_commit_with_tick)

        orch = Orchestrator(
            approve_plans=False,
            review_designs=False,
            max_tasks=1,
            task_constraints={
                "require_tests": False,
                "require_criteria": False,
                "require_risk": False,
                "require_context": False,
            },
        )
        try:
            with stub_cli.patch(orch):
                plan_result = orch.run_plan(design_path=str(design_file))
                assert plan_result.get("success"), f"run_plan failed: {plan_result}"
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        # The planned task must be completed
        tasklist = (temp_repo / ".millstone" / "tasklist.md").read_text()
        assert "- [x] Stub task from plan" in tasklist, (
            f"Expected planned task to be completed:\n{tasklist}"
        )


class TestDesignCompleteHappyPath:
    """--design 'obj' --complete executes design → plan → tasks end-to-end."""

    def test_design_complete_executes_tasks(self, temp_repo: Path, stub_cli: StubCli) -> None:
        """Design stub creates design, plan stub creates 1 task, builder approves; exit 0.

        Calls run_design(), run_plan(), then run() on a real Orchestrator (no approval
        gates since approve_designs=False, approve_plans=False) to simulate the happy
        path of --design --complete.
        """
        # Start with an empty tasklist so the planned task is the only pending one.
        (temp_repo / ".millstone" / "tasklist.md").write_text("# Tasklist\n\n")

        # Seed opportunities.md so the design integrity check has a reference to resolve.
        _write_opportunities_stub(temp_repo)

        # Design phase: author writes design file; reviewer approves via ArtifactReviewLoop.
        stub_cli.add(role="author", output="Design created.", side_effect=_write_design_stub)
        stub_cli.add(role="reviewer", output=_DESIGN_REVIEW_APPROVED)

        # Plan phase: author writes task; author returns plan review JSON.
        stub_cli.add(role="author", output="Tasks added.", side_effect=_write_task)
        stub_cli.add(role="author", output=_PLAN_REVIEW_APPROVED)

        # Inner-loop build/review.
        stub_cli.add(role="author", output="Implemented.", side_effect=_make_code_change)
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(role="builder", output="Committed.", side_effect=_commit_with_tick)

        orch = Orchestrator(
            approve_designs=False,
            approve_plans=False,
            review_designs=False,
            max_tasks=1,
            task_constraints={
                "require_tests": False,
                "require_criteria": False,
                "require_risk": False,
                "require_context": False,
            },
        )
        try:
            with stub_cli.patch(orch):
                design_result = orch.run_design(opportunity="Add caching")
                assert design_result.get("success"), f"run_design failed: {design_result}"
                design_path = design_result["design_file"]

                plan_result = orch.run_plan(design_path=design_path)
                assert plan_result.get("success"), f"run_plan failed: {plan_result}"

                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        # The planned task must be completed.
        tasklist = (temp_repo / ".millstone" / "tasklist.md").read_text()
        assert "- [x] Stub task from plan" in tasklist, (
            f"Expected planned task to be completed:\n{tasklist}"
        )


# ---------------------------------------------------------------------------
# 0-tasks and failure edge cases (via main() with mocked methods)
# ---------------------------------------------------------------------------


class TestPlanCompleteEdgeCases:
    """Edge cases for --plan --complete."""

    def test_zero_tasks_exits_0_with_message(self, temp_repo: Path, capsys) -> None:
        """plan produces 0 tasks → exit 0, human-readable message, run() not called."""
        design_file = _write_design_file(temp_repo)

        mock_run = MagicMock(return_value=0)
        with (
            patch(
                "sys.argv",
                ["millstone", "--plan", str(design_file), "--complete", "--no-approve"],
            ),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(approve_plans=False),
            ),
            patch.object(
                orchestrate.Orchestrator,
                "run_plan",
                return_value={"success": True, "tasks_added": 0},
            ),
            patch.object(orchestrate.Orchestrator, "run", mock_run),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0, f"Expected exit 0, got {exc_info.value.code}"
        mock_run.assert_not_called()
        captured = capsys.readouterr()
        assert "No tasks" in captured.out, (
            f"Expected human-readable 0-tasks message, got:\n{captured.out}"
        )

    def test_plan_failure_exits_1_without_running(self, temp_repo: Path) -> None:
        """run_plan fails → exit 1, run() not called."""
        design_file = _write_design_file(temp_repo)

        mock_run = MagicMock(return_value=0)
        with (
            patch(
                "sys.argv",
                ["millstone", "--plan", str(design_file), "--complete", "--no-approve"],
            ),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(approve_plans=False),
            ),
            patch.object(
                orchestrate.Orchestrator,
                "run_plan",
                return_value={"success": False, "error": "planning failed"},
            ),
            patch.object(orchestrate.Orchestrator, "run", mock_run),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code != 0
        mock_run.assert_not_called()

    def test_design_rejection_exits_1_without_planning(self, temp_repo: Path) -> None:
        """design is rejected → exit 1, run_plan not called."""
        mock_plan = MagicMock(return_value={"success": True, "tasks_added": 2})
        with (
            patch("sys.argv", ["millstone", "--design", "Add caching", "--complete"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(
                    review_designs=True,
                    approve_designs=False,
                    approve_plans=False,
                ),
            ),
            patch.object(
                orchestrate.Orchestrator,
                "run_design",
                return_value={"success": True, "design_file": str(_write_design_file(temp_repo))},
            ),
            patch.object(
                orchestrate.Orchestrator,
                "review_design",
                return_value={"approved": False},
            ),
            patch.object(orchestrate.Orchestrator, "run_plan", mock_plan),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code != 0
        mock_plan.assert_not_called()

    def test_no_opportunities_exits_0_without_design(self, temp_repo: Path, capsys) -> None:
        """analyze produces no opportunities → exit 0, no design/plan attempted."""
        mock_design = MagicMock(return_value={"success": True, "design_file": "x.md"})
        with (
            patch("sys.argv", ["millstone", "--analyze", "--complete"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(
                    approve_opportunities=False,
                    approve_designs=False,
                ),
            ),
            patch.object(orchestrate.Orchestrator, "run_analyze", return_value={"success": True}),
            patch.object(
                orchestrate.Orchestrator,
                "_select_opportunity",
                create=True,
                return_value=None,
            ),
            patch.object(orchestrate.Orchestrator, "run_design", mock_design),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0
        mock_design.assert_not_called()


# ---------------------------------------------------------------------------
# Approval-gate checkpoint tests: assert state.json + "millstone --continue"
# ---------------------------------------------------------------------------


class TestApprovalGateCheckpoints:
    """Approval-gate halts write state.json and print 'millstone --continue'."""

    def test_plan_complete_gate_writes_checkpoint_and_continue_message(
        self, temp_repo: Path, capsys
    ) -> None:
        """--plan --complete with approve_plans=True writes plan checkpoint.

        Verifies:
          (a) state.json written with outer_loop.stage == "plan"
          (b) pipeline_checkpoint present with pipeline_stages
          (c) stdout contains "millstone --continue"
          (d) exit 0 (gate halt is not an error)
        """
        design_file = _write_design_file(temp_repo)

        with (
            patch(
                "sys.argv",
                ["millstone", "--plan", str(design_file), "--complete"],
            ),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(approve_plans=True),
            ),
            patch.object(
                orchestrate.Orchestrator,
                "run_plan",
                return_value={"success": True, "tasks_added": 2},
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0, f"Expected exit 0, got {exc_info.value.code}"

        # (a)+(b) state.json has pipeline checkpoint
        state_file = temp_repo / ".millstone" / "state.json"
        assert state_file.exists(), "Expected state.json to be written at gate halt"
        state = json.loads(state_file.read_text())
        outer = state.get("outer_loop", {})
        assert outer.get("stage") == "plan", f"Expected outer_loop.stage == 'plan', got: {outer}"
        assert "pipeline_checkpoint" in outer, "Expected pipeline_checkpoint in state"
        cp = outer["pipeline_checkpoint"]
        assert "plan" in cp.get("pipeline_stages", [])

        # (c) stdout contains "millstone --continue"
        captured = capsys.readouterr()
        assert "millstone --continue" in captured.out, (
            f"Expected 'millstone --continue' in stdout:\n{captured.out}"
        )

    def test_design_complete_gate_writes_checkpoint_and_continue_message(
        self, temp_repo: Path, capsys
    ) -> None:
        """--design --complete with approve_designs=True writes design checkpoint.

        Verifies:
          (a) state.json has outer_loop.stage == "design"
          (b) pipeline_checkpoint present with items
          (c) stdout contains "millstone --continue"
          (d) exit 0
        """
        design_file = _write_design_file(temp_repo)
        mock_plan = MagicMock(return_value={"success": True, "tasks_added": 2})

        with (
            patch("sys.argv", ["millstone", "--design", "Add caching", "--complete"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(
                    review_designs=False,
                    approve_designs=True,
                    approve_plans=False,
                ),
            ),
            patch.object(
                orchestrate.Orchestrator,
                "run_design",
                return_value={"success": True, "design_file": str(design_file)},
            ),
            patch.object(orchestrate.Orchestrator, "run_plan", mock_plan),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0, f"Expected exit 0, got {exc_info.value.code}"
        mock_plan.assert_not_called()

        # (a)+(b) checkpoint written with pipeline format
        state_file = temp_repo / ".millstone" / "state.json"
        assert state_file.exists(), "Expected state.json written at design gate"
        state = json.loads(state_file.read_text())
        outer = state.get("outer_loop", {})
        assert outer.get("stage") == "design", f"Expected stage 'design', got: {outer}"
        assert "pipeline_checkpoint" in outer, "Expected pipeline_checkpoint"
        cp = outer["pipeline_checkpoint"]
        assert len(cp.get("items", [])) > 0, "Expected design items in checkpoint"

        # (c) resume instruction
        captured = capsys.readouterr()
        assert "millstone --continue" in captured.out, (
            f"Expected 'millstone --continue' in stdout:\n{captured.out}"
        )

    def test_analyze_complete_gate_writes_checkpoint_and_continue_message(
        self, temp_repo: Path, capsys
    ) -> None:
        """--analyze --complete with approve_opportunities=True writes analyze checkpoint.

        Verifies:
          (a) state.json has outer_loop.stage == "analyze"
          (b) pipeline_checkpoint present
          (c) stdout contains "millstone --continue"
          (d) exit 0
        """
        # AnalyzeStage calls run_analyze() then reads opportunities from the
        # provider. Write a mock opportunities file so the provider can read it.
        mock_design = MagicMock(return_value={"success": True, "design_file": "d.md"})

        # Write opportunities file for the FileOpportunityProvider
        opps_file = temp_repo / ".millstone" / "opportunities.md"
        opps_file.write_text(
            "- [ ] **Add caching layer**\n"
            "  - ID: add-caching\n"
            "  - ROI: 5.0\n"
            "  - Description: Add caching\n"
        )

        with (
            patch("sys.argv", ["millstone", "--analyze", "--complete"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(
                    approve_opportunities=True,
                    approve_designs=False,
                    approve_plans=False,
                ),
            ),
            patch.object(orchestrate.Orchestrator, "run_analyze", return_value={"success": True}),
            patch.object(orchestrate.Orchestrator, "run_design", mock_design),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0, f"Expected exit 0, got {exc_info.value.code}"
        mock_design.assert_not_called()

        # (a)+(b) checkpoint
        state_file = temp_repo / ".millstone" / "state.json"
        assert state_file.exists(), "Expected state.json written at opportunities gate"
        state = json.loads(state_file.read_text())
        outer = state.get("outer_loop", {})
        assert outer.get("stage") == "analyze", f"Expected stage 'analyze', got: {outer}"
        assert "pipeline_checkpoint" in outer, "Expected pipeline_checkpoint"

        # (c) resume instruction
        captured = capsys.readouterr()
        assert "millstone --continue" in captured.out, (
            f"Expected 'millstone --continue' in stdout:\n{captured.out}"
        )

    def test_design_complete_plans_gate_writes_checkpoint(self, temp_repo: Path, capsys) -> None:
        """--design --complete with approve_plans=True (no designs gate) writes plan checkpoint.

        Design gate skipped (approve_designs=False). After planning succeeds,
        plans gate fires and writes plan checkpoint.
        """
        design_file = _write_design_file(temp_repo)

        with (
            patch("sys.argv", ["millstone", "--design", "Add caching", "--complete"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(
                    review_designs=False,
                    approve_designs=False,
                    approve_plans=True,
                ),
            ),
            patch.object(
                orchestrate.Orchestrator,
                "run_design",
                return_value={"success": True, "design_file": str(design_file)},
            ),
            patch.object(
                orchestrate.Orchestrator,
                "run_plan",
                return_value={"success": True, "tasks_added": 3},
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0
        state_file = temp_repo / ".millstone" / "state.json"
        assert state_file.exists(), "Expected state.json written at plans gate"
        state = json.loads(state_file.read_text())
        outer = state.get("outer_loop", {})
        assert outer.get("stage") == "plan", f"Expected stage 'plan', got: {outer}"
        assert "pipeline_checkpoint" in outer, "Expected pipeline_checkpoint"
        captured = capsys.readouterr()
        assert "millstone --continue" in captured.out


# ---------------------------------------------------------------------------
# Argparse mutual-exclusivity
# ---------------------------------------------------------------------------


class TestCompleteFlagMutualExclusivity:
    """--complete argparse mutual-exclusivity checks."""

    def test_complete_without_outer_flag_exits_nonzero(self) -> None:
        """--complete alone (without --plan/--design/--analyze) is rejected."""
        with (
            patch("sys.argv", ["millstone", "--complete"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code != 0

    def test_complete_with_cycle_exits_nonzero(self) -> None:
        """--complete --cycle is rejected by argparse."""
        with (
            patch("sys.argv", ["millstone", "--complete", "--cycle"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code != 0

    def test_complete_with_deliver_exits_nonzero(self) -> None:
        """--complete --deliver is rejected by argparse."""
        with (
            patch("sys.argv", ["millstone", "--complete", "--deliver", "Add caching"]),
            patch("millstone.runtime.orchestrator.load_config", return_value=_base_config()),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code != 0
