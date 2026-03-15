"""Tests for json.loads error handling guards.

Verifies that malformed JSON at eval-file parse sites logs a warning and
skips/falls back rather than crashing, and that review-decision parse sites
default to "needs revision" on malformed input.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from millstone.artifacts.eval_manager import EvalManager


def _make_eval_manager(tmp_path: Path) -> EvalManager:
    """Create a minimal EvalManager for testing."""
    work_dir = tmp_path / ".millstone"
    work_dir.mkdir()
    return EvalManager(
        work_dir=work_dir,
        repo_dir=tmp_path,
        project_config={},
        policy={},
        category_weights={"tests": 1.0},
        category_thresholds={"tests": 100},
    )


def _write_eval_file(evals_dir: Path, name: str, data: dict) -> Path:
    """Write a valid eval JSON file and return its path."""
    f = evals_dir / name
    f.write_text(json.dumps(data))
    return f


class TestEvalManagerJsonGuards:
    """Malformed JSON in eval files should not crash."""

    def test_run_eval_malformed_previous_eval(self, tmp_path: Path) -> None:
        """run_eval: corrupt previous eval file skips delta, doesn't crash."""
        em = _make_eval_manager(tmp_path)
        evals_dir = em.work_dir / "evals"
        evals_dir.mkdir()

        # Write a corrupt previous eval file
        (evals_dir / "2026-01-01T00-00-00.json").write_text("NOT VALID JSON {{{")

        # Patch internals that aren't under test
        em._run_tests = MagicMock(
            return_value={"passed": 1, "failed": 0, "errors": 0, "total": 1, "duration": 0.1}
        )
        em._run_custom_eval_scripts = MagicMock(return_value=[])
        em._run_category_evals = MagicMock(return_value={"categories": {}, "composite_score": 1.0})
        em.git = MagicMock(return_value="abc1234")

        result = em.run_eval()
        # Should complete without crash; no delta since previous was corrupt
        assert "delta" not in result

    def test_compare_evals_malformed_older_file(self, tmp_path: Path) -> None:
        """compare_evals: corrupt older file is skipped, falls back to next valid."""
        em = _make_eval_manager(tmp_path)
        evals_dir = em.work_dir / "evals"
        evals_dir.mkdir()

        _write_eval_file(
            evals_dir, "2026-01-01T00-00-00.json", {"tests": {"passed": 3}, "failed_tests": []}
        )
        (evals_dir / "2026-01-02T00-00-00.json").write_text("CORRUPT")
        _write_eval_file(
            evals_dir, "2026-01-03T00-00-00.json", {"tests": {"passed": 5}, "failed_tests": []}
        )

        result = em.compare_evals()
        # Skipped the corrupt middle file, compared file 1 and 3
        assert result["status"] in ("REGRESSION", "IMPROVEMENT", "NO_CHANGE")
        assert result["older_file"] == "2026-01-01T00-00-00.json"
        assert result["newer_file"] == "2026-01-03T00-00-00.json"

    def test_compare_evals_malformed_newer_file(self, tmp_path: Path) -> None:
        """compare_evals: corrupt newest file is skipped, uses next valid pair."""
        em = _make_eval_manager(tmp_path)
        evals_dir = em.work_dir / "evals"
        evals_dir.mkdir()

        _write_eval_file(
            evals_dir, "2026-01-01T00-00-00.json", {"tests": {"passed": 3}, "failed_tests": []}
        )
        _write_eval_file(
            evals_dir, "2026-01-02T00-00-00.json", {"tests": {"passed": 5}, "failed_tests": []}
        )
        (evals_dir / "2026-01-03T00-00-00.json").write_text("CORRUPT")

        result = em.compare_evals()
        # Skipped the corrupt newest file, compared the two valid ones
        assert result["status"] in ("REGRESSION", "IMPROVEMENT", "NO_CHANGE")
        assert result["older_file"] == "2026-01-01T00-00-00.json"
        assert result["newer_file"] == "2026-01-02T00-00-00.json"

    def test_compare_evals_fewer_than_two_valid(self, tmp_path: Path) -> None:
        """compare_evals: fewer than 2 valid files raises FileNotFoundError."""
        em = _make_eval_manager(tmp_path)
        evals_dir = em.work_dir / "evals"
        evals_dir.mkdir()

        (evals_dir / "2026-01-01T00-00-00.json").write_text("{bad")
        (evals_dir / "2026-01-02T00-00-00.json").write_text("{bad")

        with pytest.raises(FileNotFoundError, match="valid eval files"):
            em.compare_evals()

    def test_compare_evals_one_valid_one_corrupt(self, tmp_path: Path) -> None:
        """compare_evals: only 1 valid file (other corrupt) raises FileNotFoundError."""
        em = _make_eval_manager(tmp_path)
        evals_dir = em.work_dir / "evals"
        evals_dir.mkdir()

        _write_eval_file(
            evals_dir, "2026-01-01T00-00-00.json", {"tests": {"passed": 3}, "failed_tests": []}
        )
        (evals_dir / "2026-01-02T00-00-00.json").write_text("CORRUPT")

        with pytest.raises(FileNotFoundError, match="valid eval files"):
            em.compare_evals()

    def test_update_eval_summary_corrupt_reconstructs_from_eval_files(self, tmp_path: Path) -> None:
        """_update_eval_summary: corrupt summary.json is renamed and reconstructed from eval files.

        Per criteria: corrupt summary.json with 3 valid eval files in evals/
        must produce a summary with 3 entries after reconstruction (not 1).
        """
        em = _make_eval_manager(tmp_path)
        evals_dir = em.work_dir / "evals"
        evals_dir.mkdir()

        summary_file = evals_dir / "summary.json"
        summary_file.write_text("NOT JSON!!!")

        # Write 3 valid eval files (these are the source of truth)
        eval1 = {
            "timestamp": "2026-01-01",
            "composite_score": 0.8,
            "tests": {"passed": 10, "failed": 2},
        }
        eval2 = {
            "timestamp": "2026-01-02",
            "composite_score": 0.85,
            "tests": {"passed": 12, "failed": 1},
        }
        eval3 = {
            "timestamp": "2026-01-03",
            "composite_score": 0.9,
            "tests": {"passed": 15, "failed": 0},
        }
        _write_eval_file(evals_dir, "2026-01-01T00-00-00.json", eval1)
        _write_eval_file(evals_dir, "2026-01-02T00-00-00.json", eval2)
        _write_eval_file(evals_dir, "2026-01-03T00-00-00.json", eval3)

        # Call _update_eval_summary — it should detect corrupt summary, reconstruct
        em._update_eval_summary(
            evals_dir,
            "2026-01-03T00-00-00",
            eval3,
        )

        # Verify corrupt file was renamed
        assert (evals_dir / "summary.json.corrupt").exists()

        # Verify reconstructed summary has entries from ALL valid eval files
        result = json.loads(summary_file.read_text())
        assert "evals" in result
        # Must have exactly 3 entries (from scanning eval files), no duplicates
        assert len(result["evals"]) == 3

    def test_update_eval_summary_no_duplicate_current_entry(self, tmp_path: Path) -> None:
        """_update_eval_summary: reconstruction must not duplicate the current eval entry.

        When run_eval writes the current eval file BEFORE calling _update_eval_summary,
        reconstruction must exclude it so the final append doesn't create a duplicate.
        """
        em = _make_eval_manager(tmp_path)
        evals_dir = em.work_dir / "evals"
        evals_dir.mkdir()

        summary_file = evals_dir / "summary.json"
        summary_file.write_text("NOT JSON!!!")

        eval1 = {
            "timestamp": "2026-01-01",
            "composite_score": 0.8,
            "tests": {"passed": 10, "failed": 2},
        }
        eval2 = {
            "timestamp": "2026-01-02",
            "composite_score": 0.85,
            "tests": {"passed": 12, "failed": 1},
        }
        # Simulate run_eval having already written the current eval file
        current = {
            "timestamp": "2026-01-03",
            "composite_score": 0.9,
            "tests": {"passed": 15, "failed": 0},
        }
        _write_eval_file(evals_dir, "2026-01-01T00-00-00.json", eval1)
        _write_eval_file(evals_dir, "2026-01-02T00-00-00.json", eval2)
        _write_eval_file(evals_dir, "2026-01-03T00-00-00.json", current)

        em._update_eval_summary(evals_dir, "2026-01-03T00-00-00", current)

        result = json.loads(summary_file.read_text())
        # Exactly 3 entries: 2 from reconstruction + 1 appended current
        assert len(result["evals"]) == 3
        # No duplicate filenames
        filenames = [e["file"] for e in result["evals"]]
        assert filenames.count("2026-01-03T00-00-00.json") == 1

    def test_get_latest_eval_malformed(self, tmp_path: Path) -> None:
        """_get_latest_eval: corrupt eval file returns None, not crash."""
        em = _make_eval_manager(tmp_path)
        evals_dir = em.work_dir / "evals"
        evals_dir.mkdir()

        (evals_dir / "2026-01-01T00-00-00.json").write_text("INVALID JSON")

        result = em._get_latest_eval()
        assert result is None


