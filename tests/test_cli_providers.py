"""Tests for CLI provider abstraction."""

from unittest.mock import MagicMock, patch

import pytest

from millstone.agent_providers import (
    PROVIDERS,
    ClaudeProvider,
    CLIResult,
    CodexProvider,
    get_provider,
    list_providers,
)


class TestCLIResult:
    """Tests for CLIResult dataclass."""

    def test_has_expected_fields(self):
        """CLIResult has all expected fields."""
        result = CLIResult(
            output="combined output",
            returncode=0,
            stdout="stdout content",
            stderr="stderr content",
        )
        assert result.output == "combined output"
        assert result.returncode == 0
        assert result.stdout == "stdout content"
        assert result.stderr == "stderr content"


class TestClaudeProvider:
    """Tests for ClaudeProvider implementation."""

    def test_name_is_claude_code(self):
        """ClaudeProvider name is 'Claude Code'."""
        provider = ClaudeProvider()
        assert provider.name == "Claude Code"

    def test_command_is_claude(self):
        """ClaudeProvider command is 'claude'."""
        provider = ClaudeProvider()
        assert provider.command == "claude"

    def test_install_instructions_contain_npm(self):
        """Install instructions contain npm install command."""
        provider = ClaudeProvider()
        assert "npm install" in provider.install_instructions
        assert "claude-code" in provider.install_instructions

    def test_version_command(self):
        """Version command is ['claude', '--version']."""
        provider = ClaudeProvider()
        assert provider.version_command() == ["claude", "--version"]

    def test_build_command_basic(self):
        """Basic command builds correctly."""
        provider = ClaudeProvider()
        cmd = provider.build_command("fix the bug")
        assert cmd == ["claude", "-p", "fix the bug", "--dangerously-skip-permissions"]

    def test_build_command_with_resume(self):
        """Command with resume session includes --resume."""
        provider = ClaudeProvider()
        cmd = provider.build_command("continue", resume="session-123")
        assert "--resume" in cmd
        assert "session-123" in cmd

    def test_build_command_with_model(self):
        """Command with model override includes --model."""
        provider = ClaudeProvider()
        cmd = provider.build_command("task", model="sonnet")
        assert "--model" in cmd
        assert "sonnet" in cmd

    def test_build_command_with_all_options(self):
        """Command with all options includes everything."""
        provider = ClaudeProvider()
        cmd = provider.build_command(
            "do something",
            resume="sess-456",
            model="opus"
        )
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "do something" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--resume" in cmd
        assert "sess-456" in cmd
        assert "--model" in cmd
        assert "opus" in cmd


class TestCodexProvider:
    """Tests for CodexProvider implementation."""

    def test_name_is_codex_cli(self):
        """CodexProvider name is 'Codex CLI'."""
        provider = CodexProvider()
        assert provider.name == "Codex CLI"

    def test_command_is_codex(self):
        """CodexProvider command is 'codex'."""
        provider = CodexProvider()
        assert provider.command == "codex"

    def test_install_instructions_contain_npm(self):
        """Install instructions contain npm install command."""
        provider = CodexProvider()
        assert "npm install" in provider.install_instructions
        assert "codex" in provider.install_instructions

    def test_version_command(self):
        """Version command is ['codex', '--version']."""
        provider = CodexProvider()
        assert provider.version_command() == ["codex", "--version"]

    def test_build_command_basic(self):
        """Basic command uses stdin sentinel and --yolo."""
        provider = CodexProvider()
        cmd = provider.build_command("fix the bug")
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "-" in cmd
        assert "fix the bug" not in cmd
        assert "--yolo" in cmd

    def test_build_command_with_resume(self):
        """Command with resume uses 'codex exec resume'."""
        provider = CodexProvider()
        cmd = provider.build_command("continue", resume="session-123")
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "resume" in cmd
        assert "session-123" in cmd
        # Follow-up prompt should be after session ID
        assert "continue" in cmd

    def test_build_command_with_model(self):
        """Command with model override includes --model."""
        provider = CodexProvider()
        cmd = provider.build_command("task", model="gpt-5")
        assert "--model" in cmd
        assert "gpt-5" in cmd

    def test_build_command_resume_with_model(self):
        """Resume command with model includes both."""
        provider = CodexProvider()
        cmd = provider.build_command(
            "follow up",
            resume="sess-789",
            model="gpt-5"
        )
        assert "resume" in cmd
        assert "sess-789" in cmd
        assert "--model" in cmd
        assert "gpt-5" in cmd


