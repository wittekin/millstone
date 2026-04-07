"""E2E tests for advisory flags → reviewer/builder information flow.

These tests verify that sanity check and dangerous pattern flags propagate
correctly through the orchestrator pipeline rather than halting the session:

1. Impl sanity flags arrive in the reviewer prompt as advisory context.
2. Review sanity flags arrive in the builder feedback text.
3. Dangerous pattern flags (action=flag) arrive in the reviewer prompt.
4. The full flow continues (no halt) when flags are present.
5. Clean checks produce no flag artifacts in prompts.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from millstone.runtime.orchestrator import Orchestrator
from tests.e2e.conftest import StubCli

_original_subprocess_run = subprocess.run

_APPROVED_JSON = (
    '{"status": "APPROVED", "review": "Looks good", "summary": "Looks good!",'
    ' "findings": [], "findings_by_severity":'
    ' {"critical": [], "high": [], "medium": [], "low": [], "nit": []},'
    ' "impossible_condition": null, "tasklist_fix_recommendation": null}'
)
_REQUEST_CHANGES_JSON = (
    '{"status": "REQUEST_CHANGES", "review": "Needs work", "summary": "Issues found",'
    ' "findings": ["Missing tests"], "findings_by_severity":'
    ' {"critical": [], "high": ["Missing tests"], "medium": [], "low": [], "nit": []},'
    ' "impossible_condition": null, "tasklist_fix_recommendation": null}'
)
_SANITY_OK_JSON = '{"status": "OK", "reason": ""}'
_SANITY_FLAG_JSON = '{"status": "HALT", "reason": "Builder output looks incoherent — no meaningful code changes detected"}'


def _make_file_change(filename: str = "feature.py", content: str = "def f(): pass\n"):
    def _effect(repo: Path) -> None:
        (repo / filename).write_text(content)
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=False)

    return _effect


def _do_commit(repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "-m", "stub-cli e2e test commit"],
        cwd=repo,
        capture_output=True,
        check=False,
    )


class TestImplSanityFlagsReachReviewer:
    """Impl sanity flags are injected into the reviewer prompt."""

    def test_flagged_impl_injects_sanity_section_into_reviewer_prompt(
        self, stub_cli: StubCli, temp_repo: Path
    ) -> None:
        """When impl sanity check returns HALT, the reviewer prompt contains
        the flag reason under 'Automated Sanity Check Flags'."""
        stub_cli.add(role="author", output="gibberish output", side_effect=_make_file_change())
        stub_cli.add(role="sanity", output=_SANITY_FLAG_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(role="builder", output="Committed.", side_effect=_do_commit)

        orch = Orchestrator(max_tasks=1)
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0

        reviewer_calls = [c for c in stub_cli.calls if c.role == "reviewer"]
        assert reviewer_calls, "No reviewer call recorded"
        reviewer_prompt = reviewer_calls[0].prompt

        assert "Automated Sanity Check Flags" in reviewer_prompt
        assert "incoherent" in reviewer_prompt
        assert "false positives" in reviewer_prompt.lower()

    def test_clean_impl_has_no_sanity_section_in_reviewer_prompt(
        self, stub_cli: StubCli, temp_repo: Path
    ) -> None:
        """When impl sanity check is OK, the reviewer prompt has no sanity flags section."""
        stub_cli.add(role="author", output="Implemented feature.", side_effect=_make_file_change())
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(role="builder", output="Committed.", side_effect=_do_commit)

        orch = Orchestrator(max_tasks=1)
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0

        reviewer_calls = [c for c in stub_cli.calls if c.role == "reviewer"]
        assert reviewer_calls
        reviewer_prompt = reviewer_calls[0].prompt

        assert "Automated Sanity Check Flags" not in reviewer_prompt
        assert "SANITY" not in reviewer_prompt.upper() or "{{SANITY_FLAGS}}" not in reviewer_prompt


class TestReviewSanityFlagsReachBuilder:
    """Review sanity flags flow into builder feedback when review is unparseable."""

    def test_flagged_review_includes_reason_in_builder_feedback(
        self, stub_cli: StubCli, temp_repo: Path
    ) -> None:
        """When the review is unparseable and review sanity check flags it,
        the builder gets the flag reason prepended to its feedback and the
        review is treated as REQUEST_CHANGES (builder gets a second chance)."""
        stub_cli.add(role="author", output="Implemented.", side_effect=_make_file_change())
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)  # impl sanity: OK
        # Unparseable review output — triggers review sanity check
        stub_cli.add(role="reviewer", output="I dunno, looks weird to me maybe?")
        stub_cli.add(
            role="sanity",
            output='{"status": "HALT", "reason": "Review is incoherent and provides no actionable feedback"}',
        )
        # Builder retry after REQUEST_CHANGES from flagged review
        stub_cli.add(
            role="author",
            output="Fixed.",
            side_effect=_make_file_change("feature.py", "def f(): return 1\n"),
        )
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)  # impl sanity on retry: OK
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(role="builder", output="Committed.", side_effect=_do_commit)

        orch = Orchestrator(max_tasks=1)
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0

        # The retry builder call should contain the review sanity flag in its prompt
        author_calls = [c for c in stub_cli.calls if c.role == "author"]
        assert len(author_calls) >= 2, f"Expected retry, got {len(author_calls)} author calls"
        retry_prompt = author_calls[1].prompt
        assert "Review sanity check flagged" in retry_prompt or "incoherent" in retry_prompt


class TestFullFlowContinuesWithFlags:
    """Sanity flags never halt the session — the flow always continues."""

    def test_flagged_impl_approved_by_reviewer_commits(
        self, stub_cli: StubCli, temp_repo: Path, capsys
    ) -> None:
        """Even with an impl sanity flag, if the reviewer approves, the task
        commits successfully. The sanity flag is advisory, not a veto."""
        stub_cli.add(role="author", output="looks like gibberish", side_effect=_make_file_change())
        stub_cli.add(role="sanity", output=_SANITY_FLAG_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(role="builder", output="Committed.", side_effect=_do_commit)

        orch = Orchestrator(max_tasks=1)
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "SANITY FLAG" in captured.out
        assert "Completed and committed" in captured.out

    def test_flagged_impl_rejected_by_reviewer_loops(
        self, stub_cli: StubCli, temp_repo: Path
    ) -> None:
        """When impl is flagged and reviewer rejects, the fix loop continues
        normally (no halt). Second cycle succeeds."""
        # Cycle 1: flagged impl → reviewer rejects
        stub_cli.add(role="author", output="bad attempt", side_effect=_make_file_change())
        stub_cli.add(role="sanity", output=_SANITY_FLAG_JSON)
        stub_cli.add(role="reviewer", output=_REQUEST_CHANGES_JSON)
        # Cycle 2: clean impl → reviewer approves
        stub_cli.add(
            role="author",
            output="fixed it",
            side_effect=_make_file_change("feature.py", "def fixed(): return True\n"),
        )
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(role="builder", output="Committed.", side_effect=_do_commit)

        orch = Orchestrator(max_tasks=1)
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0

        # Verify two author calls (original + fix cycle)
        author_calls = [c for c in stub_cli.calls if c.role == "author"]
        assert len(author_calls) == 2

        # Verify two sanity calls (one per cycle)
        sanity_calls = [c for c in stub_cli.calls if c.role == "sanity"]
        assert len(sanity_calls) == 2

    def test_no_stop_file_remains_after_flagged_run(
        self, stub_cli: StubCli, temp_repo: Path
    ) -> None:
        """STOP.md must never persist after a run with sanity flags."""
        stub_cli.add(role="author", output="gibberish", side_effect=_make_file_change())
        stub_cli.add(role="sanity", output=_SANITY_FLAG_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(role="builder", output="Committed.", side_effect=_do_commit)

        orch = Orchestrator(max_tasks=1)
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()

            assert exit_code == 0
            assert not (orch.work_dir / "STOP.md").exists()
        finally:
            orch.cleanup()


class TestDangerousFlagsReachReviewer:
    """Dangerous pattern flags (action=flag) are injected into the reviewer prompt."""

    def test_flagged_dangerous_pattern_appears_in_reviewer_prompt(
        self, stub_cli: StubCli, temp_repo: Path
    ) -> None:
        """When a dangerous pattern with action=flag matches, the reviewer
        prompt contains the flag in 'Automated Sanity Check Flags'."""
        # Write policy with action=flag for DROP TABLE
        policy_file = temp_repo / ".millstone" / "policy.toml"
        policy_file.write_text(
            "[limits]\nmax_loc_per_task = 10000\n\n"
            '[dangerous]\naction = "flag"\n'
            'patterns = ["DROP TABLE"]\n'
        )

        def _write_sql(repo: Path) -> None:
            (repo / "migrate.sql").write_text("DROP TABLE old_users;\n")
            subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=False)

        stub_cli.add(role="author", output="Added migration.", side_effect=_write_sql)
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(role="builder", output="Committed.", side_effect=_do_commit)

        orch = Orchestrator(max_tasks=1)
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0

        reviewer_calls = [c for c in stub_cli.calls if c.role == "reviewer"]
        assert reviewer_calls
        reviewer_prompt = reviewer_calls[0].prompt

        assert "Automated Sanity Check Flags" in reviewer_prompt
        assert "DROP TABLE" in reviewer_prompt

    def test_blocked_dangerous_pattern_still_halts(
        self, stub_cli: StubCli, temp_repo: Path
    ) -> None:
        """action=block dangerous patterns still halt — no regression."""
        policy_file = temp_repo / ".millstone" / "policy.toml"
        policy_file.write_text(
            "[limits]\nmax_loc_per_task = 10000\n\n"
            '[dangerous]\naction = "block"\n'
            'patterns = ["DROP TABLE"]\n'
        )

        def _write_sql(repo: Path) -> None:
            (repo / "migrate.sql").write_text("DROP TABLE old_users;\n")
            subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=False)

        stub_cli.add(role="author", output="Added migration.", side_effect=_write_sql)

        orch = Orchestrator(max_tasks=1)
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        # Should halt — mechanical check blocks
        assert exit_code == 1

    def test_per_pattern_flag_override_reaches_reviewer(
        self, stub_cli: StubCli, temp_repo: Path
    ) -> None:
        """Per-pattern action=flag overrides the default block action."""
        policy_file = temp_repo / ".millstone" / "policy.toml"
        policy_file.write_text(
            "[limits]\nmax_loc_per_task = 10000\n\n"
            '[dangerous]\naction = "block"\n'
            '[[dangerous.patterns]]\npattern = "DROP TABLE"\naction = "flag"\n'
        )

        def _write_sql(repo: Path) -> None:
            (repo / "migrate.sql").write_text("DROP TABLE old_users;\n")
            subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=False)

        stub_cli.add(role="author", output="Added migration.", side_effect=_write_sql)
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(role="builder", output="Committed.", side_effect=_do_commit)

        orch = Orchestrator(max_tasks=1)
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0

        reviewer_calls = [c for c in stub_cli.calls if c.role == "reviewer"]
        assert reviewer_calls
        assert "DROP TABLE" in reviewer_calls[0].prompt
