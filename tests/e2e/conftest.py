"""E2E fixtures scoped to tests/e2e/. Do not import into the main test suite."""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from millstone.agent_providers.registry import get_provider

# ---------------------------------------------------------------------------
# Real-CLI / real-MCP marker skip helpers
# ---------------------------------------------------------------------------


def _real_cli_skip_reason(
    provider: str,
    *,
    flag_passed: bool,
) -> str | None:
    """Return a skip reason string, or None if the test should run."""
    if not flag_passed:
        return "--run-real-cli not passed"
    cli_map: dict[str, list[str]] = {
        "claude": ["claude"],
        "codex": ["codex"],
        "mixed": ["claude", "codex"],
    }
    for cli_name in cli_map.get(provider, [provider]):
        available, msg = get_provider(cli_name).check_available()
        if not available:
            return msg
    return None


def _real_mcp_skip_reason(
    *,
    flag_passed: bool,
) -> str | None:
    """Return a skip reason string, or None if the test should run."""
    if not flag_passed:
        return "--run-real-mcp not passed"
    return None


# ---------------------------------------------------------------------------
# pytest hooks
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    run_real_cli = config.getoption("--run-real-cli")
    run_real_mcp = config.getoption("--run-real-mcp")

    for item in items:
        cli_marker = item.get_closest_marker("real_cli")
        if cli_marker is not None:
            provider = cli_marker.kwargs.get("provider", "claude")
            reason = _real_cli_skip_reason(provider, flag_passed=run_real_cli)
            if reason:
                item.add_marker(pytest.mark.skip(reason=reason))

        mcp_marker = item.get_closest_marker("real_mcp")
        if mcp_marker is not None:
            reason = _real_mcp_skip_reason(flag_passed=run_real_mcp)
            if reason:
                item.add_marker(pytest.mark.skip(reason=reason))


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass
class CallRecord:
    """One recorded call to Orchestrator.run_agent."""

    role: str
    prompt: str
    output_schema: str | None = None
    resume: str | None = None


@dataclass
class _StubEntry:
    """One configured stub response."""

    role: str | None = None
    output_schema: str | None = None
    prompt_substring: str | None = None
    output: str = ""
    side_effect: Callable[[Path], None] | None = None
    consumed: bool = False


# ---------------------------------------------------------------------------
# StubCli harness
# ---------------------------------------------------------------------------


