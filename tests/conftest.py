"""Shared fixtures for orchestrator tests."""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-real-cli",
        action="store_true",
        default=False,
        help="Opt-in: run tests marked @pytest.mark.real_cli (requires API keys)",
    )
    parser.addoption(
        "--run-real-mcp",
        action="store_true",
        default=False,
        help="Opt-in: run tests marked @pytest.mark.real_mcp (requires GH_TOKEN)",
    )


@pytest.fixture(autouse=True)
def disable_retries():
    """Disable retries on empty response for all tests to ensure deterministic call counts."""
    with patch.dict("millstone.config.DEFAULT_CONFIG", {"retry_on_empty_response": False}):
        yield


@pytest.fixture
def temp_repo(tmp_path):
    """Create a temporary git repository with basic structure."""
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo_dir,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_dir,
        capture_output=True,
    )

    # Create .gitignore with millstone already included (so orchestrator doesn't modify it)
    (repo_dir / ".gitignore").write_text("# Test repo gitignore\n/.millstone/\n")

    # Create initial commit
    (repo_dir / "README.md").write_text("# Test Repo")
    subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_dir,
        capture_output=True,
    )

    # Create docs/tasklist.md
    docs_dir = repo_dir / "docs"
    docs_dir.mkdir()
    (docs_dir / "tasklist.md").write_text(
        "# Tasklist\n\n- [ ] Task 1: Do something\n- [ ] Task 2: Do another thing\n"
    )

    # Create .millstone/ dir and tasklist at new default path
    millstone_dir = repo_dir / ".millstone"
    millstone_dir.mkdir(exist_ok=True)
    (millstone_dir / "tasklist.md").write_text(
        "# Tasklist\n\n- [ ] Task 1: Do something\n- [ ] Task 2: Do another thing\n"
    )

    # Commit everything so we start clean
    subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add tasklist"],
        cwd=repo_dir,
        capture_output=True,
    )

    original_cwd = os.getcwd()
    os.chdir(repo_dir)
    yield repo_dir
    os.chdir(original_cwd)


@pytest.fixture
def orchestrator_dir(tmp_path):
    """Create a mock orchestrator working directory."""
    work_dir = tmp_path / "orchestrator_work"
    work_dir.mkdir()
    return work_dir


@pytest.fixture
def mock_claude():
    """Mock the claude CLI subprocess calls."""
    with patch("subprocess.run") as mock_run:
        # Default: return empty successful response
        mock_run.return_value = MagicMock(
            stdout="Task completed successfully.",
            stderr="",
            returncode=0,
        )
        yield mock_run


@pytest.fixture
def script_dir():
    """Return the path to the millstone package directory."""
    return Path(__file__).parent.parent / "src" / "millstone"


@pytest.fixture
def prompts_dir(script_dir):
    """Return the path to the prompts directory."""
    return script_dir / "prompts"
