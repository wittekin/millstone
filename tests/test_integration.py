"""Integration tests for the full orchestration flow."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from millstone.runtime.orchestrator import Orchestrator

pytestmark = pytest.mark.integration


# Store original subprocess.run to use for git commands
_original_subprocess_run = subprocess.run


def make_mock_runner(temp_repo, claude_responses):
    """
    Create a mock subprocess.run that:
    - Intercepts claude CLI calls and returns configured responses
    - Passes through git and other commands to the real subprocess.run

    claude_responses is a callable that takes (prompt, call_count) and returns:
    - (stdout, side_effect_fn) where side_effect_fn is called for file changes
    """
    call_count = {"claude": 0, "review": 0}

    def mock_run(cmd, **kwargs):
        if cmd[0] == "claude":
            call_count["claude"] += 1
            prompt = cmd[2] if len(cmd) > 2 else ""

            stdout, side_effect = claude_responses(prompt, call_count)
            if side_effect:
                side_effect(temp_repo)

            return MagicMock(stdout=stdout, stderr="", returncode=0)
        else:
            # Pass through to real subprocess for git, etc.
            return _original_subprocess_run(cmd, **kwargs)

    return mock_run


def is_builder_prompt(prompt: str) -> bool:
    """Check if this is the builder/tasklist prompt."""
    return "complete exactly one task" in prompt.lower()


def is_reviewer_prompt(prompt: str) -> bool:
    """Check if this is the code review prompt (not sanity check)."""
    return "review of local, uncommitted changes" in prompt.lower()


def is_sanity_check(prompt: str) -> bool:
    """Check if this is a sanity check prompt."""
    return "sanity check" in prompt.lower()


def is_commit_prompt(prompt: str) -> bool:
    """Check if this is the commit delegation prompt."""
    return "commit your changes" in prompt.lower() or "commit the changes" in prompt.lower()


def do_commit(repo):
    """Actually commit staged changes."""
    _original_subprocess_run(["git", "add", "-A"], cwd=repo, capture_output=True)
    _original_subprocess_run(["git", "commit", "-m", "Test commit"], cwd=repo, capture_output=True)


def do_commit_without_tasklist(repo):
    """Commit code changes but leave tasklist.md unstaged.

    This simulates the bug where builder commits code but forgets to
    stage the tasklist tick.
    """
    # Stage only non-tasklist files
    _original_subprocess_run(["git", "add", "."], cwd=repo, capture_output=True)
    _original_subprocess_run(
        ["git", "reset", "HEAD", "docs/tasklist.md"], cwd=repo, capture_output=True
    )
    _original_subprocess_run(
        ["git", "commit", "-m", "Test commit (without tasklist)"], cwd=repo, capture_output=True
    )


# =============================================================================
# Composable Response Handlers
# =============================================================================
#
# These factories create response handlers that tests can compose rather than
# duplicate. When a new phase is added to the orchestration flow (like commit
# delegation), only these factories need to be updated.


def make_file_change(filename="feature.py", content="def new_feature():\n    pass\n"):
    """Factory for a side effect that creates/modifies a file and stages it."""

    def change(repo):
        (repo / filename).write_text(content)
        _original_subprocess_run(["git", "add", "."], cwd=repo, capture_output=True)

    return change


def make_binary_file_change(filename="image.png"):
    """Factory for a side effect that creates a binary file."""

    def change(repo):
        (repo / filename).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        (repo / "code.py").write_text("pass")
        _original_subprocess_run(["git", "add", "."], cwd=repo, capture_output=True)

    return change


def make_sensitive_file_change(filename=".env", content="SECRET=exposed"):
    """Factory for a side effect that creates a sensitive file."""

    def change(repo):
        (repo / filename).write_text(content)
        _original_subprocess_run(["git", "add", "."], cwd=repo, capture_output=True)

    return change


def make_large_file_change(lines=11):
    """Factory for a side effect that creates a file exceeding LoC threshold."""

    def change(repo):
        content = "\n".join([f"line{i}" for i in range(lines)])
        (repo / "file.txt").write_text(content)
        _original_subprocess_run(["git", "add", "."], cwd=repo, capture_output=True)

    return change


def make_exact_lines_change(lines=10):
    """Factory for a side effect that creates a file with exact line count."""

    def change(repo):
        content = "\n".join([f"line{i}" for i in range(lines)])
        (repo / "file.txt").write_text(content)
        _original_subprocess_run(["git", "add", "."], cwd=repo, capture_output=True)

    return change


class ResponseBuilder:
    """
    Composable response handler builder.

    Usage:
        responses = (ResponseBuilder()
            .on_builder(make_change, output="Implemented feature.")
            .on_reviewer(approve=True)
            .on_commit(success=True)
            .build())

    Or use pre-built factories:
        responses = standard_approval_flow(make_file_change())
        responses = rejection_then_approval_flow(make_file_change(), rejections=1)
    """

    def __init__(self):
        self._builder_change = None
        self._builder_output = "Task completed."
        self._reviewer_responses = []  # List of (approve, response_json)
        self._commit_success = True
        # Use proper JSON format for sanity check to match expected schema
        self._sanity_check_response = ('{"status": "OK"}', None)
        self._default_response = ('{"status": "OK"}', None)

    def on_builder(self, change_fn=None, output="Task completed."):
        """Configure builder phase response."""
        self._builder_change = change_fn
        self._builder_output = output
        return self

    def on_reviewer(self, approve=True, response=None):
        """
        Add a reviewer response. Call multiple times for multiple review cycles.
        First call configures first review, second call configures retry after fixes, etc.
        """
        if response is None:
            if approve:
                response = '{"status": "APPROVED", "review": "Looks good", "summary": "Looks good!", "findings": [], "findings_by_severity": {"critical": [], "high": [], "medium": [], "low": [], "nit": []}}'
            else:
                response = '{"status": "REQUEST_CHANGES", "review": "Needs work", "summary": "Blocking issues", "findings": ["Needs work"], "findings_by_severity": {"critical": [], "high": ["Needs work"], "medium": [], "low": [], "nit": []}}'
        self._reviewer_responses.append((approve, response))
        return self

    def on_commit(self, success=True):
        """Configure commit phase response."""
        self._commit_success = success
        return self

    def on_sanity_check(self, response="OK", side_effect=None):
        """Configure sanity check response."""
        self._sanity_check_response = (response, side_effect)
        return self

    def build(self):
        """Build the response handler function."""
        review_count = [0]

        def is_fix_prompt(prompt: str) -> bool:
            """Check if this is a fix prompt (builder addressing review feedback)."""
            lower = prompt.lower()
            return "address this review feedback" in lower or "feedback to address" in lower

        def responses(prompt, counts):
            if is_builder_prompt(prompt) or is_fix_prompt(prompt):
                return (self._builder_output, self._builder_change)
            elif is_reviewer_prompt(prompt):
                review_count[0] += 1
                idx = min(review_count[0] - 1, len(self._reviewer_responses) - 1)
                if idx >= 0 and idx < len(self._reviewer_responses):
                    _, response = self._reviewer_responses[idx]
                    return (response, None)
                # Default to approval if no more responses configured
                return (
                    '{"status": "APPROVED", "review": "Looks good", "summary": "No blockers", "findings": [], "findings_by_severity": {"critical": [], "high": [], "medium": [], "low": [], "nit": []}}',
                    None,
                )
            elif is_commit_prompt(prompt):
                if self._commit_success:
                    return ("Committed changes.", lambda repo: do_commit(repo))
                else:
                    return ("Failed to commit.", None)
            elif is_sanity_check(prompt):
                return self._sanity_check_response
            else:
                return self._default_response

        return responses


def standard_approval_flow(change_fn=None, builder_output="Implemented feature."):
    """
    Create a response handler for the standard approval flow:
    Builder makes changes → Reviewer approves → Commit succeeds

    Args:
        change_fn: Side effect function to create file changes (default: make_file_change())
        builder_output: Text the builder "says" (default: "Implemented feature.")

    Returns:
        Response handler function for make_mock_runner
    """
    if change_fn is None:
        change_fn = make_file_change()

    return (
        ResponseBuilder()
        .on_builder(change_fn, builder_output)
        .on_reviewer(approve=True)
        .on_commit(success=True)
        .build()
    )


def rejection_then_approval_flow(change_fn=None, rejections=1, builder_output="Made changes."):
    """
    Create a response handler for rejection-then-approval flow:
    Builder makes changes → Reviewer rejects N times → Reviewer approves → Commit succeeds

    Args:
        change_fn: Side effect function to create file changes
        rejections: Number of times reviewer rejects before approving
        builder_output: Text the builder "says"

    Returns:
        Response handler function for make_mock_runner
    """
    if change_fn is None:
        change_fn = make_file_change()

    builder = ResponseBuilder().on_builder(change_fn, builder_output)

    for _ in range(rejections):
        builder.on_reviewer(approve=False)
    builder.on_reviewer(approve=True)
    builder.on_commit(success=True)

    return builder.build()


def always_reject_flow(change_fn=None, builder_output="Made changes."):
    """
    Create a response handler that always rejects (for max cycles tests):
    Builder makes changes → Reviewer always rejects

    Args:
        change_fn: Side effect function to create file changes
        builder_output: Text the builder "says"

    Returns:
        Response handler function for make_mock_runner
    """
    if change_fn is None:
        change_fn = make_file_change()

    # Add many rejections - more than any reasonable max_cycles
    builder = ResponseBuilder().on_builder(change_fn, builder_output)
    for _ in range(10):
        builder.on_reviewer(approve=False)

    return builder.build()


def no_changes_flow(builder_output="Task completed successfully!"):
    """
    Create a response handler where builder claims success but makes no changes.

    Args:
        builder_output: Text the builder "says"

    Returns:
        Response handler function for make_mock_runner
    """
    return ResponseBuilder().on_builder(change_fn=None, output=builder_output).build()


class TestFullFlow:
    """Integration tests for the complete orchestration flow."""

    def test_approval_on_first_cycle(self, temp_repo):
        """
        Scenario: Builder makes changes, reviewer approves immediately.
        Expected: Exit 0 after one cycle.
        """
        responses = standard_approval_flow()

        orch = Orchestrator(max_tasks=1)
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            assert exit_code == 0
            assert orch.cycle == 1
        finally:
            orch.cleanup()

    def test_approval_after_one_fix_cycle(self, temp_repo):
        """
        Scenario: Builder makes changes, reviewer requests changes, builder fixes, reviewer approves.
        Expected: Exit 0 after two cycles.
        """
        responses = rejection_then_approval_flow(rejections=1)

        orch = Orchestrator(max_tasks=1)
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            assert exit_code == 0
            assert orch.cycle == 2
        finally:
            orch.cleanup()

    def test_max_cycles_exceeded(self, temp_repo):
        """
        Scenario: Reviewer keeps requesting changes beyond max cycles.
        Expected: Exit 1 with loop detection.
        """
        responses = always_reject_flow()

        orch = Orchestrator(max_cycles=2)
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            assert exit_code == 1
            assert orch.cycle >= orch.max_cycles
        finally:
            orch.cleanup()

    def test_sanity_check_creates_stop_file(self, temp_repo):
        """
        Scenario: Sanity check agent detects gibberish and creates STOP.md.
        Expected: Exit 1 immediately.
        """
        orch = Orchestrator()

        def create_stop(repo):
            # Create STOP.md in orchestrator's work dir
            stop_file = orch.work_dir / "STOP.md"
            stop_file.write_text("Builder output appears to be gibberish")

        responses = (
            ResponseBuilder()
            .on_builder(make_file_change(), output="asdfghjkl gibberish ???")
            .on_sanity_check(response="Creating STOP.md", side_effect=create_stop)
            .build()
        )

        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            assert exit_code == 1
            assert orch.cycle == 1
        finally:
            orch.cleanup()

    def test_no_changes_detected(self, temp_repo, capsys):
        """
        Scenario: Builder claims success but makes no changes.
        Expected: Exit 0 (Success) with warning, proceeding to review.
        """
        responses = no_changes_flow()

        orch = Orchestrator()
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            # Now succeeds (returns 0) instead of failing
            assert exit_code == 0
            captured = capsys.readouterr()
            # Check for the warning message
            assert "WARN: No changes detected" in captured.out
            # Should proceed to review
            assert orch.cycle == 1
        finally:
            orch.cleanup()

    def test_builder_early_commit_detected(self, temp_repo, capsys):
        """
        Scenario: Builder commits its own changes (HEAD advances).
        Expected: Orchestrator detects the early commit, uses the committed
        diff for review, skips delegate_commit, and succeeds.
        """

        def make_change_and_commit(repo):
            """Simulate builder creating a file AND committing it."""
            (repo / "feature.py").write_text("def new_feature():\n    pass\n")
            _original_subprocess_run(["git", "add", "."], cwd=repo, capture_output=True)
            _original_subprocess_run(
                ["git", "commit", "-m", "feat: builder committed early"],
                cwd=repo,
                capture_output=True,
            )

        # Builder commits directly; no commit delegation needed
        responses = (
            ResponseBuilder()
            .on_builder(change_fn=make_change_and_commit, output="Implemented feature.")
            .on_reviewer(approve=True)
            .build()
        )

        orch = Orchestrator(max_tasks=1)
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            assert exit_code == 0
            captured = capsys.readouterr()
            # Should detect the early commit
            assert "Builder committed changes directly" in captured.out
            # Should skip commit delegation
            assert "Skipping commit delegation" in captured.out
            assert orch.cycle == 1
        finally:
            orch.cleanup()

    def test_builder_early_commit_auto_commits_tasklist(self, temp_repo, capsys):
        """
        Scenario: Builder commits code but leaves the tasklist checkbox unstaged.
        Expected: Orchestrator detects the early commit, auto-commits the
        tasklist tick so the task won't be re-selected on the next run.

        Uses docs/tasklist.md (git-tracked) to exercise the fallback path.
        """

        def commit_code_but_leave_tasklist(repo):
            """Builder commits code but modifies tasklist without staging it."""
            (repo / "feature.py").write_text("def new_feature():\n    pass\n")
            _original_subprocess_run(["git", "add", "feature.py"], cwd=repo, capture_output=True)
            _original_subprocess_run(
                ["git", "commit", "-m", "feat: builder committed early"],
                cwd=repo,
                capture_output=True,
            )
            # Modify tasklist (check off the task) but don't stage/commit it
            tasklist = repo / "docs" / "tasklist.md"
            tasklist.write_text(
                "# Tasklist\n\n- [x] Task 1: Do something\n- [ ] Task 2: Do another thing\n"
            )

        responses = (
            ResponseBuilder()
            .on_builder(change_fn=commit_code_but_leave_tasklist, output="Implemented feature.")
            .on_reviewer(approve=True)
            .build()
        )

        orch = Orchestrator(max_tasks=1, tasklist="docs/tasklist.md")
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            assert exit_code == 0
            captured = capsys.readouterr()
            assert "Builder committed changes directly" in captured.out
            assert "Skipping commit delegation" in captured.out
            # Should auto-commit the tasklist tick
            assert "Auto-committed tasklist tick" in captured.out

            # Verify no uncommitted changes remain
            status = _original_subprocess_run(
                ["git", "status", "--porcelain"],
                cwd=temp_repo,
                capture_output=True,
                text=True,
            )
            assert status.stdout.strip() == ""

            # Verify the tasklist was actually committed
            log = _original_subprocess_run(
                ["git", "log", "--oneline", "-3"],
                cwd=temp_repo,
                capture_output=True,
                text=True,
            )
            assert "Mark task complete" in log.stdout
        finally:
            orch.cleanup()

    def test_builder_early_commit_then_review_fix_uncommitted(self, temp_repo, capsys):
        """
        Scenario: Builder commits in cycle 1, reviewer rejects, builder fixes
        in cycle 2 WITHOUT committing. The orchestrator must detect the dirty
        worktree and delegate a commit for the remaining edits.

        Regression test for: builder_on_success skipping delegate_commit when
        builder_committed was True, leaving uncommitted changes.
        """
        cycle_count = [0]

        def make_change_and_commit(repo):
            """Cycle 1: builder creates file AND commits it."""
            (repo / "feature.py").write_text("def new_feature():\n    return 1\n")
            _original_subprocess_run(["git", "add", "."], cwd=repo, capture_output=True)
            _original_subprocess_run(
                ["git", "commit", "-m", "feat: builder committed early"],
                cwd=repo,
                capture_output=True,
            )
            cycle_count[0] += 1

        def make_fix_without_commit(repo):
            """Cycle 2: builder edits file but does NOT commit."""
            (repo / "feature.py").write_text("def new_feature():\n    return 2  # fixed\n")
            _original_subprocess_run(["git", "add", "."], cwd=repo, capture_output=True)
            cycle_count[0] += 1

        build_count = [0]

        def responses(prompt, counts):
            if is_builder_prompt(prompt) or "address this review feedback" in prompt.lower():
                build_count[0] += 1
                if build_count[0] == 1:
                    return ("Implemented feature.", make_change_and_commit)
                else:
                    return ("Fixed review feedback.", make_fix_without_commit)
            elif is_reviewer_prompt(prompt):
                if build_count[0] <= 1:
                    return (
                        '{"status": "REQUEST_CHANGES", "review": "Return value should be 2", '
                        '"summary": "Needs fix", "findings": ["Wrong return value"], '
                        '"findings_by_severity": {"critical": [], "high": ["Wrong return value"], '
                        '"medium": [], "low": [], "nit": []}}',
                        None,
                    )
                return (
                    '{"status": "APPROVED", "review": "Looks good", "summary": "LGTM", '
                    '"findings": [], "findings_by_severity": {"critical": [], "high": [], '
                    '"medium": [], "low": [], "nit": []}}',
                    None,
                )
            elif is_commit_prompt(prompt):
                return ("Committed changes.", lambda repo: do_commit(repo))
            elif is_sanity_check(prompt):
                return ('{"status": "OK"}', None)
            return ('{"status": "OK"}', None)

        orch = Orchestrator(max_tasks=1, max_cycles=3)
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            assert exit_code == 0
            captured = capsys.readouterr()
            # Cycle 1 should detect early commit
            assert "Builder committed changes directly" in captured.out
            # Cycle 2: builder didn't commit, so normal delegate_commit runs
            assert "Delegating commit to builder" in captured.out

            # Verify no uncommitted changes remain
            status = _original_subprocess_run(
                ["git", "status", "--porcelain"],
                cwd=temp_repo,
                capture_output=True,
                text=True,
            )
            assert status.stdout.strip() == "", (
                "Working directory should be clean — all changes committed"
            )
        finally:
            orch.cleanup()

    def test_builder_early_commit_updates_loc_baseline(self, temp_repo, capsys):
        """
        Scenario: Builder commits directly (early commit) in a multi-task run.
        Expected: loc_baseline_ref is updated after the early-commit path so the
        next task's LoC measurement starts from the new HEAD, not the old baseline.

        Regression test for: builder_on_success skipping _update_loc_baseline()
        when builder_committed was True, causing stale baselines in multi-task runs.
        """

        def make_change_and_commit(repo):
            (repo / "feature.py").write_text("def new_feature():\n    pass\n")
            _original_subprocess_run(["git", "add", "."], cwd=repo, capture_output=True)
            _original_subprocess_run(
                ["git", "commit", "-m", "feat: builder committed early"],
                cwd=repo,
                capture_output=True,
            )

        responses = (
            ResponseBuilder()
            .on_builder(change_fn=make_change_and_commit, output="Implemented feature.")
            .on_reviewer(approve=True)
            .build()
        )

        orch = Orchestrator(max_tasks=1)
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            assert exit_code == 0
            # After the early-commit path, loc_baseline_ref should point to HEAD
            current_head = _original_subprocess_run(
                ["git", "rev-parse", "HEAD"],
                cwd=temp_repo,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert orch.loc_baseline_ref == current_head, (
                f"loc_baseline_ref should be updated to HEAD ({current_head}) "
                f"but was {orch.loc_baseline_ref}"
            )
        finally:
            orch.cleanup()

    def test_builder_early_commit_tasklist_autocommit_git_failure(self, temp_repo, capsys):
        """
        Scenario: Builder commits code early but the tasklist auto-commit fails.
        Expected: A warning is printed and the failure is not silently swallowed.

        Regression test for: _auto_commit_tasklist_if_needed ignoring git failures.
        """

        def commit_code_but_leave_tasklist(repo):
            (repo / "feature.py").write_text("def new_feature():\n    pass\n")
            _original_subprocess_run(["git", "add", "feature.py"], cwd=repo, capture_output=True)
            _original_subprocess_run(
                ["git", "commit", "-m", "feat: builder committed early"],
                cwd=repo,
                capture_output=True,
            )
            # Modify tasklist but don't stage it
            tasklist = repo / "docs" / "tasklist.md"
            tasklist.write_text(
                "# Tasklist\n\n- [x] Task 1: Do something\n- [ ] Task 2: Do another thing\n"
            )

        responses = (
            ResponseBuilder()
            .on_builder(change_fn=commit_code_but_leave_tasklist, output="Implemented feature.")
            .on_reviewer(approve=True)
            .build()
        )

        orch = Orchestrator(max_tasks=1, tasklist="docs/tasklist.md")
        try:
            # Patch subprocess.run to fail on git commit for tasklist
            real_mock = make_mock_runner(temp_repo, responses)

            def mock_with_commit_failure(cmd, **kwargs):
                # Intercept the tasklist auto-commit
                if (
                    cmd[0] == "git"
                    and cmd[1] == "commit"
                    and len(cmd) > 3
                    and "Mark task complete" in cmd[3]
                ):
                    return MagicMock(returncode=1, stdout=b"", stderr=b"commit failed")
                return real_mock(cmd, **kwargs)

            with patch("subprocess.run", side_effect=mock_with_commit_failure):
                exit_code = orch.run()

            # Task must fail — tasklist not marked complete means task can be
            # re-selected on the next run, breaking completion semantics.
            assert exit_code == 1
            captured = capsys.readouterr()
            # Should report the failed tasklist commit
            assert "ERROR: git commit for tasklist failed" in captured.out
        finally:
            orch.cleanup()

    def test_sensitive_file_no_longer_halts_flow(self, temp_repo, capsys):
        """
        Scenario: Builder modifies a sensitive file.
        Expected: Exit 0 and no sensitive file halt when policy disables the check.
        """
        responses = (
            ResponseBuilder()
            .on_builder(make_sensitive_file_change(), output="Updated configuration.")
            .build()
        )

        orch = Orchestrator()
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            assert exit_code == 0
            captured = capsys.readouterr()
            assert "Sensitive files" not in captured.out
        finally:
            orch.cleanup()

    def test_reviewer_inaccuracy_does_not_halt(self, temp_repo, capsys):
        """
        Regression test for reviewer factual inaccuracy bug.

        Scenario: Reviewer claims a 104-char line is 100 chars (minor inaccuracy).
        The sanity check should NOT halt the run for minor factual inaccuracies
        in reviewer feedback, as reviewers are probabilistic and the underlying
        code change may still be correct.

        Bug report: Reviewer said "this line is 100 chars so it will pass Ruff"
        when the line was actually 104 chars. Sanity check halted with
        "Review Feedback Contains Inaccuracy" even though the fix was trivial.

        Expected: Continue with the review (approve or request changes as normal),
        do NOT create STOP.md for minor reviewer inaccuracies about line lengths,
        character counts, or similar trivial factual claims.

        This test verifies that the sanity_check_review.md prompt explicitly
        instructs the sanity checker to ignore minor factual inaccuracies.
        """
        # Create a file with a long line (104 chars)
        long_line = 'signal = TaskSignal.system_cancel(org_id="oo", user_id="u", task_id="t", reason="test", source="system")'
        assert len(long_line) == 104, f"Test setup: line should be 104 chars, got {len(long_line)}"

        def make_long_line_change(repo):
            (repo / "test_store.py").write_text(f"# Test file\n{long_line}\n")
            _original_subprocess_run(["git", "add", "."], cwd=repo, capture_output=True)

        # Reviewer makes a factual inaccuracy: claims line is 100 chars when it's 104
        reviewer_response_with_inaccuracy = """
        {
            "status": "APPROVED",
            "review": "Looks good overall. Minor note: line length may be close to limit.",
            "summary": "The changes look good. The long line on line 2 is 100 characters which is within the Ruff limit.",
            "findings": [],
            "findings_by_severity": {"critical": [], "high": [], "medium": [], "low": [], "nit": []}
        }
        """

        responses = (
            ResponseBuilder()
            .on_builder(make_long_line_change, output="Added test file.")
            .on_reviewer(approve=True, response=reviewer_response_with_inaccuracy)
            .on_commit(success=True)
            .build()
        )

        orch = Orchestrator(max_tasks=1)
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            # Should succeed - reviewer inaccuracy about char count is not a hard failure
            assert exit_code == 0, (
                "Reviewer inaccuracy about line length should not halt the run. "
                "The sanity check should only halt for truly problematic reviews, "
                "not minor factual errors about character counts."
            )

            # STOP.md should NOT be created for minor inaccuracies
            stop_file = orch.work_dir / "STOP.md"
            assert not stop_file.exists(), (
                "STOP.md should not be created for reviewer inaccuracies about "
                "line lengths or character counts"
            )
        finally:
            orch.cleanup()

    def test_sanity_check_prompt_ignores_minor_inaccuracies(self, temp_repo):
        """
        Verify the sanity_check_review.md prompt explicitly mentions that
        minor factual inaccuracies (line counts, character counts, etc.)
        should NOT trigger a halt.

        This is a "prompt content" test that ensures the fix for the
        reviewer inaccuracy bug remains in place.
        """
        from millstone.runtime.orchestrator import Orchestrator

        orch = Orchestrator(task="test")
        try:
            prompt = orch.load_prompt("sanity_check_review.md")

            # The prompt should explicitly mention ignoring minor inaccuracies
            assert any(
                phrase in prompt.lower()
                for phrase in [
                    "minor",
                    "inaccurac",
                    "character count",
                    "line length",
                    "numeric",
                ]
            ), (
                "sanity_check_review.md should explicitly mention that minor "
                "factual inaccuracies (like character counts) should not halt the run"
            )
        finally:
            orch.cleanup()


class TestCommitBehavior:
    """Tests for commit delegation behavior."""

    def test_auto_commits_tasklist_when_builder_forgets(self, temp_repo, capsys):
        """
        Regression test for tasklist commit bug.

        Scenario: Builder commits code changes but forgets to stage the
        tasklist tick (docs/tasklist.md). Previously this would halt
        with "COMMIT FAILED".

        Expected: Orchestrator auto-commits the tasklist tick and continues.
        """

        def make_change_and_tick_tasklist(repo):
            """Make a code change AND tick the tasklist (but don't stage tasklist)."""
            # Create code change
            (repo / "feature.py").write_text("def new_feature():\n    pass\n")
            # Tick the tasklist (mark task complete)
            tasklist = repo / "docs" / "tasklist.md"
            content = tasklist.read_text()
            content = content.replace("- [ ]", "- [x]", 1)
            tasklist.write_text(content)
            # Only stage the code, not the tasklist
            _original_subprocess_run(["git", "add", "feature.py"], cwd=repo, capture_output=True)

        def commit_code_only(repo):
            """Commit only the staged code, leaving tasklist unstaged."""
            _original_subprocess_run(
                ["git", "commit", "-m", "Add feature"], cwd=repo, capture_output=True
            )

        responses = (
            ResponseBuilder()
            .on_builder(make_change_and_tick_tasklist, output="Done.")
            .on_reviewer(approve=True)
            .on_commit(success=True)
            .build()
        )

        # Override the commit side effect to only commit code
        original_responses = responses

        def custom_responses(prompt, counts):
            if is_commit_prompt(prompt):
                return ("Committed.", commit_code_only)
            return original_responses(prompt, counts)

        orch = Orchestrator(max_tasks=1, tasklist="docs/tasklist.md")
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, custom_responses)):
                exit_code = orch.run()

            # Should succeed - orchestrator should auto-commit the tasklist
            assert exit_code == 0, (
                "Orchestrator should auto-commit tasklist.md when builder "
                "forgets to stage it, not halt with COMMIT FAILED"
            )

            # Verify the tasklist was committed (working directory should be clean)
            status = _original_subprocess_run(
                ["git", "status", "--porcelain"], cwd=temp_repo, capture_output=True, text=True
            )
            assert status.stdout.strip() == "", (
                "Working directory should be clean after auto-commit"
            )
        finally:
            orch.cleanup()


