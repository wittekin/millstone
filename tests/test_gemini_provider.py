"""Tests for GeminiProvider implementation."""

import json
from unittest.mock import MagicMock, patch

from millstone.agent_providers import (
    PROVIDERS,
    GeminiProvider,
    get_provider,
)


class TestGeminiProvider:
    """Tests for GeminiProvider implementation."""

    def test_name_is_gemini_cli(self):
        """GeminiProvider name is 'Gemini CLI'."""
        provider = GeminiProvider()
        assert provider.name == "Gemini CLI"

    def test_command_is_gemini(self):
        """GeminiProvider command is 'gemini'."""
        provider = GeminiProvider()
        assert provider.command == "gemini"

    def test_install_instructions_contain_npm(self):
        """Install instructions contain npm install command."""
        provider = GeminiProvider()
        assert "npm install" in provider.install_instructions
        assert "gemini-cli" in provider.install_instructions

    def test_version_command(self):
        """Version command is ['gemini', '--version']."""
        provider = GeminiProvider()
        assert provider.version_command() == ["gemini", "--version"]

    def test_build_command_basic(self):
        """Basic command includes -y and -o json."""
        provider = GeminiProvider()
        cmd = provider.build_command("fix the bug")
        assert cmd[0] == "gemini"
        assert "-y" in cmd
        assert "-o" in cmd
        assert "json" in cmd
        assert "fix the bug" in cmd

    def test_build_command_with_resume(self):
        """Command with resume session includes -r."""
        provider = GeminiProvider()
        cmd = provider.build_command("continue", resume="session-123")
        assert "-r" in cmd
        assert "session-123" in cmd

    def test_build_command_with_model(self):
        """Command with model override includes -m."""
        provider = GeminiProvider()
        cmd = provider.build_command("task", model="gemini-3-flash-preview")
        assert "-m" in cmd
        assert "gemini-3-flash-preview" in cmd

    def test_build_command_with_output_schema(self):
        """Command with output_schema injects schema into prompt."""
        provider = GeminiProvider()
        cmd = provider.build_command(
            "review changes",
            output_schema="review_decision"
        )

        # Check prompt for injected schema
        prompt = cmd[-1]
        assert "IMPORTANT: You MUST return a valid JSON object" in prompt
        assert "status" in prompt # field from review_decision schema
        assert "review" in prompt # field from review_decision schema
        assert "summary" in prompt # field from review_decision schema
        assert "APPROVED" in prompt # enum value from schema

    def test_run_unwraps_json_response(self):
        """run() unwraps the 'response' field from Gemini's JSON output."""
        provider = GeminiProvider()

        # Mock Gemini CLI output: { "response": "Actual content", "stats": ... }
        mock_output = json.dumps({
            "response": "Here is the code",
            "stats": {"tokens": 100}
        })

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=mock_output,
                stderr="",
                returncode=0
            )
            result = provider.run("test prompt")

            # stdout should be unwrapped
            assert result.stdout == "Here is the code"
            # output should contain full raw output (stdout + stderr)
            assert mock_output in result.output

    def test_run_fallback_on_invalid_json(self):
        """run() returns raw output if JSON parsing fails."""
        provider = GeminiProvider()

        raw_output = "Not JSON output"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=raw_output,
                stderr="",
                returncode=0
            )
            result = provider.run("test prompt")

            assert result.stdout == raw_output

    def test_run_fallback_on_missing_response_field(self):
        """run() returns raw output if JSON doesn't contain 'response'."""
        provider = GeminiProvider()

        # JSON but no response field
        raw_output = json.dumps({"error": "something went wrong"})

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=raw_output,
                stderr="",
                returncode=0
            )
            result = provider.run("test prompt")

            assert result.stdout == raw_output

    def test_provider_registry(self):
        """Gemini provider is registered."""
        assert "gemini" in PROVIDERS
        assert isinstance(get_provider("gemini"), GeminiProvider)

    def test_run_retries_capacity_error_then_succeeds(self, caplog):
        """run() retries transient capacity errors with exponential backoff."""
        provider = GeminiProvider()
        first = MagicMock(
            stdout="",
            stderr="No capacity available for model gemini-3-flash-preview reason: MODEL_CAPACITY_EXHAUSTED",
            returncode=1,
        )
        second = MagicMock(
            stdout=json.dumps({"response": "Recovered output", "stats": {}}),
            stderr="",
            returncode=0,
        )

        with patch("subprocess.run") as mock_run, patch("time.sleep") as mock_sleep:
            mock_run.side_effect = [first, second]
            result = provider.run("test prompt", model="gemini-3-flash-preview")

        assert mock_run.call_count == 2
        mock_sleep.assert_called_once_with(1.0)
        assert result.returncode == 0
        assert result.stdout == "Recovered output"
        assert "Gemini retry 1/3 in 1.0s" in result.output
        assert "reason=model_capacity_exhausted" in caplog.text

    def test_run_does_not_retry_non_retryable_error(self):
        """run() does not retry non-transient errors."""
        provider = GeminiProvider()
        failure = MagicMock(
            stdout="",
            stderr="PERMISSION_DENIED: invalid credentials",
            returncode=1,
        )

        with patch("subprocess.run") as mock_run, patch("time.sleep") as mock_sleep:
            mock_run.return_value = failure
            result = provider.run("test prompt")

        mock_run.assert_called_once()
        mock_sleep.assert_not_called()
        assert result.returncode == 1
        assert "PERMISSION_DENIED" in result.stderr

    def test_run_retries_until_exhausted(self):
        """run() gives up after bounded retry attempts."""
        provider = GeminiProvider()
        failure = MagicMock(
            stdout="",
            stderr='{"status":"RESOURCE_EXHAUSTED","code":429}',
            returncode=1,
        )

        with patch("subprocess.run") as mock_run, patch("time.sleep") as mock_sleep:
            mock_run.side_effect = [failure, failure, failure, failure]
            result = provider.run("test prompt")

        assert mock_run.call_count == provider.RETRY_MAX_ATTEMPTS
        assert [call.args[0] for call in mock_sleep.call_args_list] == [1.0, 2.0, 4.0]
        assert result.returncode == 1
