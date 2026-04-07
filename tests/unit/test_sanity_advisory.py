"""Tests for advisory (non-blocking) sanity check behavior.

Sanity checks return flag text (str) when concerns are found, or None when OK.
They never halt the session or create STOP.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from millstone.loops.inner import InnerLoopManager


@pytest.fixture
def inner_loop(tmp_path: Path) -> InnerLoopManager:
    work_dir = tmp_path / ".millstone"
    work_dir.mkdir()
    return InnerLoopManager(work_dir=work_dir, repo_dir=tmp_path)


class TestSanityCheckImplAdvisory:
    """Implementation sanity check returns flags instead of halting."""

    def test_ok_returns_none(self, inner_loop: InnerLoopManager):
        result = inner_loop.sanity_check_impl(
            agent_output="Implemented the feature",
            git_status="M src/foo.py",
            git_diff="diff --git a/src/foo.py",
            load_prompt_callback=lambda _: (
                "{{ORCHESTRATOR_DIR}} {{AGENT_OUTPUT}} {{GIT_STATUS}} {{GIT_DIFF}}"
            ),
            run_agent_callback=lambda *_, **__: '{"status": "OK"}',
        )
        assert result is None

    def test_halt_returns_flag_string(self, inner_loop: InnerLoopManager):
        result = inner_loop.sanity_check_impl(
            agent_output="asdfghjkl gibberish",
            git_status="",
            git_diff="",
            load_prompt_callback=lambda _: (
                "{{ORCHESTRATOR_DIR}} {{AGENT_OUTPUT}} {{GIT_STATUS}} {{GIT_DIFF}}"
            ),
            run_agent_callback=lambda *_, **__: (
                '{"status": "HALT", "reason": "Output is incoherent"}'
            ),
        )
        assert result == "Output is incoherent"

    def test_halt_does_not_create_stop_file(self, inner_loop: InnerLoopManager):
        inner_loop.sanity_check_impl(
            agent_output="gibberish",
            git_status="",
            git_diff="",
            load_prompt_callback=lambda _: (
                "{{ORCHESTRATOR_DIR}} {{AGENT_OUTPUT}} {{GIT_STATUS}} {{GIT_DIFF}}"
            ),
            run_agent_callback=lambda *_, **__: '{"status": "HALT", "reason": "bad output"}',
        )
        stop_file = inner_loop.work_dir / "STOP.md"
        assert not stop_file.exists()

    def test_halt_without_reason_returns_default_message(self, inner_loop: InnerLoopManager):
        result = inner_loop.sanity_check_impl(
            agent_output="gibberish",
            git_status="",
            git_diff="",
            load_prompt_callback=lambda _: (
                "{{ORCHESTRATOR_DIR}} {{AGENT_OUTPUT}} {{GIT_STATUS}} {{GIT_DIFF}}"
            ),
            run_agent_callback=lambda *_, **__: '{"status": "HALT"}',
        )
        assert result is not None
        assert "flagged" in result.lower() or "concern" in result.lower()

    def test_legacy_stop_file_consumed_and_removed(self, inner_loop: InnerLoopManager):
        """If an external tool creates STOP.md, it's read as a flag and removed."""
        stop_file = inner_loop.work_dir / "STOP.md"
        stop_file.write_text("External tool detected a problem")

        result = inner_loop.sanity_check_impl(
            agent_output="some output",
            git_status="",
            git_diff="",
            load_prompt_callback=lambda _: (
                "{{ORCHESTRATOR_DIR}} {{AGENT_OUTPUT}} {{GIT_STATUS}} {{GIT_DIFF}}"
            ),
            run_agent_callback=lambda *_, **__: '{"status": "OK"}',
        )
        assert "External tool detected a problem" in result
        assert not stop_file.exists()


class TestSanityCheckReviewAdvisory:
    """Review sanity check returns flags instead of halting."""

    def test_ok_returns_none(self, inner_loop: InnerLoopManager):
        result = inner_loop.sanity_check_review(
            review_output='{"status": "APPROVED", "review": "Looks good"}',
            load_prompt_callback=lambda _: "{{ORCHESTRATOR_DIR}} {{REVIEW_OUTPUT}}",
            run_agent_callback=lambda *_, **__: '{"status": "OK"}',
        )
        assert result is None

    def test_halt_returns_flag_string(self, inner_loop: InnerLoopManager):
        result = inner_loop.sanity_check_review(
            review_output="totally incoherent review text",
            load_prompt_callback=lambda _: "{{ORCHESTRATOR_DIR}} {{REVIEW_OUTPUT}}",
            run_agent_callback=lambda *_, **__: (
                '{"status": "HALT", "reason": "Review is incoherent"}'
            ),
        )
        assert result == "Review is incoherent"

    def test_halt_does_not_create_stop_file(self, inner_loop: InnerLoopManager):
        inner_loop.sanity_check_review(
            review_output="incoherent",
            load_prompt_callback=lambda _: "{{ORCHESTRATOR_DIR}} {{REVIEW_OUTPUT}}",
            run_agent_callback=lambda *_, **__: '{"status": "HALT", "reason": "bad review"}',
        )
        stop_file = inner_loop.work_dir / "STOP.md"
        assert not stop_file.exists()

    def test_legacy_stop_file_consumed_and_removed(self, inner_loop: InnerLoopManager):
        stop_file = inner_loop.work_dir / "STOP.md"
        stop_file.write_text("External review problem")

        result = inner_loop.sanity_check_review(
            review_output="some review",
            load_prompt_callback=lambda _: "{{ORCHESTRATOR_DIR}} {{REVIEW_OUTPUT}}",
            run_agent_callback=lambda *_, **__: '{"status": "OK"}',
        )
        assert "External review problem" in result
        assert not stop_file.exists()


class TestConsumeStopFile:
    """_consume_stop_file reads, removes, and returns STOP.md content."""

    def test_returns_none_when_no_file(self, inner_loop: InnerLoopManager):
        assert inner_loop._consume_stop_file() is None

    def test_returns_content_and_removes_file(self, inner_loop: InnerLoopManager):
        stop_file = inner_loop.work_dir / "STOP.md"
        stop_file.write_text("something went wrong")
        result = inner_loop._consume_stop_file()
        assert result == "something went wrong"
        assert not stop_file.exists()