class TestSessionResumption:
    """Tests for session resumption behavior."""

    def test_extracts_session_id(self, temp_repo):
        """Session ID is extracted from builder output."""
        responses = standard_approval_flow(
            builder_output='session_id: "abc-123-def-456"\nTask done.'
        )

        orch = Orchestrator()
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                orch.run()

            assert orch.session_id == "abc-123-def-456"
        finally:
            orch.cleanup()

    def test_resumes_session_on_fix(self, temp_repo):
        """Fix phase resumes the builder session."""
        resume_sessions = []
        review_count = [0]

        def make_change(repo):
            (repo / "feature.py").write_text("pass")
            _original_subprocess_run(["git", "add", "."], cwd=repo, capture_output=True)

        original_run = _original_subprocess_run

        def mock_run(cmd, **kwargs):
            if cmd[0] == "claude":
                # Track --resume usage
                if "--resume" in cmd:
                    idx = cmd.index("--resume")
                    resume_sessions.append(cmd[idx + 1])

                prompt = cmd[2] if len(cmd) > 2 else ""

                # Check sanity check first (prompt contains "address" which would trigger builder)
                if is_sanity_check(prompt):
                    return MagicMock(stdout='{"status": "OK"}', stderr="", returncode=0)
                # Check reviewer NEXT
                elif is_reviewer_prompt(prompt):
                    review_count[0] += 1
                    if review_count[0] == 1:
                        return MagicMock(
                            stdout='{"status": "REQUEST_CHANGES", "review": "Needs fixes", "summary": "Blocking issues", "findings": ["Needs fixes"], "findings_by_severity": {"critical": [], "high": ["Needs fixes"], "medium": [], "low": [], "nit": []}}',
                            stderr="",
                            returncode=0,
                        )
                    else:
                        return MagicMock(
                            stdout='{"status": "APPROVED", "review": "Looks good", "summary": "No blockers", "findings": [], "findings_by_severity": {"critical": [], "high": [], "medium": [], "low": [], "nit": []}}',
                            stderr="",
                            returncode=0,
                        )
                elif is_builder_prompt(prompt) or "address this review feedback" in prompt.lower():
                    make_change(temp_repo)
                    return MagicMock(
                        stdout='session_id: "abc-def-123-456"\nDone.', stderr="", returncode=0
                    )
                else:
                    # Fallback - return proper JSON format
                    return MagicMock(stdout='{"status": "OK"}', stderr="", returncode=0)
            else:
                return original_run(cmd, **kwargs)

        orch = Orchestrator()
        try:
            with patch("subprocess.run", side_effect=mock_run):
                orch.run()

            assert "abc-def-123-456" in resume_sessions
        finally:
            orch.cleanup()


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_handles_empty_review_output(self, temp_repo):
        """Handles case where reviewer returns empty output."""
        # Empty review output is treated as not approved
        responses = (
            ResponseBuilder()
            .on_builder(make_file_change())
            .on_reviewer(approve=False, response="")  # Empty response
            .build()
        )

        orch = Orchestrator(max_cycles=2)
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            # Should eventually hit max cycles (empty = not approved)
            assert exit_code == 1
        finally:
            orch.cleanup()

    def test_handles_binary_files_in_diff(self, temp_repo):
        """Binary files don't crash LoC calculation."""
        responses = standard_approval_flow(
            change_fn=make_binary_file_change(), builder_output="Added image and code."
        )

        orch = Orchestrator(max_tasks=1)
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            assert exit_code == 0
        finally:
            orch.cleanup()

    def test_loc_threshold_at_boundary(self, temp_repo):
        """Changes exactly at threshold pass."""
        responses = standard_approval_flow(change_fn=make_exact_lines_change(lines=10))

        orch = Orchestrator(loc_threshold=10, max_tasks=1)
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            assert exit_code == 0
        finally:
            orch.cleanup()

    def test_loc_threshold_exceeded(self, temp_repo, capsys):
        """Changes over threshold fail."""
        # Use ResponseBuilder since this test doesn't reach approval/commit phase
        responses = ResponseBuilder().on_builder(make_large_file_change(lines=11)).build()

        orch = Orchestrator(loc_threshold=10)
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            assert exit_code == 1
            captured = capsys.readouterr()
            # Verify the actionable halt message format
            assert "Halted:" in captured.out
            assert "lines changed" in captured.out
            assert "Options:" in captured.out
            assert "git diff" in captured.out
        finally:
            orch.cleanup()


