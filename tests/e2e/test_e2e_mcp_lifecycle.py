"""E2E MCP provider lifecycle tests using the stub_cli harness."""

from __future__ import annotations

import subprocess
from pathlib import Path

from millstone.runtime.orchestrator import Orchestrator
from tests.e2e.conftest import StubCli

_APPROVED_JSON = (
    '{"status": "APPROVED", "review": "Looks good", "summary": "Looks good!",'
    ' "findings": [], "findings_by_severity":'
    ' {"critical": [], "high": [], "medium": [], "low": [], "nit": []}}'
)
_SANITY_OK_JSON = '{"status": "OK", "reason": ""}'


def _do_commit(repo: Path) -> None:
    """Perform an actual git commit (used as builder/commit side_effect)."""
    subprocess.run(
        ["git", "commit", "-m", "stub-cli mcp e2e test commit"],
        cwd=repo,
        capture_output=True,
        check=False,
    )


def _make_file_change(filename: str = "feature.py", content: str = "def f(): pass\n"):
    """Return a side_effect callable that creates a file and stages it."""

    def _effect(repo: Path) -> None:
        (repo / filename).write_text(content)
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=False)

    return _effect


class TestMCPTasklistPromptContent:
    """MCP tasklist: rendered builder prompt contains correct READ and COMPLETE clauses."""

    def test_mcp_tasklist_prompt_read_and_complete_instructions(
        self, stub_cli: StubCli, temp_repo: Path
    ) -> None:
        """
        Wire MCPTasklistProvider with mcp_server="github" via config.toml.
        Run a single-task inner loop via stub_cli.

        Assertions on stub_cli.calls[0].prompt (author role):
          (a) "github" appears in the READ section (server name is the key output).
          (b) A non-empty completion instruction is present.
          (c) No {{TASKLIST_READ_INSTRUCTIONS}} or {{TASKLIST_COMPLETE_INSTRUCTIONS}}
              tokens remain unresolved.
          (d) exit 0.
        """
        # Configure MCP provider via config.toml
        config_toml = temp_repo / ".millstone" / "config.toml"
        config_toml.write_text(
            'tasklist_provider = "mcp"\n\n[tasklist_provider_options]\nmcp_server = "github"\n'
        )

        # Set up stubs for the full inner-loop pipeline
        stub_cli.add(
            role="author",
            output="Task implemented.",
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

        # (d) exit 0
        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        # Locate the first author-role call
        author_calls = [c for c in stub_cli.calls if c.role == "author"]
        assert author_calls, "No author-role call was recorded"
        prompt = author_calls[0].prompt

        # (a) The configured server identifier "github" appears in the rendered prompt.
        assert "github" in prompt, (
            f"Expected server name 'github' in rendered prompt.\nPrompt snippet: {prompt[:600]!r}"
        )

        # (b) Completion guidance is non-empty and semantically present.
        # Check for completion-related keywords without pinning to exact provider phrases.
        prompt_lower = prompt.lower()
        assert any(kw in prompt_lower for kw in ("complete", "close", "mark", "done", "finish")), (
            f"Expected completion guidance in rendered prompt (none of: complete/close/mark/done/finish).\n"
            f"Prompt snippet: {prompt[:600]!r}"
        )

        # (c) No unresolved placeholder tokens remain.
        assert "{{TASKLIST_READ_INSTRUCTIONS}}" not in prompt, (
            "{{TASKLIST_READ_INSTRUCTIONS}} was not resolved in the rendered prompt"
        )
        assert "{{TASKLIST_COMPLETE_INSTRUCTIONS}}" not in prompt, (
            "{{TASKLIST_COMPLETE_INSTRUCTIONS}} was not resolved in the rendered prompt"
        )


class TestMCPTasklistFilterLabel:
    """MCP tasklist_filter label → instruction clause in rendered prompt."""

    def test_label_filter_propagates_to_rendered_prompt(
        self, stub_cli: StubCli, temp_repo: Path
    ) -> None:
        """
        Config: tasklist_provider="mcp", tasklist_filter.label="sprint-9".
        Assert the rendered builder prompt captured via stub_cli contains "sprint-9".
        """
        config_toml = temp_repo / ".millstone" / "config.toml"
        config_toml.write_text(
            'tasklist_provider = "mcp"\n'
            "\n"
            "[tasklist_provider_options]\n"
            'mcp_server = "github"\n'
            "\n"
            "[tasklist_filter]\n"
            'label = "sprint-9"\n'
        )

        # Set up stubs for the full inner-loop pipeline.
        stub_cli.add(
            role="author",
            output="Task implemented.",
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

        author_calls = [c for c in stub_cli.calls if c.role == "author"]
        assert author_calls, "No author-role call was recorded"
        prompt = author_calls[0].prompt
        assert "sprint-9" in prompt, (
            f"Expected 'sprint-9' in rendered builder prompt.\nPrompt snippet: {prompt[:600]!r}"
        )


class TestMCPTasklistFilterProject:
    """MCP tasklist_filter project → instruction clause in rendered prompt."""

    def test_project_filter_propagates_to_rendered_prompt(
        self, stub_cli: StubCli, temp_repo: Path
    ) -> None:
        """
        Config: tasklist_provider="mcp", tasklist_filter.projects=["eng-platform"].
        Assert the rendered builder prompt captured via stub_cli contains "eng-platform".
        """
        config_toml = temp_repo / ".millstone" / "config.toml"
        config_toml.write_text(
            'tasklist_provider = "mcp"\n'
            "\n"
            "[tasklist_provider_options]\n"
            'mcp_server = "github"\n'
            "\n"
            "[tasklist_filter]\n"
            'projects = ["eng-platform"]\n'
        )

        # Set up stubs for the full inner-loop pipeline.
        stub_cli.add(
            role="author",
            output="Task implemented.",
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

        author_calls = [c for c in stub_cli.calls if c.role == "author"]
        assert author_calls, "No author-role call was recorded"
        prompt = author_calls[0].prompt
        assert "eng-platform" in prompt, (
            f"Expected 'eng-platform' in rendered builder prompt.\nPrompt snippet: {prompt[:600]!r}"
        )


class TestMCPExplicitEmptyLabelsOverridesFilter:
    """Explicit empty top-level labels beats tasklist_filter — no label clause."""

    def test_explicit_empty_labels_suppresses_filter_label_clause(
        self, stub_cli: StubCli, temp_repo: Path
    ) -> None:
        """
        Config: tasklist_provider_options.labels=[] overrides tasklist_filter.label="sprint-9".

        An explicit empty labels list in provider options suppresses the filter label clause,
        so "sprint-9" must not appear in the rendered builder prompt.
        """
        config_toml = temp_repo / ".millstone" / "config.toml"
        config_toml.write_text(
            'tasklist_provider = "mcp"\n'
            "\n"
            "[tasklist_provider_options]\n"
            'mcp_server = "github"\n'
            "labels = []\n"
            "\n"
            "[tasklist_filter]\n"
            'label = "sprint-9"\n'
        )

        # Set up stubs for the full inner-loop pipeline.
        stub_cli.add(
            role="author",
            output="Task implemented.",
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

        # Assert "sprint-9" is absent from the rendered builder prompt.
        author_calls = [c for c in stub_cli.calls if c.role == "author"]
        assert author_calls, "No author-role call was recorded"
        prompt = author_calls[0].prompt
        assert "sprint-9" not in prompt, (
            f"Expected 'sprint-9' absent from rendered builder prompt "
            f"(explicit empty labels suppresses filter label clause).\n"
            f"Prompt snippet: {prompt[:600]!r}"
        )
