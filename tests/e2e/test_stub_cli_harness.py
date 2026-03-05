"""Verify the stub_cli harness: role routing, sequence consumption,
side-effect invocation, prompt capture, and assert_roles_consumed."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.e2e.conftest import StubCli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orch(repo_dir: Path | None = None) -> MagicMock:
    orch = MagicMock()
    orch.repo_dir = repo_dir
    return orch


# ---------------------------------------------------------------------------
# Role routing
# ---------------------------------------------------------------------------


class TestRoleRouting:
    def test_routes_to_matching_role(self, stub_cli: StubCli) -> None:
        """Correct entry is returned when role matches."""
        stub_cli.add(role="reviewer", output="review output")
        stub_cli.add(role="author", output="author output")

        orch = _make_orch()
        with stub_cli.patch(orch):
            result = orch.run_agent("some prompt", role="author")

        assert result == "author output"

    def test_reviewer_entry_not_consumed_by_author_call(self, stub_cli: StubCli) -> None:
        """Reviewer entry is not consumed by an author-role call."""
        stub_cli.add(role="reviewer", output="review output")
        stub_cli.add(role="author", output="author output")

        orch = _make_orch()
        with stub_cli.patch(orch):
            orch.run_agent("prompt", role="author")
            result = orch.run_agent("prompt", role="reviewer")

        assert result == "review output"

    def test_sequence_consumption_same_role(self, stub_cli: StubCli) -> None:
        """Two entries for the same role are consumed in FIFO order."""
        stub_cli.add(role="reviewer", output="first review")
        stub_cli.add(role="reviewer", output="second review")

        orch = _make_orch()
        with stub_cli.patch(orch):
            r1 = orch.run_agent("p1", role="reviewer")
            r2 = orch.run_agent("p2", role="reviewer")

        assert r1 == "first review"
        assert r2 == "second review"

    def test_falls_back_to_output_schema(self, stub_cli: StubCli) -> None:
        """Entry with output_schema is matched when no role entry exists."""
        stub_cli.add(output_schema="my_schema", output="schema output")

        orch = _make_orch()
        with stub_cli.patch(orch):
            result = orch.run_agent("prompt", role="author", output_schema="my_schema")

        assert result == "schema output"

    def test_falls_back_to_prompt_substring(self, stub_cli: StubCli) -> None:
        """Entry with prompt_substring is matched when no role or schema entry exists."""
        stub_cli.add(prompt_substring="magic phrase", output="substring match")

        orch = _make_orch()
        with stub_cli.patch(orch):
            result = orch.run_agent("a prompt with magic phrase here", role="author")

        assert result == "substring match"

    def test_role_beats_schema_in_priority(self, stub_cli: StubCli) -> None:
        """Role match takes priority over output_schema match."""
        stub_cli.add(role="author", output="role match")
        stub_cli.add(output_schema="some_schema", output="schema match")

        orch = _make_orch()
        with stub_cli.patch(orch):
            result = orch.run_agent("prompt", role="author", output_schema="some_schema")

        assert result == "role match"

    def test_schema_beats_substring_in_priority(self, stub_cli: StubCli) -> None:
        """Schema match takes priority over prompt substring match."""
        stub_cli.add(output_schema="my_schema", output="schema match")
        stub_cli.add(prompt_substring="magic", output="substring match")

        orch = _make_orch()
        with stub_cli.patch(orch):
            result = orch.run_agent("magic prompt", role="author", output_schema="my_schema")

        assert result == "schema match"

    def test_reviewer_schema_entry_not_consumed_by_author_with_same_schema(
        self, stub_cli: StubCli
    ) -> None:
        """Reviewer+schema entry must not be consumed by an author call with the same schema."""
        stub_cli.add(role="reviewer", output_schema="my_schema", output="review-schema")

        orch = _make_orch()
        with stub_cli.patch(orch):
            result = orch.run_agent("p", role="author", output_schema="my_schema")

        assert result == "", "reviewer-scoped entry should not match an author call"

    def test_no_match_returns_empty_string(self, stub_cli: StubCli) -> None:
        """Returns empty string when no entry matches."""
        orch = _make_orch()
        with stub_cli.patch(orch):
            result = orch.run_agent("prompt", role="author")

        assert result == ""


# ---------------------------------------------------------------------------
# Prompt capture
# ---------------------------------------------------------------------------


class TestPromptCapture:
    def test_captures_prompt_text(self, stub_cli: StubCli) -> None:
        """The prompt text is accessible via stub_cli.calls[n].prompt."""
        stub_cli.add(role="author", output="ok")

        orch = _make_orch()
        with stub_cli.patch(orch):
            orch.run_agent("my special prompt", role="author")

        assert stub_cli.calls[0].prompt == "my special prompt"

    def test_captures_role(self, stub_cli: StubCli) -> None:
        """The role kwarg is recorded in stub_cli.calls."""
        stub_cli.add(role="reviewer", output="ok")

        orch = _make_orch()
        with stub_cli.patch(orch):
            orch.run_agent("prompt", role="reviewer")

        assert stub_cli.calls[0].role == "reviewer"

    def test_captures_multiple_calls_in_order(self, stub_cli: StubCli) -> None:
        """All calls are recorded in invocation order."""
        stub_cli.add(role="author", output="ok")
        stub_cli.add(role="reviewer", output="ok")

        orch = _make_orch()
        with stub_cli.patch(orch):
            orch.run_agent("first prompt", role="author")
            orch.run_agent("second prompt", role="reviewer")

        assert len(stub_cli.calls) == 2
        assert stub_cli.calls[0].prompt == "first prompt"
        assert stub_cli.calls[0].role == "author"
        assert stub_cli.calls[1].prompt == "second prompt"
        assert stub_cli.calls[1].role == "reviewer"

    def test_captures_output_schema(self, stub_cli: StubCli) -> None:
        """The output_schema kwarg is recorded in stub_cli.calls."""
        stub_cli.add(output_schema="my_schema", output="ok")

        orch = _make_orch()
        with stub_cli.patch(orch):
            orch.run_agent("prompt", role="author", output_schema="my_schema")

        assert stub_cli.calls[0].output_schema == "my_schema"

    def test_captures_resume(self, stub_cli: StubCli) -> None:
        """The resume kwarg is recorded in stub_cli.calls."""
        stub_cli.add(role="author", output="ok")

        orch = _make_orch()
        with stub_cli.patch(orch):
            orch.run_agent("prompt", role="author", resume="session-abc")

        assert stub_cli.calls[0].resume == "session-abc"


# ---------------------------------------------------------------------------
# Side-effect invocation
# ---------------------------------------------------------------------------


class TestSideEffect:
    def test_side_effect_called_with_repo_dir(self, stub_cli: StubCli, tmp_path: Path) -> None:
        """side_effect receives the orchestrator's repo_dir."""
        received: list[Path] = []
        stub_cli.add(role="author", output="ok", side_effect=lambda repo: received.append(repo))

        orch = _make_orch(repo_dir=tmp_path)
        with stub_cli.patch(orch):
            orch.run_agent("prompt", role="author")

        assert received == [tmp_path]

    def test_side_effect_can_write_files(self, stub_cli: StubCli, tmp_path: Path) -> None:
        """side_effect can create files in the repo directory."""
        target = tmp_path / "output.txt"
        stub_cli.add(
            role="author",
            output="ok",
            side_effect=lambda repo: (repo / "output.txt").write_text("written"),
        )

        orch = _make_orch(repo_dir=tmp_path)
        with stub_cli.patch(orch):
            orch.run_agent("prompt", role="author")

        assert target.exists()
        assert target.read_text() == "written"

    def test_side_effect_not_called_when_no_repo_dir(self, stub_cli: StubCli) -> None:
        """side_effect is silently skipped when repo_dir is None."""
        called: list[bool] = []
        stub_cli.add(role="author", output="ok", side_effect=lambda repo: called.append(True))

        orch = _make_orch(repo_dir=None)
        with stub_cli.patch(orch):
            orch.run_agent("prompt", role="author")

        assert called == []

    def test_side_effect_not_called_for_no_match(self, stub_cli: StubCli, tmp_path: Path) -> None:
        """side_effect is not called when the entry does not match."""
        called: list[bool] = []
        stub_cli.add(role="reviewer", output="ok", side_effect=lambda repo: called.append(True))

        orch = _make_orch(repo_dir=tmp_path)
        with stub_cli.patch(orch):
            orch.run_agent("prompt", role="author")  # wrong role

        assert called == []