class TestOuterLoopJsonGuards:
    """Malformed JSON in eval files should not crash outer loop signal gathering."""

    def test_collect_hard_signals_newest_corrupt_falls_back(self, tmp_path: Path) -> None:
        """collect_hard_signals: corrupt newest eval falls back to older valid eval.

        Per criteria: newest eval corrupt + older eval with failures must return
        the older eval's failures, not empty.
        """
        from millstone.loops.outer import OuterLoopManager

        work_dir = tmp_path / ".millstone"
        work_dir.mkdir()
        evals_dir = work_dir / "evals"
        evals_dir.mkdir()

        # Older valid eval with a real failure
        _write_eval_file(evals_dir, "2026-01-01T00-00-00.json", {"failed_tests": ["test_x"]})
        # Newest eval is corrupt
        (evals_dir / "2026-01-02T00-00-00.json").write_text("NOT JSON")

        olm = OuterLoopManager(
            repo_dir=tmp_path,
            work_dir=work_dir,
            tasklist=".millstone/tasklist.md",
            task_constraints={},
        )
        signals = olm.collect_hard_signals()

        # Should fall back to the older valid eval, not return empty
        assert signals["test_failures"] == ["test_x"]

    def test_collect_hard_signals_all_evals_corrupt(self, tmp_path: Path) -> None:
        """collect_hard_signals: all corrupt evals result in empty test_failures."""
        from millstone.loops.outer import OuterLoopManager

        work_dir = tmp_path / ".millstone"
        work_dir.mkdir()
        evals_dir = work_dir / "evals"
        evals_dir.mkdir()

        (evals_dir / "2026-01-01T00-00-00.json").write_text("NOT JSON")
        (evals_dir / "2026-01-02T00-00-00.json").write_text("ALSO NOT JSON")

        olm = OuterLoopManager(
            repo_dir=tmp_path,
            work_dir=work_dir,
            tasklist=".millstone/tasklist.md",
            task_constraints={},
        )
        signals = olm.collect_hard_signals()

        # No valid eval to fall back to — empty is correct here
        assert signals["test_failures"] == []