class TestEvalOnTask:
    """Tests for eval_on_task config option."""

    def test_eval_on_task_none_skips_eval(self, temp_repo):
        """When eval_on_task is 'none', no eval is run after task approval."""
        responses = standard_approval_flow()

        orch = Orchestrator(max_tasks=1, eval_on_task="none")
        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            # Should succeed without running any eval
            assert exit_code == 0
            # No baseline should be captured since eval is disabled
            assert orch.baseline_eval is None
        finally:
            orch.cleanup()

    def test_eval_on_task_config_sets_mode(self, temp_repo):
        """eval_on_task config option sets the correct mode."""
        orch = Orchestrator(task="test", eval_on_task="smoke")
        try:
            assert orch.eval_on_task == "smoke"
        finally:
            orch.cleanup()

        orch2 = Orchestrator(task="test", eval_on_task="full")
        try:
            assert orch2.eval_on_task == "full"
        finally:
            orch2.cleanup()

        orch3 = Orchestrator(task="test", eval_on_task="tests/custom/")
        try:
            assert orch3.eval_on_task == "tests/custom/"
        finally:
            orch3.cleanup()

    def test_eval_on_task_default_is_none(self, temp_repo):
        """Default value for eval_on_task is 'none'."""
        from millstone.runtime.orchestrator import DEFAULT_CONFIG

        assert DEFAULT_CONFIG.get("eval_on_task") == "none"

        # Also verify orchestrator default
        orch = Orchestrator(task="test")
        try:
            assert orch.eval_on_task == "none"
        finally:
            orch.cleanup()


