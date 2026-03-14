"""Tests for --complete flag CLI control-flow and approval gates.

Verifies that --plan/--design/--analyze --complete honor approve_plans,
approve_designs, and approve_opportunities gates the same way --cycle does.
"""

from unittest.mock import MagicMock, patch

import pytest

from millstone.artifacts.models import Opportunity, OpportunityStatus
from millstone.config import DEFAULT_CONFIG


def _base_config(**overrides) -> dict:
    """Return DEFAULT_CONFIG with overrides applied."""
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(overrides)
    return cfg


def _make_opportunity(title="Add caching"):
    return Opportunity(
        opportunity_id="add-caching",
        title=title,
        status=OpportunityStatus.identified,
        description="Add caching to improve performance",
        roi_score=5.0,
    )


def _setup_tmp_repo(tmp_path):
    """Create minimal repo structure so Orchestrator can initialize."""
    ms_dir = tmp_path / ".millstone"
    ms_dir.mkdir(parents=True, exist_ok=True)
    (ms_dir / "tasklist.md").write_text("# Tasklist\n")
    # Create opportunities file so FileOpportunityProvider.update_opportunity_status
    # doesn't fail when the pipeline's SelectionStrategy adopt callback runs.
    (ms_dir / "opportunities.md").write_text(
        "- [ ] **Add caching**\n"
        "  - ID: add-caching\n"
        "  - Status: identified\n"
        "  - ROI Score: 5.0\n"
        "  - Description: Add caching to improve performance\n"
    )


# ---------------------------------------------------------------------------
# --plan --complete
# ---------------------------------------------------------------------------


