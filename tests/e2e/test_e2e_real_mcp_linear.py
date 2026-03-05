"""Real Linear MCP smoke tests — skipped by default; opt-in via --run-real-mcp."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import requests

# ---------------------------------------------------------------------------
# Linear API helpers
# ---------------------------------------------------------------------------


def _get_linear_token() -> str | None:
    """Return Linear API token from LINEAR_API_KEY or codex OAuth credentials."""
    token = os.environ.get("LINEAR_API_KEY")
    if token:
        return token
    # Fall back to codex OAuth credentials
    creds_path = Path.home() / ".codex" / ".credentials.json"
    if creds_path.exists():
        try:
            data = json.loads(creds_path.read_text())
            entries = data if isinstance(data, list) else data.get("credentials", [])
            for entry in entries:
                if isinstance(entry, dict) and entry.get("server_name") == "linear":
                    return entry.get("access_token")
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def _linear_api(query: str, variables: dict | None = None) -> dict:
    """Execute a Linear GraphQL query/mutation; return the response data dict."""
    token = _get_linear_token()
    if not token:
        pytest.skip(
            "No Linear API token found (set LINEAR_API_KEY or configure codex OAuth for linear)"
        )
    response = requests.post(
        "https://api.linear.app/graphql",
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": token, "Content-Type": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if "errors" in result:
        raise RuntimeError(f"Linear API error: {result['errors']}")
    return result.get("data", {})


def _get_team_id(team_name_or_id: str) -> str:
    """Resolve a team name or ID string to a Linear team ID (UUID)."""
    data = _linear_api("query { teams { nodes { id name } } }")
    for team in data["teams"]["nodes"]:
        if team["id"] == team_name_or_id or team["name"].lower() == team_name_or_id.lower():
            return team["id"]
    raise ValueError(f"Linear team not found: {team_name_or_id!r}")


def _get_or_create_label(team_id: str, label_name: str) -> str:
    """Return the ID of a label in the given team, creating it if absent."""
    data = _linear_api(
        """
        query Labels($teamId: ID!) {
          issueLabels(filter: { team: { id: { eq: $teamId } } }) {
            nodes { id name }
          }
        }
        """,
        {"teamId": team_id},
    )
    for label in data["issueLabels"]["nodes"]:
        if label["name"].lower() == label_name.lower():
            return label["id"]
    create_data = _linear_api(
        """
        mutation IssueLabelCreate($input: IssueLabelCreateInput!) {
          issueLabelCreate(input: $input) {
            success
            issueLabel { id name }
          }
        }
        """,
        {"input": {"name": label_name, "teamId": team_id}},
    )
    return create_data["issueLabelCreate"]["issueLabel"]["id"]


def _create_issue(
    team_id: str,
    title: str,
    description: str,
    label_ids: list[str] | None = None,
) -> str:
    """Create a Linear issue and return its ID."""
    input_data: dict = {"teamId": team_id, "title": title, "description": description}
    if label_ids:
        input_data["labelIds"] = label_ids
    data = _linear_api(
        """
        mutation IssueCreate($input: IssueCreateInput!) {
          issueCreate(input: $input) {
            success
            issue { id identifier }
          }
        }
        """,
        {"input": input_data},
    )
    return data["issueCreate"]["issue"]["id"]


def _get_issue_state_type(issue_id: str) -> str:
    """Return the state type ('completed', 'cancelled', 'started', etc.) for an issue."""
    data = _linear_api(
        """
        query Issue($id: String!) {
          issue(id: $id) {
            state { type name }
          }
        }
        """,
        {"id": issue_id},
    )
    return data["issue"]["state"]["type"]


def _list_issues_with_label(team_id: str, label_name: str) -> list[dict]:
    """Return list of {id, state_type} dicts for all issues with the given label in the team."""
    data = _linear_api(
        """
        query Issues($teamId: ID!, $labelName: String!) {
          issues(filter: {
            team: { id: { eq: $teamId } }
            labels: { some: { name: { eqIgnoreCase: $labelName } } }
          }) {
            nodes { id state { type } }
          }
        }
        """,
        {"teamId": team_id, "labelName": label_name},
    )
    return [
        {"id": node["id"], "state_type": node["state"]["type"]}
        for node in data["issues"]["nodes"]
    ]


def _archive_issue(issue_id: str) -> None:
    """Archive (soft-delete) a Linear issue for teardown."""
    _linear_api(
        """
        mutation IssueArchive($id: String!) {
          issueArchive(id: $id) { success }
        }
        """,
        {"id": issue_id},
    )


def _teardown_label_issues(team_id: str, label_name: str) -> None:
    """Archive all non-completed, non-cancelled issues with the given label."""
    import contextlib

    for issue in _list_issues_with_label(team_id, label_name):
        if issue["state_type"] not in ("completed", "cancelled"):
            with contextlib.suppress(Exception):
                _archive_issue(issue["id"])


# ---------------------------------------------------------------------------
# Repo setup helpers
# ---------------------------------------------------------------------------


def _setup_repo(tmp_path: Path, filename: str, initial_content: str) -> Path:
    """Create a git repo with one initial file and return the repo directory."""
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
    target_file = repo_dir / filename
    target_file.write_text(initial_content)
    subprocess.run(["git", "add", filename], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    return repo_dir


def _configure_millstone(repo_dir: Path, team_id: str, label: str) -> None:
    """Write .millstone/config.toml configured for the Linear MCP provider."""
    millstone_dir = repo_dir / ".millstone"
    millstone_dir.mkdir()
    (millstone_dir / "config.toml").write_text(
        'tasklist_provider = "mcp"\n'
        "\n"
        "[tasklist_provider_options]\n"
        'mcp_server = "linear"\n'
        f'team = "{team_id}"\n'
        "\n"
        "[tasklist_filter]\n"
        f'label = "{label}"\n'
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.real_mcp
def test_real_linear_mcp_task_creates_commit(tmp_path: Path) -> None:
    """
    Full Linear MCP lifecycle: create a Linear issue, run millstone with MCP
    tasklist provider, assert issue completed and git commit created.

    Requires:
    - MILLSTONE_TEST_LINEAR_TEAM env var (Linear team name or ID)
    - LINEAR_API_KEY env var or codex OAuth credentials for linear
    - --run-real-mcp pytest flag
    - linear MCP server configured for the codex agent
    """
    team_name_or_id = os.environ.get("MILLSTONE_TEST_LINEAR_TEAM", "")
    if not team_name_or_id:
        pytest.skip("MILLSTONE_TEST_LINEAR_TEAM env var not set")

    team_id = _get_team_id(team_name_or_id)
    label_id = _get_or_create_label(team_id, "millstone-e2e")
    issue_id: str | None = None

    try:
        issue_id = _create_issue(
            team_id,
            title="millstone e2e: add hello() to hello.py",
            description="Add a hello() function that prints 'hello world' to hello.py.",
            label_ids=[label_id],
        )

        repo_dir = _setup_repo(tmp_path, "hello.py", "# hello module\n")
        _configure_millstone(repo_dir, team_id, "millstone-e2e")

        result = subprocess.run(
            ["millstone", "--cli", "codex", "--max-tasks", "1"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=600,
        )

        # (a) exit 0
        assert result.returncode == 0, (
            f"millstone exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

        # (b) hello.py contains the requested implementation
        hello_py = repo_dir / "hello.py"
        assert "def hello(" in hello_py.read_text(), (
            "hello.py does not contain 'def hello(' after millstone run"
        )

        # (d) secondary: HEAD diff touches hello.py (content-based commit check)
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
        assert "hello.py" in head_diff.stdout, (
            "HEAD commit does not include changes to hello.py"
        )

        # (c) issue state is completed
        state_type = _get_issue_state_type(issue_id)
        assert state_type in ("completed", "done"), (
            f"Linear issue not completed after millstone run; state_type={state_type}"
        )

    finally:
        _teardown_label_issues(team_id, "millstone-e2e")


@pytest.mark.real_mcp
def test_real_linear_label_filter_narrows_scope(tmp_path: Path) -> None:
    """
    Label filter narrows scope: create one labelled and one unlabelled issue.
    Run millstone with tasklist_filter.label=millstone-e2e and assert only
    the labelled issue is completed; the unlabelled issue remains open.

    Requires:
    - MILLSTONE_TEST_LINEAR_TEAM env var (Linear team name or ID)
    - LINEAR_API_KEY env var or codex OAuth credentials for linear
    - --run-real-mcp pytest flag
    - linear MCP server configured for the codex agent
    """
    team_name_or_id = os.environ.get("MILLSTONE_TEST_LINEAR_TEAM", "")
    if not team_name_or_id:
        pytest.skip("MILLSTONE_TEST_LINEAR_TEAM env var not set")

    team_id = _get_team_id(team_name_or_id)
    label_id = _get_or_create_label(team_id, "millstone-e2e")
    labelled_id: str | None = None
    unlabelled_id: str | None = None

    try:
        labelled_id = _create_issue(
            team_id,
            title="millstone e2e: add greet() function [label-filter]",
            description="Add a greet() function that prints 'hello' to greet.py.",
            label_ids=[label_id],
        )
        unlabelled_id = _create_issue(
            team_id,
            title="millstone e2e: unlabelled issue should be ignored [label-filter]",
            description="This issue has no label and must not be selected by millstone.",
        )

        repo_dir = _setup_repo(tmp_path, "greet.py", "# greet module\n")
        _configure_millstone(repo_dir, team_id, "millstone-e2e")

        result = subprocess.run(
            ["millstone", "--cli", "codex", "--max-tasks", "1"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=600,
        )

        assert result.returncode == 0, (
            f"millstone exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

        # Assert: labelled issue is completed (was selected and processed)
        labelled_state = _get_issue_state_type(labelled_id)
        assert labelled_state in ("completed", "done"), (
            f"Labelled issue should be completed after millstone run; "
            f"state_type={labelled_state}"
        )

        # Assert: unlabelled issue remains open (was not selected by filter)
        unlabelled_state = _get_issue_state_type(unlabelled_id)
        assert unlabelled_state not in ("completed", "cancelled", "done"), (
            f"Unlabelled issue should remain open (not selected by filter); "
            f"state_type={unlabelled_state}"
        )

    finally:
        # Archive all remaining open millstone-e2e issues
        _teardown_label_issues(team_id, "millstone-e2e")
        # Archive the unlabelled issue if it is still open
        if unlabelled_id is not None:
            try:
                if _get_issue_state_type(unlabelled_id) not in ("completed", "cancelled"):
                    _archive_issue(unlabelled_id)
            except Exception:
                pass
