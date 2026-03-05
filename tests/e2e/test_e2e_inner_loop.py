"""E2E inner-loop tests using the stub_cli harness."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from millstone.runtime.orchestrator import Orchestrator
from tests.e2e.conftest import StubCli

_original_subprocess_run = subprocess.run

_APPROVED_JSON = (
    '{"status": "APPROVED", "review": "Looks good", "summary": "Looks good!",'
    ' "findings": [], "findings_by_severity":'
    ' {"critical": [], "high": [], "medium": [], "low": [], "nit": []}}'
)
_SANITY_OK_JSON = '{"status": "OK", "reason": ""}'

# Lines that are part of the task body (user-supplied content) must be excluded
# from the unresolved-token scan to avoid false positives.
_TASK_LINE_RE = re.compile(r"^- \[")
_TEMPLATE_TOKEN_RE = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")


def _unresolved_tokens(prompt: str) -> list[str]:
    """Return any {{UPPER_SNAKE}} tokens found outside task-list lines."""
    found = []
    for line in prompt.splitlines():
        if _TASK_LINE_RE.match(line):
            continue
        found.extend(_TEMPLATE_TOKEN_RE.findall(line))
    return found


def _make_file_change(filename: str = "feature.py", content: str = "def f(): pass\n"):
    """Return a side_effect callable that creates a file and stages it."""

    def _effect(repo: Path) -> None:
        (repo / filename).write_text(content)
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=False)

    return _effect


def _do_commit(repo: Path) -> None:
    """Perform an actual git commit (used as builder/commit side_effect)."""
    subprocess.run(
        ["git", "commit", "-m", "stub-cli e2e test commit"],
        cwd=repo,
        capture_output=True,
        check=False,
    )


class TestWorkingDirectorySubstitution:
    """WORKING_DIRECTORY is resolved and no bare template tokens remain."""

    def test_working_directory_substituted_and_no_tokens(
        self, stub_cli: StubCli, temp_repo: Path
    ) -> None:
        """
        Run a single-task happy path via stub_cli.

        Assertions on stub_cli.calls[0].prompt (the author/builder call):
          (a) str(temp_repo) appears — {{WORKING_DIRECTORY}} was substituted.
          (b) No {{UPPER_SNAKE_CASE}} tokens remain outside task-list lines.
        """
        stub_cli.add(
            role="author",
            output="Implemented feature.",
            side_effect=_make_file_change(),
        )
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(
            role="builder",
            output="Committed.",
            side_effect=_do_commit,
        )

        orch = Orchestrator(max_tasks=1)
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        # There must be at least one 'author' call recorded.
        author_calls = [c for c in stub_cli.calls if c.role == "author"]
        assert author_calls, "No author-role call was recorded"

        builder_prompt = author_calls[0].prompt

        # (a) WORKING_DIRECTORY substituted
        assert str(temp_repo) in builder_prompt, (
            f"Expected {str(temp_repo)!r} in builder prompt but it was absent.\n"
            f"Prompt snippet: {builder_prompt[:500]!r}"
        )

        # (b) No unresolved template tokens
        leftovers = _unresolved_tokens(builder_prompt)
        assert not leftovers, (
            f"Unresolved template tokens found in builder prompt: {leftovers}\n"
            f"Prompt snippet: {builder_prompt[:500]!r}"
        )


_REQUEST_CHANGES_JSON = (
    '{"status": "REQUEST_CHANGES", "review": "Needs type annotations", "summary": "Blocking issues",'
    ' "findings": ["missing type annotations"], "findings_by_severity":'
    ' {"critical": [], "high": ["missing type annotations"], "medium": [], "low": [], "nit": []}}'
)


class TestReviewerFeedbackForwarding:
    """Reviewer feedback is forwarded verbatim to the second builder call."""

    def test_reviewer_feedback_in_second_builder_prompt(
        self, stub_cli: StubCli, temp_repo: Path
    ) -> None:
        """
        Run a two-cycle flow: reviewer returns REQUEST_CHANGES (with finding
        'missing type annotations') on cycle 1, APPROVED on cycle 2.

        Assert that the second author-role call's prompt contains the exact
        feedback text from the reviewer.
        """
        # Cycle 1: builder implements, sanity passes, reviewer requests changes
        stub_cli.add(
            role="author",
            output="Implemented feature.",
            side_effect=_make_file_change(),
        )
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_REQUEST_CHANGES_JSON)

        # Cycle 2: builder applies fixes, sanity passes, reviewer approves
        stub_cli.add(
            role="author",
            output="Added type annotations.",
            side_effect=_make_file_change("feature.py", "def f() -> None: pass\n"),
        )
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)

        # Commit
        stub_cli.add(
            role="builder",
            output="Committed.",
            side_effect=_do_commit,
        )

        orch = Orchestrator(max_tasks=1)
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        # Filter to author-role calls; the second one is the fix-cycle prompt
        author_calls = [c for c in stub_cli.calls if c.role == "author"]
        assert len(author_calls) >= 2, (
            f"Expected at least 2 author-role calls, got {len(author_calls)}"
        )

        second_author_prompt = author_calls[1].prompt
        assert "missing type annotations" in second_author_prompt, (
            "Expected reviewer finding 'missing type annotations' to appear in the "
            f"second builder prompt, but it was absent.\n"
            f"Prompt snippet: {second_author_prompt[:500]!r}"
        )


class TestPerRoleCliDispatch:
    """Correct binary is invoked per role when cli_builder != cli_reviewer."""

    def test_per_role_cli_dispatch(self, temp_repo: Path) -> None:
        """
        Configure cli_builder="codex", cli_reviewer="claude".

        The subprocess stubs are role-specific by design:
          - "codex" can only do builder work (create/stage files, commit)
          - "claude" can only do reviewer work (return valid review/sanity JSON)

        This means only the correct role mapping can produce a successful run.
        If routing were swapped, the reviewer would fail to create files and the
        builder would return unparseable review output — causing the run to fail.

        Assert only externally meaningful outcomes: exit 0, commit created, and
        the expected file is present with correct content.
        """

        def mock_run(cmd, **kwargs):
            binary = cmd[0] if cmd else ""

            if binary == "claude":
                # claude only knows how to do reviewer/sanity work
                return MagicMock(stdout=_APPROVED_JSON, stderr="", returncode=0)

            elif binary == "codex":
                # codex only knows how to do builder work: create files or commit
                prompt = kwargs.get("input") or (cmd[2] if len(cmd) > 2 else "")
                cwd = kwargs.get("cwd")
                if "commit" in prompt.lower():
                    if cwd:
                        _original_subprocess_run(
                            ["git", "add", "-A"], cwd=cwd, capture_output=True
                        )
                        _original_subprocess_run(
                            ["git", "commit", "-m", "per-role dispatch test"],
                            cwd=cwd,
                            capture_output=True,
                        )
                    return MagicMock(stdout="committed", stderr="", returncode=0)
                else:
                    if cwd:
                        (Path(cwd) / "feature.py").write_text("def f(): pass\n")
                        _original_subprocess_run(
                            ["git", "add", "."], cwd=cwd, capture_output=True
                        )
                    return MagicMock(
                        stdout="Implemented feature.", stderr="", returncode=0
                    )

            else:
                return _original_subprocess_run(cmd, **kwargs)

        orch = Orchestrator(
            cli_builder="codex",
            cli_reviewer="claude",
            max_tasks=1,
            repo_dir=temp_repo,
        )
        try:
            with patch("subprocess.run", side_effect=mock_run):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        # Behavioral outcomes: file created with expected content
        feature_file = temp_repo / "feature.py"
        assert feature_file.exists(), "Expected feature.py to be created by builder"
        assert "def f():" in feature_file.read_text(), (
            "Expected feature.py to contain 'def f():' — builder did not produce expected output"
        )

        # Commit created by builder
        log = _original_subprocess_run(
            ["git", "log", "--oneline"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )
        assert "per-role dispatch test" in log.stdout, (
            "Expected a commit from the builder role, but it was not found in git log"
        )


class TestDryRunNoUnresolvedTokens:
    """--dry-run output must not contain any unresolved {{...}} template tokens."""

    def test_dry_run_no_unresolved_tokens(self, temp_repo: Path, capsys) -> None:
        """
        Run Orchestrator(dry_run=True).run_dry_run() and capture stdout.

        Assert that no {{UPPER_SNAKE_CASE}} tokens remain in the output.
        Dry-run output contains no injected user task text, so a full scan
        is safe (no exclusions needed).
        """
        orch = Orchestrator(dry_run=True)
        try:
            exit_code = orch.run_dry_run()
        finally:
            orch.cleanup()

        assert exit_code == 0, f"Expected exit 0 from dry-run, got {exit_code}"

        captured = capsys.readouterr()
        tokens = re.findall(r"\{\{[A-Z][A-Z0-9_]*\}\}", captured.out)
        assert not tokens, (
            f"Unresolved template tokens found in dry-run output: {tokens}\n"
            f"Output snippet: {captured.out[:500]!r}"
        )