class TestPlanCompleteFlag:
    """--plan ... --complete execution paths."""

    def test_halts_at_plans_gate_by_default(self, tmp_path, capsys, monkeypatch):
        """--plan --complete exits 0 at plans gate when approve_plans defaults to True."""
        from millstone import orchestrate

        _setup_tmp_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        mock_run = MagicMock(return_value=0)
        with (
            patch("sys.argv", ["millstone", "--plan", "designs/foo.md", "--complete"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(),
            ),
            patch.object(orchestrate.Orchestrator, "preflight_checks"),
            patch.object(orchestrate.Orchestrator, "save_outer_loop_checkpoint"),
            patch.object(
                orchestrate.Orchestrator,
                "run_plan",
                return_value={"success": True, "tasks_added": 2},
            ),
            patch.object(orchestrate.Orchestrator, "run", mock_run),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0
        mock_run.assert_not_called()
        captured = capsys.readouterr()
        assert "APPROVAL GATE: Tasks added to tasklist" in captured.out

    def test_no_approve_skips_gate_and_runs(self, tmp_path, monkeypatch):
        """--plan --complete --no-approve skips the plans gate and executes tasks."""
        from millstone import orchestrate

        _setup_tmp_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        call_order: list[str] = []

        with (
            patch(
                "sys.argv",
                ["millstone", "--plan", "designs/foo.md", "--complete", "--no-approve"],
            ),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(approve_plans=True),
            ),
            patch.object(orchestrate.Orchestrator, "preflight_checks"),
        ):
            mock_run_plan = MagicMock(
                side_effect=lambda *_args, **_kw: (
                    call_order.append("run_plan"),
                    {"success": True, "tasks_added": 2},
                )[1]
            )
            mock_run = MagicMock(side_effect=lambda *_args, **_kw: (call_order.append("run"), 0)[1])
            with (
                patch.object(orchestrate.Orchestrator, "run_plan", mock_run_plan),
                patch.object(orchestrate.Orchestrator, "run", mock_run),
                pytest.raises(SystemExit) as exc_info,
            ):
                orchestrate.main()

        assert exc_info.value.code == 0
        mock_run.assert_called_once()
        assert call_order == ["run_plan", "run"], f"Expected run_plan then run, got {call_order}"

    def test_config_approve_plans_false_skips_gate(self, tmp_path, monkeypatch):
        """--plan --complete with approve_plans=False in config runs tasks immediately."""
        from millstone import orchestrate

        _setup_tmp_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        call_order: list[str] = []
        mock_run_plan = MagicMock(
            side_effect=lambda *_args, **_kw: (
                call_order.append("run_plan"),
                {"success": True, "tasks_added": 2},
            )[1]
        )
        mock_run = MagicMock(side_effect=lambda *_args, **_kw: (call_order.append("run"), 0)[1])
        with (
            patch("sys.argv", ["millstone", "--plan", "designs/foo.md", "--complete"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(approve_plans=False),
            ),
            patch.object(orchestrate.Orchestrator, "preflight_checks"),
            patch.object(orchestrate.Orchestrator, "run_plan", mock_run_plan),
            patch.object(orchestrate.Orchestrator, "run", mock_run),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0
        mock_run.assert_called_once()
        assert call_order == ["run_plan", "run"], f"Expected run_plan then run, got {call_order}"

    def test_without_complete_flag_exits_after_plan(self, tmp_path, monkeypatch):
        """--plan without --complete exits 0 after planning without running tasks."""
        from millstone import orchestrate

        monkeypatch.chdir(tmp_path)
        _setup_tmp_repo(tmp_path)

        mock_run = MagicMock(return_value=0)
        with (
            patch("sys.argv", ["millstone", "--plan", "designs/foo.md"]),
            patch("millstone.runtime.orchestrator.load_config", return_value=_base_config()),
            patch.object(orchestrate.Orchestrator, "preflight_checks"),
            patch.object(
                orchestrate.Orchestrator,
                "run_plan",
                return_value={"success": True, "tasks_added": 2},
            ),
            patch.object(orchestrate.Orchestrator, "run", mock_run),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0
        mock_run.assert_not_called()

    def test_run_plan_failure_exits_nonzero_without_running(self, tmp_path, monkeypatch):
        """--plan --complete exits nonzero when run_plan() fails, without calling run()."""
        from millstone import orchestrate

        _setup_tmp_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        with (
            patch(
                "sys.argv",
                ["millstone", "--plan", "designs/foo.md", "--complete", "--no-approve"],
            ),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(approve_plans=False),
            ),
            patch.object(orchestrate.Orchestrator, "preflight_checks"),
            patch.object(
                orchestrate.Orchestrator,
                "run_plan",
                return_value={"success": False, "error": "planning failed"},
            ),
        ):
            mock_run = MagicMock(return_value=0)
            with (
                patch.object(orchestrate.Orchestrator, "run", mock_run),
                pytest.raises(SystemExit) as exc_info,
            ):
                orchestrate.main()

        assert exc_info.value.code != 0
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# --design --complete
# ---------------------------------------------------------------------------


class TestDesignCompleteFlag:
    """--design ... --complete execution paths."""

    def test_halts_at_designs_gate_by_default(self, tmp_path, capsys, monkeypatch):
        """--design --complete exits 0 at designs gate when approve_designs defaults to True."""
        from millstone import orchestrate

        _setup_tmp_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        mock_plan = MagicMock(return_value={"success": True, "tasks_added": 2})
        with (
            patch("sys.argv", ["millstone", "--design", "Add caching", "--complete"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(review_designs=False),
            ),
            patch.object(orchestrate.Orchestrator, "preflight_checks"),
            patch.object(orchestrate.Orchestrator, "save_outer_loop_checkpoint"),
            patch.object(
                orchestrate.Orchestrator,
                "run_design",
                return_value={"success": True, "design_file": "designs/foo.md"},
            ),
            patch.object(orchestrate.Orchestrator, "run_plan", mock_plan),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0
        mock_plan.assert_not_called()
        captured = capsys.readouterr()
        assert "APPROVAL GATE: Design created" in captured.out

    def test_skips_designs_gate_halts_at_plans_gate(self, tmp_path, capsys, monkeypatch):
        """With approve_designs=False, chains to plan then halts at plans gate."""
        from millstone import orchestrate

        _setup_tmp_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        mock_plan = MagicMock(return_value={"success": True, "tasks_added": 2})
        mock_run = MagicMock(return_value=0)
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
            patch.object(orchestrate.Orchestrator, "preflight_checks"),
            patch.object(orchestrate.Orchestrator, "save_outer_loop_checkpoint"),
            patch.object(
                orchestrate.Orchestrator,
                "run_design",
                return_value={"success": True, "design_file": "designs/foo.md"},
            ),
            patch.object(orchestrate.Orchestrator, "run_plan", mock_plan),
            patch.object(orchestrate.Orchestrator, "run", mock_run),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0
        mock_plan.assert_called_once()
        mock_run.assert_not_called()
        captured = capsys.readouterr()
        assert "APPROVAL GATE: Tasks added to tasklist" in captured.out

    def test_without_complete_flag_exits_after_design(self):
        """--design without --complete exits 0 after design without chaining to plan."""
        from millstone import orchestrate

        with (
            patch("sys.argv", ["millstone", "--design", "Add caching"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(review_designs=False),
            ),
            patch.object(orchestrate.Orchestrator, "__init__", return_value=None),
            patch.object(
                orchestrate.Orchestrator,
                "run_design",
                return_value={"success": True, "design_file": "designs/foo.md"},
            ),
        ):
            mock_plan = MagicMock(return_value={"success": True, "tasks_added": 2})
            with (
                patch.object(orchestrate.Orchestrator, "run_plan", mock_plan),
                pytest.raises(SystemExit) as exc_info,
            ):
                orchestrate.main()

        assert exc_info.value.code == 0
        mock_plan.assert_not_called()

    def test_no_approve_runs_full_chain(self, tmp_path, monkeypatch):
        """--design --complete --no-approve runs design -> plan -> execute in order."""
        from millstone import orchestrate

        _setup_tmp_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        call_order: list[str] = []

        with (
            patch(
                "sys.argv",
                ["millstone", "--design", "Add caching", "--complete", "--no-approve"],
            ),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(
                    review_designs=False,
                    approve_designs=True,
                    approve_plans=True,
                ),
            ),
            patch.object(orchestrate.Orchestrator, "preflight_checks"),
        ):
            mock_design = MagicMock(
                side_effect=lambda *_args, **_kw: (
                    call_order.append("run_design"),
                    {"success": True, "design_file": "designs/foo.md"},
                )[1]
            )
            mock_plan = MagicMock(
                side_effect=lambda *_args, **_kw: (
                    call_order.append("run_plan"),
                    {"success": True, "tasks_added": 2},
                )[1]
            )
            mock_run = MagicMock(side_effect=lambda *_args, **_kw: (call_order.append("run"), 0)[1])
            with (
                patch.object(orchestrate.Orchestrator, "run_design", mock_design),
                patch.object(orchestrate.Orchestrator, "run_plan", mock_plan),
                patch.object(orchestrate.Orchestrator, "run", mock_run),
                pytest.raises(SystemExit) as exc_info,
            ):
                orchestrate.main()

        assert exc_info.value.code == 0
        mock_plan.assert_called_once()
        mock_run.assert_called_once()
        assert call_order == ["run_design", "run_plan", "run"], (
            f"Expected run_design -> run_plan -> run, got {call_order}"
        )


# ---------------------------------------------------------------------------
# --analyze --complete
# ---------------------------------------------------------------------------


class TestAnalyzeCompleteFlag:
    """--analyze --complete execution paths."""

    def test_halts_at_opportunities_gate_by_default(self, tmp_path, capsys, monkeypatch):
        """--analyze --complete exits 0 at opportunities gate when approve_opportunities=True."""
        from millstone import orchestrate
        from millstone.loops.pipeline.stages import AnalyzeStage

        _setup_tmp_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        mock_design = MagicMock(return_value={"success": True, "design_file": "designs/foo.md"})
        with (
            patch("sys.argv", ["millstone", "--analyze", "--complete"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(),
            ),
            patch.object(orchestrate.Orchestrator, "preflight_checks"),
            patch.object(orchestrate.Orchestrator, "save_outer_loop_checkpoint"),
            patch.object(orchestrate.Orchestrator, "run_analyze", return_value={"success": True}),
            patch.object(
                AnalyzeStage,
                "_load_opportunities",
                return_value=[_make_opportunity()],
            ),
            patch.object(orchestrate.Orchestrator, "run_design", mock_design),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0
        mock_design.assert_not_called()
        captured = capsys.readouterr()
        assert "APPROVAL GATE: Opportunities identified" in captured.out

    def test_without_complete_flag_exits_after_analyze(self):
        """--analyze without --complete exits 0 after analysis without chaining to design."""
        from millstone import orchestrate
        from millstone.loops.pipeline.stages import AnalyzeStage

        mock_design = MagicMock(return_value={"success": True, "design_file": "designs/foo.md"})
        with (
            patch("sys.argv", ["millstone", "--analyze"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(tasklist_provider="mcp"),
            ),
            patch.object(orchestrate.Orchestrator, "__init__", return_value=None),
            patch.object(orchestrate.Orchestrator, "run_analyze", return_value={"success": True}),
            patch.object(AnalyzeStage, "_load_opportunities", return_value=[]),
            patch.object(orchestrate.Orchestrator, "run_design", mock_design),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0
        mock_design.assert_not_called()

    def test_no_opportunities_exits_zero_without_chaining(self, tmp_path, monkeypatch):
        """--analyze --complete with no opportunities exits 0 without chaining."""
        from millstone import orchestrate
        from millstone.loops.pipeline.stages import AnalyzeStage

        _setup_tmp_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        mock_design = MagicMock(return_value={"success": True, "design_file": "designs/foo.md"})
        with (
            patch("sys.argv", ["millstone", "--analyze", "--complete"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(),
            ),
            patch.object(orchestrate.Orchestrator, "preflight_checks"),
            patch.object(orchestrate.Orchestrator, "run_analyze", return_value={"success": True}),
            patch.object(
                AnalyzeStage,
                "_load_opportunities",
                return_value=[],
            ),
            patch.object(orchestrate.Orchestrator, "run_design", mock_design),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0
        mock_design.assert_not_called()

    def test_skips_opps_gate_halts_at_designs_gate(self, tmp_path, capsys, monkeypatch):
        """With approve_opportunities=False, chains to design then halts at designs gate."""
        from millstone import orchestrate
        from millstone.loops.pipeline.stages import AnalyzeStage

        _setup_tmp_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        mock_design = MagicMock(return_value={"success": True, "design_file": "designs/foo.md"})
        mock_plan = MagicMock(return_value={"success": True})
        with (
            patch("sys.argv", ["millstone", "--analyze", "--complete"]),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(
                    review_designs=False,
                    approve_opportunities=False,
                    approve_designs=True,
                ),
            ),
            patch.object(orchestrate.Orchestrator, "preflight_checks"),
            patch.object(orchestrate.Orchestrator, "save_outer_loop_checkpoint"),
            patch.object(orchestrate.Orchestrator, "run_analyze", return_value={"success": True}),
            patch.object(
                AnalyzeStage,
                "_load_opportunities",
                return_value=[_make_opportunity()],
            ),
            patch.object(orchestrate.Orchestrator, "run_design", mock_design),
            patch.object(orchestrate.Orchestrator, "run_plan", mock_plan),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code == 0
        mock_design.assert_called_once()
        mock_plan.assert_not_called()
        captured = capsys.readouterr()
        assert "APPROVAL GATE: Design created" in captured.out

    def test_no_approve_runs_full_chain(self, tmp_path, monkeypatch):
        """--analyze --complete --no-approve runs analyze -> design -> plan -> execute in order."""
        from millstone import orchestrate
        from millstone.loops.pipeline.stages import AnalyzeStage

        _setup_tmp_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        call_order: list[str] = []

        with (
            patch(
                "sys.argv",
                ["millstone", "--analyze", "--complete", "--no-approve"],
            ),
            patch(
                "millstone.runtime.orchestrator.load_config",
                return_value=_base_config(
                    review_designs=False,
                    approve_opportunities=True,
                    approve_designs=True,
                    approve_plans=True,
                ),
            ),
            patch.object(orchestrate.Orchestrator, "preflight_checks"),
        ):
            mock_analyze = MagicMock(
                side_effect=lambda *_args, **_kw: (
                    call_order.append("run_analyze"),
                    {"success": True},
                )[1]
            )
            mock_design = MagicMock(
                side_effect=lambda *_args, **_kw: (
                    call_order.append("run_design"),
                    {"success": True, "design_file": "designs/foo.md"},
                )[1]
            )
            mock_plan = MagicMock(
                side_effect=lambda *_args, **_kw: (
                    call_order.append("run_plan"),
                    {"success": True, "tasks_added": 2},
                )[1]
            )
            mock_run = MagicMock(side_effect=lambda *_args, **_kw: (call_order.append("run"), 0)[1])
            with (
                patch.object(orchestrate.Orchestrator, "run_analyze", mock_analyze),
                patch.object(
                    AnalyzeStage,
                    "_load_opportunities",
                    return_value=[_make_opportunity()],
                ),
                patch.object(orchestrate.Orchestrator, "run_design", mock_design),
                patch.object(orchestrate.Orchestrator, "run_plan", mock_plan),
                patch.object(orchestrate.Orchestrator, "run", mock_run),
                pytest.raises(SystemExit) as exc_info,
            ):
                orchestrate.main()

        assert exc_info.value.code == 0
        mock_analyze.assert_called_once()
        mock_design.assert_called_once()
        mock_plan.assert_called_once()
        mock_run.assert_called_once()
        assert call_order == ["run_analyze", "run_design", "run_plan", "run"], (
            f"Expected run_analyze -> run_design -> run_plan -> run, got {call_order}"
        )


# ---------------------------------------------------------------------------
# argparse validation
# ---------------------------------------------------------------------------


class TestCompleteFlagArgparse:
    """--complete argparse validation."""

    def test_complete_without_outer_loop_flag_exits_nonzero(self):
        """--complete alone (without --plan, --design, or --analyze) is rejected."""
        from millstone import orchestrate

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

    def test_complete_with_cycle_exits_nonzero(self):
        """--complete --cycle is rejected by argparse."""
        from millstone import orchestrate

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

    def test_complete_with_deliver_exits_nonzero(self):
        """--complete --deliver is rejected by argparse."""
        from millstone import orchestrate

        with (
            patch("sys.argv", ["millstone", "--complete", "--deliver", "Add caching"]),
            patch("millstone.runtime.orchestrator.load_config", return_value=_base_config()),
            pytest.raises(SystemExit) as exc_info,
        ):
            orchestrate.main()

        assert exc_info.value.code != 0