# ---------------------------------------------------------------------------
# assert_roles_consumed
# ---------------------------------------------------------------------------


class TestAssertRolesConsumed:
    def test_passes_with_correct_sequence(self, stub_cli: StubCli) -> None:
        stub_cli.add(role="author", output="ok")
        stub_cli.add(role="reviewer", output="ok")
        stub_cli.add(role="sanity", output="ok")

        orch = _make_orch()
        with stub_cli.patch(orch):
            orch.run_agent("p1", role="author")
            orch.run_agent("p2", role="reviewer")
            orch.run_agent("p3", role="sanity")

        stub_cli.assert_roles_consumed(["author", "reviewer", "sanity"])

    def test_fails_with_wrong_role(self, stub_cli: StubCli) -> None:
        stub_cli.add(role="author", output="ok")

        orch = _make_orch()
        with stub_cli.patch(orch):
            orch.run_agent("p1", role="author")

        with pytest.raises(AssertionError):
            stub_cli.assert_roles_consumed(["reviewer"])

    def test_fails_with_wrong_order(self, stub_cli: StubCli) -> None:
        stub_cli.add(role="author", output="ok")
        stub_cli.add(role="reviewer", output="ok")

        orch = _make_orch()
        with stub_cli.patch(orch):
            orch.run_agent("p1", role="author")
            orch.run_agent("p2", role="reviewer")

        with pytest.raises(AssertionError):
            stub_cli.assert_roles_consumed(["reviewer", "author"])

    def test_fails_when_extra_calls_made(self, stub_cli: StubCli) -> None:
        stub_cli.add(role="author", output="ok")
        stub_cli.add(role="reviewer", output="ok")

        orch = _make_orch()
        with stub_cli.patch(orch):
            orch.run_agent("p1", role="author")
            orch.run_agent("p2", role="reviewer")

        with pytest.raises(AssertionError):
            stub_cli.assert_roles_consumed(["author"])

    def test_empty_sequence(self, stub_cli: StubCli) -> None:
        """No calls → assert empty list passes."""
        orch = _make_orch()
        with stub_cli.patch(orch):
            pass

        stub_cli.assert_roles_consumed([])