class TestProviderRegistry:
    """Tests for provider registry functions."""

    def test_list_providers_returns_expected(self):
        """list_providers returns expected providers."""
        providers = list_providers()
        assert "claude" in providers
        assert "codex" in providers

    def test_get_provider_claude(self):
        """get_provider('claude') returns ClaudeProvider."""
        provider = get_provider("claude")
        assert isinstance(provider, ClaudeProvider)

    def test_get_provider_codex(self):
        """get_provider('codex') returns CodexProvider."""
        provider = get_provider("codex")
        assert isinstance(provider, CodexProvider)

    def test_get_provider_unknown_raises(self):
        """get_provider with unknown name raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            get_provider("unknown_cli")
        assert "unknown_cli" in str(exc_info.value)
        assert "Available" in str(exc_info.value)

    def test_providers_dict_has_expected_entries(self):
        """PROVIDERS dict contains expected entries."""
        assert "claude" in PROVIDERS
        assert "codex" in PROVIDERS
        assert PROVIDERS["claude"] == ClaudeProvider
        assert PROVIDERS["codex"] == CodexProvider


class TestProviderRun:
    """Tests for provider run() method."""

    def test_run_returns_cli_result(self):
        """run() returns CLIResult with correct fields."""
        provider = ClaudeProvider()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="output text",
                stderr="",
                returncode=0
            )
            result = provider.run("test prompt")
            assert isinstance(result, CLIResult)
            assert result.output == "output text"
            assert result.returncode == 0

    def test_run_combines_stdout_stderr(self):
        """run() combines stdout and stderr in output."""
        provider = ClaudeProvider()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="stdout part",
                stderr="stderr part",
                returncode=0
            )
            result = provider.run("test")
            assert result.output == "stdout part\nstderr part"

    def test_run_passes_cwd(self):
        """run() passes cwd to subprocess."""
        provider = ClaudeProvider()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            provider.run("test", cwd="/some/path")
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["cwd"] == "/some/path"


class TestProviderCheckAvailable:
    """Tests for provider check_available() method."""

    def test_check_available_returns_true_when_found(self):
        """check_available() returns (True, message) when CLI is found."""
        provider = ClaudeProvider()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="claude v1.0.0",
                stderr="",
                returncode=0
            )
            available, message = provider.check_available()
            assert available is True
            assert "Claude Code available" in message

    def test_check_available_returns_false_when_not_found(self):
        """check_available() returns (False, message) when FileNotFoundError."""
        provider = ClaudeProvider()
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("not found")
            available, message = provider.check_available()
            assert available is False
            assert "not found" in message
            assert "npm install" in message

    def test_check_available_returns_false_on_error(self):
        """check_available() returns (False, message) on non-zero returncode."""
        provider = ClaudeProvider()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="",
                stderr="some error",
                returncode=1
            )
            available, message = provider.check_available()
            assert available is False
            assert "error" in message.lower()


class TestStructuredOutputSchema:
    """Tests for structured output schema support."""

    def test_claude_build_command_with_output_schema(self):
        """ClaudeProvider includes --output-format json and --json-schema when output_schema is set."""
        provider = ClaudeProvider()
        cmd = provider.build_command(
            "review changes",
            output_schema="review_decision",
        )
        # Must include --output-format json for Claude Code to return pure JSON
        assert "--output-format" in cmd
        output_format_idx = cmd.index("--output-format")
        assert cmd[output_format_idx + 1] == "json"
        # Must include --json-schema with the schema
        assert "--json-schema" in cmd
        json_schema_idx = cmd.index("--json-schema")
        schema_json = cmd[json_schema_idx + 1]
        # Verify it's valid JSON with expected fields
        import json
        schema = json.loads(schema_json)
        assert "status" in schema.get("properties", {})

    def test_codex_build_command_with_output_schema(self, tmp_path):
        """CodexProvider includes --output-schema with file path."""
        provider = CodexProvider()
        work_dir = tmp_path / ".millstone"
        work_dir.mkdir()
        cmd = provider.build_command(
            "review changes",
            output_schema="review_decision",
            schema_work_dir=str(work_dir),
        )
        assert "--output-schema" in cmd
        # Should be a file path
        output_schema_idx = cmd.index("--output-schema")
        schema_path = cmd[output_schema_idx + 1]
        assert "review_decision.json" in schema_path
        # File should exist
        from pathlib import Path
        assert Path(schema_path).exists()

    def test_codex_output_schema_requires_work_dir(self):
        """CodexProvider ignores output_schema without schema_work_dir."""
        provider = CodexProvider()
        cmd = provider.build_command(
            "review changes",
            output_schema="review_decision",
            # No schema_work_dir
        )
        assert "--output-schema" not in cmd

    def test_claude_output_schema_no_work_dir_needed(self):
        """ClaudeProvider works without schema_work_dir (uses inline JSON)."""
        provider = ClaudeProvider()
        cmd = provider.build_command(
            "review changes",
            output_schema="review_decision",
            # No schema_work_dir - should still work
        )
        assert "--output-format" in cmd
        assert "--json-schema" in cmd


class TestSchemasParsing:
    """Tests for schema parsing functions."""

    def test_parse_review_decision_approved(self):
        """Parses APPROVED status from JSON."""
        from millstone.policy.schemas import ReviewStatus, parse_review_decision
        output = '{"status": "APPROVED", "review": "Looks good", "summary": "No blockers"}'
        result = parse_review_decision(output)
        assert result is not None
        assert result.status == ReviewStatus.APPROVED
        assert result.is_approved is True

    def test_parse_review_decision_request_changes(self):
        """Parses REQUEST_CHANGES status with findings."""
        from millstone.policy.schemas import ReviewStatus, parse_review_decision
        output = '{"status": "REQUEST_CHANGES", "review": "Needs fixes", "summary": "Blocking issues", "findings": ["fix bug", "add tests"]}'
        result = parse_review_decision(output)
        assert result is not None
        assert result.status == ReviewStatus.REQUEST_CHANGES
        assert result.is_approved is False
        assert result.findings == ["fix bug", "add tests"]

    def test_parse_review_decision_from_prose(self):
        """Parses approval from surrounding text."""
        from millstone.policy.schemas import ReviewStatus, parse_review_decision
        output = """
        ## Review Summary
        The changes look good.

        ```json
        {"status": "APPROVED", "review": "Looks good", "summary": "Clean implementation"}
        ```
        """
        result = parse_review_decision(output)
        assert result is not None
        assert result.status == ReviewStatus.APPROVED
        assert result.summary == "Clean implementation"

    def test_parse_review_decision_fallback_safe_to_merge(self):
        """Does not parse prose-only without required JSON fields."""
        from millstone.policy.schemas import parse_review_decision
        output = "The changes are safe to merge."
        result = parse_review_decision(output)
        assert result is None

    def test_parse_review_decision_with_findings_by_severity(self):
        """Parses findings_by_severity from JSON."""
        from millstone.policy.schemas import ReviewStatus, parse_review_decision
        output = '''{
            "status": "REQUEST_CHANGES",
            "review": "Needs fixes",
            "summary": "Blocking issues",
            "findings_by_severity": {
                "critical": ["security vulnerability in auth"],
                "high": ["missing input validation"],
                "medium": [],
                "low": ["typo in comment"],
                "nit": []
            }
        }'''
        result = parse_review_decision(output)
        assert result is not None
        assert result.status == ReviewStatus.REQUEST_CHANGES
        assert result.findings_by_severity is not None
        assert result.findings_by_severity["critical"] == ["security vulnerability in auth"]
        assert result.findings_by_severity["high"] == ["missing input validation"]
        assert result.findings_by_severity["low"] == ["typo in comment"]

    def test_parse_review_decision_findings_count(self):
        """ReviewDecision.findings_count aggregates all findings."""
        from millstone.policy.schemas import parse_review_decision
        output = '''{
            "status": "REQUEST_CHANGES",
            "review": "Needs fixes",
            "summary": "Blocking issues",
            "findings": ["general issue"],
            "findings_by_severity": {
                "critical": ["critical1", "critical2"],
                "high": ["high1"]
            }
        }'''
        result = parse_review_decision(output)
        assert result is not None
        # 1 from findings + 2 critical + 1 high = 4
        assert result.findings_count == 4

    def test_parse_review_decision_severity_counts(self):
        """ReviewDecision.get_severity_counts returns correct counts."""
        from millstone.policy.schemas import parse_review_decision
        output = '''{
            "status": "REQUEST_CHANGES",
            "review": "Needs fixes",
            "summary": "Blocking issues",
            "findings_by_severity": {
                "critical": ["a", "b"],
                "high": ["c"],
                "medium": [],
                "low": [],
                "nit": ["d", "e", "f"]
            }
        }'''
        result = parse_review_decision(output)
        assert result is not None
        counts = result.get_severity_counts()
        assert counts["critical"] == 2
        assert counts["high"] == 1
        assert counts["medium"] == 0
        assert counts["low"] == 0
        assert counts["nit"] == 3

    def test_parse_sanity_result_ok(self):
        """Parses OK status from JSON."""
        from millstone.policy.schemas import SanityStatus, parse_sanity_result
        output = '{"status": "OK"}'
        result = parse_sanity_result(output)
        assert result is not None
        assert result.status == SanityStatus.OK
        assert result.should_halt is False

    def test_parse_sanity_result_halt(self):
        """Parses HALT status with reason."""
        from millstone.policy.schemas import SanityStatus, parse_sanity_result
        output = '{"status": "HALT", "reason": "Implementation is gibberish"}'
        result = parse_sanity_result(output)
        assert result is not None
        assert result.status == SanityStatus.HALT
        assert result.should_halt is True
        assert result.reason == "Implementation is gibberish"

    def test_parse_sanity_result_defaults_to_ok(self):
        """Defaults to OK when no HALT signal found."""
        from millstone.policy.schemas import SanityStatus, parse_sanity_result
        output = "The implementation looks reasonable."
        result = parse_sanity_result(output)
        assert result is not None
        assert result.status == SanityStatus.OK

    def test_parse_builder_completion_completed(self):
        """Parses completed builder signal."""
        from millstone.policy.schemas import parse_builder_completion
        output = '{"completed": true, "summary": "Added new feature", "files_changed": ["foo.py"]}'
        result = parse_builder_completion(output)
        assert result is not None
        assert result.completed is True
        assert result.summary == "Added new feature"
        assert result.files_changed == ["foo.py"]

    def test_parse_builder_completion_not_completed(self):
        """Parses incomplete builder signal."""
        from millstone.policy.schemas import parse_builder_completion
        output = '{"completed": false, "summary": "Hit an error"}'
        result = parse_builder_completion(output)
        assert result is not None
        assert result.completed is False

    def test_parse_design_review_approved(self):
        """Parses APPROVED design review."""
        from millstone.policy.schemas import DesignReviewVerdict, parse_design_review
        output = '{"verdict": "APPROVED", "strengths": ["clear scope"], "issues": [], "questions": []}'
        result = parse_design_review(output)
        assert result is not None
        assert result.verdict == DesignReviewVerdict.APPROVED
        assert result.is_approved is True
        assert result.strengths == ["clear scope"]
        assert result.issues == []

    def test_parse_design_review_needs_revision(self):
        """Parses NEEDS_REVISION design review with issues."""
        from millstone.policy.schemas import DesignReviewVerdict, parse_design_review
        output = '{"verdict": "NEEDS_REVISION", "strengths": [], "issues": ["missing tests", "scope too broad"]}'
        result = parse_design_review(output)
        assert result is not None
        assert result.verdict == DesignReviewVerdict.NEEDS_REVISION
        assert result.is_approved is False
        assert result.issues == ["missing tests", "scope too broad"]

    def test_parse_design_review_with_questions(self):
        """Parses design review with questions field."""
        from millstone.policy.schemas import parse_design_review
        output = '{"verdict": "APPROVED", "strengths": ["good"], "issues": [], "questions": ["Why this approach?"]}'
        result = parse_design_review(output)
        assert result is not None
        assert result.questions == ["Why this approach?"]

    def test_parse_design_review_from_code_block(self):
        """Parses design review from markdown code block."""
        from millstone.policy.schemas import DesignReviewVerdict, parse_design_review
        output = '''Here is my review:

```json
{"verdict": "APPROVED", "strengths": ["well designed"], "issues": []}
```

That's my assessment.'''
        result = parse_design_review(output)
        assert result is not None
        assert result.verdict == DesignReviewVerdict.APPROVED
        assert result.strengths == ["well designed"]

    def test_parse_design_review_fallback_approved(self):
        """Falls back to minimal result when only verdict keyword found."""
        from millstone.policy.schemas import DesignReviewVerdict, parse_design_review
        output = 'The design looks good. "verdict": "APPROVED" is my conclusion.'
        result = parse_design_review(output)
        assert result is not None
        assert result.verdict == DesignReviewVerdict.APPROVED
        assert result.strengths == []
        assert result.issues == []

    def test_parse_design_review_fallback_needs_revision(self):
        """Falls back to minimal result for NEEDS_REVISION keyword."""
        from millstone.policy.schemas import DesignReviewVerdict, parse_design_review
        output = 'After review, "verdict": "NEEDS_REVISION" due to missing details.'
        result = parse_design_review(output)
        assert result is not None
        assert result.verdict == DesignReviewVerdict.NEEDS_REVISION

    def test_parse_design_review_returns_none_for_empty(self):
        """Returns None for empty input."""
        from millstone.policy.schemas import parse_design_review
        assert parse_design_review("") is None
        assert parse_design_review(None) is None

    def test_parse_design_review_returns_none_for_no_verdict(self):
        """Returns None when no verdict can be found."""
        from millstone.policy.schemas import parse_design_review
        output = "This is just some random text without any verdict."
        assert parse_design_review(output) is None

    def test_get_schema_json_returns_valid_json(self):
        """get_schema_json returns valid JSON string."""
        import json

        from millstone.policy.schemas import get_schema_json
        schema_json = get_schema_json("review_decision")
        schema = json.loads(schema_json)
        assert schema["type"] == "object"
        assert "status" in schema["properties"]

    def test_get_schema_json_raises_for_unknown(self):
        """get_schema_json raises ValueError for unknown schema."""
        from millstone.policy.schemas import get_schema_json
        with pytest.raises(ValueError) as exc_info:
            get_schema_json("unknown_schema")
        assert "unknown_schema" in str(exc_info.value)


class TestOrchestratorStructuredOutput:
    """Tests for Orchestrator structured output handling."""

    def test_is_approved_returns_tuple_with_decision(self):
        """is_approved returns (bool, ReviewDecision) tuple."""
        from millstone.policy.schemas import ReviewDecision, ReviewStatus
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            orch = Orchestrator(task="test")
            try:
                approved, decision = orch.is_approved('{"status": "APPROVED", "review": "Looks good", "summary": "No blockers"}')
                assert approved is True
                assert isinstance(decision, ReviewDecision)
                assert decision.status == ReviewStatus.APPROVED
            finally:
                orch.cleanup()

    def test_is_approved_request_changes_returns_findings(self):
        """is_approved returns findings from REQUEST_CHANGES."""
        from millstone.policy.schemas import ReviewStatus
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            orch = Orchestrator(task="test")
            try:
                output = '{"status": "REQUEST_CHANGES", "review": "Needs fixes", "summary": "Blocking issues", "findings": ["fix bug"]}'
                approved, decision = orch.is_approved(output)
                assert approved is False
                assert decision.status == ReviewStatus.REQUEST_CHANGES
                assert decision.findings == ["fix bug"]
            finally:
                orch.cleanup()

    def test_is_approved_fallback_patterns_work(self):
        """is_approved returns None for prose-only responses."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            orch = Orchestrator(task="test")
            try:
                approved, decision = orch.is_approved("This looks good to me!")
                assert approved is False
                assert decision is None
            finally:
                orch.cleanup()

    def test_is_approved_none_for_unrecognized_output(self):
        """is_approved returns (False, None) for unrecognized output."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            orch = Orchestrator(task="test")
            try:
                approved, decision = orch.is_approved("I'm not sure what to say")
                assert approved is False
                assert decision is None
            finally:
                orch.cleanup()

    def test_run_agent_passes_output_schema(self):
        """run_agent passes output_schema to provider."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"status": "OK"}',
                stderr=""
            )
            orch = Orchestrator(task="test")
            try:
                orch.run_agent(
                    "check this",
                    role="sanity",
                    output_schema="sanity_check",
                )
                # Verify --json-schema was passed (for claude provider)
                calls = mock_run.call_args_list
                agent_call = [c for c in calls if c[0][0][0] == "claude"][-1]
                cmd = agent_call[0][0]
                assert "--json-schema" in cmd
            finally:
                orch.cleanup()


