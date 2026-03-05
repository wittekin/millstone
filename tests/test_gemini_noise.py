"""Tests for Gemini noise handling and retry logic."""

import json
from unittest.mock import MagicMock, patch

from millstone.agent_providers import GeminiProvider
from millstone.runtime.orchestrator import Orchestrator


class TestGeminiNoiseHandling:
    """Tests for handling CLI noise in Gemini output."""

    def test_run_unwraps_json_with_prefix_noise(self):
        """run() finds JSON block when preceded by CLI noise."""
        provider = GeminiProvider()

        # CLI noise + JSON
        noise = "YOLO mode enabled.\nCached credentials loaded.\n"
        json_content = json.dumps({
            "response": "Clean response",
            "stats": {}
        })
        full_output = noise + json_content

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=full_output,
                stderr="",
                returncode=0
            )
            result = provider.run("test")

            assert result.stdout == "Clean response"
            assert result.output == full_output

    def test_run_unwraps_json_with_suffix_noise(self):
        """run() finds JSON block when followed by CLI noise."""
        provider = GeminiProvider()

        # JSON + CLI noise
        json_content = json.dumps({
            "response": "Clean response",
            "stats": {}
        })
        noise = "\nSome trailing log message."
        full_output = json_content + noise

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=full_output,
                stderr="",
                returncode=0
            )
            result = provider.run("test")

            assert result.stdout == "Clean response"

    def test_run_unwraps_json_surrounded_by_noise(self):
        """run() finds JSON block surrounded by noise."""
        provider = GeminiProvider()

        json_content = json.dumps({
            "response": "Clean response",
            "stats": {}
        })
        full_output = f"Prefix noise\n{json_content}\nSuffix noise"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=full_output,
                stderr="",
                returncode=0
            )
            result = provider.run("test")

            assert result.stdout == "Clean response"


class TestOrchestratorGeminiRetry:
    """Tests for Orchestrator retry logic with Gemini noise."""

    def test_retry_path_handles_noise(self, temp_repo):
        """Orchestrator retry path correctly unwraps noisy output."""
        # Setup Orchestrator with Gemini and enabled retries
        orch = Orchestrator(cli="gemini", retry_on_empty_response=True)

        # 1. First response: Empty content (triggers retry)
        # 2. Second response: Valid content but with noise (regression test for retry path)

        empty_json = json.dumps({"response": "", "stats": {}})
        valid_json = json.dumps({"response": "Valid after retry", "stats": {}})
        noisy_valid_json = f"Noise\n{valid_json}\nMore Noise"

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Call 1: Empty response (wrapped in JSON)
                MagicMock(returncode=0, stdout=empty_json, stderr=""),
                # Call 2 (Retry): Valid response (wrapped in JSON + Noise)
                MagicMock(returncode=0, stdout=noisy_valid_json, stderr="")
            ]

            output = orch.run_agent("test prompt", role="builder")

            # Should have retried
            assert mock_run.call_count == 2

            # Output should be clean (unwrapped from noise)
            assert output == "Valid after retry"

            # Log should show retry
            log_content = orch.log_file.read_text()
            assert "empty_response_retry" in log_content
