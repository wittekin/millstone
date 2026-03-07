"""Tests for outer-loop --continue checkpoint and resume logic."""

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from millstone.runtime.orchestrator import Orchestrator


def _make_orchestrator(tmp_path: Path, **kwargs) -> Orchestrator:
    """Return a minimal Orchestrator pointing at a temp repo."""
    tasklist = tmp_path / ".millstone" / "tasklist.md"
    tasklist.parent.mkdir(parents=True, exist_ok=True)
    tasklist.write_text("- [ ] Dummy task\n")
    return Orchestrator(repo_dir=tmp_path, tasklist=str(tasklist), **kwargs)


@contextmanager
def _patch_run_infrastructure(orch: Orchestrator):
    """Patch all heavy run() infrastructure so routing tests stay unit-level."""
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


# ---------------------------------------------------------------------------
# save_outer_loop_checkpoint
# ---------------------------------------------------------------------------


def test_save_outer_loop_checkpoint_creates_outer_loop_section(tmp_path):
    orch = _make_orchestrator(tmp_path)
    orch.save_outer_loop_checkpoint("analyze_complete", opportunity="Foo")
    state_file = orch._get_state_file_path()
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert state["outer_loop"]["stage"] == "analyze_complete"
    assert state["outer_loop"]["opportunity"] == "Foo"
    assert "timestamp" in state["outer_loop"]


def test_save_outer_loop_checkpoint_preserves_inner_loop_fields(tmp_path):
    orch = _make_orchestrator(tmp_path)
    # Write an existing inner-loop state.
    state_file = orch._get_state_file_path()
    state_file.write_text(json.dumps({"current_task_num": 3, "halt_reason": "loc"}))
    orch.save_outer_loop_checkpoint(
        "design_complete", design_path="foo.md", opportunity="Add retry logic"
    )
    state = json.loads(state_file.read_text())
    assert state["current_task_num"] == 3
    assert state["halt_reason"] == "loc"
    assert state["outer_loop"]["stage"] == "design_complete"
    assert state["outer_loop"]["design_path"] == "foo.md"
    assert state["outer_loop"]["opportunity"] == "Add retry logic"


def test_save_outer_loop_checkpoint_overwrites_previous_stage(tmp_path):
    orch = _make_orchestrator(tmp_path)
    orch.save_outer_loop_checkpoint("analyze_complete", opportunity="A")
    orch.save_outer_loop_checkpoint("design_complete", design_path="b.md", opportunity="B")
    state = json.loads(orch._get_state_file_path().read_text())
    assert state["outer_loop"]["stage"] == "design_complete"
    assert state["outer_loop"]["design_path"] == "b.md"
    assert state["outer_loop"]["opportunity"] == "B"


# ---------------------------------------------------------------------------
# load_state — outer_loop key always present
# ---------------------------------------------------------------------------


def test_load_state_returns_outer_loop_key_when_absent(tmp_path):
    orch = _make_orchestrator(tmp_path)
    # Write a state file that has no outer_loop key (old format).
    state_file = orch._get_state_file_path()
    state_file.write_text(json.dumps({"current_task_num": 1}))
    state = orch.load_state()
    assert state is not None
    assert "outer_loop" in state
    assert state["outer_loop"] is None


def test_load_state_returns_none_when_no_file(tmp_path):
    orch = _make_orchestrator(tmp_path)
    assert orch.load_state() is None


def test_load_state_preserves_outer_loop_when_present(tmp_path):
    orch = _make_orchestrator(tmp_path)
    orch.save_outer_loop_checkpoint("plan_complete", design_path="designs/foo.md", tasks_created=5)
    state = orch.load_state()
    assert state is not None
    assert state["outer_loop"]["stage"] == "plan_complete"
    assert state["outer_loop"]["design_path"] == "designs/foo.md"
    assert state["outer_loop"]["tasks_created"] == 5


# ---------------------------------------------------------------------------
# clear_state — existing behaviour unchanged
# ---------------------------------------------------------------------------


def test_clear_state_removes_file(tmp_path):
    orch = _make_orchestrator(tmp_path)
    orch.save_outer_loop_checkpoint("analyze_complete")
    assert orch._get_state_file_path().exists()
    orch.clear_state()
    assert not orch._get_state_file_path().exists()


# ---------------------------------------------------------------------------
# Resume-routing: --continue with outer_loop.stage checkpoints
# ---------------------------------------------------------------------------

_DESIGN_SUCCESS = {"success": True, "design_file": ".millstone/designs/test.md"}
_PLAN_SUCCESS = {"success": True, "tasks_created": 3}