class TestOrchestratorCLIIntegration:
    """Tests for Orchestrator CLI provider integration."""

    def test_orchestrator_default_cli_is_claude(self):
        """Orchestrator defaults to claude CLI."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            orch = Orchestrator(task="test")
            try:
                assert orch._cli_default == "claude"
            finally:
                orch.cleanup()

    def test_orchestrator_accepts_cli_parameter(self):
        """Orchestrator accepts cli parameter."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            orch = Orchestrator(task="test", cli="codex")
            try:
                assert orch._cli_default == "codex"
            finally:
                orch.cleanup()

    def test_orchestrator_accepts_role_specific_cli(self):
        """Orchestrator accepts role-specific CLI parameters."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            orch = Orchestrator(
                task="test",
                cli="claude",
                cli_builder="codex",
                cli_reviewer="claude",
            )
            try:
                assert orch._cli_default == "claude"
                assert orch._cli_builder == "codex"
                assert orch._cli_reviewer == "claude"
            finally:
                orch.cleanup()

    def test_orchestrator_role_specific_defaults_to_main_cli(self):
        """Role-specific CLI defaults to main cli when not specified."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            orch = Orchestrator(task="test", cli="codex")
            try:
                assert orch._cli_builder == "codex"
                assert orch._cli_reviewer == "codex"
                assert orch._cli_sanity == "codex"
                assert orch._cli_analyzer == "codex"
            finally:
                orch.cleanup()

    def test_orchestrator_get_provider_returns_correct_provider(self):
        """_get_provider returns correct provider for role."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            orch = Orchestrator(
                task="test",
                cli="claude",
                cli_builder="codex",
            )
            try:
                default_provider = orch._get_provider("default")
                builder_provider = orch._get_provider("builder")
                assert isinstance(default_provider, ClaudeProvider)
                assert isinstance(builder_provider, CodexProvider)
            finally:
                orch.cleanup()

    def test_orchestrator_get_provider_author_matches_builder(self):
        """_get_provider('author') returns same provider as builder alias."""
        from millstone.runtime.orchestrator import Orchestrator

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            orch = Orchestrator(task="test", cli="claude", cli_builder="codex")
            try:
                author_provider = orch._get_provider("author")
                builder_provider = orch._get_provider("builder")
                assert author_provider is builder_provider
                assert isinstance(author_provider, CodexProvider)
            finally:
                orch.cleanup()

    def test_orchestrator_run_agent_uses_provider(self):
        """run_agent uses the correct provider."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="test output",
                stderr=""
            )
            orch = Orchestrator(task="test", cli="claude", retry_on_empty_response=True)
            try:
                output = orch.run_agent("test prompt", role="builder")
                assert "test output" in output
                # Verify claude was called
                calls = [c for c in mock_run.call_args_list if c[0][0][0] == "claude"]
                assert len(calls) > 0
            finally:
                orch.cleanup()

    def test_orchestrator_run_agent_author_dispatches_to_builder_cli(self):
        """run_agent(role='author') dispatches to builder CLI provider."""
        from millstone.runtime.orchestrator import Orchestrator

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="test output", stderr="")
            orch = Orchestrator(task="test", cli="claude", cli_builder="codex")
            try:
                output = orch.run_agent("test prompt", role="author")
                assert "test output" in output
                calls = [c for c in mock_run.call_args_list if c[0][0][0] == "codex"]
                assert len(calls) > 0
            finally:
                orch.cleanup()

    def test_orchestrator_run_claude_still_works(self):
        """run_claude (deprecated) still works as wrapper."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="legacy output",
                stderr=""
            )
            orch = Orchestrator(task="test")
            try:
                output = orch.run_claude("test prompt")
                assert "legacy output" in output
            finally:
                orch.cleanup()

    def test_run_agent_retries_on_empty_response(self):
        """run_agent retries once when response is empty."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            # First call returns empty, second call returns valid output
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),  # Empty response
                MagicMock(returncode=0, stdout="valid output", stderr=""),  # Retry succeeds
            ]
            orch = Orchestrator(task="test", cli="claude", retry_on_empty_response=True)
            try:
                output = orch.run_agent("test prompt", role="builder")
                assert "valid output" in output
                # Verify claude was called twice (original + retry)
                calls = [c for c in mock_run.call_args_list if c[0][0][0] == "claude"]
                assert len(calls) == 2
            finally:
                orch.cleanup()

    def test_run_agent_retries_on_whitespace_only_response(self):
        """run_agent retries when response is whitespace-only."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="   \n\t  ", stderr=""),  # Whitespace-only
                MagicMock(returncode=0, stdout="retry output", stderr=""),
            ]
            orch = Orchestrator(task="test", cli="claude", retry_on_empty_response=True)
            try:
                output = orch.run_agent("test prompt", role="builder")
                assert "retry output" in output
            finally:
                orch.cleanup()

    def test_run_agent_no_retry_on_valid_response(self):
        """run_agent does not retry when response has content meeting min_response_length."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            # Response must meet min_response_length (default 50 chars) to avoid retry
            valid_response = "This is a valid response that meets the minimum response length threshold."
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=valid_response,
                stderr=""
            )
            orch = Orchestrator(task="test", cli="claude", retry_on_empty_response=True)
            try:
                output = orch.run_agent("test prompt", role="builder")
                assert valid_response in output
                # Should only be called once since response was valid
                calls = [c for c in mock_run.call_args_list if c[0][0][0] == "claude"]
                assert len(calls) == 1
            finally:
                orch.cleanup()

    def test_run_agent_retries_on_missing_schema_structure(self):
        """run_agent retries when response lacks expected schema structure."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Response has content but missing expected sanity_check schema
                MagicMock(returncode=0, stdout="some text without json", stderr=""),
                # Retry returns proper format
                MagicMock(returncode=0, stdout='{"status": "OK", "reason": "test"}', stderr=""),
            ]
            orch = Orchestrator(task="test", cli="claude", retry_on_empty_response=True)
            try:
                output = orch.run_agent(
                    "test prompt",
                    role="sanity",
                    output_schema="sanity_check"
                )
                assert '"status"' in output
                assert '"OK"' in output
                # Should be called twice due to schema mismatch
                calls = [c for c in mock_run.call_args_list if c[0][0][0] == "claude"]
                assert len(calls) == 2
            finally:
                orch.cleanup()

    def test_run_agent_logs_retry_event(self):
        """run_agent logs retry_empty_response event when retrying."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="valid output", stderr=""),
            ]
            orch = Orchestrator(task="test", cli="claude", retry_on_empty_response=True)
            try:
                orch.run_agent("test prompt", role="builder")
                # Check the log file contains retry event
                log_content = orch.log_file.read_text()
                assert "empty_response_retry" in log_content
            finally:
                orch.cleanup()

    def test_run_agent_returns_halt_fallback_for_sanity_check_after_retry_fails(self):
        """run_agent returns HALT fallback when retry also returns empty for sanity_check schema."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            # Both attempts return empty responses
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            orch = Orchestrator(task="test", cli="claude", retry_on_empty_response=True)
            try:
                output = orch.run_agent("test prompt", role="sanity", output_schema="sanity_check")
                # Should return HALT fallback verdict
                assert '"status": "HALT"' in output
                assert "empty response" in output.lower()
                # Check the log file contains fallback event
                log_content = orch.log_file.read_text()
                assert "empty_response_fallback" in log_content
            finally:
                orch.cleanup()

    def test_run_agent_fallback_for_review_decision_schema(self):
        """run_agent returns REQUEST_CHANGES fallback when retry also returns empty for review_decision schema."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            # Both attempts return empty responses
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            orch = Orchestrator(task="test", cli="claude", retry_on_empty_response=True)
            try:
                output = orch.run_agent("test prompt", role="reviewer", output_schema="review_decision")
                # Should return REQUEST_CHANGES fallback verdict
                assert '"status": "REQUEST_CHANGES"' in output
                assert "empty response" in output.lower()
                assert "findings" in output
                assert '"review"' in output
                assert '"summary"' in output
                # Check the log file contains fallback event
                log_content = orch.log_file.read_text()
                assert "empty_response_fallback" in log_content
            finally:
                orch.cleanup()

    def test_run_agent_no_fallback_for_builder_completion_schema(self):
        """run_agent does NOT return fallback for builder_completion schema (no safety implications)."""
        from millstone.runtime.orchestrator import Orchestrator
        with patch("subprocess.run") as mock_run:
            # Both attempts return empty responses
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            orch = Orchestrator(task="test", cli="claude", retry_on_empty_response=True)
            try:
                output = orch.run_agent("test prompt", role="builder", output_schema="builder_completion")
                # Should return the empty output, not a fallback
                assert output == ""
                # Check the log file contains fallback event
                log_content = orch.log_file.read_text()
                assert "empty_response_fallback" in log_content
            finally:
                orch.cleanup()


