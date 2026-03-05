"""Real MCP smoke tests — skipped by default; opt-in via --run-real-mcp."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


def _gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a gh CLI command and return the result."""
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.mark.real_mcp
def test_real_mcp_github_issues_full_lifecycle(tmp_path: Path) -> None:
    """
    Full MCP lifecycle: create a GitHub Issue, run millstone with MCP tasklist
    provider, assert issue closed and git commit created.

    Requires:
    - GH_TOKEN env var (set by conftest skip guard)
    - MILLSTONE_TEST_REPO env var (e.g. "owner/millstone-test")
    - --run-real-mcp pytest flag
    - github MCP server configured for the claude agent
    """
    test_repo = os.environ.get("MILLSTONE_TEST_REPO", "")
    if not test_repo:
        pytest.skip("MILLSTONE_TEST_REPO env var not set (e.g. owner/millstone-test)")

    # ------------------------------------------------------------------
    # Setup: create a GitHub Issue labelled millstone-test via gh api
    # ------------------------------------------------------------------
    create_result = _gh(
        "api",
        f"repos/{test_repo}/issues",
        "--method",
        "POST",
        "--field",
        "title=millstone e2e: add hello() function",
        "--field",
        "body=Add a `hello()` function that prints 'hello' to `hello.py`.",
        "--field",
        "labels[]=millstone-test",
    )
    issue_number = str(json.loads(create_result.stdout)["number"])

    # Teardown is guaranteed from here on — try/finally covers all remaining setup
    try:
        # ------------------------------------------------------------------
        # Set up a local git repo for millstone to work in
        # ------------------------------------------------------------------
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        hello_py = repo_dir / "hello.py"
        hello_py.write_text("# hello module\n")
        subprocess.run(["git", "add", "hello.py"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial commit"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )

        # ------------------------------------------------------------------
        # Configure millstone to use MCP tasklist provider
        # ------------------------------------------------------------------
        millstone_dir = repo_dir / ".millstone"
        millstone_dir.mkdir()
        (millstone_dir / "config.toml").write_text(
            'tasklist_provider = "mcp"\n'
            "\n"
            "[tasklist_provider_options]\n"
            f'mcp_server = "github"\n'
            f'repo = "{test_repo}"\n'
            "\n"
            "[tasklist_filter]\n"
            'label = "millstone-test"\n'
        )

        # ------------------------------------------------------------------
        # Run millstone
        # ------------------------------------------------------------------
        result = subprocess.run(
            ["millstone", "--cli", "claude"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert result.returncode == 0, (
            f"millstone exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

        # (b) hello.py contains the requested implementation
        assert "def hello(" in hello_py.read_text(), (
            "hello.py does not contain 'def hello(' after millstone run"
        )

        # (b2) secondary: HEAD diff touches hello.py (content-based commit check)
        head_diff = subprocess.run(
            ["git", "diff", "HEAD~1", "HEAD", "--name-only"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        assert head_diff.returncode == 0, (
            "No new commit was created after millstone run "
            f"(git diff HEAD~1 HEAD failed):\n{head_diff.stderr}"
        )
        assert "hello.py" in head_diff.stdout, "HEAD commit does not include changes to hello.py"

        # (c) issue closed/done
        issue_view = _gh("issue", "view", issue_number, "--repo", test_repo, "--json", "state")
        issue_state = json.loads(issue_view.stdout).get("state", "")
        assert issue_state.upper() in ("CLOSED", "DONE"), (
            f"Issue #{issue_number} not closed after millstone run; state={issue_state}"
        )

    finally:
        # Teardown: close any remaining open millstone-test issues
        open_issues = _gh(
            "issue",
            "list",
            "--repo",
            test_repo,
            "--label",
            "millstone-test",
            "--state",
            "open",
            "--json",
            "number",
            check=False,
        )
        if open_issues.returncode == 0 and open_issues.stdout.strip() not in ("", "[]"):
            for issue in json.loads(open_issues.stdout):
                _gh(
                    "issue",
                    "close",
                    str(issue["number"]),
                    "--repo",
                    test_repo,
                    check=False,
                )


@pytest.mark.real_mcp
def test_real_mcp_label_filter_narrows_scope(tmp_path: Path) -> None:
    """
    Label filter narrows scope: create one labelled and one unlabelled issue.
    Run millstone with tasklist_filter.label=millstone-test and assert only
    the labelled issue is selected; the unlabelled issue remains open.

    Requires:
    - GH_TOKEN env var (set by conftest skip guard)
    - MILLSTONE_TEST_REPO env var (e.g. "owner/millstone-test")
    - --run-real-mcp pytest flag
    - github MCP server configured for the claude agent
    """
    test_repo = os.environ.get("MILLSTONE_TEST_REPO", "")
    if not test_repo:
        pytest.skip("MILLSTONE_TEST_REPO env var not set (e.g. owner/millstone-test)")

    # ------------------------------------------------------------------
    # Setup: create two issues — one labelled, one unlabelled
    # Both creations happen inside try so teardown always runs even if
    # the second creation fails after the first succeeds.
    # ------------------------------------------------------------------
    labelled_number: str | None = None
    unlabelled_number: str | None = None

    try:
        labelled_result = _gh(
            "api",
            f"repos/{test_repo}/issues",
            "--method",
            "POST",
            "--field",
            "title=millstone e2e: add greet() function [label-filter]",
            "--field",
            "body=Add a `greet()` function that prints 'hello' to `greet.py`.",
            "--field",
            "labels[]=millstone-test",
        )
        labelled_number = str(json.loads(labelled_result.stdout)["number"])

        unlabelled_result = _gh(
            "api",
            f"repos/{test_repo}/issues",
            "--method",
            "POST",
            "--field",
            "title=millstone e2e: unlabelled issue should be ignored [label-filter]",
            "--field",
            "body=This issue has no label and must not be selected by millstone.",
        )
        unlabelled_number = str(json.loads(unlabelled_result.stdout)["number"])
        # ------------------------------------------------------------------
        # Set up a local git repo for millstone to work in
        # ------------------------------------------------------------------
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        greet_py = repo_dir / "greet.py"
        greet_py.write_text("# greet module\n")
        subprocess.run(["git", "add", "greet.py"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial commit"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )

        # ------------------------------------------------------------------
        # Configure millstone to use MCP tasklist provider with label filter
        # ------------------------------------------------------------------
        millstone_dir = repo_dir / ".millstone"
        millstone_dir.mkdir()
        (millstone_dir / "config.toml").write_text(
            'tasklist_provider = "mcp"\n'
            "\n"
            "[tasklist_provider_options]\n"
            'mcp_server = "github"\n'
            f'repo = "{test_repo}"\n'
            "\n"
            "[tasklist_filter]\n"
            'label = "millstone-test"\n'
        )

        # ------------------------------------------------------------------
        # Run millstone
        # ------------------------------------------------------------------
        result = subprocess.run(
            ["millstone", "--cli", "claude"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert result.returncode == 0, (
            f"millstone exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

        # ------------------------------------------------------------------
        # Assert: labelled issue is closed (was selected and processed)
        # ------------------------------------------------------------------
        labelled_view = _gh(
            "issue", "view", labelled_number, "--repo", test_repo, "--json", "state"
        )
        labelled_state = json.loads(labelled_view.stdout).get("state", "")
        assert labelled_state.upper() in ("CLOSED", "DONE"), (
            f"Labelled issue #{labelled_number} should be closed after millstone run; "
            f"state={labelled_state}"
        )

        # ------------------------------------------------------------------
        # Assert: unlabelled issue remains open (was not selected by filter)
        # ------------------------------------------------------------------
        unlabelled_view = _gh(
            "issue", "view", unlabelled_number, "--repo", test_repo, "--json", "state"
        )
        unlabelled_state = json.loads(unlabelled_view.stdout).get("state", "")
        assert unlabelled_state.upper() == "OPEN", (
            f"Unlabelled issue #{unlabelled_number} should remain open (not selected); "
            f"state={unlabelled_state}"
        )

    finally:
        # Teardown: close whichever issues were successfully created
        for number in (labelled_number, unlabelled_number):
            if number is not None:
                _gh("issue", "close", number, "--repo", test_repo, check=False)
