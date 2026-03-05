"""Integration tests for no-code verification workflows."""

import subprocess
from unittest.mock import MagicMock, patch

from millstone.runtime.orchestrator import Orchestrator

# Store original subprocess.run to use for git commands
_original_subprocess_run = subprocess.run


def make_mock_runner(temp_repo, claude_responses):
    """Mock runner factory (copied from test_integration.py)."""
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
            return _original_subprocess_run(cmd, **kwargs)

    return mock_run


class ResponseBuilder:
    """Response builder (simplified from test_integration.py)."""

    def __init__(self):
        self._builder_output = "Task completed."
        self._sanity_check_response = ('{"status": "OK"}', None)
        self._reviewer_response = '{"status": "APPROVED", "review": "Looks good", "summary": "No blockers", "findings": [], "findings_by_severity": {"critical": [], "high": [], "medium": [], "low": [], "nit": []}}'

    def on_builder(self, output="Task completed."):
        self._builder_output = output
        return self

    def on_sanity_check(self, response="OK"):
        self._sanity_check_response = (response, None)
        return self

    def on_reviewer(
        self,
        response='{"status": "APPROVED", "review": "Looks good", "summary": "No blockers", "findings": [], "findings_by_severity": {"critical": [], "high": [], "medium": [], "low": [], "nit": []}}',
    ):
        self._reviewer_response = (response, None)
        return self

    def build(self):
        def responses(prompt, counts):
            prompt_lower = prompt.lower()
            if "sanity check" in prompt_lower:
                return self._sanity_check_response
            elif "review" in prompt_lower and "code" in prompt_lower:
                return self._reviewer_response
            elif "complete exactly one task" in prompt_lower or "task:" in prompt_lower:
                return (self._builder_output, None)
            elif "commit" in prompt_lower:
                return ("Nothing to commit.", None)
            else:
                return ('{"status": "OK"}', None)

        return responses


class TestNoCodeFlow:
    """Tests for workflows that result in no code changes."""

    def test_verification_task_success(self, temp_repo, capsys):
        """
        Scenario: User requests a verification task (e.g. "Check if X is installed").
        Builder runs command, sees it is installed, reports success.
        Mechanical checks warn but pass.
        Sanity check passes.
        Reviewer sees empty diff, checks task type, approves.
        Orchestrator succeeds.
        """
        builder_output = """
        I have verified that the 'requests' library is installed by running:
        `pip show requests`

        Output:
        Name: requests
        Version: 2.31.0

        Task complete.
        """

        reviewer_response = """
        {
            "status": "APPROVED",
            "review": "Looks good. Verification-only task.",
            "summary": "Verification task completed successfully. No code changes required.",
            "findings": [],
            "findings_by_severity": {"critical": [], "high": [], "medium": [], "low": [], "nit": []}
        }
        """

        responses = (
            ResponseBuilder()
            .on_builder(output=builder_output)
            .on_reviewer(response=reviewer_response)
            .build()
        )

        # Use --task mode to avoid tasklist interactions for this specific test
        orch = Orchestrator(task="Verify requests library is installed")

        try:
            with patch("subprocess.run", side_effect=make_mock_runner(temp_repo, responses)):
                exit_code = orch.run()

            # Should succeed
            assert exit_code == 0

            captured = capsys.readouterr()
            # Verify the warning was logged
            assert "WARN: No changes detected" in captured.out
            # Verify success
            assert "=== SUCCESS ===" in captured.out

        finally:
            orch.cleanup()