class TestExtractClaudeResult:
    """Tests for extract_claude_result function."""

    def test_extracts_result_from_json_wrapper(self):
        """Extracts result field from Claude Code JSON wrapper."""
        from millstone.runtime.orchestrator import extract_claude_result
        wrapper = '{"type":"result","subtype":"success","result":"Hello world","session_id":"abc"}'
        assert extract_claude_result(wrapper) == "Hello world"

    def test_extracts_result_with_json_in_content(self):
        """Extracts result that contains JSON status."""
        import json

        from millstone.runtime.orchestrator import extract_claude_result
        wrapper = json.dumps({
            "type": "result",
            "result": 'Analysis complete.\n\n```json\n{"status": "OK"}\n```',
            "session_id": "123"
        })
        result = extract_claude_result(wrapper)
        assert '"status": "OK"' in result

    def test_returns_original_when_not_wrapper(self):
        """Returns original output when not a JSON wrapper."""
        from millstone.runtime.orchestrator import extract_claude_result
        plain = "This is just plain text output"
        assert extract_claude_result(plain) == plain

    def test_returns_original_for_other_json(self):
        """Returns original output when JSON doesn't have type=result."""
        from millstone.runtime.orchestrator import extract_claude_result
        other_json = '{"status": "OK", "reason": "test"}'
        assert extract_claude_result(other_json) == other_json

    def test_handles_empty_string(self):
        """Handles empty string gracefully."""
        from millstone.runtime.orchestrator import extract_claude_result
        assert extract_claude_result("") == ""

    def test_handles_none(self):
        """Handles None gracefully."""
        from millstone.runtime.orchestrator import extract_claude_result
        assert extract_claude_result(None) is None

    def test_handles_malformed_json(self):
        """Returns original on malformed JSON that starts with type:result."""
        from millstone.runtime.orchestrator import extract_claude_result
        malformed = '{"type":"result", broken json'
        assert extract_claude_result(malformed) == malformed

    def test_extracts_structured_output_when_result_empty(self):
        """Extracts structured_output when result is empty (used with --json-schema)."""
        import json

        from millstone.runtime.orchestrator import extract_claude_result
        # This is what Claude Code returns when using --json-schema
        wrapper = json.dumps({
            "type": "result",
            "result": "",
            "structured_output": {"status": "APPROVED", "summary": "Looks good"},
            "session_id": "abc123"
        })
        result = extract_claude_result(wrapper)
        # Should return the structured_output as JSON string
        parsed = json.loads(result)
        assert parsed["status"] == "APPROVED"
        assert parsed["summary"] == "Looks good"

    def test_prefers_structured_output_over_text_result(self):
        """Prefers structured_output over text result when both present.

        Regression test: When Claude Code returns both a text summary in 'result'
        and the actual structured data in 'structured_output', we must prefer
        structured_output. The text result is just a human-friendly summary.

        Without this fix, sanity checks fail because:
        1. Claude CLI returns {"result": "Done. Sanity check passed...", "structured_output": {"status": "OK"}}
        2. We extract the text "Done. Sanity check passed..."
        3. is_empty_response looks for '"status"' and '"OK"' in that text
        4. Not found -> retry -> fallback -> HALT
        """
        import json

        from millstone.runtime.orchestrator import extract_claude_result
        # This is what Claude Code actually returns with --json-schema
        wrapper = json.dumps({
            "type": "result",
            "result": "Done. The sanity check passed — the implementation is coherent and ready for code review.",
            "structured_output": {"status": "OK"},
            "session_id": "abc123"
        })
        result = extract_claude_result(wrapper)
        # Should return structured_output as JSON, NOT the text result
        parsed = json.loads(result)
        assert parsed["status"] == "OK"

    def test_returns_original_when_both_result_and_structured_empty(self):
        """Returns original when both result and structured_output are empty/missing."""
        import json

        from millstone.runtime.orchestrator import extract_claude_result
        wrapper = json.dumps({
            "type": "result",
            "result": "",
            "session_id": "abc123"
        })
        result = extract_claude_result(wrapper)
        # Should return original since nothing to extract
        assert result == wrapper
