"""E2E tests for --continue outer-loop resume and --cycle gate checkpoint behavior.

Covers gaps not addressed by unit tests in tests/unit/test_continue_outer_loop.py:
  - run_cycle() approval gates write state.json to disk with correct fields
  - run_cycle() with --no-approve does not write outer_loop checkpoint
  - --continue with no saved state prints warning then runs inner loop (task executed)
  - design_complete resume with a missing design file: exits 1 with user-facing message
  - analyze_complete resume where design fails: state.json preserved, run_plan skipped
  - unknown outer_loop stage: warning printed, inner loop still runs (task executed)

Unit tests cover resume routing correctness; these tests verify disk I/O behavior.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from millstone.runtime.orchestrator import Orchestrator
from tests.e2e.conftest import StubCli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator(repo_dir: Path, **kwargs) -> Orchestrator:
    """Return a minimal Orchestrator pointing at a temp repo with a tasklist."""
    millstone_dir = repo_dir / ".millstone"
    millstone_dir.mkdir(exist_ok=True)
    tasklist = millstone_dir / "tasklist.md"
    if not tasklist.exists():
        tasklist.write_text("# Tasklist\n\n")
    return Orchestrator(
        repo_dir=repo_dir,
        tasklist=str(tasklist),
        review_designs=False,
        task_constraints={
            "require_tests": False,
            "require_criteria": False,
            "require_risk": False,
            "require_context": False,
        },
        **kwargs,
    )


@contextmanager
def _patch_run_infrastructure(orch: Orchestrator):
    """Patch expensive run() infrastructure so routing tests stay fast."""
    with (
        patch.object(orch, "preflight_checks"),
        patch.object(orch, "auto_clear_stale_sessions", return_value=False),
        patch.object(orch, "_init_loc_baseline"),
        patch.object(orch, "check_dirty_working_directory"),
        patch.object(orch, "check_uncommitted_tasklist"),
        patch.object(orch, "count_completed_tasks", return_value=0),
        patch.object(orch, "should_compact", return_value=False),
        patch.object(orch, "has_remaining_tasks", return_value=False),
        patch.object(orch, "log"),
    ):
        yield


def _write_design_file(repo: Path, name: str = "test-design") -> Path:
    designs_dir = repo / ".millstone" / "designs"
    designs_dir.mkdir(parents=True, exist_ok=True)
    path = designs_dir / f"{name}.md"
    path.write_text(
        f"# {name}\n\n- **design_id**: {name}\n- **title**: {name}\n"
        "- **status**: draft\n\n---\n\nTest design.\n"
    )
    return path


def _mock_opportunity(title: str = "Add caching", opp_id: str = "add-caching") -> MagicMock:
    opp = MagicMock()
    opp.title = title
    opp.opportunity_id = opp_id
    opp.roi_score = 8.0
    opp.requires_design = True
    return opp


# ---------------------------------------------------------------------------
# --cycle gate checkpoints: state.json written on disk at each approval gate
# ---------------------------------------------------------------------------


class TestCycleGateCheckpoints:
    """run_cycle() approval gates write state.json with correct outer_loop fields."""

    def test_analyze_gate_writes_analyze_complete_checkpoint(self, temp_repo: Path) -> None:
        """run_cycle() with approve_opportunities=True writes analyze_complete to state.json.

        Verifies:
          (a) exit 0 (gate halt is not an error)
          (b) state.json created with outer_loop.stage == "analyze_complete"
          (c) outer_loop.opportunity matches the selected opportunity title
        """
        orch = _make_orchestrator(temp_repo, approve_opportunities=True)
        mock_opp = _mock_opportunity()

        with (
            patch.object(orch, "has_remaining_tasks", return_value=False),
            patch.object(
                orch, "run_analyze", return_value={"success": True, "opportunity_count": 1}
            ),
            patch.object(orch._outer_loop_manager, "_select_opportunity", return_value=mock_opp),
            patch.object(
                orch._outer_loop_manager.opportunity_provider, "update_opportunity_status"
            ),
        ):
            exit_code = orch.run_cycle()
        orch.cleanup()

        assert exit_code == 0, f"Expected exit 0 at analyze gate, got {exit_code}"

        state_file = temp_repo / ".millstone" / "state.json"
        assert state_file.exists(), "Expected state.json written at analyze gate"
        state = json.loads(state_file.read_text())
        outer = state.get("outer_loop", {})
        assert outer.get("stage") == "analyze_complete", (
            f"Expected stage 'analyze_complete', got: {outer}"
        )
        assert outer.get("opportunity") == "Add caching", (
            f"Expected opportunity 'Add caching', got: {outer}"
        )

    def test_design_gate_writes_design_complete_checkpoint(self, temp_repo: Path) -> None:
        """run_cycle() with approve_designs=True writes design_complete to state.json.

        Verifies:
          (a) exit 0
          (b) state.json has outer_loop.stage == "design_complete"
          (c) outer_loop.design_path and outer_loop.opportunity set
        """
        orch = _make_orchestrator(
            temp_repo,
            approve_opportunities=False,
            approve_designs=True,
        )
        mock_opp = _mock_opportunity()
        design_file = _write_design_file(temp_repo)

        with (
            patch.object(orch, "has_remaining_tasks", return_value=False),
            patch.object(
                orch, "run_analyze", return_value={"success": True, "opportunity_count": 1}
            ),
            patch.object(orch._outer_loop_manager, "_select_opportunity", return_value=mock_opp),
            patch.object(
                orch._outer_loop_manager.opportunity_provider, "update_opportunity_status"
            ),
            patch.object(
                orch,
                "run_design",
                return_value={"success": True, "design_file": str(design_file)},
            ),
        ):
            exit_code = orch.run_cycle()
        orch.cleanup()

        assert exit_code == 0, f"Expected exit 0 at design gate, got {exit_code}"

        state_file = temp_repo / ".millstone" / "state.json"
        assert state_file.exists(), "Expected state.json written at design gate"
        state = json.loads(state_file.read_text())
        outer = state.get("outer_loop", {})
        assert outer.get("stage") == "design_complete", (
            f"Expected stage 'design_complete', got: {outer}"
        )
        assert outer.get("design_path") == str(design_file), (
            f"Expected design_path == {design_file}, got: {outer}"
        )
        assert outer.get("opportunity") == "Add caching", (
            f"Expected opportunity 'Add caching', got: {outer}"
        )

    def test_plan_gate_writes_plan_complete_checkpoint_with_tasks_created(
        self, temp_repo: Path
    ) -> None:
        """run_cycle() with approve_plans=True writes plan_complete with tasks_created.

        Verifies:
          (a) exit 0
          (b) outer_loop.stage == "plan_complete"
          (c) outer_loop.tasks_created == tasks_added from run_plan result
          (d) outer_loop.design_path set
        """
        orch = _make_orchestrator(
            temp_repo,
            approve_opportunities=False,
            approve_designs=False,
            approve_plans=True,
        )
        mock_opp = _mock_opportunity()
        design_file = _write_design_file(temp_repo)

        with (
            patch.object(orch, "has_remaining_tasks", return_value=False),
            patch.object(
                orch, "run_analyze", return_value={"success": True, "opportunity_count": 1}
            ),
            patch.object(orch._outer_loop_manager, "_select_opportunity", return_value=mock_opp),
            patch.object(
                orch._outer_loop_manager.opportunity_provider, "update_opportunity_status"
            ),
            patch.object(
                orch,
                "run_design",
                return_value={"success": True, "design_file": str(design_file)},
            ),
            patch.object(
                orch,
                "run_plan",
                return_value={"success": True, "tasks_added": 5},
            ),
        ):
            exit_code = orch.run_cycle()
        orch.cleanup()

        assert exit_code == 0, f"Expected exit 0 at plan gate, got {exit_code}"

        state_file = temp_repo / ".millstone" / "state.json"
        assert state_file.exists(), "Expected state.json written at plan gate"
        state = json.loads(state_file.read_text())
        outer = state.get("outer_loop", {})
        assert outer.get("stage") == "plan_complete", (
            f"Expected stage 'plan_complete', got: {outer}"
        )
        assert outer.get("tasks_created") == 5, f"Expected tasks_created == 5, got: {outer}"
        assert outer.get("design_path") == str(design_file), (
            f"Expected design_path == {design_file}, got: {outer}"
        )

    def test_no_approve_does_not_write_outer_loop_checkpoint(self, temp_repo: Path) -> None:
        """run_cycle() with all gates disabled does not write an outer_loop checkpoint.

        When no approval gate fires, save_checkpoint_callback is never called,
        so state.json should either not exist or have no outer_loop section.
        """
        orch = _make_orchestrator(
            temp_repo,
            approve_opportunities=False,
            approve_designs=False,
            approve_plans=False,
        )
        mock_opp = _mock_opportunity()
        design_file = _write_design_file(temp_repo)

        with (
            patch.object(orch, "has_remaining_tasks", return_value=False),
            patch.object(
                orch, "run_analyze", return_value={"success": True, "opportunity_count": 1}
            ),
            patch.object(orch._outer_loop_manager, "_select_opportunity", return_value=mock_opp),
            patch.object(
                orch._outer_loop_manager.opportunity_provider, "update_opportunity_status"
            ),
            patch.object(
                orch,
                "run_design",
                return_value={"success": True, "design_file": str(design_file)},
            ),
            patch.object(
                orch,
                "run_plan",
                return_value={"success": True, "tasks_added": 2},
            ),
            # Patch the inner-loop run() so we don't need full task infrastructure
            patch.object(orch, "run", return_value=0),
        ):
            exit_code = orch.run_cycle()
        orch.cleanup()

        assert exit_code == 0, f"Expected exit 0 with no-approve, got {exit_code}"

        state_file = temp_repo / ".millstone" / "state.json"
        if state_file.exists():
            state = json.loads(state_file.read_text())
            outer = state.get("outer_loop")
            assert outer is None, (
                f"Expected no outer_loop checkpoint with all gates disabled, got: {outer}"
            )


# ---------------------------------------------------------------------------
# --continue with no saved state
# ---------------------------------------------------------------------------


class TestContinueNoState:
    """--continue with no state.json prints a warning and runs the inner loop normally."""

    def test_warns_and_runs_inner_loop_normally(self, temp_repo: Path, capsys) -> None:
        """Orchestrator(continue_run=True).run() with no state.json warns then runs inner loop.

        Verifies:
          (a) exit 0 (one task executed successfully)
          (b) stdout contains the no-state warning
          (c) run_single_task is called (inner loop ran after the warning)
          (d) state.json is NOT created (nothing to save)
        """
        orch = _make_orchestrator(temp_repo, continue_run=True)

        with (
            _patch_run_infrastructure(orch),
            # Override has_remaining_tasks to return True once (so inner loop executes),
            # then False (so the loop terminates gracefully).
            patch.object(orch, "has_remaining_tasks", side_effect=[True, False]),
            patch.object(orch, "run_single_task", return_value=True) as mock_rst,
            patch.object(orch, "clear_state"),
        ):
            exit_code = orch.run()
        orch.cleanup()

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        captured = capsys.readouterr()
        assert "--continue" in captured.out and "no saved state" in captured.out.lower(), (
            f"Expected no-state warning in stdout:\n{captured.out}"
        )

        mock_rst.assert_called_once()

        state_file = temp_repo / ".millstone" / "state.json"
        assert not state_file.exists(), "Expected no state.json created when no saved state"


# ---------------------------------------------------------------------------
# --continue edge cases
# ---------------------------------------------------------------------------


class TestContinueEdgeCases:
    """Edge cases for outer-loop resume via --continue."""

    def test_design_complete_missing_file_exits_with_message(self, temp_repo: Path, capsys) -> None:
        """design_complete checkpoint with nonexistent design_path: exits 1, prints message.

        The implementation (outer.py _run_plan_impl) detects the missing file and prints
        a clear user-facing message identifying the file before returning failure.

        Verifies:
          (a) exit code 1
          (b) stdout contains "Design file not found" and the missing file path
        """
        nonexistent_path = str(temp_repo / ".millstone" / "designs" / "missing.md")

        orch_setup = _make_orchestrator(temp_repo)
        orch_setup.save_outer_loop_checkpoint(
            "design_complete", design_path=nonexistent_path, opportunity="Add caching"
        )
        orch_setup.cleanup()

        orch = _make_orchestrator(temp_repo, continue_run=True)

        with _patch_run_infrastructure(orch):
            exit_code = orch.run()
        orch.cleanup()

        assert exit_code == 1, f"Expected exit 1 when design file is missing, got {exit_code}"
        captured = capsys.readouterr()
        assert "Design file not found" in captured.out, (
            f"Expected 'Design file not found' message in stdout:\n{captured.out}"
        )
        assert "missing.md" in captured.out, (
            f"Expected missing file name in stdout:\n{captured.out}"
        )

    def test_analyze_complete_design_failure_preserves_state(self, temp_repo: Path) -> None:
        """analyze_complete + design failure: state.json is NOT cleared (preserved for retry).

        When --continue resumes from analyze_complete and run_design fails, the outer-loop
        checkpoint must remain in state.json so the user can re-run --continue after fixing
        the design issue.

        Verifies:
          (a) exit code 1
          (b) state.json still exists after the failure
          (c) outer_loop.stage is still "analyze_complete"
        """
        orch_setup = _make_orchestrator(temp_repo)
        orch_setup.save_outer_loop_checkpoint("analyze_complete", opportunity="Add caching")
        orch_setup.cleanup()

        state_file = temp_repo / ".millstone" / "state.json"
        assert state_file.exists(), "Precondition: state.json must exist before resume"

        orch = _make_orchestrator(temp_repo, continue_run=True)

        with (
            _patch_run_infrastructure(orch),
            patch.object(orch, "run_design", return_value={"success": False}),
            patch.object(orch, "run_plan") as mock_run_plan,
        ):
            exit_code = orch.run()
        orch.cleanup()

        assert exit_code == 1, f"Expected exit 1 when design fails, got {exit_code}"
        mock_run_plan.assert_not_called()
        assert state_file.exists(), "Expected state.json preserved after design failure"
        state = json.loads(state_file.read_text())
        assert state["outer_loop"]["stage"] == "analyze_complete", (
            f"Expected analyze_complete checkpoint preserved, got: {state['outer_loop']}"
        )

    def test_unknown_stage_prints_warning_and_runs_inner_loop(
        self, temp_repo: Path, capsys
    ) -> None:
        """Unknown outer_loop stage: warning printed and inner loop runs normally.

        Verifies:
          (a) exit 0 (one task executed successfully)
          (b) stdout contains "unknown outer_loop stage" warning
          (c) run_single_task is called (inner loop ran after the warning)
        """
        orch_setup = _make_orchestrator(temp_repo)
        orch_setup.save_outer_loop_checkpoint("future_stage_not_yet_known")
        orch_setup.cleanup()

        orch = _make_orchestrator(temp_repo, continue_run=True)

        with (
            _patch_run_infrastructure(orch),
            # Override has_remaining_tasks to return True once so inner loop executes,
            # then False so the loop terminates gracefully.
            patch.object(orch, "has_remaining_tasks", side_effect=[True, False]),
            patch.object(orch, "run_single_task", return_value=True) as mock_rst,
            patch.object(orch, "clear_state"),
        ):
            exit_code = orch.run()
        orch.cleanup()

        assert exit_code == 0, f"Expected exit 0 for unknown stage fallthrough, got {exit_code}"
        captured = capsys.readouterr()
        assert "unknown outer_loop stage" in captured.out, (
            f"Expected 'unknown outer_loop stage' warning in stdout:\n{captured.out}"
        )
        mock_rst.assert_called_once()


# ---------------------------------------------------------------------------
# Canned responses for StubCli-based resume tests
# ---------------------------------------------------------------------------

_APPROVED_JSON = (
    '{"status": "APPROVED", "review": "Looks good", "summary": "Looks good!",'
    ' "findings": [], "findings_by_severity":'
    ' {"critical": [], "high": [], "medium": [], "low": [], "nit": []}}'
)
_SANITY_OK_JSON = '{"status": "OK", "reason": ""}'
_PLAN_REVIEW_APPROVED = '{"verdict": "APPROVED", "feedback": [], "score": 9}'
_DESIGN_REVIEW_APPROVED = "APPROVED"

_DESIGN_STUB_ID = "add-caching"
_DESIGN_STUB_OPP_ID = "add-caching"


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


def _write_opportunities_stub(repo: Path) -> None:
    """Write a minimal opportunities.md so the design integrity check resolves the ref."""
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


# ---------------------------------------------------------------------------
# --continue after --complete gate halt: full resume-path scenarios
# ---------------------------------------------------------------------------


class TestContinueAfterCompleteGate:
    """--continue resumes and completes after a --complete approval-gate halt."""

    def test_continue_from_plan_complete_checkpoint(
        self, temp_repo: Path, stub_cli: StubCli
    ) -> None:
        """state.json has plan_complete → --continue runs inner loop and clears state.

        Simulates the scenario where --plan --complete halted at the plans gate,
        writing plan_complete to state.json. Then --continue is run.

        Given:
          - state.json has outer_loop.stage == "plan_complete"
          - tasklist has a pending task
          - builder stubs approve

        When: Orchestrator(continue_run=True).run()

        Then:
          - exit 0
          - task committed
          - state.json cleared
        """
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        tasklist_path.write_text("# Tasklist\n\n- [ ] Stub task from plan\n- [x] Old task\n")

        orch_for_checkpoint = Orchestrator(repo_dir=temp_repo)
        orch_for_checkpoint.save_outer_loop_checkpoint("plan_complete", design_path="test.md")
        orch_for_checkpoint.cleanup()

        stub_cli.add(role="author", output="Implemented.", side_effect=_make_code_change)
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(role="builder", output="Committed.", side_effect=_commit_with_tick)

        orch = Orchestrator(
            continue_run=True,
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
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        tasklist = tasklist_path.read_text()
        assert "- [x] Stub task from plan" in tasklist, (
            f"Expected pending task to be completed:\n{tasklist}"
        )

        state_file = temp_repo / ".millstone" / "state.json"
        assert not state_file.exists(), "Expected state.json cleared after successful --continue"

    def test_continue_from_design_complete_checkpoint(
        self, temp_repo: Path, stub_cli: StubCli
    ) -> None:
        """state.json has design_complete → run_plan called, then inner loop runs.

        Given:
          - state.json has design_complete with a valid design_path
          - plan stub writes task, builder stubs approve

        Then: exit 0, task committed, state.json cleared.
        """
        design_file = _write_design_file(temp_repo)

        orch_for_checkpoint = Orchestrator(repo_dir=temp_repo)
        orch_for_checkpoint.save_outer_loop_checkpoint(
            "design_complete",
            design_path=str(design_file),
            opportunity="Add caching",
        )
        orch_for_checkpoint.cleanup()

        stub_cli.add(role="author", output="Tasks added.", side_effect=_write_task)
        stub_cli.add(role="author", output=_PLAN_REVIEW_APPROVED)

        stub_cli.add(role="author", output="Implemented.", side_effect=_make_code_change)
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(role="builder", output="Committed.", side_effect=_commit_with_tick)

        orch = Orchestrator(
            continue_run=True,
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
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        state_file = temp_repo / ".millstone" / "state.json"
        assert not state_file.exists(), "Expected state.json cleared after successful --continue"

    def test_continue_from_analyze_complete_checkpoint(
        self, temp_repo: Path, stub_cli: StubCli
    ) -> None:
        """state.json has analyze_complete → run_design, run_plan, inner loop run, state cleared.

        Given:
          - state.json has analyze_complete with opportunity="Add caching"
          - design stub writes design file; reviewer approves via ArtifactReviewLoop
          - plan stub writes task; inner-loop builder stubs approve

        Then:
          - exit 0
          - task committed
          - state.json cleared
        """
        orch_for_checkpoint = Orchestrator(repo_dir=temp_repo)
        orch_for_checkpoint.save_outer_loop_checkpoint(
            "analyze_complete", opportunity="Add caching"
        )
        orch_for_checkpoint.cleanup()

        (temp_repo / ".millstone" / "tasklist.md").write_text("# Tasklist\n\n")
        _write_opportunities_stub(temp_repo)

        stub_cli.add(role="author", output="Design created.", side_effect=_write_design_stub)
        stub_cli.add(role="reviewer", output=_DESIGN_REVIEW_APPROVED)

        stub_cli.add(role="author", output="Tasks added.", side_effect=_write_task)
        stub_cli.add(role="author", output=_PLAN_REVIEW_APPROVED)

        stub_cli.add(role="author", output="Implemented.", side_effect=_make_code_change)
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(role="builder", output="Committed.", side_effect=_commit_with_tick)

        orch = Orchestrator(
            continue_run=True,
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
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        tasklist = (temp_repo / ".millstone" / "tasklist.md").read_text()
        assert "- [x] Stub task from plan" in tasklist, (
            f"Expected pending task to be completed:\n{tasklist}"
        )

        state_file = temp_repo / ".millstone" / "state.json"
        assert not state_file.exists(), "Expected state.json cleared after successful --continue"
