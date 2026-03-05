"""Regression test for builder failing to stage files (git add)."""

from unittest.mock import patch

from millstone.runtime.orchestrator import Orchestrator


def test_builder_producer_stages_untracked_files(temp_repo):
    """Verify that if builder creates a file but forgets to git add, orchestrator runs git add -N."""
    orch = Orchestrator(retry_on_empty_response=True)

    # Define a side effect for the builder that writes a file but DOES NOT git add it
    def builder_action(*args, **kwargs):
        # Create a new file
        (temp_repo / "new_feature.py").write_text("print('hello world')")
        return "I created the file."

    call_count = 0
    def mock_run_agent_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Builder
            return builder_action()
        elif call_count == 2:
            # Sanity check
            return '{"status": "OK"}'
        elif call_count == 3:
            # Reviewer
            return '{"status": "APPROVED", "verdict": "APPROVED"}'
        return ""

    with patch.object(orch, 'run_agent') as mock_agent:
        mock_agent.side_effect = mock_run_agent_side_effect

        # Mock git calls to let the orchestrator do its thing,
        # BUT we need real git behavior for the repo to detect untracked files.
        # Orchestrator uses self.git() which uses subprocess.
        # We should NOT mock self.git if we want real repo behavior,
        # but we mock run_agent to simulate the "forgetful builder".

        # We need to spy on the log to see if the diff was captured

        orch.run_single_task()

        # Check the logs to see if the diff for new_feature.py was captured
        log_content = orch.log_file.read_text()

        # If git add -N worked, the diff should show the new file
        # A new file diff typically looks like:
        # diff --git a/new_feature.py b/new_feature.py
        # or just listed in the summary if simplified

        assert "new_feature.py" in log_content

        # Specifically, check the git_state event
        # It should show status '??' (untracked) became 'AM' or similar?
        # Actually git add -N makes it ' A' or 'AN'?
        # git status --short shows '??' for untracked.
        # git add -N makes it show as ' A' (added but empty content in index?) or just tracked?

        # Use git status to verify it's now tracked (intent-to-add)
        import subprocess
        status = subprocess.run(["git", "status", "--short"], cwd=temp_repo, capture_output=True, text=True).stdout

        # Expectation: orchestrator ran git add -N, so it should NOT be '??' anymore,
        # OR it is tracked so it appears in diff.
        # git add -N entries show as ' A' in short status usually.
        # But checking the LOG is the most direct proof that the orchestrator *saw* the diff.

        # Verify the diff was logged with proper diff header
        # This confirms git diff actually saw the file content
        assert "diff --git a/new_feature.py" in log_content

        # Verify git status shows it as added (intent-to-add), not untracked
        # git add -N results in ' A' status usually
        assert "?? new_feature.py" not in status
        assert "A new_feature.py" in status or "AM new_feature.py" in status
