"""Tests for git() helper error handling in InnerLoopManager and EvalManager.

These tests assert that failed git operations (nonzero returncode) raise
GitCommandError rather than silently returning empty stdout, which could
be misinterpreted as "no changes" or "no diff".
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from millstone.artifacts.eval_manager import EvalManager
from millstone.loops.inner import InnerLoopManager


@pytest.fixture
def inner_loop(tmp_path: Path) -> InnerLoopManager:
    work_dir = tmp_path / ".millstone"
    work_dir.mkdir()
    return InnerLoopManager(work_dir=work_dir, repo_dir=tmp_path)


@pytest.fixture
def eval_mgr(tmp_path: Path) -> EvalManager:
    work_dir = tmp_path / ".millstone"
    work_dir.mkdir()
    return EvalManager(
        repo_dir=tmp_path,
        work_dir=work_dir,
        project_config={},
        policy={},
        category_weights={},
        category_thresholds={},
    )


# ---------------------------------------------------------------------------
# InnerLoopManager.git()
# ---------------------------------------------------------------------------


class TestInnerLoopGit:
    """InnerLoopManager.git() must raise on failed git commands."""

    def test_successful_command_returns_stdout(self, inner_loop: InnerLoopManager):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="abc\n", stderr="")
        with patch("subprocess.run", return_value=ok):
            assert inner_loop.git("status") == "abc\n"

    def test_failed_command_raises(self, inner_loop: InnerLoopManager):
        fail = subprocess.CompletedProcess(
            args=["git", "diff"], returncode=128, stdout="", stderr="fatal: bad object"
        )
        with patch("subprocess.run", return_value=fail):
            with pytest.raises(subprocess.CalledProcessError) as exc_info:
                inner_loop.git("diff")
            assert exc_info.value.returncode == 128
            assert "fatal: bad object" in exc_info.value.stderr

    def test_failed_diff_not_silent(self, inner_loop: InnerLoopManager):
        """A failed git diff must NOT be silently interpreted as 'no changes'."""
        fail = subprocess.CompletedProcess(
            args=["git", "diff", "HEAD"], returncode=1, stdout="", stderr="error"
        )
        with (
            patch("subprocess.run", return_value=fail),
            pytest.raises(subprocess.CalledProcessError),
        ):
            inner_loop.git("diff", "HEAD")

    def test_empty_stdout_on_success_is_fine(self, inner_loop: InnerLoopManager):
        """A zero-returncode command with empty stdout is legitimate (e.g. clean status)."""
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=ok):
            assert inner_loop.git("status", "--porcelain") == ""


# ---------------------------------------------------------------------------
# EvalManager.git()
# ---------------------------------------------------------------------------


class TestEvalManagerGit:
    """EvalManager.git() must raise on failed git commands."""

    def test_successful_command_returns_stdout(self, eval_mgr: EvalManager):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="abc123\n", stderr="")
        with patch("subprocess.run", return_value=ok):
            assert eval_mgr.git("rev-parse", "HEAD") == "abc123\n"

    def test_failed_command_raises(self, eval_mgr: EvalManager):
        fail = subprocess.CompletedProcess(
            args=["git", "rev-parse", "HEAD"],
            returncode=128,
            stdout="",
            stderr="fatal: not a git repo",
        )
        with patch("subprocess.run", return_value=fail):
            with pytest.raises(subprocess.CalledProcessError) as exc_info:
                eval_mgr.git("rev-parse", "HEAD")
            assert exc_info.value.returncode == 128

    def test_failed_rev_parse_not_silent(self, eval_mgr: EvalManager):
        """A failed rev-parse must raise, not return empty string."""
        fail = subprocess.CompletedProcess(
            args=["git", "rev-parse", "HEAD"],
            returncode=1,
            stdout="",
            stderr="error",
        )
        with (
            patch("subprocess.run", return_value=fail),
            pytest.raises(subprocess.CalledProcessError),
        ):
            eval_mgr.git("rev-parse", "HEAD")

    def test_empty_stdout_on_success_is_fine(self, eval_mgr: EvalManager):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=ok):
            assert eval_mgr.git("status", "--porcelain") == ""