class TestReviewDecisionJsonGuards:
    """Review-decision parse sites handle malformed JSON — verify contract."""

    def test_parse_review_decision_malformed(self) -> None:
        """parse_review_decision returns None on malformed JSON."""
        from millstone.policy.schemas import parse_review_decision

        result = parse_review_decision("This is not JSON at all {{{")
        assert result is None

    def test_analyze_review_verdict_malformed(self) -> None:
        """_parse_analyze_review_verdict returns NEEDS_REVISION on malformed JSON."""
        from millstone.loops.outer import OuterLoopManager

        olm = OuterLoopManager.__new__(OuterLoopManager)
        result = olm._parse_analyze_review_verdict("This is not JSON at all {{{")
        assert result["verdict"] == "NEEDS_REVISION"


class TestCorruptJsonWarningEmission:
    """Corrupt JSON must emit a warning via progress(), not silently skip."""

    def test_run_eval_warns_on_corrupt_previous(self, tmp_path: Path) -> None:
        em = _make_eval_manager(tmp_path)
        evals_dir = em.work_dir / "evals"
        evals_dir.mkdir()
        (evals_dir / "2026-01-01T00-00-00.json").write_text("NOT VALID JSON")

        em._run_tests = MagicMock(
            return_value={"passed": 1, "failed": 0, "errors": 0, "total": 1, "duration": 0.1}
        )
        em._run_custom_eval_scripts = MagicMock(return_value=[])
        em._run_category_evals = MagicMock(return_value={"categories": {}, "composite_score": 1.0})
        em.git = MagicMock(return_value="abc1234")

        with patch("millstone.artifacts.eval_manager.progress") as mock_progress:
            em.run_eval()
            warning_calls = [
                c
                for c in mock_progress.call_args_list
                if "corrupt" in str(c).lower() or "warning" in str(c).lower()
            ]
            assert len(warning_calls) >= 1

    def test_compare_evals_warns_on_corrupt_file(self, tmp_path: Path) -> None:
        em = _make_eval_manager(tmp_path)
        evals_dir = em.work_dir / "evals"
        evals_dir.mkdir()
        _write_eval_file(
            evals_dir, "2026-01-01T00-00-00.json", {"tests": {"passed": 3}, "failed_tests": []}
        )
        (evals_dir / "2026-01-02T00-00-00.json").write_text("CORRUPT")
        _write_eval_file(
            evals_dir, "2026-01-03T00-00-00.json", {"tests": {"passed": 5}, "failed_tests": []}
        )

        with patch("millstone.artifacts.eval_manager.progress") as mock_progress:
            em.compare_evals()
            warning_calls = [
                c
                for c in mock_progress.call_args_list
                if "corrupt" in str(c).lower() or "warning" in str(c).lower()
            ]
            assert len(warning_calls) >= 1

    def test_update_eval_summary_warns_on_corrupt_summary(self, tmp_path: Path) -> None:
        em = _make_eval_manager(tmp_path)
        evals_dir = em.work_dir / "evals"
        evals_dir.mkdir()
        (evals_dir / "summary.json").write_text("NOT JSON")
        eval1 = {
            "timestamp": "2026-01-01",
            "composite_score": 0.8,
            "tests": {"passed": 10, "failed": 0},
        }
        _write_eval_file(evals_dir, "2026-01-01T00-00-00.json", eval1)

        with patch("millstone.artifacts.eval_manager.progress") as mock_progress:
            em._update_eval_summary(evals_dir, "2026-01-01T00-00-00", eval1)
            warning_calls = [
                c
                for c in mock_progress.call_args_list
                if "corrupt" in str(c).lower() or "warning" in str(c).lower()
            ]
            assert len(warning_calls) >= 1

    def test_get_latest_eval_warns_on_corrupt(self, tmp_path: Path) -> None:
        em = _make_eval_manager(tmp_path)
        evals_dir = em.work_dir / "evals"
        evals_dir.mkdir()
        (evals_dir / "2026-01-01T00-00-00.json").write_text("INVALID")

        with patch("millstone.artifacts.eval_manager.progress") as mock_progress:
            em._get_latest_eval()
            warning_calls = [
                c
                for c in mock_progress.call_args_list
                if "corrupt" in str(c).lower() or "warning" in str(c).lower()
            ]
            assert len(warning_calls) >= 1

    def test_collect_hard_signals_warns_on_corrupt(self, tmp_path: Path) -> None:
        from millstone.loops.outer import OuterLoopManager

        work_dir = tmp_path / ".millstone"
        work_dir.mkdir()
        evals_dir = work_dir / "evals"
        evals_dir.mkdir()
        # Corrupt file is newest so it's tried first and triggers warning
        _write_eval_file(evals_dir, "2026-01-01T00-00-00.json", {"failed_tests": []})
        (evals_dir / "2026-01-02T00-00-00.json").write_text("NOT JSON")

        olm = OuterLoopManager(
            repo_dir=tmp_path,
            work_dir=work_dir,
            tasklist=".millstone/tasklist.md",
            task_constraints={},
        )

        with patch("millstone.loops.outer.progress") as mock_progress:
            olm.collect_hard_signals()
            warning_calls = [
                c
                for c in mock_progress.call_args_list
                if "corrupt" in str(c).lower() or "warning" in str(c).lower()
            ]
            assert len(warning_calls) >= 1