def test_resume_analyze_complete_calls_design_then_plan(tmp_path):
    """analyze_complete stage: run_design() then run_plan() are called."""
    orch = _make_orchestrator(tmp_path, continue_run=True)
    orch.save_outer_loop_checkpoint("analyze_complete", opportunity="Improve caching")

    with (
        _patch_run_infrastructure(orch),
        patch.object(orch, "run_design", return_value=_DESIGN_SUCCESS) as mock_design,
        patch.object(orch, "run_plan", return_value=_PLAN_SUCCESS) as mock_plan,
    ):
        exit_code = orch.run()

    mock_design.assert_called_once_with(opportunity="Improve caching")
    mock_plan.assert_called_once_with(design_path=".millstone/designs/test.md")
    assert exit_code == 0


def test_resume_analyze_complete_stops_if_design_fails(tmp_path):
    """analyze_complete: if run_design fails, run_plan is not called and exit is 1."""
    orch = _make_orchestrator(tmp_path, continue_run=True)
    orch.save_outer_loop_checkpoint("analyze_complete", opportunity="Foo")

    with (
        _patch_run_infrastructure(orch),
        patch.object(orch, "run_design", return_value={"success": False}),
        patch.object(orch, "run_plan", return_value=_PLAN_SUCCESS) as mock_plan,
    ):
        exit_code = orch.run()

    mock_plan.assert_not_called()
    assert exit_code == 1


def test_resume_design_complete_calls_plan_only(tmp_path):
    """design_complete stage: run_plan() is called; run_design() is not."""
    orch = _make_orchestrator(tmp_path, continue_run=True)
    orch.save_outer_loop_checkpoint("design_complete", design_path="designs/foo.md")

    with (
        _patch_run_infrastructure(orch),
        patch.object(orch, "run_design", return_value=_DESIGN_SUCCESS) as mock_design,
        patch.object(orch, "run_plan", return_value=_PLAN_SUCCESS) as mock_plan,
    ):
        exit_code = orch.run()

    mock_design.assert_not_called()
    mock_plan.assert_called_once_with(design_path="designs/foo.md")
    assert exit_code == 0


def test_resume_design_complete_stops_if_plan_fails(tmp_path):
    """design_complete: if run_plan fails, exit is 1."""
    orch = _make_orchestrator(tmp_path, continue_run=True)
    orch.save_outer_loop_checkpoint("design_complete", design_path="designs/foo.md")

    with (
        _patch_run_infrastructure(orch),
        patch.object(orch, "run_plan", return_value={"success": False}),
    ):
        exit_code = orch.run()

    assert exit_code == 1


def test_resume_plan_complete_skips_design_and_plan(tmp_path):
    """plan_complete stage: neither run_design nor run_plan are called."""
    orch = _make_orchestrator(tmp_path, continue_run=True)
    orch.save_outer_loop_checkpoint("plan_complete", tasks_created=4)

    with (
        _patch_run_infrastructure(orch),
        patch.object(orch, "run_design", return_value=_DESIGN_SUCCESS) as mock_design,
        patch.object(orch, "run_plan", return_value=_PLAN_SUCCESS) as mock_plan,
    ):
        exit_code = orch.run()

    mock_design.assert_not_called()
    mock_plan.assert_not_called()
    assert exit_code == 0


def test_resume_no_outer_loop_key_runs_inner_loop_normally(tmp_path):
    """No outer_loop checkpoint: inner loop runs without calling design/plan."""
    orch = _make_orchestrator(tmp_path, continue_run=True)
    # Write inner-loop-only state (no outer_loop key).
    state_file = orch._get_state_file_path()
    state_file.write_text(json.dumps({"current_task_num": 1, "halt_reason": "loc"}))

    with (
        _patch_run_infrastructure(orch),
        patch.object(orch, "run_design", return_value=_DESIGN_SUCCESS) as mock_design,
        patch.object(orch, "run_plan", return_value=_PLAN_SUCCESS) as mock_plan,
    ):
        exit_code = orch.run()

    mock_design.assert_not_called()
    mock_plan.assert_not_called()
    assert exit_code == 0


def test_resume_unknown_stage_runs_inner_loop_normally(tmp_path, capsys):
    """Unknown outer_loop stage: warning is printed and inner loop runs."""
    orch = _make_orchestrator(tmp_path, continue_run=True)
    orch.save_outer_loop_checkpoint("future_stage_not_yet_known")

    with (
        _patch_run_infrastructure(orch),
        patch.object(orch, "run_design", return_value=_DESIGN_SUCCESS) as mock_design,
        patch.object(orch, "run_plan", return_value=_PLAN_SUCCESS) as mock_plan,
    ):
        exit_code = orch.run()

    mock_design.assert_not_called()
    mock_plan.assert_not_called()
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "unknown outer_loop stage" in captured.out