class TestEvalGating:
    """Tests for eval result gating - preventing commit when evals fail."""

    def test_eval_gate_blocks_commit_on_new_failure(self, temp_repo):
        """When eval gate detects new test failures, commit is blocked."""
        orch = Orchestrator(max_tasks=1, eval_on_task="smoke")
        try:
            # Set up baseline with no failures
            orch.baseline_eval = {"failed_tests": [], "_passed": True}

            # Mock run_eval to return a new failure
            with patch.object(orch, "run_eval") as mock_eval:
                mock_eval.return_value = {
                    "failed_tests": ["test_foo.py::test_bar"],
                    "_passed": False,
                    "tests": {"total": 10, "passed": 9, "failed": 1},
                }

                gate_passed, eval_result = orch._run_eval_gate(task_text="Test task")
                assert gate_passed is False
                assert eval_result is not None
                assert "test_foo.py::test_bar" in eval_result["failed_tests"]
        finally:
            orch.cleanup()

    def test_eval_gate_allows_preexisting_failures(self, temp_repo):
        """Eval gate passes when failures existed in baseline (no NEW failures)."""
        orch = Orchestrator(max_tasks=1, eval_on_task="smoke")
        try:
            # Set up baseline with an existing failure
            orch.baseline_eval = {"failed_tests": ["test_foo.py::test_bar"], "_passed": False}

            # Mock run_eval to return the same failure
            with patch.object(orch, "run_eval") as mock_eval:
                mock_eval.return_value = {
                    "failed_tests": ["test_foo.py::test_bar"],
                    "_passed": False,
                    "tests": {"total": 10, "passed": 9, "failed": 1},
                }

                gate_passed, eval_result = orch._run_eval_gate(task_text="Test task")
                assert gate_passed is True  # No NEW failures
        finally:
            orch.cleanup()

    def test_eval_gate_passes_when_all_tests_pass(self, temp_repo):
        """Eval gate passes when all tests pass."""
        orch = Orchestrator(max_tasks=1, eval_on_task="smoke")
        try:
            # Set up baseline with no failures
            orch.baseline_eval = {"failed_tests": [], "_passed": True}

            # Mock run_eval to return all passing
            with patch.object(orch, "run_eval") as mock_eval:
                mock_eval.return_value = {
                    "failed_tests": [],
                    "_passed": True,
                    "tests": {"total": 10, "passed": 10, "failed": 0},
                }

                gate_passed, eval_result = orch._run_eval_gate(task_text="Test task")
                assert gate_passed is True
        finally:
            orch.cleanup()

    def test_eval_gate_blocks_on_score_regression(self, temp_repo):
        """Eval gate blocks commit when composite score regresses beyond threshold."""
        orch = Orchestrator(max_tasks=1, eval_on_task="smoke")
        try:
            # Set up baseline with good score
            orch.baseline_eval = {"failed_tests": [], "_passed": True, "composite_score": 0.95}

            # Mock run_eval to return regressed score (> 0.05 max_regression default)
            with patch.object(orch, "run_eval") as mock_eval:
                mock_eval.return_value = {
                    "failed_tests": [],
                    "_passed": True,
                    "tests": {"total": 10, "passed": 10, "failed": 0},
                    "composite_score": 0.85,  # 0.10 regression > 0.05 threshold
                }

                gate_passed, eval_result = orch._run_eval_gate(task_text="Test task")
                assert gate_passed is False
        finally:
            orch.cleanup()

    def test_eval_gate_skipped_when_disabled(self, temp_repo):
        """Eval gate is skipped when eval_on_task is 'none'."""
        orch = Orchestrator(max_tasks=1, eval_on_task="none")
        try:
            # Should return True immediately without running eval
            gate_passed, eval_result = orch._run_eval_gate(task_text="Test task")
            assert gate_passed is True
            assert eval_result is None
        finally:
            orch.cleanup()

    def test_eval_gate_logs_failure_event(self, temp_repo):
        """Eval gate logs 'eval_gate_failed' event with details when blocking."""
        orch = Orchestrator(max_tasks=1, eval_on_task="smoke")
        try:
            orch.baseline_eval = {"failed_tests": [], "_passed": True}

            with patch.object(orch, "run_eval") as mock_eval:
                mock_eval.return_value = {
                    "failed_tests": ["test_new.py::test_fail"],
                    "_passed": False,
                    "tests": {"total": 10, "passed": 9, "failed": 1},
                }

                orch._run_eval_gate(task_text="Test task")

                # Check that eval_gate_failed was logged
                log_content = orch.log_file.read_text()
                assert "eval_gate_failed" in log_content
                assert "test_new.py::test_fail" in log_content
        finally:
            orch.cleanup()

    def test_eval_gate_logs_passed_event(self, temp_repo):
        """Eval gate logs 'eval_gate_passed' event when passing."""
        orch = Orchestrator(max_tasks=1, eval_on_task="smoke")
        try:
            orch.baseline_eval = {"failed_tests": [], "_passed": True}

            with patch.object(orch, "run_eval") as mock_eval:
                mock_eval.return_value = {
                    "failed_tests": [],
                    "_passed": True,
                    "tests": {"total": 10, "passed": 10, "failed": 0},
                }

                orch._run_eval_gate(task_text="Test task")

                # Check that eval_gate_passed was logged
                log_content = orch.log_file.read_text()
                assert "eval_gate_passed" in log_content
        finally:
            orch.cleanup()

    def test_eval_gate_runs_before_commit_in_task_flow(self, temp_repo):
        """Verify eval gate runs BEFORE commit in run_single_task when enabled."""
        responses = standard_approval_flow()

        orch = Orchestrator(max_tasks=1, eval_on_task="smoke")
        try:
            orch.baseline_eval = {"failed_tests": [], "_passed": True}

            # Track the order of operations
            operation_order = []

            original_delegate_commit = orch.delegate_commit

            def tracking_eval_gate(task_text=""):
                operation_order.append("eval_gate")
                return True, {"failed_tests": [], "_passed": True}

            def tracking_delegate_commit():
                operation_order.append("delegate_commit")
                return original_delegate_commit()

            with patch.object(orch, "_run_eval_gate", side_effect=tracking_eval_gate):
                with patch.object(orch, "delegate_commit", side_effect=tracking_delegate_commit):
                    with patch(
                        "subprocess.run", side_effect=make_mock_runner(temp_repo, responses)
                    ):
                        orch.run()

            # Verify eval_gate ran before delegate_commit
            if "eval_gate" in operation_order and "delegate_commit" in operation_order:
                assert operation_order.index("eval_gate") < operation_order.index("delegate_commit")
        finally:
            orch.cleanup()

    def test_eval_gate_failure_prevents_commit(self, temp_repo):
        """When eval gate fails, delegate_commit should not be called."""
        responses = standard_approval_flow()

        orch = Orchestrator(max_tasks=1, eval_on_task="smoke")
        try:
            orch.baseline_eval = {"failed_tests": [], "_passed": True}

            delegate_commit_called = {"value": False}

            def failing_eval_gate(task_text=""):
                return False, {"failed_tests": ["test_fail.py::test_new"], "_passed": False}

            def tracking_delegate_commit():
                delegate_commit_called["value"] = True
                return True

            with patch.object(orch, "_run_eval_gate", side_effect=failing_eval_gate):
                with patch.object(orch, "delegate_commit", side_effect=tracking_delegate_commit):
                    with patch(
                        "subprocess.run", side_effect=make_mock_runner(temp_repo, responses)
                    ):
                        exit_code = orch.run()

            # Commit should NOT have been called
            assert delegate_commit_called["value"] is False
            # Should return failure exit code
            assert exit_code != 0
        finally:
            orch.cleanup()

    def test_eval_gate_failure_saves_metrics(self, temp_repo):
        """When eval gate fails, task metrics are saved with 'eval_gate_failed' status."""
        responses = standard_approval_flow()

        orch = Orchestrator(max_tasks=1, eval_on_task="smoke")
        try:
            orch.baseline_eval = {"failed_tests": [], "_passed": True}

            saved_metrics = []

            def tracking_save_metrics(task_text, status, cycles, eval_before=None, eval_after=None):
                saved_metrics.append({"task": task_text, "status": status, "cycles": cycles})

            def failing_eval_gate(task_text=""):
                return False, {"failed_tests": ["test_fail.py::test_new"], "_passed": False}

            with patch.object(orch, "_run_eval_gate", side_effect=failing_eval_gate):
                with patch.object(orch, "save_task_metrics", side_effect=tracking_save_metrics):
                    with patch(
                        "subprocess.run", side_effect=make_mock_runner(temp_repo, responses)
                    ):
                        orch.run()

            # Verify metrics were saved with eval_gate_failed status
            assert len(saved_metrics) == 1
            assert saved_metrics[0]["status"] == "eval_gate_failed"
        finally:
            orch.cleanup()

    def test_skip_eval_bypasses_eval_gate(self, temp_repo):
        """When skip_eval=True, eval gate is bypassed even if eval_on_task is configured."""
        orch = Orchestrator(max_tasks=1, eval_on_task="smoke", skip_eval=True)
        try:
            # Should return True immediately without running eval
            gate_passed, eval_result = orch._run_eval_gate(task_text="Test task")
            assert gate_passed is True
            assert eval_result is None
        finally:
            orch.cleanup()

    def test_skip_eval_skips_baseline_capture(self, temp_repo):
        """When skip_eval=True with eval_on_task, baseline eval is not captured."""
        orch = Orchestrator(max_tasks=1, eval_on_task="smoke", skip_eval=True)
        try:
            # Check that skip_eval is set correctly
            assert orch.skip_eval is True
            assert orch.eval_on_task == "smoke"

            # The effective eval_on_task should be disabled by skip_eval
            # This mimics the logic in run() for baseline capture
            eval_on_task_effective = orch.eval_on_task != "none" and not orch.skip_eval
            assert eval_on_task_effective is False
        finally:
            orch.cleanup()

    def test_skip_eval_does_not_affect_eval_on_commit(self, temp_repo):
        """skip_eval only affects eval_on_task gate, not eval_on_commit."""
        orch = Orchestrator(max_tasks=1, eval_on_commit=True, eval_on_task="smoke", skip_eval=True)
        try:
            # skip_eval should be True
            assert orch.skip_eval is True

            # eval_on_commit should still be enabled
            assert orch.eval_on_commit is True

            # The effective eval_enabled for baseline should still be True (due to eval_on_commit)
            eval_on_task_effective = orch.eval_on_task != "none" and not orch.skip_eval
            eval_enabled = orch.eval_on_commit or eval_on_task_effective
            assert eval_enabled is True
        finally:
            orch.cleanup()

    def test_skip_eval_default_is_false(self, temp_repo):
        """Default value for skip_eval is False."""
        orch = Orchestrator(max_tasks=1)
        try:
            assert orch.skip_eval is False
        finally:
            orch.cleanup()