class StubCli:
    """
    Role-aware harness that patches ``Orchestrator.run_agent``.

    Routing priority (highest → lowest):
        1. role kwarg (exact match against entry.role)
        2. output_schema kwarg (exact match against entry.output_schema)
        3. prompt_substring (substring match against the prompt text)

    Usage::

        stub_cli.add(role="author",   output="<summary>done</summary>",
                     side_effect=lambda repo: (repo / "f.py").write_text("x"))
        stub_cli.add(role="reviewer", output='{"status":"APPROVED","findings":[]}')
        stub_cli.add(role="sanity",   output='{"status":"OK","reason":""}')
        stub_cli.add(role="builder",  output="committed")

        with stub_cli.patch(orch):          # patches orch.run_agent in-place
            exit_code = orch.run()

        stub_cli.assert_roles_consumed(["author", "reviewer", "sanity", "builder"])
        assert stub_cli.calls[0].role == "author"
        assert "sprint-9" in stub_cli.calls[0].prompt
    """

    def __init__(self) -> None:
        self._entries: list[_StubEntry] = []
        self.calls: list[CallRecord] = []
        self._repo_dir: Path | None = None

    # ------------------------------------------------------------------
    # Configuration API
    # ------------------------------------------------------------------

    def add(
        self,
        *,
        role: str | None = None,
        output: str = "",
        side_effect: Callable[[Path], None] | None = None,
        output_schema: str | None = None,
        prompt_substring: str | None = None,
    ) -> StubCli:
        """Add a stub response entry. Returns self for chaining."""
        self._entries.append(
            _StubEntry(
                role=role,
                output_schema=output_schema,
                prompt_substring=prompt_substring,
                output=output,
                side_effect=side_effect,
            )
        )
        return self

    # ------------------------------------------------------------------
    # Context-manager patch
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def patch(self, orch: Any):
        """Patch ``orch.run_agent`` with this stub for the duration of the block."""
        self._repo_dir = getattr(orch, "repo_dir", None)
        with patch.object(orch, "run_agent", self._dispatch):
            yield self

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------

    def assert_roles_consumed(self, expected_roles: list[str]) -> None:
        """Assert that run_agent was called with exactly these roles in order."""
        actual = [c.role for c in self.calls]
        assert actual == expected_roles, f"Expected role sequence {expected_roles}, got {actual}"

    # ------------------------------------------------------------------
    # Internal dispatch (replaces orch.run_agent)
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        prompt: str,
        *,
        role: str = "default",
        output_schema: str | None = None,
        resume: str | None = None,
        **_kwargs: Any,
    ) -> str:
        self.calls.append(
            CallRecord(
                role=role,
                prompt=prompt,
                output_schema=output_schema,
                resume=resume,
            )
        )
        entry = self._find_entry(role=role, output_schema=output_schema, prompt=prompt)
        if entry is None:
            return ""
        if entry.side_effect is not None and self._repo_dir is not None:
            entry.side_effect(self._repo_dir)
        return entry.output

    def _find_entry(
        self,
        role: str,
        output_schema: str | None,
        prompt: str,
    ) -> _StubEntry | None:
        # Priority 1: role-only match (entry has a role and no secondary criteria)
        for entry in self._entries:
            if (
                not entry.consumed
                and entry.role == role
                and entry.output_schema is None
                and entry.prompt_substring is None
            ):
                entry.consumed = True
                return entry

        # Priority 2: output_schema match (role must match if entry specifies one)
        if output_schema is not None:
            for entry in self._entries:
                if (
                    not entry.consumed
                    and (entry.role is None or entry.role == role)
                    and entry.output_schema == output_schema
                ):
                    entry.consumed = True
                    return entry

        # Priority 3: prompt substring match (role must match if entry specifies one)
        for entry in self._entries:
            if (
                not entry.consumed
                and (entry.role is None or entry.role == role)
                and entry.prompt_substring is not None
                and entry.prompt_substring in prompt
            ):
                entry.consumed = True
                return entry

        # Fallback: role match for entries that also carry secondary criteria
        for entry in self._entries:
            if not entry.consumed and entry.role == role:
                entry.consumed = True
                return entry

        return None


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_cli() -> StubCli:
    """Role-aware Orchestrator.run_agent stub. See StubCli docstring for usage."""
    return StubCli()


@pytest.fixture
def empty_repo(tmp_path) -> Path:
    """Git repo with an empty tasklist (no pending tasks).

    Suitable for full-cycle tests that need to start from analyze→design→plan→execute.
    Changes the working directory to the repo for the duration of the test.
    """
    import os

    repo_dir = tmp_path / "empty_repo"
    repo_dir.mkdir()

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

    (repo_dir / ".gitignore").write_text("# Test repo gitignore\n/.millstone/\n")
    (repo_dir / "README.md").write_text("# Empty Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_dir,
        capture_output=True,
    )

    millstone_dir = repo_dir / ".millstone"
    millstone_dir.mkdir(exist_ok=True)
    # Empty tasklist — no '- [ ]' entries so run_cycle proceeds to analyze.
    # .millstone/ is gitignored so the file is not committed; the orchestrator
    # reads it from disk directly.
    (millstone_dir / "tasklist.md").write_text("# Tasklist\n\n")

    original_cwd = os.getcwd()
    os.chdir(repo_dir)
    yield repo_dir
    os.chdir(original_cwd)
