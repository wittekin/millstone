"""Tests for the run summary printed at exit (T6)."""

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from millstone.runtime.orchestrator import Orchestrator
from millstone.utils import format_elapsed

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator(tmp_path: Path, **kwargs) -> Orchestrator:
    """Return a minimal Orchestrator pointing at a temp repo."""
    tasklist = tmp_path / ".millstone" / "tasklist.md"
    tasklist.parent.mkdir(parents=True, exist_ok=True)
    tasklist.write_text("- [ ] Task A\n- [ ] Task B\n")
    return Orchestrator(repo_dir=tmp_path, tasklist=str(tasklist), max_tasks=2, **kwargs)


@contextmanager
def _patch_run_infrastructure(orch: Orchestrator):
    """Patch heavy run() infrastructure so tests stay unit-level."""
    with (
        patch.object(orch, "preflight_checks"),
        patch.object(orch, "auto_clear_stale_sessions", return_value=False),
        patch.object(orch, "_init_loc_baseline"),
        patch.object(orch, "check_dirty_working_directory"),
        patch.object(orch, "check_uncommitted_tasklist"),
        patch.object(orch, "count_completed_tasks", return_value=0),
        patch.object(orch, "should_compact", return_value=False),
        patch.object(orch, "log"),
        patch.object(orch, "cleanup"),
        patch.object(orch, "clear_state"),
    ):
        yield


# ---------------------------------------------------------------------------
# format_elapsed unit tests
# ---------------------------------------------------------------------------


class TestFormatElapsed:
    def test_zero(self):
        assert format_elapsed(0) == "0s"

    def test_seconds_only(self):
        assert format_elapsed(45) == "45s"

    def test_minutes_and_seconds(self):
        assert format_elapsed(155) == "2m 35s"

    def test_fractional_seconds(self):
        assert format_elapsed(90.7) == "1m 30s"


# ---------------------------------------------------------------------------
# _print_run_summary tests
# ---------------------------------------------------------------------------


class TestPrintRunSummary:
    def test_summary_format_success(self, tmp_path, capsys):
        """Summary prints the 3 expected lines on success."""
        orch = _make_orchestrator(tmp_path)
        _ = capsys.readouterr()  # discard constructor output
        import time

        start = time.monotonic() - 65  # 1m 5s ago
        orch._print_run_summary(2, 0, "success", start, remaining=0)
        out = capsys.readouterr().out
        lines = out.strip().split("\n")
        assert len(lines) == 3
        assert lines[0] == "=== Run Summary ==="
        assert "2 completed" in lines[1]
        assert "0 failed" in lines[1]
        assert "0 remaining" in lines[1]
        assert "1m 5s" in lines[1]
        assert "Log:" in lines[2]

    def test_summary_format_with_failures(self, tmp_path, capsys):
        """Summary correctly reports failed and remaining counts."""
        orch = _make_orchestrator(tmp_path)
        import time

        start = time.monotonic() - 130  # 2m 10s
        orch._print_run_summary(1, 1, "halted", start, remaining=3)
        out = capsys.readouterr().out
        assert "=== Run Summary ===" in out
        assert "1 completed" in out
        assert "1 failed" in out
        assert "3 remaining" in out

    def test_summary_remaining_unknown(self, tmp_path, capsys):
        """MCP providers pass remaining='unknown'."""
        orch = _make_orchestrator(tmp_path)
        _ = capsys.readouterr()
        import time

        orch._print_run_summary(0, 0, "decision_gate", time.monotonic(), remaining="unknown")
        out = capsys.readouterr().out
        assert "unknown remaining" in out

    def test_summary_skipped_in_quiet_mode(self, tmp_path, capsys):
        """No summary output when quiet=True."""
        orch = _make_orchestrator(tmp_path, quiet=True)
        _ = capsys.readouterr()  # discard constructor output
        import time

        orch._print_run_summary(1, 0, "success", time.monotonic())
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Integration: run() prints summary
# ---------------------------------------------------------------------------


class TestRunSummaryIntegration:
    def test_two_tasks_one_success_one_failure(self, tmp_path, capsys):
        """Run through 2 tasks (1 success, 1 failure) and verify summary."""
        orch = _make_orchestrator(tmp_path)
        call_count = 0

        def _mock_run_single_task():
            nonlocal call_count
            call_count += 1
            return call_count == 1  # first succeeds, second fails

        with (
            _patch_run_infrastructure(orch),
            patch.object(orch, "has_remaining_tasks", return_value=True),
            patch.object(orch, "run_single_task", side_effect=_mock_run_single_task),
            patch.object(orch, "has_pending_decision_gate", return_value=False),
            patch.object(orch, "save_state"),
        ):
            exit_code = orch.run()

        assert exit_code == 1
        out = capsys.readouterr().out
        assert "=== Run Summary ===" in out
        assert "1 completed" in out
        assert "1 failed" in out
        assert "0 remaining" in out
        assert "Log:" in out

    def test_all_tasks_succeed(self, tmp_path, capsys):
        """Run 2 successful tasks and verify success summary."""
        orch = _make_orchestrator(tmp_path)

        with (
            _patch_run_infrastructure(orch),
            patch.object(orch, "has_remaining_tasks", return_value=True),
            patch.object(orch, "run_single_task", return_value=True),
        ):
            exit_code = orch.run()

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "=== Run Summary ===" in out
        assert "2 completed" in out
        assert "0 failed" in out
        assert "0 remaining" in out

    def test_no_remaining_tasks(self, tmp_path, capsys):
        """Summary prints 0 remaining when no tasks remain."""
        orch = _make_orchestrator(tmp_path)

        with (
            _patch_run_infrastructure(orch),
            patch.object(orch, "has_remaining_tasks", return_value=False),
        ):
            exit_code = orch.run()

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "=== Run Summary ===" in out
        assert "0 completed" in out
        assert "0 remaining" in out

    def test_direct_task_success_zero_remaining(self, tmp_path, capsys):
        """--task mode: success shows 0 remaining, not max_tasks-1."""
        orch = _make_orchestrator(tmp_path)
        orch.task = "Do something"

        with (
            _patch_run_infrastructure(orch),
            patch.object(orch, "run_single_task", return_value=True),
        ):
            exit_code = orch.run()

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "=== Run Summary ===" in out
        assert "1 completed" in out
        assert "0 remaining" in out

    def test_direct_task_failure_zero_remaining(self, tmp_path, capsys):
        """--task mode: failure shows 0 remaining."""
        orch = _make_orchestrator(tmp_path)
        orch.task = "Do something"

        with (
            _patch_run_infrastructure(orch),
            patch.object(orch, "run_single_task", return_value=False),
            patch.object(orch, "has_pending_decision_gate", return_value=False),
        ):
            exit_code = orch.run()

        assert exit_code == 1
        out = capsys.readouterr().out
        assert "=== Run Summary ===" in out
        assert "1 failed" in out
        assert "0 remaining" in out

    def test_quiet_mode_no_summary(self, tmp_path, capsys):
        """Summary is suppressed in quiet mode."""
        orch = _make_orchestrator(tmp_path, quiet=True)

        with (
            _patch_run_infrastructure(orch),
            patch.object(orch, "has_remaining_tasks", return_value=False),
        ):
            orch.run()

        out = capsys.readouterr().out
        assert "=== Run Summary" not in out
