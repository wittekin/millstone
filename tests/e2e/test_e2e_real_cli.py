"""Real CLI smoke tests — skipped by default; opt-in via --run-real-cli."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from millstone.runtime.orchestrator import Orchestrator

_original_subprocess_run = subprocess.run


@pytest.mark.real_cli(provider="claude")
def test_real_claude_task_creates_commit(temp_repo: Path) -> None:
    """Run millstone --task via the real claude binary; assert commit created."""
    # Create a minimal Python file for the agent to modify.
    greet_py = temp_repo / "greet.py"
    greet_py.write_text("# greet module\n")
    subprocess.run(["git", "add", "greet.py"], cwd=temp_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add greet.py stub"],
        cwd=temp_repo,
        check=True,
        capture_output=True,
    )

    initial_log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=temp_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().splitlines()

    timeout = 120
    start = time.monotonic()
    result = subprocess.run(
        ["millstone", "--task", "add a greet(name) function to greet.py", "--cli", "claude"],
        cwd=temp_repo,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    elapsed = time.monotonic() - start

    assert result.returncode == 0, (
        f"millstone exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "def greet(" in greet_py.read_text(), (
        "greet.py does not contain def greet( — requested function not implemented"
    )
    assert elapsed < timeout, f"wall-clock {elapsed:.1f}s exceeded {timeout}s limit"

    final_log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=temp_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().splitlines()
    assert len(final_log) > len(initial_log), (
        "No new git commit was created after millstone --task"
    )


@pytest.mark.real_cli(provider="mixed")
def test_real_mixed_task_creates_commit(temp_repo: Path) -> None:
    """Run orchestrator in-process with cli_builder=claude, cli_reviewer=codex.

    A subprocess spy is installed in the same process so we can observe which
    binaries are invoked by the orchestrator internally.  Both "claude" and
    "codex" must appear in the filtered set of agent calls.
    """
    greet_py = temp_repo / "greet.py"
    greet_py.write_text("# greet module\n")
    _original_subprocess_run(
        ["git", "add", "greet.py"], cwd=temp_repo, check=True, capture_output=True
    )
    _original_subprocess_run(
        ["git", "commit", "-m", "add greet.py stub"],
        cwd=temp_repo,
        check=True,
        capture_output=True,
    )

    initial_log = _original_subprocess_run(
        ["git", "log", "--oneline"],
        cwd=temp_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().splitlines()

    recorded: list[tuple] = []

    def _spy(cmd, **kwargs):
        recorded.append((cmd, kwargs))
        return _original_subprocess_run(cmd, **kwargs)

    orch = Orchestrator(
        cli_builder="claude",
        cli_reviewer="codex",
        task="add a greet(name) function to greet.py",
        repo_dir=temp_repo,
        max_cycles=6,
    )
    start = time.monotonic()
    try:
        with patch("subprocess.run", side_effect=_spy):
            exit_code = orch.run()
    finally:
        orch.cleanup()
    elapsed = time.monotonic() - start

    assert exit_code == 0, f"Orchestrator exited {exit_code}"
    assert elapsed < 300, f"wall-clock {elapsed:.1f}s exceeded 300s limit"
    assert "def greet(" in greet_py.read_text(), (
        "greet.py does not contain def greet( — requested function not implemented"
    )

    # Verify mixed CLI routing: both claude (builder) and codex (reviewer) were invoked.
    agent_cmds = [cmd for cmd, _ in recorded if isinstance(cmd, list) and cmd and cmd[0] in ("claude", "codex")]
    invoked_binaries = {cmd[0] for cmd in agent_cmds}
    assert "claude" in invoked_binaries, "claude binary was not invoked as builder"
    assert "codex" in invoked_binaries, "codex binary was not invoked as reviewer"

    final_log = _original_subprocess_run(
        ["git", "log", "--oneline"],
        cwd=temp_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().splitlines()
    assert len(final_log) > len(initial_log), (
        "No new git commit was created after orchestrator run"
    )

@pytest.mark.real_cli(provider="codex")
def test_real_codex_task_creates_commit(temp_repo: Path) -> None:
    """Run millstone --task via the real codex binary; assert commit created."""
    greet_py = temp_repo / "greet.py"
    greet_py.write_text("# greet module\n")
    subprocess.run(["git", "add", "greet.py"], cwd=temp_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add greet.py stub"],
        cwd=temp_repo,
        check=True,
        capture_output=True,
    )

    initial_log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=temp_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().splitlines()

    timeout = 300  # codex (gpt-5.3-codex with high reasoning effort) can exceed 120s
    start = time.monotonic()
    result = subprocess.run(
        ["millstone", "--task", "add a greet(name) function to greet.py", "--cli", "codex"],
        cwd=temp_repo,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    elapsed = time.monotonic() - start

    assert result.returncode == 0, (
        f"millstone exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "def greet(" in greet_py.read_text(), (
        "greet.py does not contain def greet( — requested function not implemented"
    )
    assert elapsed < timeout, f"wall-clock {elapsed:.1f}s exceeded {timeout}s limit"

    final_log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=temp_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().splitlines()
    assert len(final_log) > len(initial_log), (
        "No new git commit was created after millstone --task"
    )
