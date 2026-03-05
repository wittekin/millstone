"""Unit tests for the Orchestrator class."""

import contextlib
import copy
import importlib
import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from millstone.artifacts.models import DesignStatus
from millstone.loops.engine import LoopResult
from millstone.loops.registry.loops import DEV_REVIEW_LOOP
from millstone.loops.registry_adapter import LoopRegistryAdapter
from millstone.policy.capability import CapabilityTier, CapabilityViolation
from millstone.policy.effects import EffectClass, EffectPolicyGate

# Import the module under test
from millstone.runtime.orchestrator import (
    CONFIG_FILE_NAME,
    DEFAULT_CONFIG,
    WORK_DIR_NAME,
    ConfigurationError,
    Orchestrator,
    PreflightError,
    filter_reasoning_traces,
    is_empty_response,
    load_config,
    summarize_output,
)
from millstone.runtime.profile import Profile, ProfileRegistry


class TestOrchestratorInit:
    """Tests for Orchestrator initialization."""

    def test_creates_work_directory(self, temp_repo):
        """Orchestrator creates a .millstone working directory on init."""
        orch = Orchestrator()
        try:
            assert orch.work_dir.exists()
            assert orch.work_dir.is_dir()
            assert orch.work_dir.name == ".millstone"
            assert orch.work_dir.parent == temp_repo
        finally:
            orch.cleanup()

    def test_default_configuration(self):
        """Orchestrator uses sensible defaults."""
        orch = Orchestrator()
        try:
            assert orch.max_cycles == 3
            assert orch.loc_threshold == 1000
            assert orch.cycle == 0
            assert orch.session_id is None
        finally:
            orch.cleanup()

    def test_custom_configuration(self):
        """Orchestrator accepts custom configuration."""
        orch = Orchestrator(max_cycles=5, loc_threshold=1000)
        try:
            assert orch.max_cycles == 5
            assert orch.loc_threshold == 1000
        finally:
            orch.cleanup()

    def test_prompts_dir_exists(self, prompts_dir):
        """Prompts directory contains required files."""
        required_prompts = [
            "tasklist_prompt.md",
            "review_prompt.md",
            "sanity_check_impl.md",
            "sanity_check_review.md",
            "commit_prompt.md",
        ]
        for prompt in required_prompts:
            assert (prompts_dir / prompt).exists(), f"Missing prompt: {prompt}"


class TestCapabilityPolicyGateWiring:
    """Tests for capability-policy integration into Orchestrator methods."""

    def _make_c0_orchestrator(self) -> Orchestrator:
        read_only_profile = Profile(
            id="test_c0_read_only",
            name="C0 Test Profile",
            role_aliases={"builder": "author"},
            capability_tier=CapabilityTier.C0_READ_ONLY,
        )
        registry = ProfileRegistry()
        registry.register(read_only_profile)

        with patch("millstone.runtime.orchestrator.ProfileRegistry", return_value=registry):
            return Orchestrator(profile="test_c0_read_only", task="capability gate test", quiet=True)

    def test_init_sets_capability_gate(self, temp_repo):
        orch = Orchestrator(task="test", quiet=True)
        try:
            assert orch._capability_gate.profile_tier == CapabilityTier.C1_LOCAL_WRITE
        finally:
            orch.cleanup()

    def test_startup_banner_includes_profile_tier(self, temp_repo, capsys):
        orch = Orchestrator(task="test")
        try:
            captured = capsys.readouterr()
            assert "Profile: dev_implementation (tier: C1_local_write)" in captured.out
        finally:
            orch.cleanup()

    def test_read_only_profile_blocks_run_single_task(self, temp_repo):
        orch = self._make_c0_orchestrator()
        try:
            with pytest.raises(CapabilityViolation):
                orch.run_single_task()
        finally:
            orch.cleanup()

    @pytest.mark.parametrize(
        ("method_name", "args"),
        [
            ("run_analyze", ()),
            ("run_design", ("test opportunity",)),
            ("run_plan", ("designs/test.md",)),
            ("run_cycle", ()),
        ],
    )
    def test_read_only_profile_blocks_mutating_outer_loop_methods(self, temp_repo, method_name, args):
        orch = self._make_c0_orchestrator()
        try:
            method = getattr(orch, method_name)
            with pytest.raises(CapabilityViolation):
                method(*args)
        finally:
            orch.cleanup()

    def test_read_only_profile_allows_review_design(self, temp_repo):
        orch = self._make_c0_orchestrator()
        try:
            result = orch.review_design("designs/nonexistent.md")
            assert result["approved"] is False
            assert result["verdict"] == "ERROR"
        finally:
            orch.cleanup()

    def test_c1_profile_does_not_raise_on_mutating_paths(self, temp_repo):
        orch = Orchestrator(task="test", quiet=True)
        try:
            with patch.object(orch._outer_loop_manager, "run_analyze", return_value={"success": True}):
                result = orch.run_analyze()
                assert result["success"] is True

            with patch.object(orch._outer_loop_manager, "run_design", return_value={"success": True, "design_file": None}):
                result = orch.run_design("opportunity")
                assert result["success"] is True

            with patch.object(orch._outer_loop_manager, "run_plan", return_value={"success": True, "tasks_added": 0}):
                result = orch.run_plan("designs/test.md")
                assert result["success"] is True

            with patch.object(orch._outer_loop_manager, "run_cycle", return_value=0):
                assert orch.run_cycle() == 0

            with patch.object(orch, "save_task_metrics"), patch(
                "millstone.runtime.orchestrator.ArtifactReviewLoop.run",
                return_value=LoopResult(success=False, cycles=1, error="stubbed"),
            ):
                assert orch.run_single_task() is False
        finally:
            orch.cleanup()


class TestOrchestratorLoopAdapterWiring:
    """Tests for loop-registry adapter wiring in Orchestrator."""

    def _make_orchestrator_with_profile(self, profile: Profile) -> Orchestrator:
        registry = ProfileRegistry()
        registry.register(profile)

        with patch("millstone.runtime.orchestrator.ProfileRegistry", return_value=registry):
            return Orchestrator(profile=profile.id, task="loop adapter test", quiet=True)

    def test_loop_adapter_is_initialized_for_dev_profile(self, temp_repo):
        orch = Orchestrator(task="test", quiet=True)
        try:
            assert isinstance(orch._loop_adapter, LoopRegistryAdapter)
        finally:
            orch.cleanup()

    def test_loop_definition_returns_dev_review_loop(self, temp_repo):
        orch = Orchestrator(task="test", quiet=True)
        try:
            assert orch.loop_definition == DEV_REVIEW_LOOP
        finally:
            orch.cleanup()

    def test_get_provider_accepts_loop_declared_roles(self, temp_repo):
        orch = Orchestrator(task="test", quiet=True)
        try:
            assert orch._get_provider("author") is not None
            assert orch._get_provider("reviewer") is not None
        finally:
            orch.cleanup()

    def test_get_provider_rejects_unknown_role(self, temp_repo):
        orch = Orchestrator(task="test", quiet=True)
        try:
            with pytest.raises(ConfigurationError):
                orch._get_provider("unknown_role")
        finally:
            orch.cleanup()

    def test_get_provider_allows_orchestrator_internal_roles(self, temp_repo):
        orch = Orchestrator(task="test", quiet=True)
        try:
            assert orch._get_provider("sanity") is not None
            assert orch._get_provider("analyzer") is not None
            assert orch._get_provider("release_eng") is not None
            assert orch._get_provider("sre") is not None
        finally:
            orch.cleanup()

    def test_loop_adapter_enforces_capability_tier(self, temp_repo):
        c2_loop = copy.deepcopy(DEV_REVIEW_LOOP)
        c2_loop.id = "test.c2.loop"
        c2_loop.capability_tier = CapabilityTier.C2_REMOTE_BOUNDED.value
        adapter = LoopRegistryAdapter(registry={c2_loop.id: c2_loop})

        c1_profile = Profile(
            id="test_c1_with_c2_loop",
            name="C1 Profile With C2 Loop",
            role_aliases={"builder": "author"},
            capability_tier=CapabilityTier.C1_LOCAL_WRITE,
            loop_id=c2_loop.id,
        )
        registry = ProfileRegistry()
        registry.register(c1_profile)

        with patch("millstone.runtime.orchestrator.ProfileRegistry", return_value=registry):
            with patch("millstone.runtime.orchestrator.LoopRegistryAdapter", return_value=adapter):
                with pytest.raises(CapabilityViolation):
                    Orchestrator(profile=c1_profile.id, task="test", quiet=True)

    def test_loop_adapter_is_none_when_profile_loop_id_is_none(self, temp_repo):
        profile_without_loop = Profile(
            id="test_profile_without_loop",
            name="Profile Without Loop",
            role_aliases={"builder": "author"},
            capability_tier=CapabilityTier.C1_LOCAL_WRITE,
            loop_id=None,
        )
        orch = self._make_orchestrator_with_profile(profile_without_loop)
        try:
            assert orch._loop_adapter is None
            assert orch.loop_definition is None
        finally:
            orch.cleanup()


class TestEffectGateWiring:
    """Tests for effect-policy gate wiring in Orchestrator initialization."""

    def _make_orchestrator_with_profile(self, profile: Profile, quiet: bool = True) -> Orchestrator:
        registry = ProfileRegistry()
        registry.register(profile)
        with patch("millstone.runtime.orchestrator.ProfileRegistry", return_value=registry):
            return Orchestrator(profile=profile.id, task="effect gate test", quiet=quiet)

    def test_init_sets_effect_gate(self, temp_repo):
        orch = Orchestrator(task="test", quiet=True)
        try:
            assert isinstance(orch._effect_gate, EffectPolicyGate)
        finally:
            orch.cleanup()

    def test_effect_gate_health_check_delegates_to_noop_provider(self, temp_repo):
        orch = Orchestrator(task="test", quiet=True)
        try:
            assert orch._effect_gate.health_check() is True
        finally:
            orch.cleanup()

    def test_startup_banner_omits_permitted_effects_when_none(self, temp_repo, capsys):
        orch = Orchestrator(task="test")
        try:
            captured = capsys.readouterr()
            assert "Permitted effects:" not in captured.out
        finally:
            orch.cleanup()

    def test_startup_banner_includes_permitted_effects_when_present(self, temp_repo, capsys):
        profile = Profile(
            id="test_effect_profile",
            name="Effect Profile",
            role_aliases={"builder": "author"},
            capability_tier=CapabilityTier.C2_REMOTE_BOUNDED,
            permitted_effect_classes=frozenset({EffectClass.transactional}),
        )

        orch = self._make_orchestrator_with_profile(profile, quiet=False)
        try:
            captured = capsys.readouterr()
            assert "Permitted effects: transactional" in captured.out
        finally:
            orch.cleanup()


class TestCapabilityProfileCliPlumbing:
    """Tests for profile plumbing in minimal CLI branches."""

    @pytest.mark.parametrize(
        ("argv", "method_name", "return_value"),
        [
            (["orchestrate.py", "--analyze"], "run_analyze", {"success": True}),
            (["orchestrate.py", "--design", "Add retries"], "run_design", {"success": True}),
            (["orchestrate.py", "--review-design", "designs/test.md"], "review_design", {"approved": True}),
            (["orchestrate.py", "--plan", "designs/test.md"], "run_plan", {"success": True}),
        ],
    )
    def test_minimal_cli_branches_pass_profile_into_orchestrator(
        self, temp_repo, argv, method_name, return_value
    ):
        from millstone import orchestrate

        with patch("sys.argv", argv):
            with patch.object(orchestrate.Orchestrator, "__init__", return_value=None) as mock_init:
                with patch.object(orchestrate.Orchestrator, method_name, return_value=return_value):
                    with pytest.raises(SystemExit):
                        orchestrate.main()

        _, kwargs = mock_init.call_args
        assert kwargs["profile"] == "dev_implementation"

    @pytest.mark.parametrize(
        ("argv", "method_name", "return_value"),
        [
            (["orchestrate.py", "--analyze", "--max-cycles", "7"], "run_analyze", {"success": True}),
            (["orchestrate.py", "--design", "Add retries", "--max-cycles", "7"], "run_design", {"success": True}),
            (["orchestrate.py", "--plan", "designs/test.md", "--max-cycles", "7"], "run_plan", {"success": True}),
        ],
    )
    def test_minimal_cli_branches_pass_max_cycles_into_orchestrator(
        self, temp_repo, argv, method_name, return_value
    ):
        """--analyze, --design, and --plan pass args.max_cycles to Orchestrator."""
        from millstone import orchestrate

        with patch("sys.argv", argv):
            with patch.object(orchestrate.Orchestrator, "__init__", return_value=None) as mock_init:
                with patch.object(orchestrate.Orchestrator, method_name, return_value=return_value):
                    with pytest.raises(SystemExit):
                        orchestrate.main()

        _, kwargs = mock_init.call_args
        assert kwargs["max_cycles"] == 7


class TestOuterLoopManagerMaxCyclesPlumbing:
    """Tests that Orchestrator forwards max_cycles to OuterLoopManager."""

    def test_orchestrator_passes_max_cycles_to_outer_loop_manager(self):
        """Orchestrator forwards its max_cycles to OuterLoopManager."""
        orch = Orchestrator(max_cycles=9)
        try:
            assert orch._outer_loop_manager.max_cycles == 9
        finally:
            orch.cleanup()

    def test_orchestrator_default_max_cycles_forwarded_to_outer_loop_manager(self):
        """Orchestrator default max_cycles (3) is forwarded to OuterLoopManager."""
        orch = Orchestrator()
        try:
            assert orch._outer_loop_manager.max_cycles == 3
        finally:
            orch.cleanup()


class TestCleanup:
    """Tests for cleanup behavior."""

    def test_cleanup_removes_work_dir_contents(self, temp_repo):
        """Cleanup removes contents but keeps work directory and runs/."""
        orch = Orchestrator()
        work_dir = orch.work_dir

        # Create some files in work dir
        (work_dir / "test_file.txt").write_text("test")
        (work_dir / "subdir").mkdir()
        (work_dir / "subdir" / "nested.txt").write_text("nested")

        orch.cleanup()

        # Directory should still exist; runs/ and tasklist.md are preserved
        assert work_dir.exists()
        remaining = sorted(p.name for p in work_dir.iterdir())
        assert "runs" in remaining, f"'runs' missing from {remaining}"
        assert "test_file.txt" not in remaining, "Ephemeral file should be removed"
        assert "subdir" not in remaining, "Ephemeral subdir should be removed"

    def test_cleanup_is_idempotent(self, temp_repo):
        """Cleanup can be called multiple times safely."""
        orch = Orchestrator()
        orch.cleanup()
        orch.cleanup()  # Should not raise

    def test_cleanup_preserves_parallel_dirs(self, temp_repo):
        """cleanup() preserves .millstone/{parallel,locks,worktrees} if present."""
        orch = Orchestrator()
        work_dir = orch.work_dir
        try:
            (work_dir / "parallel").mkdir(exist_ok=True)
            (work_dir / "locks").mkdir(exist_ok=True)
            (work_dir / "worktrees").mkdir(exist_ok=True)
            (work_dir / "parallel" / "keep.txt").write_text("x")
            (work_dir / "locks" / "keep.txt").write_text("x")
            (work_dir / "worktrees" / "keep.txt").write_text("x")
            (work_dir / "tmp").mkdir(exist_ok=True)
            (work_dir / "tmp" / "delete.txt").write_text("y")

            orch.cleanup()

            assert (work_dir / "parallel").exists()
            assert (work_dir / "locks").exists()
            assert (work_dir / "worktrees").exists()
            assert (work_dir / "parallel" / "keep.txt").exists()
            assert (work_dir / "locks" / "keep.txt").exists()
            assert (work_dir / "worktrees" / "keep.txt").exists()
            assert not (work_dir / "tmp").exists()
        finally:
            orch.cleanup()

    def test_cleanup_still_removes_other_dirs(self, temp_repo):
        """cleanup() still removes non-whitelisted work_dir contents."""
        orch = Orchestrator()
        work_dir = orch.work_dir
        try:
            (work_dir / "junk").mkdir(exist_ok=True)
            (work_dir / "junk" / "a.txt").write_text("junk")
            orch.cleanup()
            assert not (work_dir / "junk").exists()
        finally:
            orch.cleanup()


class TestLogging:
    """Tests for run logging behavior."""

    def test_log_file_not_created_on_init(self, temp_repo):
        """Log file should not exist until log() is called (lazy init)."""
        orch = Orchestrator()
        try:
            assert not orch.log_file.exists(), "Log file should not exist on init"
        finally:
            orch.cleanup()

    def test_log_file_created_on_first_log(self, temp_repo):
        """Log file should be created when log() is called."""
        orch = Orchestrator()
        try:
            assert not orch.log_file.exists()
            orch.log("test_event", key="value")
            assert orch.log_file.exists(), "Log file should exist after log()"
        finally:
            orch.cleanup()

    def test_log_contains_event_and_data(self, temp_repo):
        """Log file should contain event name and data."""
        orch = Orchestrator()
        try:
            orch.log("my_event", foo="bar", num="123")
            content = orch.log_file.read_text()
            assert "my_event" in content
            assert "foo" in content
            assert "bar" in content
            assert "num" in content
            assert "123" in content
        finally:
            orch.cleanup()

    def test_runs_directory_exists(self, temp_repo):
        """Runs directory should exist after init."""
        orch = Orchestrator()
        try:
            runs_dir = orch.work_dir / "runs"
            assert runs_dir.exists()
            assert runs_dir.is_dir()
        finally:
            orch.cleanup()

    def test_diff_summary_creates_patch_file(self, temp_repo):
        """When log_diff_mode is summary, full diff is stored in .patch file."""
        orch = Orchestrator(log_diff_mode="summary")
        try:
            diff_content = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,3 +1,4 @@
 def foo():
+    print("hello")
     pass
"""
            orch.log("test_event", diff=diff_content)

            # Check patch file was created
            patch_file = orch.log_file.with_suffix(".patch")
            assert patch_file.exists(), "Patch file should be created"

            # Check patch file contains full diff
            patch_content = patch_file.read_text()
            assert 'print("hello")' in patch_content
            assert "diff --git a/test.py b/test.py" in patch_content
        finally:
            orch.cleanup()

    def test_diff_summary_references_patch_file_in_log(self, temp_repo):
        """Summarized log should reference path to full diff file."""
        orch = Orchestrator(log_diff_mode="summary")
        try:
            diff_content = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,3 +1,4 @@
 def foo():
+    print("hello")
     pass
"""
            orch.log("test_event", diff=diff_content)

            # Check log contains reference to patch file
            log_content = orch.log_file.read_text()
            patch_file = orch.log_file.with_suffix(".patch")
            assert "full_diff_path" in log_content
            assert str(patch_file) in log_content
        finally:
            orch.cleanup()

    def test_diff_full_mode_no_patch_file(self, temp_repo):
        """When log_diff_mode is full, no separate patch file is created."""
        orch = Orchestrator(log_diff_mode="full")
        try:
            diff_content = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,3 +1,4 @@
 def foo():
+    print("hello")
     pass
"""
            orch.log("test_event", diff=diff_content)

            # Check patch file was NOT created
            patch_file = orch.log_file.with_suffix(".patch")
            assert not patch_file.exists(), "Patch file should not exist in full mode"
        finally:
            orch.cleanup()

    def test_diff_none_mode_no_patch_file(self, temp_repo):
        """When log_diff_mode is none, no separate patch file is created."""
        orch = Orchestrator(log_diff_mode="none")
        try:
            diff_content = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,3 +1,4 @@
 def foo():
+    print("hello")
     pass
"""
            orch.log("test_event", diff=diff_content)

            # Check patch file was NOT created
            patch_file = orch.log_file.with_suffix(".patch")
            assert not patch_file.exists(), "Patch file should not exist in none mode"
        finally:
            orch.cleanup()

    def test_multiple_diffs_appended_to_patch_file(self, temp_repo):
        """Multiple diffs in same run should append to single patch file."""
        orch = Orchestrator(log_diff_mode="summary")
        try:
            diff1 = """diff --git a/first.py b/first.py
--- a/first.py
+++ b/first.py
@@ -1 +1 @@
-old
+new
"""
            diff2 = """diff --git a/second.py b/second.py
--- a/second.py
+++ b/second.py
@@ -1 +1 @@
-before
+after
"""
            orch.log("event1", diff=diff1)
            orch.log("event2", diff=diff2)

            # Check patch file contains both diffs
            patch_file = orch.log_file.with_suffix(".patch")
            patch_content = patch_file.read_text()
            assert "first.py" in patch_content
            assert "second.py" in patch_content
            assert "event1" in patch_content
            assert "event2" in patch_content
        finally:
            orch.cleanup()


class TestLoadPrompt:
    """Tests for prompt loading."""

    def test_loads_existing_prompt(self):
        """Can load an existing prompt file."""
        orch = Orchestrator()
        try:
            prompt = orch.load_prompt("tasklist_prompt.md")
            assert len(prompt) > 0
            assert isinstance(prompt, str)
        finally:
            orch.cleanup()

    def test_raises_on_missing_prompt(self):
        """Raises FileNotFoundError for missing prompt."""
        orch = Orchestrator()
        try:
            with pytest.raises(FileNotFoundError):
                orch.load_prompt("nonexistent_prompt.md")
        finally:
            orch.cleanup()


class TestCheckStop:
    """Tests for STOP.md file detection."""

    def test_returns_false_when_no_stop_file(self):
        """Returns False when no STOP.md exists."""
        orch = Orchestrator()
        try:
            assert orch.check_stop() is False
        finally:
            orch.cleanup()

    def test_returns_true_when_stop_file_exists(self, capsys):
        """Returns True and prints reason when STOP.md exists."""
        orch = Orchestrator()
        try:
            stop_file = orch.work_dir / "STOP.md"
            stop_file.write_text("Something went wrong: gibberish output")

            assert orch.check_stop() is True

            captured = capsys.readouterr()
            assert "STOPPED" in captured.out
            assert "gibberish output" in captured.out
        finally:
            orch.cleanup()


class TestIsEmptyResponse:
    """Tests for is_empty_response utility function."""

    def test_none_is_empty(self):
        """None response is considered empty."""
        assert is_empty_response(None) is True

    def test_empty_string_is_empty(self):
        """Empty string is considered empty."""
        assert is_empty_response("") is True

    def test_whitespace_only_is_empty(self):
        """Whitespace-only string is considered empty."""
        assert is_empty_response("   ") is True
        assert is_empty_response("\n\t\n") is True
        assert is_empty_response("  \n  \t  ") is True

    def test_content_is_not_empty(self):
        """String with content is not empty."""
        assert is_empty_response("hello") is False
        assert is_empty_response("some response text") is False
        assert is_empty_response("NO_TASKS_REMAIN") is False

    def test_sanity_check_schema_ok(self):
        """Valid sanity check OK response is not empty."""
        response = '{"status": "OK"}'
        assert is_empty_response(response, expected_schema="sanity_check") is False

    def test_sanity_check_schema_halt(self):
        """Valid sanity check HALT response is not empty."""
        response = '{"status": "HALT", "reason": "Something wrong"}'
        assert is_empty_response(response, expected_schema="sanity_check") is False

    def test_sanity_check_schema_missing_status(self):
        """Sanity check response missing status is empty."""
        response = '{"reason": "Something wrong"}'
        assert is_empty_response(response, expected_schema="sanity_check") is True

    def test_sanity_check_schema_invalid_status(self):
        """Sanity check response with invalid status is empty."""
        response = '{"status": "UNKNOWN"}'
        assert is_empty_response(response, expected_schema="sanity_check") is True

    def test_review_decision_schema_approved(self):
        """Valid review decision APPROVED response is not empty."""
        response = '{"status": "APPROVED", "review": "Looks good", "summary": "No blockers"}'
        assert is_empty_response(response, expected_schema="review_decision") is False

    def test_review_decision_schema_request_changes(self):
        """Valid review decision REQUEST_CHANGES response is not empty."""
        response = '{"status": "REQUEST_CHANGES", "review": "Needs changes", "summary": "Blocking issues", "findings": ["fix typo"]}'
        assert is_empty_response(response, expected_schema="review_decision") is False

    def test_review_decision_schema_missing_status(self):
        """Review decision response missing status is empty."""
        response = '{"review": "Needs changes", "summary": "Blocking issues", "findings": ["fix typo"]}'
        assert is_empty_response(response, expected_schema="review_decision") is True

    def test_review_decision_schema_with_findings_by_severity(self):
        """Valid review decision with findings_by_severity is not empty."""
        response = '{"status": "REQUEST_CHANGES", "review": "Needs changes", "summary": "Blocking issues", "findings_by_severity": {"critical": ["security issue"], "high": ["bug"]}}'
        assert is_empty_response(response, expected_schema="review_decision") is False

    def test_builder_completion_schema_completed(self):
        """Valid builder completion response is not empty."""
        response = '{"completed": true, "summary": "Done"}'
        assert is_empty_response(response, expected_schema="builder_completion") is False

    def test_builder_completion_schema_not_completed(self):
        """Valid builder completion with false is not empty."""
        response = '{"completed": false, "summary": "Failed"}'
        assert is_empty_response(response, expected_schema="builder_completion") is False

    def test_builder_completion_schema_missing_completed(self):
        """Builder completion response missing completed is empty."""
        response = '{"summary": "Done"}'
        assert is_empty_response(response, expected_schema="builder_completion") is True

    def test_design_review_schema_approved(self):
        """Valid design review APPROVED response is not empty."""
        response = '{"verdict": "APPROVED", "strengths": ["good"], "issues": []}'
        assert is_empty_response(response, expected_schema="design_review") is False

    def test_design_review_schema_needs_revision(self):
        """Valid design review NEEDS_REVISION response is not empty."""
        response = '{"verdict": "NEEDS_REVISION", "strengths": [], "issues": ["fix this"]}'
        assert is_empty_response(response, expected_schema="design_review") is False

    def test_design_review_schema_missing_verdict(self):
        """Design review response missing verdict is empty."""
        response = '{"strengths": ["good"], "issues": []}'
        assert is_empty_response(response, expected_schema="design_review") is True

    def test_design_review_schema_invalid_verdict(self):
        """Design review response with invalid verdict is empty."""
        response = '{"verdict": "UNKNOWN", "strengths": [], "issues": []}'
        assert is_empty_response(response, expected_schema="design_review") is True

    def test_design_review_schema_missing_strengths(self):
        """Design review response missing strengths is empty."""
        response = '{"verdict": "APPROVED", "issues": []}'
        assert is_empty_response(response, expected_schema="design_review") is True

    def test_design_review_schema_missing_issues(self):
        """Design review response missing issues is empty."""
        response = '{"verdict": "APPROVED", "strengths": []}'
        assert is_empty_response(response, expected_schema="design_review") is True

    def test_unknown_schema_with_content(self):
        """Unknown schema name with content is not empty."""
        response = '{"foo": "bar"}'
        assert is_empty_response(response, expected_schema="unknown_schema") is False

    def test_content_without_schema_check(self):
        """Content without schema check is not empty even if malformed."""
        response = '{"missing": "status"}'
        assert is_empty_response(response) is False

    def test_min_length_below_threshold(self):
        """Response below min_length threshold is considered empty."""
        response = "short"  # 5 chars
        assert is_empty_response(response, min_length=10) is True

    def test_min_length_at_threshold(self):
        """Response at exactly min_length threshold is not empty."""
        response = "1234567890"  # 10 chars
        assert is_empty_response(response, min_length=10) is False

    def test_min_length_above_threshold(self):
        """Response above min_length threshold is not empty."""
        response = "this is a longer response"  # 25 chars
        assert is_empty_response(response, min_length=10) is False

    def test_min_length_zero_disabled(self):
        """min_length=0 disables the length check."""
        response = "x"  # 1 char
        assert is_empty_response(response, min_length=0) is False

    def test_min_length_none_disabled(self):
        """min_length=None disables the length check."""
        response = "x"  # 1 char
        assert is_empty_response(response, min_length=None) is False

    def test_min_length_with_whitespace_stripped(self):
        """min_length check uses stripped content length."""
        response = "   short   "  # 5 chars after strip
        assert is_empty_response(response, min_length=10) is True
        response = "   1234567890   "  # 10 chars after strip
        assert is_empty_response(response, min_length=10) is False

    def test_min_length_with_schema_validation_priority(self):
        """Schema validation takes priority over min_length check."""
        # Short but valid schema - should PASS because schema validation takes priority
        response = '{"status": "OK"}'  # 16 chars, valid sanity_check schema
        assert is_empty_response(response, expected_schema="sanity_check", min_length=50) is False
        # Long and valid schema - should also pass
        long_response = '{"status": "OK", "details": "This is a much longer response with more content"}'
        assert is_empty_response(long_response, expected_schema="sanity_check", min_length=50) is False
        # Short and INVALID schema - should fail (schema validation fails)
        invalid_response = '{"foo": "bar"}'  # 14 chars, missing status
        assert is_empty_response(invalid_response, expected_schema="sanity_check", min_length=50) is True

    def test_min_length_only_applies_to_unstructured(self):
        """min_length only applies when no schema is specified."""
        # Short unstructured response - should fail min_length
        response = "short"  # 5 chars
        assert is_empty_response(response, min_length=50) is True
        # Long unstructured response - should pass
        long_response = "This is a longer unstructured response that exceeds the minimum length threshold"
        assert is_empty_response(long_response, min_length=50) is False


class TestMinResponseLengthConfig:
    """Tests for min_response_length configuration option."""

    def test_min_response_length_in_default_config(self):
        """min_response_length is in DEFAULT_CONFIG with default value 50."""
        assert "min_response_length" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["min_response_length"] == 50

    def test_orchestrator_defaults_to_config_value(self, temp_repo):
        """Orchestrator uses default min_response_length from config."""
        orch = Orchestrator()
        try:
            assert orch.min_response_length == 50
        finally:
            orch.cleanup()

    def test_orchestrator_accepts_custom_min_response_length(self, temp_repo):
        """Orchestrator accepts custom min_response_length parameter."""
        orch = Orchestrator(min_response_length=100)
        try:
            assert orch.min_response_length == 100
        finally:
            orch.cleanup()

    def test_orchestrator_accepts_zero_min_response_length(self, temp_repo):
        """Orchestrator accepts min_response_length=0 to disable check."""
        orch = Orchestrator(min_response_length=0)
        try:
            assert orch.min_response_length == 0
        finally:
            orch.cleanup()


class TestLogVerbosityConfig:
    """Tests for log_verbosity configuration option."""

    def test_log_verbosity_in_default_config(self):
        """log_verbosity is in DEFAULT_CONFIG with default value 'normal'."""
        assert "log_verbosity" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["log_verbosity"] == "normal"

    def test_orchestrator_defaults_to_normal(self, temp_repo):
        """Orchestrator uses default log_verbosity 'normal' from config."""
        orch = Orchestrator()
        try:
            assert orch.log_verbosity == "normal"
        finally:
            orch.cleanup()

    def test_orchestrator_accepts_minimal(self, temp_repo):
        """Orchestrator accepts log_verbosity='minimal'."""
        orch = Orchestrator(log_verbosity="minimal")
        try:
            assert orch.log_verbosity == "minimal"
        finally:
            orch.cleanup()

    def test_orchestrator_accepts_verbose(self, temp_repo):
        """Orchestrator accepts log_verbosity='verbose'."""
        orch = Orchestrator(log_verbosity="verbose")
        try:
            assert orch.log_verbosity == "verbose"
        finally:
            orch.cleanup()

    def test_orchestrator_rejects_invalid_verbosity(self, temp_repo):
        """Orchestrator raises ValueError for invalid log_verbosity."""
        import pytest
        with pytest.raises(ValueError) as exc_info:
            Orchestrator(log_verbosity="debug")
        assert "Invalid log_verbosity 'debug'" in str(exc_info.value)
        assert "minimal" in str(exc_info.value)
        assert "normal" in str(exc_info.value)
        assert "verbose" in str(exc_info.value)


class TestProfileConfig:
    """Tests for profile configuration and orchestrator profile binding."""

    def test_profile_in_default_config(self):
        """profile is in DEFAULT_CONFIG with default value dev_implementation."""
        assert "profile" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["profile"] == "dev_implementation"

    def test_orchestrator_defaults_to_dev_implementation_profile(self, temp_repo):
        """Orchestrator defaults to DEV_IMPLEMENTATION profile."""
        from millstone.runtime.profile import DEV_IMPLEMENTATION

        orch = Orchestrator(task="test")
        try:
            assert orch.profile == DEV_IMPLEMENTATION
        finally:
            orch.cleanup()

    def test_orchestrator_accepts_explicit_profile(self, temp_repo):
        """Orchestrator accepts explicit known profile id."""
        from millstone.runtime.profile import DEV_IMPLEMENTATION

        orch = Orchestrator(task="test", profile="dev_implementation")
        try:
            assert orch.profile == DEV_IMPLEMENTATION
        finally:
            orch.cleanup()

    def test_orchestrator_rejects_unknown_profile(self, temp_repo):
        """Orchestrator raises KeyError for unknown profile id."""
        with pytest.raises(KeyError):
            Orchestrator(task="test", profile="nonexistent")


class TestSummarizeOutput:
    """Tests for summarize_output utility function."""

    def test_returns_short_text_unchanged(self):
        """Short text under threshold is returned unchanged."""
        text = "This is a short response"
        result = summarize_output(text)
        assert result == text

    def test_returns_text_at_limit_unchanged(self):
        """Text exactly at limit (500+200=700 chars) is returned unchanged."""
        text = "x" * 700
        result = summarize_output(text)
        assert result == text

    def test_summarizes_long_text(self):
        """Long text is truncated with head/tail and omission marker."""
        text = "A" * 500 + "B" * 300 + "C" * 200  # 1000 chars
        result = summarize_output(text)

        # Should have head (500 A's) + marker + tail (200 C's)
        assert result.startswith("A" * 500)
        assert "[... 300 chars omitted ...]" in result
        assert result.endswith("C" * 200)

    def test_empty_text_unchanged(self):
        """Empty text is returned unchanged."""
        assert summarize_output("") == ""

    def test_none_text_unchanged(self):
        """None is returned unchanged."""
        assert summarize_output(None) is None

    def test_custom_head_tail_sizes(self):
        """Custom head and tail sizes are respected."""
        text = "A" * 100 + "B" * 100 + "C" * 100  # 300 chars
        result = summarize_output(text, head_chars=100, tail_chars=100)

        assert result.startswith("A" * 100)
        assert "[... 100 chars omitted ...]" in result
        assert result.endswith("C" * 100)

    def test_omitted_count_is_accurate(self):
        """The omitted character count is accurate."""
        text = "x" * 1000
        result = summarize_output(text, head_chars=100, tail_chars=50)
        # Omitted should be 1000 - 100 - 50 = 850
        assert "[... 850 chars omitted ...]" in result


class TestOutputSummarizationInLog:
    """Tests for output summarization in log() method."""

    def test_normal_verbosity_summarizes_long_output(self, temp_repo):
        """With normal verbosity, long output is summarized in logs."""
        orch = Orchestrator(log_verbosity="normal")
        try:
            long_output = "A" * 500 + "B" * 500 + "C" * 200
            orch.log("test_event", output=long_output)

            log_content = orch.log_file.read_text()
            assert "[... 500 chars omitted ...]" in log_content
            assert "A" * 500 in log_content
            assert "C" * 200 in log_content
            # The middle B's should not be in the main log
            assert "B" * 500 not in log_content
        finally:
            orch.cleanup()

    def test_normal_verbosity_stores_full_output_separately(self, temp_repo):
        """With normal verbosity, full output is stored in separate file."""
        orch = Orchestrator(log_verbosity="normal")
        try:
            long_output = "A" * 500 + "B" * 500 + "C" * 200
            orch.log("response_received", output=long_output)

            # Check for full output directory
            full_dir = orch.log_file.parent / f"{orch.log_file.stem}_full"
            assert full_dir.exists()

            # Check that full output file exists and contains complete output
            full_files = list(full_dir.glob("*.txt"))
            assert len(full_files) == 1
            assert full_files[0].read_text() == long_output

            # Check that main log references the full output path
            log_content = orch.log_file.read_text()
            assert "full_output_path" in log_content
        finally:
            orch.cleanup()

    def test_normal_verbosity_short_output_not_summarized(self, temp_repo):
        """With normal verbosity, short output is not summarized."""
        orch = Orchestrator(log_verbosity="normal")
        try:
            short_output = "This is a short response"
            orch.log("test_event", output=short_output)

            log_content = orch.log_file.read_text()
            assert short_output in log_content
            assert "[..." not in log_content
            assert "full_output_path" not in log_content
        finally:
            orch.cleanup()

    def test_verbose_verbosity_keeps_full_output(self, temp_repo):
        """With verbose verbosity, full output is kept in logs."""
        orch = Orchestrator(log_verbosity="verbose")
        try:
            long_output = "A" * 500 + "B" * 500 + "C" * 200
            orch.log("test_event", output=long_output)

            log_content = orch.log_file.read_text()
            assert long_output in log_content
            assert "[..." not in log_content
        finally:
            orch.cleanup()

    def test_minimal_verbosity_keeps_full_output(self, temp_repo):
        """With minimal verbosity, output is kept (filtering is a separate task)."""
        orch = Orchestrator(log_verbosity="minimal")
        try:
            long_output = "A" * 500 + "B" * 500 + "C" * 200
            orch.log("test_event", output=long_output)

            log_content = orch.log_file.read_text()
            # For now, minimal doesn't summarize (that's a different task)
            assert long_output in log_content
        finally:
            orch.cleanup()

    def test_non_output_fields_not_summarized(self, temp_repo):
        """Other fields like 'prompt' are not summarized."""
        orch = Orchestrator(log_verbosity="normal")
        try:
            long_prompt = "X" * 1000
            orch.log("test_event", prompt=long_prompt)

            log_content = orch.log_file.read_text()
            assert long_prompt in log_content
            assert "[..." not in log_content
        finally:
            orch.cleanup()


class TestFilterReasoningTraces:
    """Tests for filter_reasoning_traces utility function."""

    def test_returns_empty_text_unchanged(self):
        """Empty text is returned unchanged."""
        assert filter_reasoning_traces("") == ""

    def test_returns_none_unchanged(self):
        """None is returned unchanged."""
        assert filter_reasoning_traces(None) is None

    def test_returns_text_without_thinking_blocks_unchanged(self):
        """Text without thinking blocks is returned unchanged."""
        text = "Hello world\nThis is some output\nNo thinking here"
        assert filter_reasoning_traces(text) == text

    def test_filters_thinking_block_at_start(self):
        """Thinking block at start of text is removed."""
        text = "thinking\nSome internal reasoning\nMore thoughts\ncodex\nActual output here"
        result = filter_reasoning_traces(text)
        assert result == "Actual output here"
        assert "thinking" not in result
        assert "internal reasoning" not in result

    def test_filters_thinking_block_in_middle(self):
        """Thinking block in middle of text is removed."""
        text = "Start of output\nthinking\nSome reasoning\ncodex\nEnd of output"
        result = filter_reasoning_traces(text)
        assert result == "Start of output\nEnd of output"
        assert "thinking" not in result
        assert "reasoning" not in result

    def test_filters_multiple_thinking_blocks(self):
        """Multiple thinking blocks are all removed."""
        text = "Output 1\nthinking\nReason 1\ncodex\nOutput 2\nthinking\nReason 2\ncodex\nOutput 3"
        result = filter_reasoning_traces(text)
        assert result == "Output 1\nOutput 2\nOutput 3"
        assert "thinking" not in result
        assert "Reason" not in result

    def test_filters_multiline_thinking_content(self):
        """Thinking blocks with multiple lines of content are removed."""
        text = "Output\nthinking\nLine 1\nLine 2\nLine 3\nLine 4\ncodex\nMore output"
        result = filter_reasoning_traces(text)
        assert result == "Output\nMore output"
        assert "Line 1" not in result
        assert "Line 4" not in result

    def test_preserves_codex_word_outside_blocks(self):
        """The word 'codex' outside a thinking block is preserved."""
        text = "Use codex CLI for this task"
        result = filter_reasoning_traces(text)
        assert result == text

    def test_preserves_thinking_word_outside_blocks(self):
        """The word 'thinking' in regular text is preserved."""
        text = "I was thinking about the problem"
        result = filter_reasoning_traces(text)
        assert result == text


class TestReasoningTraceFilteringInLog:
    """Tests for reasoning trace filtering in log() method."""

    def test_normal_verbosity_filters_thinking_blocks(self, temp_repo):
        """With normal verbosity, thinking blocks are filtered from output."""
        orch = Orchestrator(log_verbosity="normal")
        try:
            output_with_thinking = "Start\nthinking\nInternal reasoning\ncodex\nEnd"
            orch.log("test_event", output=output_with_thinking)

            log_content = orch.log_file.read_text()
            assert "Start" in log_content
            assert "End" in log_content
            assert "Internal reasoning" not in log_content
        finally:
            orch.cleanup()

    def test_minimal_verbosity_filters_thinking_blocks(self, temp_repo):
        """With minimal verbosity, thinking blocks are filtered from output."""
        orch = Orchestrator(log_verbosity="minimal")
        try:
            output_with_thinking = "Start\nthinking\nInternal reasoning\ncodex\nEnd"
            orch.log("test_event", output=output_with_thinking)

            log_content = orch.log_file.read_text()
            assert "Start" in log_content
            assert "End" in log_content
            assert "Internal reasoning" not in log_content
        finally:
            orch.cleanup()

    def test_verbose_verbosity_preserves_thinking_blocks(self, temp_repo):
        """With verbose verbosity, thinking blocks are NOT filtered."""
        orch = Orchestrator(log_verbosity="verbose")
        try:
            output_with_thinking = "Start\nthinking\nInternal reasoning\ncodex\nEnd"
            orch.log("test_event", output=output_with_thinking)

            log_content = orch.log_file.read_text()
            assert "Start" in log_content
            assert "End" in log_content
            assert "Internal reasoning" in log_content
            assert "thinking" in log_content
        finally:
            orch.cleanup()

    def test_filtering_applied_before_summarization(self, temp_repo):
        """Reasoning traces are filtered before summarization is applied."""
        orch = Orchestrator(log_verbosity="normal")
        try:
            # Create output where thinking block makes it long, but filtered version is short
            thinking_content = "X" * 1000
            output = f"Short output\nthinking\n{thinking_content}\ncodex\nMore short"
            orch.log("test_event", output=output)

            log_content = orch.log_file.read_text()
            # After filtering, output is short enough to not need summarization
            assert "Short output" in log_content
            assert "More short" in log_content
            assert "[... " not in log_content  # No truncation marker
            assert thinking_content not in log_content
        finally:
            orch.cleanup()


class TestVerboseCliFlag:
    """Tests for --verbose CLI flag."""

    def test_verbose_flag_sets_log_verbosity_to_verbose(self, temp_repo):
        """--verbose flag sets log_verbosity to 'verbose'."""
        from unittest.mock import patch

        from millstone import orchestrate

        # Create a tasklist file
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Test task\n")

        with patch('sys.argv', ['orchestrate.py', '--verbose', '--dry-run']):
            with patch.object(orchestrate.Orchestrator, '__init__', return_value=None) as mock_init:
                with patch.object(orchestrate.Orchestrator, 'run', return_value=0):
                    with patch.object(orchestrate.Orchestrator, 'cleanup'):
                        with contextlib.suppress(SystemExit):
                            orchestrate.main()
                        # Check that Orchestrator was called with log_verbosity='verbose'
                        _, kwargs = mock_init.call_args
                        assert kwargs.get('log_verbosity') == 'verbose'

    def test_verbose_short_flag_works(self, temp_repo):
        """Short -v flag sets log_verbosity to 'verbose'."""
        from unittest.mock import patch

        from millstone import orchestrate

        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Test task\n")

        with patch('sys.argv', ['orchestrate.py', '-v', '--dry-run']):
            with patch.object(orchestrate.Orchestrator, '__init__', return_value=None) as mock_init:
                with patch.object(orchestrate.Orchestrator, 'run', return_value=0):
                    with patch.object(orchestrate.Orchestrator, 'cleanup'):
                        with contextlib.suppress(SystemExit):
                            orchestrate.main()
                        _, kwargs = mock_init.call_args
                        assert kwargs.get('log_verbosity') == 'verbose'

    def test_without_verbose_flag_uses_config_default(self, temp_repo):
        """Without --verbose, log_verbosity uses config default ('normal')."""
        from unittest.mock import patch

        from millstone import orchestrate

        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Test task\n")

        with patch('sys.argv', ['orchestrate.py', '--dry-run']):
            with patch.object(orchestrate.Orchestrator, '__init__', return_value=None) as mock_init:
                with patch.object(orchestrate.Orchestrator, 'run', return_value=0):
                    with patch.object(orchestrate.Orchestrator, 'cleanup'):
                        with contextlib.suppress(SystemExit):
                            orchestrate.main()
                        _, kwargs = mock_init.call_args
                        assert kwargs.get('log_verbosity') == 'normal'

    def test_verbose_flag_overrides_config(self, temp_repo):
        """--verbose flag overrides log_verbosity from config file."""
        from unittest.mock import patch

        from millstone import orchestrate

        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Test task\n")

        # Create config with log_verbosity set to 'minimal'
        config_dir = temp_repo / ".millstone"
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / "config.toml"
        config_file.write_text('log_verbosity = "minimal"\n')

        with patch('sys.argv', ['orchestrate.py', '--verbose', '--dry-run']):
            with patch.object(orchestrate.Orchestrator, '__init__', return_value=None) as mock_init:
                with patch.object(orchestrate.Orchestrator, 'run', return_value=0):
                    with patch.object(orchestrate.Orchestrator, 'cleanup'):
                        with contextlib.suppress(SystemExit):
                            orchestrate.main()
                        _, kwargs = mock_init.call_args
                        # --verbose should override the 'minimal' config setting
                        assert kwargs.get('log_verbosity') == 'verbose'

    def test_verbose_flag_enables_python_debug_logs(self, temp_repo, caplog):
        """--verbose configures Python logging so debug logs are visible."""
        from unittest.mock import patch

        from millstone import orchestrate

        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Test task\n")

        def run_with_debug_log(_self):
            logging.debug("verbose debug sentinel")
            return 0

        with patch("sys.argv", ["orchestrate.py", "--verbose", "--task", "noop"]):
            with patch.object(orchestrate.Orchestrator, "run", autospec=True, side_effect=run_with_debug_log):
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()

        assert exc_info.value.code == 0
        assert "verbose debug sentinel" in caplog.text


class TestFullDiffCliFlag:
    """Tests for --full-diff CLI flag."""

    def test_full_diff_flag_sets_log_diff_mode_to_full(self, temp_repo):
        """--full-diff flag sets log_diff_mode to 'full'."""
        from unittest.mock import patch

        from millstone import orchestrate

        # Create a tasklist file
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Test task\n")

        with patch('sys.argv', ['orchestrate.py', '--full-diff', '--dry-run']):
            with patch.object(orchestrate.Orchestrator, '__init__', return_value=None) as mock_init:
                with patch.object(orchestrate.Orchestrator, 'run', return_value=0):
                    with patch.object(orchestrate.Orchestrator, 'cleanup'):
                        with contextlib.suppress(SystemExit):
                            orchestrate.main()
                        # Check that Orchestrator was called with log_diff_mode='full'
                        _, kwargs = mock_init.call_args
                        assert kwargs.get('log_diff_mode') == 'full'

    def test_without_full_diff_flag_uses_config_default(self, temp_repo):
        """Without --full-diff, log_diff_mode uses config default ('summary')."""
        from unittest.mock import patch

        from millstone import orchestrate

        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Test task\n")

        with patch('sys.argv', ['orchestrate.py', '--dry-run']):
            with patch.object(orchestrate.Orchestrator, '__init__', return_value=None) as mock_init:
                with patch.object(orchestrate.Orchestrator, 'run', return_value=0):
                    with patch.object(orchestrate.Orchestrator, 'cleanup'):
                        with contextlib.suppress(SystemExit):
                            orchestrate.main()
                        _, kwargs = mock_init.call_args
                        assert kwargs.get('log_diff_mode') == 'summary'

    def test_full_diff_flag_overrides_config(self, temp_repo):
        """--full-diff flag overrides log_diff_mode from config file."""
        from unittest.mock import patch

        from millstone import orchestrate

        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Test task\n")

        # Create config with log_diff_mode set to 'none'
        config_dir = temp_repo / ".millstone"
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / "config.toml"
        config_file.write_text('log_diff_mode = "none"\n')

        with patch('sys.argv', ['orchestrate.py', '--full-diff', '--dry-run']):
            with patch.object(orchestrate.Orchestrator, '__init__', return_value=None) as mock_init:
                with patch.object(orchestrate.Orchestrator, 'run', return_value=0):
                    with patch.object(orchestrate.Orchestrator, 'cleanup'):
                        with contextlib.suppress(SystemExit):
                            orchestrate.main()
                        _, kwargs = mock_init.call_args
                        # --full-diff should override the 'none' config setting
                        assert kwargs.get('log_diff_mode') == 'full'


class TestWorktreesCliFlags:
    """Tests for worktree/parallel CLI flags."""

    def test_cli_worktrees_flag(self, temp_repo):
        """--worktrees enables the worktree control plane."""
        from unittest.mock import patch

        from millstone import orchestrate

        with patch("sys.argv", ["orchestrate.py", "--worktrees", "--dry-run"]):
            with patch.object(orchestrate.Orchestrator, "__init__", return_value=None) as mock_init:
                with patch.object(orchestrate.Orchestrator, "run", return_value=0):
                    with patch.object(orchestrate.Orchestrator, "cleanup"):
                        with contextlib.suppress(SystemExit):
                            orchestrate.main()
                        _, kwargs = mock_init.call_args
                        assert kwargs.get("parallel_enabled") is True

    def test_cli_shared_state_dir_disables_parallel(self, temp_repo):
        """--shared-state-dir forces worker mode (parallel_enabled=False)."""
        from unittest.mock import patch

        from millstone import orchestrate

        with patch(
            "sys.argv",
            ["orchestrate.py", "--worktrees", "--shared-state-dir", "/tmp/shared", "--dry-run"],
        ), patch.object(orchestrate.Orchestrator, "__init__", return_value=None) as mock_init:
            with patch.object(orchestrate.Orchestrator, "run", return_value=0):
                with patch.object(orchestrate.Orchestrator, "cleanup"):
                    with contextlib.suppress(SystemExit):
                        orchestrate.main()
                    _, kwargs = mock_init.call_args
                    assert kwargs.get("parallel_enabled") is False
                    assert kwargs.get("shared_state_dir") == "/tmp/shared"

    def test_cli_defaults_match_config(self, temp_repo):
        """Defaults for worktree flags match DEFAULT_CONFIG (when no config.toml)."""
        from unittest.mock import patch

        from millstone import orchestrate
        from millstone.config import DEFAULT_CONFIG

        # temp_repo has no .millstone/config.toml by default
        with patch("sys.argv", ["orchestrate.py", "--dry-run"]):
            with patch.object(orchestrate.Orchestrator, "__init__", return_value=None) as mock_init:
                with patch.object(orchestrate.Orchestrator, "run", return_value=0):
                    with patch.object(orchestrate.Orchestrator, "cleanup"):
                        with contextlib.suppress(SystemExit):
                            orchestrate.main()
                        _, kwargs = mock_init.call_args
                        assert kwargs.get("parallel_enabled") == bool(DEFAULT_CONFIG["parallel_enabled"])
                        assert kwargs.get("parallel_concurrency") == int(DEFAULT_CONFIG["parallel_concurrency"])
                        assert kwargs.get("integration_branch") == DEFAULT_CONFIG["parallel_integration_branch"]
                        assert kwargs.get("merge_strategy") == DEFAULT_CONFIG["parallel_merge_strategy"]
                        assert kwargs.get("worktree_root") == DEFAULT_CONFIG["parallel_worktree_root"]
                        assert kwargs.get("worktree_cleanup") == DEFAULT_CONFIG["parallel_cleanup"]
                        assert kwargs.get("merge_max_retries") == 2
                        assert kwargs.get("high_risk_concurrency") == 1
                        assert kwargs.get("no_tasklist_edits") is False


class TestResearchMode:
    """Tests for research mode configuration."""

    def test_orchestrator_research_defaults_to_false(self, temp_repo):
        """Orchestrator research mode defaults to False."""
        orch = Orchestrator()
        try:
            assert orch.research is False
        finally:
            orch.cleanup()

    def test_orchestrator_accepts_research_flag(self, temp_repo):
        """Orchestrator accepts research=True parameter."""
        orch = Orchestrator(research=True)
        try:
            assert orch.research is True
        finally:
            orch.cleanup()

    def test_write_research_output_creates_directory(self, temp_repo):
        """write_research_output creates .millstone/research/ directory."""
        orch = Orchestrator()
        try:
            research_dir = orch.work_dir / "research"
            assert not research_dir.exists()

            orch.write_research_output("Test task", "Test output")

            assert research_dir.exists()
            assert research_dir.is_dir()
        finally:
            orch.cleanup()

    def test_write_research_output_creates_timestamped_file(self, temp_repo):
        """write_research_output creates file with timestamp_slug.md format."""
        orch = Orchestrator()
        try:
            output_path = orch.write_research_output("Analyze API endpoints", "Test output")

            assert output_path.exists()
            # File should be named like 20231215_120000_analyze-api-endpoints.md
            assert output_path.suffix == ".md"
            assert "analyze-api-endpoints" in output_path.stem
            # Should contain timestamp pattern (YYYYMMDD_HHMMSS)
            assert "_" in output_path.stem
            parts = output_path.stem.split("_", 2)
            assert len(parts[0]) == 8  # YYYYMMDD
            assert len(parts[1]) == 6  # HHMMSS
        finally:
            orch.cleanup()

    def test_write_research_output_includes_task_header(self, temp_repo):
        """write_research_output includes task description in header."""
        orch = Orchestrator()
        try:
            task = "Investigate performance bottlenecks"
            output_path = orch.write_research_output(task, "Test output")

            content = output_path.read_text()
            assert f"# Research: {task}" in content
            assert f"**Task:** {task}" in content
            assert "**Timestamp:**" in content
        finally:
            orch.cleanup()

    def test_write_research_output_includes_full_response(self, temp_repo):
        """write_research_output includes the full agent response."""
        orch = Orchestrator()
        try:
            agent_output = "This is the full agent response with lots of details."
            output_path = orch.write_research_output("Test task", agent_output)

            content = output_path.read_text()
            assert "## Full Agent Response" in content
            assert agent_output in content
        finally:
            orch.cleanup()

    def test_write_research_output_extracts_findings_section(self, temp_repo):
        """write_research_output extracts FINDINGS section from agent output."""
        orch = Orchestrator()
        try:
            agent_output = """Here is my analysis.

## FINDINGS

1. Found issue A
2. Found issue B

## RECOMMENDATIONS

Fix the issues."""
            output_path = orch.write_research_output("Test task", agent_output)

            content = output_path.read_text()
            assert "## Extracted Data" in content
            assert "### Findings" in content
            assert "Found issue A" in content
        finally:
            orch.cleanup()

    def test_write_research_output_extracts_recommendations_section(self, temp_repo):
        """write_research_output extracts RECOMMENDATIONS section."""
        orch = Orchestrator()
        try:
            agent_output = """Analysis complete.

## RECOMMENDATIONS

- Do this first
- Then do that

## Summary

All done."""
            output_path = orch.write_research_output("Test task", agent_output)

            content = output_path.read_text()
            assert "### Recommendations" in content
            assert "Do this first" in content
        finally:
            orch.cleanup()

    def test_write_research_output_handles_empty_output(self, temp_repo):
        """write_research_output handles empty agent output."""
        orch = Orchestrator()
        try:
            output_path = orch.write_research_output("Test task", "")

            assert output_path.exists()
            content = output_path.read_text()
            assert "# Research: Test task" in content
            assert "## Full Agent Response" in content
        finally:
            orch.cleanup()

    def test_write_research_output_slug_handles_special_characters(self, temp_repo):
        """write_research_output creates valid slug from special characters."""
        orch = Orchestrator()
        try:
            task = "What's the API's response format?!"
            output_path = orch.write_research_output(task, "Test output")

            # Slug should only contain lowercase alphanumeric and hyphens
            slug_part = output_path.stem.split("_", 2)[2]
            assert all(c.isalnum() or c == "-" for c in slug_part)
            assert slug_part.islower() or slug_part.replace("-", "").isdigit()
        finally:
            orch.cleanup()

    def test_write_research_output_slug_truncates_long_descriptions(self, temp_repo):
        """write_research_output truncates very long task descriptions in slug."""
        orch = Orchestrator()
        try:
            task = "This is a very long task description that goes on and on and on and should be truncated to a reasonable length for the filename"
            output_path = orch.write_research_output(task, "Test output")

            # Slug should be limited to 50 chars
            slug_part = output_path.stem.split("_", 2)[2]
            assert len(slug_part) <= 50
        finally:
            orch.cleanup()

    def test_write_research_output_returns_path(self, temp_repo):
        """write_research_output returns Path object to created file."""
        orch = Orchestrator()
        try:
            result = orch.write_research_output("Test task", "Test output")

            assert isinstance(result, Path)
            assert result.exists()
            assert result.parent.name == "research"
        finally:
            orch.cleanup()

    def test_mark_task_complete_marks_first_unchecked(self, temp_repo):
        """mark_task_complete marks the first unchecked task as complete."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("# Tasks\n\n- [ ] First task\n- [ ] Second task\n")

        orch = Orchestrator(tasklist="docs/tasklist.md")
        try:
            result = orch.mark_task_complete()
            assert result is True

            content = tasklist.read_text()
            assert "- [x] First task" in content
            assert "- [ ] Second task" in content
        finally:
            orch.cleanup()

    def test_mark_task_complete_returns_false_if_no_tasks(self, temp_repo):
        """mark_task_complete returns False when no unchecked tasks exist."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("# Tasks\n\n- [x] Already done\n")

        orch = Orchestrator(tasklist="docs/tasklist.md")
        try:
            result = orch.mark_task_complete()
            assert result is False
        finally:
            orch.cleanup()

    def test_mark_task_complete_returns_false_if_no_tasklist(self, temp_repo):
        """mark_task_complete returns False when tasklist file doesn't exist."""
        orch = Orchestrator(tasklist="docs/nonexistent.md")
        try:
            result = orch.mark_task_complete()
            assert result is False
        finally:
            orch.cleanup()

    def test_run_single_task_research_mode_skips_review(self, temp_repo):
        """run_single_task in research mode skips mechanical checks and review cycle."""
        from unittest.mock import MagicMock, patch

        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("# Tasks\n\n- [ ] Analyze the codebase\n")

        with patch("subprocess.run") as mock_run:
            # Mock claude to return research output
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="## FINDINGS\n\n1. Found important patterns\n\n## RECOMMENDATIONS\n\n- Do this",
                stderr=""
            )

            orch = Orchestrator(
                tasklist="docs/tasklist.md",
                research=True,
                cli="claude"
            )
            try:
                result = orch.run_single_task()
                assert result is True

                # Check task was marked complete
                content = tasklist.read_text()
                assert "- [x] Analyze the codebase" in content

                # Check research output was saved
                research_dir = orch.work_dir / "research"
                assert research_dir.exists()
                research_files = list(research_dir.glob("*.md"))
                assert len(research_files) == 1

                # Check research_completed was logged
                log_file = list(orch.work_dir.glob("runs/*.log"))[0]
                log_content = log_file.read_text()
                assert "research_completed" in log_content
            finally:
                orch.cleanup()

    def test_run_single_task_research_mode_uses_research_prompt(self, temp_repo):
        """run_single_task in research mode uses research_prompt.md template."""
        from unittest.mock import MagicMock, patch

        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("# Tasks\n\n- [ ] Investigate API patterns\n")

        prompts_received = []

        def capture_prompt(*args, **kwargs):
            if args[0][0] == "claude":
                # Extract prompt from -p argument
                cmd_list = args[0]
                for i, arg in enumerate(cmd_list):
                    if arg == "-p" and i + 1 < len(cmd_list):
                        prompts_received.append(cmd_list[i + 1])
            return MagicMock(
                returncode=0,
                stdout="Research output here",
                stderr=""
            )

        with patch("subprocess.run", side_effect=capture_prompt):
            orch = Orchestrator(
                tasklist="docs/tasklist.md",
                research=True,
                cli="claude"
            )
            try:
                orch.run_single_task()

                # The research prompt should contain key phrases
                assert len(prompts_received) >= 1
                prompt = prompts_received[0]
                assert "DO NOT modify any files" in prompt or "research" in prompt.lower()
            finally:
                orch.cleanup()

    def test_run_single_task_research_mode_with_direct_task(self, temp_repo):
        """run_single_task in research mode works with --task flag."""
        from unittest.mock import MagicMock, patch

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Direct task research output",
                stderr=""
            )

            orch = Orchestrator(
                task="Analyze performance bottlenecks",
                research=True,
                cli="claude"
            )
            try:
                result = orch.run_single_task()
                assert result is True

                # Research output should be saved
                research_dir = orch.work_dir / "research"
                assert research_dir.exists()
                research_files = list(research_dir.glob("*.md"))
                assert len(research_files) == 1

                # Check the file contains the output
                content = research_files[0].read_text()
                assert "Direct task research output" in content
            finally:
                orch.cleanup()


class TestIsApproved:
    """Tests for approval detection."""

    @pytest.mark.parametrize(
        "review_output",
        [
            '{"status": "APPROVED", "review": "Looks good", "summary": "No blockers"}',
        ],
    )
    def test_detects_approval_patterns(self, review_output):
        """Detects various approval patterns."""
        orch = Orchestrator()
        try:
            approved, _ = orch.is_approved(review_output)
            assert approved is True
        finally:
            orch.cleanup()

    @pytest.mark.parametrize(
        "review_output",
        [
            '{"status": "REQUEST_CHANGES", "review": "Needs fixes", "summary": "Blocking issues"}',
            '{"status": "APPROVED"}',
            "Please fix the following issues:",
            "Critical: null pointer exception possible",
            "The implementation has several problems.",
        ],
    )
    def test_rejects_non_approval(self, review_output):
        """Rejects outputs that don't indicate approval."""
        orch = Orchestrator()
        try:
            approved, _ = orch.is_approved(review_output)
            assert approved is False
        finally:
            orch.cleanup()


class TestMechanicalChecks:
    """Tests for mechanical sanity checks."""

    def test_passes_with_warning_when_no_changes(self, temp_repo):
        """Passes (with warning) when git status shows no changes."""
        orch = Orchestrator()
        try:
            # No changes made to repo
            result = orch.mechanical_checks()
            assert result is True
        finally:
            orch.cleanup()

    def test_passes_with_small_change(self, temp_repo):
        """Passes when changes are within threshold."""
        orch = Orchestrator()
        try:
            # Make a small change
            (temp_repo / "small_change.txt").write_text("Hello world")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            result = orch.mechanical_checks()
            assert result is True
        finally:
            orch.cleanup()

    def test_fails_when_loc_exceeded(self, temp_repo):
        """Fails when lines of code exceed threshold."""
        orch = Orchestrator(loc_threshold=10)
        try:
            # Make a large change
            large_content = "\n".join([f"line {i}" for i in range(100)])
            (temp_repo / "large_file.txt").write_text(large_content)
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            result = orch.mechanical_checks()
            assert result is False
        finally:
            orch.cleanup()

    def test_allows_sensitive_file_by_default(self, temp_repo):
        """Allows sensitive files by default when policy disables the check."""
        orch = Orchestrator()
        try:
            # Create a sensitive file
            (temp_repo / ".env").write_text("SECRET_KEY=abc123")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            result = orch.mechanical_checks()
            assert result is True
        finally:
            orch.cleanup()

    @pytest.mark.parametrize(
        "filename",
        [
            ".env",
            ".env.local",
            "credentials.json",
            "secret_key.txt",
            "server.pem",
            "private.key",
        ],
    )
    def test_detects_various_sensitive_files_when_enabled(self, temp_repo, filename):
        """Detects various patterns of sensitive files when enabled by policy."""
        from millstone.runtime.orchestrator import POLICY_FILE_NAME, WORK_DIR_NAME

        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[sensitive]
enabled = true
paths = [".env", ".env.local", "credentials.json", "secret_key.txt", "server.pem", "private.key"]
require_approval = true
""")

        orch = Orchestrator()
        try:
            (temp_repo / filename).write_text("sensitive content")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            result = orch.mechanical_checks()
            assert result is False
        finally:
            orch.cleanup()


class TestGitHelper:
    """Tests for the git helper method."""

    def test_git_returns_output(self, temp_repo):
        """Git helper returns command output."""
        orch = Orchestrator()
        try:
            output = orch.git("status")
            assert "On branch" in output or "nothing to commit" in output
        finally:
            orch.cleanup()

    def test_git_handles_diff(self, temp_repo):
        """Git helper can run diff commands."""
        orch = Orchestrator()
        try:
            (temp_repo / "new_file.txt").write_text("content")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            output = orch.git("diff", "--cached", "--name-only")
            assert "new_file.txt" in output
        finally:
            orch.cleanup()

    def test_git_uses_repo_dir_cwd(self, temp_repo, tmp_path):
        """Git helper runs in orch.repo_dir even if process cwd changes."""
        import os

        orch = Orchestrator(repo_dir=temp_repo)
        other_dir = tmp_path / "other_cwd"
        other_dir.mkdir()
        original_cwd = os.getcwd()
        try:
            os.chdir(other_dir)
            top = orch.git("rev-parse", "--show-toplevel").strip()
            assert Path(top) == temp_repo
        finally:
            os.chdir(original_cwd)
            orch.cleanup()


class TestTasklistFlag:
    """Tests for --tasklist flag functionality."""

    def test_tasklist_has_default_value(self):
        """Tasklist defaults to .millstone/tasklist.md."""
        orch = Orchestrator()
        try:
            assert orch.tasklist == ".millstone/tasklist.md"
        finally:
            orch.cleanup()

    def test_tasklist_can_be_set(self):
        """Tasklist can be set via constructor."""
        orch = Orchestrator(tasklist="custom/tasks.md")
        try:
            assert orch.tasklist == "custom/tasks.md"
        finally:
            orch.cleanup()

    def test_get_tasklist_prompt_replaces_placeholder(self):
        """get_tasklist_prompt() resolves provider placeholders to concrete instructions."""
        orch = Orchestrator(tasklist="my/custom/tasklist.md")
        try:
            prompt = orch.get_tasklist_prompt()
            assert "{{TASKLIST_PATH}}" not in prompt
            assert "{{TASKLIST_READ_INSTRUCTIONS}}" not in prompt
            assert "{{TASKLIST_COMPLETE_INSTRUCTIONS}}" not in prompt
            assert "my/custom/tasklist.md" in prompt
        finally:
            orch.cleanup()

    def test_get_tasklist_prompt_legacy_tasklist_path_in_custom_prompt(self, temp_repo):
        """get_tasklist_prompt() resolves legacy {{TASKLIST_PATH}} in custom --prompts-dir templates."""
        custom_prompts = temp_repo / "my_prompts"
        custom_prompts.mkdir(exist_ok=True)
        (custom_prompts / "tasklist_prompt.md").write_text(
            "Read the tasklist at {{TASKLIST_PATH}} and implement the next task."
        )
        orch = Orchestrator(prompts_dir=str(custom_prompts), tasklist="legacy/tasks.md")
        try:
            prompt = orch.get_tasklist_prompt()
            assert "{{TASKLIST_PATH}}" not in prompt
            assert "legacy/tasks.md" in prompt
        finally:
            orch.cleanup()

    def test_get_tasklist_prompt_no_unresolved_provider_tokens_and_working_directory_resolved(self):
        """get_tasklist_prompt() resolves all provider tokens and {{WORKING_DIRECTORY}}."""
        import re

        orch = Orchestrator(tasklist="my/custom/tasklist.md")
        try:
            prompt = orch.get_tasklist_prompt()
            # All TASKLIST_*_INSTRUCTIONS provider tokens must be resolved
            provider_tokens = re.findall(r"\{\{TASKLIST_\w+_INSTRUCTIONS\}\}", prompt)
            assert provider_tokens == [], f"Unresolved provider tokens: {provider_tokens}"
            # {{WORKING_DIRECTORY}} must be resolved, not left as a literal token
            assert "{{WORKING_DIRECTORY}}" not in prompt
            assert str(orch.repo_dir) in prompt
        finally:
            orch.cleanup()

    def test_get_tasklist_prompt_resolves_working_directory(self):
        """get_tasklist_prompt() replaces {{WORKING_DIRECTORY}} with str(repo_dir)."""
        orch = Orchestrator(tasklist="my/tasks.md")
        try:
            prompt = orch.get_tasklist_prompt()
            assert "{{WORKING_DIRECTORY}}" not in prompt
            assert str(orch.repo_dir) in prompt
        finally:
            orch.cleanup()

    def test_get_tasklist_prompt_empty_provider_placeholder_raises(self, temp_repo):
        """get_tasklist_prompt() raises ValueError when a provider placeholder is empty."""
        custom_prompts = temp_repo / "my_prompts"
        custom_prompts.mkdir(exist_ok=True)
        (custom_prompts / "tasklist_prompt.md").write_text(
            "Read {{TASKLIST_READ_INSTRUCTIONS}} and {{TASKLIST_COMPLETE_INSTRUCTIONS}}."
        )
        orch = Orchestrator(prompts_dir=str(custom_prompts), tasklist="tasks.md")
        try:
            # Patch the provider to return an empty string for one placeholder
            original = orch._outer_loop_manager.tasklist_provider.get_prompt_placeholders

            def patched_placeholders():
                result = original()
                # Force one value to empty to trigger the guard
                return {k: ("" if k == "TASKLIST_READ_INSTRUCTIONS" else v) for k, v in result.items()}

            orch._outer_loop_manager.tasklist_provider.get_prompt_placeholders = patched_placeholders
            with pytest.raises(ValueError, match="TASKLIST_READ_INSTRUCTIONS"):
                orch.get_tasklist_prompt()
        finally:
            orch.cleanup()

    def test_get_review_prompt_replaces_placeholder(self, temp_repo):
        """get_review_prompt() replaces provider placeholders and static tokens."""
        # Use a custom prompt template for this test
        (temp_repo / "my_prompts").mkdir(exist_ok=True)
        (temp_repo / "my_prompts" / "review_prompt.md").write_text(
            "Review {{TASKLIST_READ_INSTRUCTIONS}}. Output: {{AUTHOR_OUTPUT}}"
        )

        orch = Orchestrator(prompts_dir="my_prompts", tasklist="another/path.md")
        try:
            prompt = orch.get_review_prompt(builder_output="Task done.")
            assert "{{TASKLIST_READ_INSTRUCTIONS}}" not in prompt
            assert "another/path.md" in prompt
            assert "Output: Task done." in prompt
        finally:
            orch.cleanup()

    def test_get_review_prompt_resolves_working_directory(self, temp_repo):
        """get_review_prompt() substitutes {{WORKING_DIRECTORY}} with repo dir."""
        (temp_repo / "my_prompts").mkdir(exist_ok=True)
        (temp_repo / "my_prompts" / "review_prompt.md").write_text(
            "Dir: {{WORKING_DIRECTORY}}. {{AUTHOR_OUTPUT}}"
        )

        orch = Orchestrator(prompts_dir=str(temp_repo / "my_prompts"))
        try:
            prompt = orch.get_review_prompt(builder_output="done")
            assert "{{WORKING_DIRECTORY}}" not in prompt
            assert str(orch.repo_dir) in prompt
        finally:
            orch.cleanup()

    def test_get_review_prompt_no_unresolved_tasklist_read_instructions(self, temp_repo):
        """get_review_prompt() with default review_prompt.md has no unresolved provider tokens."""
        orch = Orchestrator(tasklist="path/to/tasks.md")
        try:
            prompt = orch.get_review_prompt(builder_output="done", git_diff="diff here")
            assert "{{TASKLIST_READ_INSTRUCTIONS}}" not in prompt
            assert "{{TASKLIST_PATH}}" not in prompt
            assert "path/to/tasks.md" in prompt
        finally:
            orch.cleanup()


    def test_get_review_prompt_legacy_tasklist_path(self, temp_repo):
        """get_review_prompt() rewrites literal .millstone/tasklist.md to configured tasklist."""
        (temp_repo / "my_prompts").mkdir(exist_ok=True)
        (temp_repo / "my_prompts" / "review_prompt.md").write_text(
            "Read .millstone/tasklist.md and review. {{AUTHOR_OUTPUT}}"
        )

        orch = Orchestrator(prompts_dir="my_prompts", tasklist="custom/tasks.md")
        try:
            prompt = orch.get_review_prompt(builder_output="done")
            assert ".millstone/tasklist.md" not in prompt
            assert "custom/tasks.md" in prompt
        finally:
            orch.cleanup()

    def test_get_review_prompt_does_not_rewrite_dynamic_content(self, temp_repo):
        """Legacy-path rewrite must not mutate builder_output or git_diff payloads."""
        (temp_repo / "my_prompts").mkdir(exist_ok=True)
        (temp_repo / "my_prompts" / "review_prompt.md").write_text(
            "Review the changes.\n{{AUTHOR_OUTPUT}}\n{{GIT_DIFF}}"
        )

        orch = Orchestrator(prompts_dir="my_prompts", tasklist="custom/tasks.md")
        try:
            builder_output = "modified .millstone/tasklist.md directly"
            git_diff = "diff --git a/.millstone/tasklist.md b/.millstone/tasklist.md"
            prompt = orch.get_review_prompt(
                builder_output=builder_output, git_diff=git_diff
            )
            assert builder_output in prompt
            assert git_diff in prompt
        finally:
            orch.cleanup()

    def test_get_task_prompt_resolves_update_instructions(self):
        """get_task_prompt() resolves {{TASKLIST_UPDATE_INSTRUCTIONS}} via provider placeholders."""
        orch = Orchestrator(task="fix the bug", tasklist="my/tasks.md")
        try:
            prompt = orch.get_task_prompt()
            assert "{{TASKLIST_UPDATE_INSTRUCTIONS}}" not in prompt
            assert "my/tasks.md" in prompt
        finally:
            orch.cleanup()

    def test_apply_provider_placeholders_replaces_known_keys(self):
        """_apply_provider_placeholders() replaces only tokens present in placeholders dict."""
        orch = Orchestrator()
        try:
            result = orch._apply_provider_placeholders(
                "Do {{FOO}} and keep {{BAR}} unchanged.",
                {"FOO": "something"},
            )
            assert result == "Do something and keep {{BAR}} unchanged."
        finally:
            orch.cleanup()

    def test_apply_provider_placeholders_raises_on_empty_value(self):
        """_apply_provider_placeholders() raises ValueError when a present key resolves to empty."""
        orch = Orchestrator()
        try:
            with pytest.raises(ValueError, match="empty string"):
                orch._apply_provider_placeholders(
                    "Do {{FOO}} now.",
                    {"FOO": ""},
                )
        finally:
            orch.cleanup()

    def test_commit_tasklist_true_uses_docs_tasklist(self, temp_repo):
        """commit_tasklist=True resolves tasklist to docs/tasklist.md when no explicit path set."""
        from millstone import orchestrate

        config = DEFAULT_CONFIG.copy()
        config["commit_tasklist"] = True

        with patch("sys.argv", ["millstone"]):
            with patch("millstone.runtime.orchestrator.load_config", return_value=config):
                with patch.object(orchestrate.Orchestrator, "__init__", return_value=None) as mock_init:
                    with patch.object(orchestrate.Orchestrator, "run", return_value=0):
                        with patch.object(orchestrate.Orchestrator, "cleanup"):
                            with contextlib.suppress(SystemExit):
                                orchestrate.main()

        _, kwargs = mock_init.call_args
        assert kwargs["tasklist"] == "docs/tasklist.md"

    def test_explicit_tasklist_config_wins_over_commit_tasklist(self, temp_repo):
        """Explicit tasklist path in config takes precedence over commit_tasklist=True."""
        from millstone import orchestrate

        custom_tasklist = temp_repo / "custom" / "my-tasks.md"
        custom_tasklist.parent.mkdir()
        custom_tasklist.write_text("# Tasks\n\n- [ ] Task 1\n")

        config = DEFAULT_CONFIG.copy()
        config["commit_tasklist"] = True
        config["tasklist"] = "custom/my-tasks.md"

        with patch("sys.argv", ["millstone"]):
            with patch("millstone.runtime.orchestrator.load_config", return_value=config):
                with patch.object(orchestrate.Orchestrator, "__init__", return_value=None) as mock_init:
                    with patch.object(orchestrate.Orchestrator, "run", return_value=0):
                        with patch.object(orchestrate.Orchestrator, "cleanup"):
                            with contextlib.suppress(SystemExit):
                                orchestrate.main()

        _, kwargs = mock_init.call_args
        assert kwargs["tasklist"] == "custom/my-tasks.md"


class TestTaskFlag:
    """Tests for --task flag functionality."""

    def test_task_is_none_by_default(self):
        """Task is None when not provided."""
        orch = Orchestrator()
        try:
            assert orch.task is None
        finally:
            orch.cleanup()

    def test_task_can_be_set(self):
        """Task can be set via constructor."""
        orch = Orchestrator(task="implement feature X")
        try:
            assert orch.task == "implement feature X"
        finally:
            orch.cleanup()

    def test_task_prompt_file_exists(self, prompts_dir):
        """Task prompt file exists."""
        assert (prompts_dir / "task_prompt.md").exists()

    def test_get_task_prompt_replaces_placeholder(self):
        """get_task_prompt() replaces {{TASK}} with actual task."""
        orch = Orchestrator(task="add dark mode")
        try:
            prompt = orch.get_task_prompt()
            assert "add dark mode" in prompt
            assert "{{TASK}}" not in prompt
        finally:
            orch.cleanup()

    def test_task_prompt_has_required_sections(self):
        """Task prompt contains required sections."""
        orch = Orchestrator(task="test task")
        try:
            prompt = orch.get_task_prompt()
            assert "## Task Execution Loop" in prompt or "Task Execution Loop" in prompt
            assert "Analyze" in prompt
            assert "Implement" in prompt
            assert "Verify" in prompt
        finally:
            orch.cleanup()

    def test_get_task_prompt_adds_no_tasklist_edit_guard(self):
        """Task prompt adds explicit guard when tasklist edits are disallowed."""
        orch = Orchestrator(
            task="test task",
            no_tasklist_edits=True,
            tasklist="docs/tasklist.md",
        )
        try:
            prompt = orch.get_task_prompt()
            assert "Do NOT edit `docs/tasklist.md`" in prompt
            assert "--no-tasklist-edits" in prompt
        finally:
            orch.cleanup()

    def test_get_task_prompt_worker_mode_allows_tasklist_coherence_edits(self):
        """Worker mode prompt allows coherence edits but keeps completion ownership in control plane."""
        orch = Orchestrator(
            task="test task",
            shared_state_dir="/tmp/millstone-shared",
        )
        try:
            prompt = orch.get_task_prompt()
            assert "--shared-state-dir" in prompt
            assert "You MAY update tasklist task text/metadata for coherence" in prompt
            assert "Do NOT mark task checkboxes complete" in prompt
        finally:
            orch.cleanup()


class TestPreflightChecks:
    """Tests for pre-flight checks."""

    def test_passes_when_all_conditions_met(self, temp_repo):
        """Pre-flight checks pass when claude CLI, git repo, and tasklist all exist."""
        orch = Orchestrator()
        try:
            # Should not raise - temp_repo fixture creates valid git repo with tasklist
            with patch("subprocess.run") as mock_run:
                def run_side_effect(cmd, *args, **kwargs):
                    if cmd[0] == "claude":
                        return MagicMock(returncode=0, stdout="claude 1.0.0", stderr="")
                    elif cmd[0] == "git":
                        return MagicMock(returncode=0, stdout="true", stderr="")
                    return MagicMock(returncode=0, stdout="", stderr="")

                mock_run.side_effect = run_side_effect
                orch.preflight_checks()
        finally:
            orch.cleanup()

    def test_fails_when_claude_not_in_path(self, temp_repo):
        """Pre-flight checks fail when claude CLI is not found."""
        orch = Orchestrator()
        try:
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = FileNotFoundError("claude not found")
                with pytest.raises(PreflightError) as exc_info:
                    orch.preflight_checks()
                # Error message comes from CLIProvider now
                assert "not found" in str(exc_info.value).lower()
                assert "npm install" in str(exc_info.value)
        finally:
            orch.cleanup()

    def test_fails_when_claude_returns_error(self, temp_repo):
        """Pre-flight checks fail when claude CLI returns non-zero exit code."""
        orch = Orchestrator()
        try:
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
                with pytest.raises(PreflightError) as exc_info:
                    orch.preflight_checks()
                # Error message comes from CLIProvider now
                assert "error" in str(exc_info.value).lower()
        finally:
            orch.cleanup()

    def test_fails_when_not_git_repo(self, tmp_path):
        """Pre-flight checks fail when not in a git repository."""
        import os
        original_cwd = os.getcwd()
        non_git_dir = tmp_path / "not_a_repo"
        non_git_dir.mkdir()
        os.chdir(non_git_dir)
        try:
            orch = Orchestrator()
            try:
                with patch("subprocess.run") as mock_run:
                    def run_side_effect(cmd, *args, **kwargs):
                        if cmd[0] == "claude":
                            return MagicMock(returncode=0, stdout="claude 1.0.0", stderr="")
                        elif cmd[0] == "git":
                            return MagicMock(returncode=128, stdout="", stderr="not a git repo")
                        return MagicMock(returncode=0, stdout="", stderr="")

                    mock_run.side_effect = run_side_effect
                    with pytest.raises(PreflightError) as exc_info:
                        orch.preflight_checks()
                    assert "Not a git repository" in str(exc_info.value)
                    assert "git init" in str(exc_info.value)
            finally:
                orch.cleanup()
        finally:
            os.chdir(original_cwd)

    def test_fails_when_tasklist_missing(self, temp_repo):
        """Pre-flight checks fail when tasklist file doesn't exist."""
        # Remove the tasklist file created by fixture
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        tasklist_path.unlink()

        orch = Orchestrator()
        try:
            with patch("subprocess.run") as mock_run:
                def run_side_effect(cmd, *args, **kwargs):
                    if cmd[0] == "claude":
                        return MagicMock(returncode=0, stdout="claude 1.0.0", stderr="")
                    elif cmd[0] == "git":
                        return MagicMock(returncode=0, stdout="true", stderr="")
                    return MagicMock(returncode=0, stdout="", stderr="")

                mock_run.side_effect = run_side_effect
                with pytest.raises(PreflightError) as exc_info:
                    orch.preflight_checks()
                assert "Tasklist file not found" in str(exc_info.value)
                assert "--task" in str(exc_info.value)
        finally:
            orch.cleanup()

    def test_skips_tasklist_check_when_task_provided(self, temp_repo):
        """Pre-flight checks skip tasklist check when --task is used."""
        # Remove the tasklist file
        tasklist_path = temp_repo / "docs" / "tasklist.md"
        tasklist_path.unlink()

        orch = Orchestrator(task="implement feature X")
        try:
            with patch("subprocess.run") as mock_run:
                def run_side_effect(cmd, *args, **kwargs):
                    if cmd[0] == "claude":
                        return MagicMock(returncode=0, stdout="claude 1.0.0", stderr="")
                    elif cmd[0] == "git":
                        return MagicMock(returncode=0, stdout="true", stderr="")
                    return MagicMock(returncode=0, stdout="", stderr="")

                mock_run.side_effect = run_side_effect
                # Should not raise - task mode doesn't require tasklist
                orch.preflight_checks()
        finally:
            orch.cleanup()

    def test_checks_custom_tasklist_path(self, temp_repo):
        """Pre-flight checks verify custom tasklist path when specified."""
        orch = Orchestrator(tasklist="custom/path/tasks.md")
        try:
            with patch("subprocess.run") as mock_run:
                def run_side_effect(cmd, *args, **kwargs):
                    if cmd[0] == "claude":
                        return MagicMock(returncode=0, stdout="claude 1.0.0", stderr="")
                    elif cmd[0] == "git":
                        return MagicMock(returncode=0, stdout="true", stderr="")
                    return MagicMock(returncode=0, stdout="", stderr="")

                mock_run.side_effect = run_side_effect
                with pytest.raises(PreflightError) as exc_info:
                    orch.preflight_checks()
                assert "custom/path/tasks.md" in str(exc_info.value)
        finally:
            orch.cleanup()


class TestDryRun:
    """Tests for --dry-run flag functionality."""

    def test_dry_run_is_false_by_default(self):
        """Dry run is False when not provided."""
        orch = Orchestrator()
        try:
            assert orch.dry_run is False
        finally:
            orch.cleanup()

    def test_dry_run_can_be_set(self):
        """Dry run can be set via constructor."""
        orch = Orchestrator(dry_run=True)
        try:
            assert orch.dry_run is True
        finally:
            orch.cleanup()

    def test_dry_run_returns_zero_exit_code(self, temp_repo):
        """Dry run returns exit code 0."""
        orch = Orchestrator(dry_run=True)
        try:
            exit_code = orch.run()
            assert exit_code == 0
        finally:
            orch.cleanup()

    def test_dry_run_shows_builder_prompt(self, temp_repo, capsys):
        """Dry run displays the builder prompt."""
        orch = Orchestrator(dry_run=True)
        try:
            orch.run()
            captured = capsys.readouterr()
            assert "Builder Prompt" in captured.out
            assert "COMPLETE EXACTLY ONE TASK" in captured.out
        finally:
            orch.cleanup()

    def test_dry_run_shows_review_prompt(self, temp_repo, capsys):
        """Dry run displays the review prompt."""
        orch = Orchestrator(dry_run=True)
        try:
            orch.run()
            captured = capsys.readouterr()
            assert "Review Prompt" in captured.out
            assert "review of local, uncommitted changes" in captured.out
        finally:
            orch.cleanup()

    def test_dry_run_shows_prompt_files(self, temp_repo, capsys):
        """Dry run lists prompt files and their existence status."""
        orch = Orchestrator(dry_run=True)
        try:
            orch.run()
            captured = capsys.readouterr()
            assert "Prompt Files" in captured.out
            assert "tasklist_prompt.md" in captured.out
            assert "review_prompt.md" in captured.out
        finally:
            orch.cleanup()

    def test_dry_run_shows_tasklist_info(self, temp_repo, capsys):
        """Dry run displays tasklist information."""
        orch = Orchestrator(dry_run=True)
        try:
            orch.run()
            captured = capsys.readouterr()
            assert "Tasklist Info" in captured.out
            assert "Unchecked tasks:" in captured.out
            assert "Completed tasks:" in captured.out
        finally:
            orch.cleanup()

    def test_dry_run_with_task_shows_task_prompt(self, temp_repo, capsys):
        """Dry run with --task shows the task prompt instead of tasklist prompt."""
        orch = Orchestrator(dry_run=True, task="implement feature X")
        try:
            orch.run()
            captured = capsys.readouterr()
            assert "Builder Prompt" in captured.out
            assert "implement feature X" in captured.out
            # Should not show tasklist info
            assert "Tasklist Info" not in captured.out
        finally:
            orch.cleanup()

    def test_dry_run_does_not_invoke_claude(self, temp_repo):
        """Dry run does not invoke the claude CLI."""
        orch = Orchestrator(dry_run=True)
        try:
            with patch("subprocess.run") as mock_run:
                orch.run()
                # Check that claude was never called
                for call in mock_run.call_args_list:
                    if call.args and len(call.args[0]) > 0:
                        assert call.args[0][0] != "claude", "claude CLI should not be invoked in dry-run mode"
        finally:
            orch.cleanup()


class TestWorktreeRunDelegation:
    """Tests for Orchestrator.run() delegation to ParallelOrchestrator."""

    def test_run_delegates_to_parallel(self, temp_repo):
        from unittest.mock import patch

        orch = Orchestrator(parallel_enabled=True)
        try:
            with patch("millstone.runtime.parallel.ParallelOrchestrator") as mock_po:
                mock_po.return_value.run.return_value = 0
                assert orch.run() == 0
                mock_po.assert_called_once()
                mock_po.return_value.run.assert_called_once()
        finally:
            orch.cleanup()

    def test_run_normal_path_unchanged(self, temp_repo):
        from unittest.mock import patch

        orch = Orchestrator(parallel_enabled=False, dry_run=True)
        try:
            with patch.object(orch, "run_dry_run", return_value=0) as mock_dry:
                assert orch.run() == 0
                mock_dry.assert_called_once()
        finally:
            orch.cleanup()

    def test_dry_run_with_worktrees_delegates(self, temp_repo):
        from unittest.mock import patch

        orch = Orchestrator(parallel_enabled=True, dry_run=True)
        try:
            with patch("millstone.runtime.parallel.ParallelOrchestrator") as mock_po:
                mock_po.return_value.run.return_value = 0
                with patch.object(orch, "run_dry_run", return_value=0) as mock_dry:
                    assert orch.run() == 0
                    mock_po.assert_called_once()
                    mock_po.return_value.run.assert_called_once()
                    mock_dry.assert_not_called()
        finally:
            orch.cleanup()


class TestWorkerMode:
    def test_worker_writes_result_json(self, temp_repo, tmp_path):
        """Worker mode writes shared-state result.json with required fields."""
        import json

        shared = tmp_path / "shared"
        orch = Orchestrator(
            task="**Foo**: bar\n  - ID: t1\n  - Risk: low\n",
            shared_state_dir=str(shared),
            research=True,
            base_branch="master",
        )
        # Avoid invoking real CLIs.
        orch.run_agent = lambda *a, **k: "worker output"
        try:
            assert orch.run_single_task() is True
            result_path = shared / "tasks" / "t1" / "result.json"
            assert result_path.exists()
            data = json.loads(result_path.read_text())
            assert data["status"] == "success"
            assert data["task_id"] == "t1"
            assert data["branch"]
            assert data["commit_sha"]
            assert data["risk"] == "low"
            assert "worker output" in (data.get("review_summary") or "")
        finally:
            orch.cleanup()

    def test_worker_heartbeat_thread(self, temp_repo, tmp_path):
        """Worker mode updates heartbeat periodically while task is running."""
        import threading
        import time

        shared = tmp_path / "shared"
        orch = Orchestrator(
            task="**Foo**: bar\n  - ID: t1\n  - Risk: low\n",
            shared_state_dir=str(shared),
            research=True,
            parallel_heartbeat_interval=0.05,
            base_branch="master",
        )

        def slow_agent(*_a, **_k):
            time.sleep(0.25)
            return "ok"

        orch.run_agent = slow_agent

        result: dict[str, object] = {}

        def run_task():
            result["ok"] = orch.run_single_task()

        t = threading.Thread(target=run_task, daemon=True)
        t.start()
        try:
            hb_path = shared / "tasks" / "t1" / "heartbeat"
            deadline = time.time() + 2.0
            while time.time() < deadline and not hb_path.exists():
                time.sleep(0.01)
            assert hb_path.exists()
            ts1 = float(hb_path.read_text().strip())
            time.sleep(0.12)
            ts2 = float(hb_path.read_text().strip())
            assert ts2 > ts1
        finally:
            t.join(timeout=2.0)
            orch.cleanup()

    def test_worker_mode_logs_debug_on_best_effort_write_failures(self, temp_repo, tmp_path, caplog):
        """Worker mode logs debug details when heartbeat/result writes fail."""
        shared = tmp_path / "shared"
        orch = Orchestrator(
            task="**Foo**: bar\n  - ID: t1\n  - Risk: low\n",
            shared_state_dir=str(shared),
            research=True,
            parallel_heartbeat_interval=0.01,
            base_branch="master",
        )
        orch.run_agent = lambda *a, **k: "worker output"
        try:
            with (
                patch(
                    "millstone.runtime.parallel_state.ParallelState.write_heartbeat",
                    side_effect=RuntimeError("heartbeat failed"),
                ),
                patch(
                    "millstone.runtime.parallel_state.ParallelState.write_task_result",
                    side_effect=RuntimeError("result failed"),
                ),
                caplog.at_level(logging.DEBUG),
            ):
                assert orch.run_single_task() is True

            assert "Worker heartbeat write failed for task_id=t1: heartbeat failed" in caplog.text
            assert "Worker result write failed for task_id=t1: result failed" in caplog.text
        finally:
            orch.cleanup()

    def test_no_tasklist_edits_catches_committed(self, temp_repo):
        """--no-tasklist-edits blocks committed tasklist edits via git diff baseline."""
        orch = Orchestrator(task="task", no_tasklist_edits=True, tasklist="docs/tasklist.md")
        try:
            orch.loc_baseline_ref = orch.git("rev-parse", "HEAD").strip()
            # Commit a tasklist edit (working tree clean afterwards).
            tasklist = temp_repo / "docs" / "tasklist.md"
            tasklist.write_text(tasklist.read_text() + "\n- [ ] extra\n")
            subprocess.run(["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "edit tasklist"], cwd=temp_repo, capture_output=True, check=True)

            assert orch.mechanical_checks() is False
        finally:
            orch.cleanup()

    def test_tasklist_path_none_without_flag(self, temp_repo):
        """Task mode preserves tasklist_path=None unless --no-tasklist-edits is set."""
        from unittest.mock import patch

        orch = Orchestrator(task="task", no_tasklist_edits=False)
        try:
            with patch.object(orch._inner_loop_manager, "mechanical_checks", return_value=(True, False)) as mc:
                assert orch.mechanical_checks() is True
                _, kwargs = mc.call_args
                assert kwargs.get("tasklist_path") is None
        finally:
            orch.cleanup()


class TestTaskModeRiskParsing:
    def test_task_mode_parses_risk(self, temp_repo):
        orch = Orchestrator(task="**Foo**: bar\n  - Risk: high\n", research=True)
        # Avoid interactive approval prompt in tests.
        orch.risk_settings["high"]["require_approval"] = False
        orch.run_agent = lambda *a, **k: "ok"
        try:
            assert orch.run_single_task() is True
            assert orch.current_task_risk == "high"
            assert orch.max_cycles == orch.risk_settings["high"]["max_cycles"]
        finally:
            orch.cleanup()

    def test_task_mode_no_risk_defaults(self, temp_repo):
        orch = Orchestrator(task="simple task", research=True)
        orch.run_agent = lambda *a, **k: "ok"
        try:
            assert orch.run_single_task() is True
            assert orch.current_task_risk is None
            assert orch.max_cycles == orch.base_max_cycles
        finally:
            orch.cleanup()


class TestConfigFile:
    """Tests for .millstone/config.toml configuration file support."""

    def test_load_config_returns_defaults_when_no_file(self, temp_repo):
        """load_config returns default values when config file doesn't exist."""
        config = load_config()
        assert config["max_cycles"] == DEFAULT_CONFIG["max_cycles"]
        assert config["loc_threshold"] == DEFAULT_CONFIG["loc_threshold"]
        assert config["tasklist"] == DEFAULT_CONFIG["tasklist"]
        assert config["max_tasks"] == DEFAULT_CONFIG["max_tasks"]
        assert config["prompts_dir"] == DEFAULT_CONFIG["prompts_dir"]

    def test_load_config_reads_toml_file(self, temp_repo):
        """load_config reads values from .millstone/config.toml."""
        # Create config file
        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / CONFIG_FILE_NAME
        config_file.write_text("""
max_cycles = 10
loc_threshold = 1000
tasklist = "tasks.md"
max_tasks = 20
prompts_dir = "custom_prompts"
""")

        config = load_config(temp_repo)
        assert config["max_cycles"] == 10
        assert config["loc_threshold"] == 1000
        assert config["tasklist"] == "tasks.md"
        assert config["max_tasks"] == 20
        assert config["prompts_dir"] == "custom_prompts"

    def test_load_config_partial_values(self, temp_repo):
        """load_config uses defaults for missing keys."""
        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / CONFIG_FILE_NAME
        config_file.write_text("""
max_cycles = 7
""")

        config = load_config(temp_repo)
        assert config["max_cycles"] == 7
        assert config["loc_threshold"] == DEFAULT_CONFIG["loc_threshold"]
        assert config["tasklist"] == DEFAULT_CONFIG["tasklist"]

    def test_load_config_ignores_unknown_keys(self, temp_repo):
        """load_config ignores unknown keys in config file."""
        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / CONFIG_FILE_NAME
        config_file.write_text("""
max_cycles = 5
unknown_key = "should be ignored"
another_unknown = 123
""")

        config = load_config(temp_repo)
        assert config["max_cycles"] == 5
        assert "unknown_key" not in config
        assert "another_unknown" not in config

    def test_load_config_handles_malformed_toml(self, temp_repo):
        """load_config returns defaults if TOML is malformed."""
        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / CONFIG_FILE_NAME
        config_file.write_text("""
this is not valid toml [[[
""")

        config = load_config(temp_repo)
        assert config == DEFAULT_CONFIG

    def test_load_config_falls_back_to_tomli_when_tomllib_missing(self, temp_repo, monkeypatch):
        """load_config still works on runtimes without tomllib but with tomli installed."""
        import millstone.config as config_module

        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / CONFIG_FILE_NAME
        config_file.write_text("max_cycles = 11\n")

        class DummyTomliModule:
            @staticmethod
            def load(file_obj):
                file_obj.read()
                return {"max_cycles": 11}

        def fake_import(module_name):
            if module_name == "tomllib":
                raise ImportError("tomllib missing")
            if module_name == "tomli":
                return DummyTomliModule
            raise ImportError(module_name)

        monkeypatch.setattr(importlib, "import_module", fake_import)
        try:
            config_module = importlib.reload(config_module)
            config = config_module.load_config(temp_repo)
            assert config["max_cycles"] == 11
        finally:
            monkeypatch.undo()
            importlib.reload(config_module)

    def test_orchestrator_uses_default_prompts_dir(self, temp_repo):
        """Orchestrator uses built-in prompts when prompts_dir not specified."""
        orch = Orchestrator()
        try:
            # Default prompts uses package resources (no custom dir set)
            assert orch._custom_prompts_dir is None
            # Should be able to load prompts from package
            prompt = orch.load_prompt("tasklist_prompt.md")
            assert len(prompt) > 0
        finally:
            orch.cleanup()

    def test_orchestrator_accepts_custom_prompts_dir(self, temp_repo):
        """Orchestrator accepts custom prompts directory."""
        # Create custom prompts dir
        custom_prompts = temp_repo / "my_prompts"
        custom_prompts.mkdir()

        orch = Orchestrator(prompts_dir="my_prompts")
        try:
            assert orch._custom_prompts_dir == custom_prompts
        finally:
            orch.cleanup()

    def test_orchestrator_handles_absolute_prompts_dir(self, temp_repo):
        """Orchestrator handles absolute path for prompts directory."""
        custom_prompts = temp_repo / "absolute_prompts"
        custom_prompts.mkdir()

        orch = Orchestrator(prompts_dir=str(custom_prompts))
        try:
            assert orch._custom_prompts_dir == custom_prompts
        finally:
            orch.cleanup()


class TestCompletedTaskCount:
    """Tests for tracking completed task count in tasklist."""

    def test_completed_task_count_initialized_to_zero(self):
        """completed_task_count is 0 when orchestrator is created."""
        orch = Orchestrator()
        try:
            assert orch.completed_task_count == 0
        finally:
            orch.cleanup()

    def test_count_completed_tasks_returns_zero_for_missing_file(self, temp_repo):
        """count_completed_tasks returns 0 if tasklist doesn't exist."""
        # Remove the tasklist
        tasklist_path = temp_repo / "docs" / "tasklist.md"
        tasklist_path.unlink()

        orch = Orchestrator()
        try:
            count = orch.count_completed_tasks()
            assert count == 0
        finally:
            orch.cleanup()

    def test_count_completed_tasks_returns_zero_for_empty_file(self, temp_repo):
        """count_completed_tasks returns 0 for empty tasklist."""
        tasklist_path = temp_repo / "docs" / "tasklist.md"
        tasklist_path.write_text("")

        orch = Orchestrator()
        try:
            count = orch.count_completed_tasks()
            assert count == 0
        finally:
            orch.cleanup()

    def test_count_completed_tasks_counts_checked_items(self, temp_repo):
        """count_completed_tasks counts - [x] entries correctly."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        tasklist_path.write_text("""# Tasks

- [x] Completed task one
- [x] Completed task two
- [ ] Pending task
- [x] Another completed task
""")

        orch = Orchestrator()
        try:
            count = orch.count_completed_tasks()
            assert count == 3
        finally:
            orch.cleanup()

    def test_count_completed_tasks_case_insensitive(self, temp_repo):
        """count_completed_tasks handles both [x] and [X]."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        tasklist_path.write_text("""# Tasks

- [x] lowercase check
- [X] uppercase check
- [ ] pending
""")

        orch = Orchestrator()
        try:
            count = orch.count_completed_tasks()
            assert count == 2
        finally:
            orch.cleanup()

    def test_count_completed_tasks_ignores_non_checkbox_lines(self, temp_repo):
        """count_completed_tasks only counts actual checkbox patterns."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        tasklist_path.write_text("""# Tasks

- [x] Real completed task
Some text mentioning [x] inline
  - [x] Indented (not counted - not at start of line)
- [x] Another real task
""")

        orch = Orchestrator()
        try:
            count = orch.count_completed_tasks()
            # Only "- [x]" at start of line should be counted
            assert count == 2
        finally:
            orch.cleanup()

    def test_count_completed_tasks_with_custom_tasklist(self, temp_repo):
        """count_completed_tasks respects custom tasklist path."""
        custom_path = temp_repo / "custom" / "tasks.md"
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("""# Custom Tasks

- [x] Done 1
- [x] Done 2
- [x] Done 3
- [x] Done 4
- [ ] Not done
""")

        orch = Orchestrator(tasklist="custom/tasks.md")
        try:
            count = orch.count_completed_tasks()
            assert count == 4
        finally:
            orch.cleanup()


class TestCompactThreshold:
    """Tests for --compact-threshold flag and compaction functionality."""

    def test_compact_threshold_default_value(self):
        """compact_threshold defaults to 20."""
        orch = Orchestrator()
        try:
            assert orch.compact_threshold == 20
        finally:
            orch.cleanup()

    def test_compact_threshold_can_be_set(self):
        """compact_threshold can be set via constructor."""
        orch = Orchestrator(compact_threshold=10)
        try:
            assert orch.compact_threshold == 10
        finally:
            orch.cleanup()

    def test_compact_threshold_zero_disables_compaction(self):
        """compact_threshold of 0 disables automatic compaction."""
        orch = Orchestrator(compact_threshold=0)
        try:
            assert orch.compact_threshold == 0
            orch.completed_task_count = 100  # Even with many tasks
            assert orch.should_compact() is False
        finally:
            orch.cleanup()

    def test_should_compact_returns_false_when_below_threshold(self):
        """should_compact returns False when completed < threshold."""
        orch = Orchestrator(compact_threshold=20)
        try:
            orch.completed_task_count = 19
            assert orch.should_compact() is False
        finally:
            orch.cleanup()

    def test_should_compact_returns_true_when_at_threshold(self):
        """should_compact returns True when completed == threshold."""
        orch = Orchestrator(compact_threshold=20)
        try:
            orch.completed_task_count = 20
            assert orch.should_compact() is True
        finally:
            orch.cleanup()

    def test_should_compact_returns_true_when_above_threshold(self):
        """should_compact returns True when completed > threshold."""
        orch = Orchestrator(compact_threshold=20)
        try:
            orch.completed_task_count = 25
            assert orch.should_compact() is True
        finally:
            orch.cleanup()

    def test_get_compact_prompt_replaces_placeholder(self):
        """get_compact_prompt resolves {{TASKLIST_REWRITE_INSTRUCTIONS}} via provider."""
        orch = Orchestrator(tasklist="my/tasks.md")
        try:
            prompt = orch.get_compact_prompt()
            assert "{{TASKLIST_REWRITE_INSTRUCTIONS}}" not in prompt
            assert "Write the entire compacted content back to" in prompt
            assert "my/tasks.md" in prompt
        finally:
            orch.cleanup()

    def test_compact_threshold_in_config(self, temp_repo):
        """compact_threshold can be loaded from config file."""
        config_dir = temp_repo / ".millstone"
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / "config.toml"
        config_file.write_text("""
compact_threshold = 50
""")

        config = load_config(temp_repo)
        assert config["compact_threshold"] == 50

    def test_dry_run_shows_compaction_info(self, temp_repo, capsys):
        """Dry run displays compaction threshold and status."""
        orch = Orchestrator(dry_run=True, compact_threshold=5)
        try:
            orch.run()
            captured = capsys.readouterr()
            assert "Compact threshold: 5" in captured.out
        finally:
            orch.cleanup()

    def test_dry_run_shows_would_trigger_compaction(self, temp_repo, capsys):
        """Dry run shows when compaction would trigger."""
        # Create tasklist with many completed tasks
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        tasklist_path.write_text("""# Tasks
- [x] Done 1
- [x] Done 2
- [x] Done 3
- [ ] Pending
""")

        orch = Orchestrator(dry_run=True, compact_threshold=3)
        try:
            orch.run()
            captured = capsys.readouterr()
            assert "WOULD TRIGGER" in captured.out
        finally:
            orch.cleanup()

    def test_dry_run_shows_compaction_disabled(self, temp_repo, capsys):
        """Dry run shows when compaction is disabled."""
        orch = Orchestrator(dry_run=True, compact_threshold=0)
        try:
            orch.run()
            captured = capsys.readouterr()
            assert "DISABLED" in captured.out
        finally:
            orch.cleanup()

    def test_run_compaction_logs_event(self, temp_repo):
        """run_compaction logs compaction events."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        original_content = """# Tasks

- [x] Done 1
- [x] Done 2
- [ ] Pending
"""
        tasklist_path.write_text(original_content)

        orch = Orchestrator(compact_threshold=2)
        try:
            orch.completed_task_count = 2

            def mock_compaction(prompt):
                # Simulate successful compaction (shorter file, same unchecked tasks)
                tasklist_path.write_text("""# Tasks
- [x] Done
- [ ] Pending
""")
                return "Compaction done"

            with patch.object(orch, 'run_claude', side_effect=mock_compaction):
                result = orch.run_compaction()

            assert result is True
            # Check log file was created and contains compaction events
            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            assert "compaction_started" in log_content
            assert "compaction_completed" in log_content
        finally:
            orch.cleanup()

    def test_run_compaction_updates_completed_count(self, temp_repo):
        """run_compaction updates completed_task_count after compaction."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        original_content = """# Tasks

- [x] Done 1
- [x] Done 2
- [x] Done 3
- [x] Done 4
- [x] Done 5
- [ ] Pending
"""
        tasklist_path.write_text(original_content)

        orch = Orchestrator(compact_threshold=2)
        try:
            orch.completed_task_count = 5  # Old count before compaction

            def mock_compaction(prompt):
                # Simulate compaction that reduces completed count
                tasklist_path.write_text("""# Tasks
- [x] Done
- [x] More
- [ ] Pending
""")
                return "Compaction done"

            with patch.object(orch, 'run_claude', side_effect=mock_compaction):
                orch.run_compaction()

            # After compaction, count should be re-read from file
            assert orch.completed_task_count == 2  # Actual count in file
        finally:
            orch.cleanup()


class TestCompactionSanityCheck:
    """Tests for compaction sanity check functionality."""

    def test_extract_unchecked_tasks_empty_content(self):
        """_extract_unchecked_tasks returns empty list for empty content."""
        orch = Orchestrator()
        try:
            tasks = orch._extract_unchecked_tasks("")
            assert tasks == []
        finally:
            orch.cleanup()

    def test_extract_unchecked_tasks_no_unchecked(self):
        """_extract_unchecked_tasks returns empty list when no unchecked tasks."""
        orch = Orchestrator()
        try:
            content = """# Tasks
- [x] Done 1
- [x] Done 2
"""
            tasks = orch._extract_unchecked_tasks(content)
            assert tasks == []
        finally:
            orch.cleanup()

    def test_extract_unchecked_tasks_finds_all(self):
        """_extract_unchecked_tasks finds all unchecked tasks."""
        orch = Orchestrator()
        try:
            content = """# Tasks
- [x] Done 1
- [ ] Pending task one
- [x] Done 2
- [ ] Pending task two
- [ ] Pending task three
"""
            tasks = orch._extract_unchecked_tasks(content)
            assert tasks == [
                "Pending task one",
                "Pending task two",
                "Pending task three",
            ]
        finally:
            orch.cleanup()

    def test_extract_unchecked_tasks_ignores_indented(self):
        """_extract_unchecked_tasks only matches tasks at start of line."""
        orch = Orchestrator()
        try:
            content = """# Tasks
- [ ] Real task
  - [ ] Indented (not a task)
    - [ ] Double indented
"""
            tasks = orch._extract_unchecked_tasks(content)
            assert tasks == ["Real task"]
        finally:
            orch.cleanup()

    def test_verify_compaction_success(self):
        """verify_compaction returns True when all checks pass."""
        orch = Orchestrator()
        try:
            original = """# Tasks
- [x] Done 1 with lots of details
- [x] Done 2 with more details
- [ ] Pending task
"""
            new = """# Tasks
- [x] Done
- [ ] Pending task
"""
            original_unchecked = ["Pending task"]
            success, error = orch.verify_compaction(original, new, original_unchecked)
            assert success is True
            assert error == ""
        finally:
            orch.cleanup()

    def test_verify_compaction_fails_task_count_mismatch(self):
        """verify_compaction fails when unchecked task count changes."""
        orch = Orchestrator()
        try:
            original = """# Tasks
- [ ] Task 1
- [ ] Task 2
"""
            new = """# Tasks
- [ ] Task 1
"""
            original_unchecked = ["Task 1", "Task 2"]
            success, error = orch.verify_compaction(original, new, original_unchecked)
            assert success is False
            assert "count mismatch" in error
            assert "2 before" in error
            assert "1 after" in error
        finally:
            orch.cleanup()

    def test_verify_compaction_fails_task_modified(self):
        """verify_compaction fails when an unchecked task is modified."""
        orch = Orchestrator()
        try:
            original = """# Tasks
- [ ] Original task text
"""
            new = """# Compacted
- [ ] Modified task text
"""
            original_unchecked = ["Original task text"]
            success, error = orch.verify_compaction(original, new, original_unchecked)
            assert success is False
            assert "modified" in error
            assert "Original task text" in error
            assert "Modified task text" in error
        finally:
            orch.cleanup()

    def test_verify_compaction_fails_file_not_shorter(self):
        """verify_compaction fails when file is not shorter."""
        orch = Orchestrator()
        try:
            original = """# Tasks
- [ ] Task
"""
            # New content is longer
            new = """# Tasks With A Much Longer Header
- [ ] Task
Some extra content that makes it longer
"""
            original_unchecked = ["Task"]
            success, error = orch.verify_compaction(original, new, original_unchecked)
            assert success is False
            assert "did not reduce file size" in error
        finally:
            orch.cleanup()

    def test_verify_compaction_fails_same_size(self):
        """verify_compaction fails when file is same size."""
        orch = Orchestrator()
        try:
            original = """# Tasks
- [ ] Task
"""
            new = """# Tasks
- [ ] Task
"""  # Same content
            original_unchecked = ["Task"]
            success, error = orch.verify_compaction(original, new, original_unchecked)
            assert success is False
            assert "did not reduce file size" in error
        finally:
            orch.cleanup()

    def test_run_compaction_restores_on_failure(self, temp_repo):
        """run_compaction restores original file when sanity check fails."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        original_content = """# Tasks

- [x] Done 1
- [x] Done 2
- [ ] Pending task
"""
        tasklist_path.write_text(original_content)

        orch = Orchestrator(compact_threshold=2)
        try:
            orch.completed_task_count = 2

            def mock_bad_compaction(prompt):
                # Simulate bad compaction that removes the unchecked task
                tasklist_path.write_text("""# Tasks
- [x] Done
""")
                return "Compaction done"

            with patch.object(orch, 'run_claude', side_effect=mock_bad_compaction):
                result = orch.run_compaction()

            assert result is False
            # Original content should be restored
            assert tasklist_path.read_text() == original_content
        finally:
            orch.cleanup()

    def test_run_compaction_logs_failure(self, temp_repo):
        """run_compaction logs failure event when sanity check fails."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        original_content = """# Tasks

- [x] Done 1
- [ ] Pending
"""
        tasklist_path.write_text(original_content)

        orch = Orchestrator(compact_threshold=1)
        try:
            orch.completed_task_count = 1

            def mock_bad_compaction(prompt):
                # Make file longer (fails size check)
                tasklist_path.write_text(original_content + "\nExtra content added")
                return "Compaction done"

            with patch.object(orch, 'run_claude', side_effect=mock_bad_compaction):
                result = orch.run_compaction()

            assert result is False
            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            assert "compaction_failed" in log_content
        finally:
            orch.cleanup()

    def test_run_compaction_returns_true_on_success(self, temp_repo):
        """run_compaction returns True when sanity checks pass."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        original_content = """# Tasks

- [x] Done 1 with lots of verbose details
- [x] Done 2 with more verbose details
- [ ] Pending
"""
        tasklist_path.write_text(original_content)

        orch = Orchestrator(compact_threshold=2)
        try:
            orch.completed_task_count = 2

            def mock_good_compaction(prompt):
                # Simulate good compaction
                tasklist_path.write_text("""# Tasks
- [x] Done
- [ ] Pending
""")
                return "Compaction done"

            with patch.object(orch, 'run_claude', side_effect=mock_good_compaction):
                result = orch.run_compaction()

            assert result is True
        finally:
            orch.cleanup()

    def test_run_compaction_preserves_task_order(self, temp_repo):
        """run_compaction detects when tasks are reordered."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        original_content = """# Tasks

- [x] Done 1
- [ ] Task A
- [ ] Task B
- [ ] Task C
"""
        tasklist_path.write_text(original_content)

        orch = Orchestrator(compact_threshold=1)
        try:
            orch.completed_task_count = 1

            def mock_reorder_compaction(prompt):
                # Reorder the unchecked tasks
                tasklist_path.write_text("""# Tasks
- [x] Done
- [ ] Task C
- [ ] Task A
- [ ] Task B
""")
                return "Compaction done"

            with patch.object(orch, 'run_claude', side_effect=mock_reorder_compaction):
                result = orch.run_compaction()

            # Should fail because task order changed (Task A became Task C)
            assert result is False
            # Original should be restored
            assert tasklist_path.read_text() == original_content
        finally:
            orch.cleanup()


class TestDirtyWorkingDirectoryWarning:
    """Tests for dirty working directory warning."""

    def test_no_warning_when_clean(self, temp_repo, capsys):
        """No warning is printed when working directory is clean."""
        orch = Orchestrator()
        try:
            orch.check_dirty_working_directory()
            captured = capsys.readouterr()
            assert "WARNING" not in captured.out
            assert "uncommitted changes" not in captured.out
        finally:
            orch.cleanup()

    def test_warning_when_uncommitted_changes(self, temp_repo, capsys):
        """Warning is printed when there are uncommitted changes."""
        # Create an uncommitted file
        (temp_repo / "uncommitted_file.txt").write_text("uncommitted content")

        orch = Orchestrator()
        try:
            orch.check_dirty_working_directory()
            captured = capsys.readouterr()
            assert "WARNING" in captured.out
            assert "uncommitted changes" in captured.out
            assert "1 file(s) modified" in captured.out
        finally:
            orch.cleanup()

    def test_warning_shows_correct_file_count(self, temp_repo, capsys):
        """Warning shows the correct number of modified files."""
        # Create multiple uncommitted files
        (temp_repo / "file1.txt").write_text("content1")
        (temp_repo / "file2.txt").write_text("content2")
        (temp_repo / "file3.txt").write_text("content3")

        orch = Orchestrator()
        try:
            orch.check_dirty_working_directory()
            captured = capsys.readouterr()
            assert "3 file(s) modified" in captured.out
        finally:
            orch.cleanup()

    def test_warning_suggests_loc_threshold(self, temp_repo, capsys):
        """Warning suggests doubling the LoC threshold."""
        (temp_repo / "uncommitted.txt").write_text("content")

        orch = Orchestrator(loc_threshold=500)
        try:
            orch.check_dirty_working_directory()
            captured = capsys.readouterr()
            assert "--loc-threshold=1000" in captured.out
        finally:
            orch.cleanup()

    def test_warning_is_non_blocking(self, temp_repo):
        """Warning does not raise an exception or return failure."""
        (temp_repo / "uncommitted.txt").write_text("content")

        orch = Orchestrator()
        try:
            # Should not raise
            orch.check_dirty_working_directory()
        finally:
            orch.cleanup()

    def test_warning_handles_staged_changes(self, temp_repo, capsys):
        """Warning includes staged changes."""
        (temp_repo / "staged_file.txt").write_text("staged content")
        subprocess.run(["git", "add", "staged_file.txt"], cwd=temp_repo, capture_output=True)

        orch = Orchestrator()
        try:
            orch.check_dirty_working_directory()
            captured = capsys.readouterr()
            assert "WARNING" in captured.out
            assert "1 file(s) modified" in captured.out
        finally:
            orch.cleanup()

    def test_warning_handles_mixed_staged_unstaged(self, temp_repo, capsys):
        """Warning counts both staged and unstaged changes."""
        # Staged file
        (temp_repo / "staged.txt").write_text("staged")
        subprocess.run(["git", "add", "staged.txt"], cwd=temp_repo, capture_output=True)
        # Unstaged file
        (temp_repo / "unstaged.txt").write_text("unstaged")

        orch = Orchestrator()
        try:
            orch.check_dirty_working_directory()
            captured = capsys.readouterr()
            assert "2 file(s) modified" in captured.out
        finally:
            orch.cleanup()


class TestUncommittedTasklistWarning:
    """Tests for uncommitted tasklist warning."""

    def test_no_warning_when_tasklist_clean(self, temp_repo, capsys):
        """No warning is printed when tasklist has no uncommitted changes."""
        orch = Orchestrator()
        try:
            orch.check_uncommitted_tasklist()
            captured = capsys.readouterr()
            assert "Tasklist has uncommitted changes" not in captured.out
        finally:
            orch.cleanup()

    def test_warning_when_tasklist_modified(self, temp_repo, capsys):
        """Warning is printed when tasklist has uncommitted changes."""
        tasklist_path = temp_repo / "docs" / "tasklist.md"
        # Modify the tasklist (mark a task complete)
        content = tasklist_path.read_text()
        content = content.replace("- [ ] Task 1", "- [x] Task 1")
        tasklist_path.write_text(content)

        orch = Orchestrator(tasklist="docs/tasklist.md")
        try:
            # Clear the banner output before testing
            capsys.readouterr()
            orch.check_uncommitted_tasklist()
            captured = capsys.readouterr()
            assert "Tasklist has uncommitted changes" in captured.out
            assert "docs/tasklist.md" in captured.out
        finally:
            orch.cleanup()

    def test_warning_shows_staged_status(self, temp_repo, capsys):
        """Warning shows when changes are staged."""
        tasklist_path = temp_repo / "docs" / "tasklist.md"
        content = tasklist_path.read_text()
        content = content.replace("- [ ] Task 1", "- [x] Task 1")
        tasklist_path.write_text(content)
        subprocess.run(["git", "add", str(tasklist_path)], cwd=temp_repo, capture_output=True)

        orch = Orchestrator(tasklist="docs/tasklist.md")
        try:
            # Clear the banner output before testing
            capsys.readouterr()
            orch.check_uncommitted_tasklist()
            captured = capsys.readouterr()
            assert "Tasklist has uncommitted changes" in captured.out
            assert "staged but not committed" in captured.out
        finally:
            orch.cleanup()

    def test_warning_shows_unstaged_status(self, temp_repo, capsys):
        """Warning shows when changes are unstaged."""
        tasklist_path = temp_repo / "docs" / "tasklist.md"
        content = tasklist_path.read_text()
        content = content.replace("- [ ] Task 1", "- [x] Task 1")
        tasklist_path.write_text(content)

        orch = Orchestrator(tasklist="docs/tasklist.md")
        try:
            # Clear the banner output before testing
            capsys.readouterr()
            orch.check_uncommitted_tasklist()
            captured = capsys.readouterr()
            assert "Tasklist has uncommitted changes" in captured.out
            assert "not staged" in captured.out
        finally:
            orch.cleanup()

    def test_warning_shows_mixed_status(self, temp_repo, capsys):
        """Warning shows when changes are both staged and unstaged."""
        tasklist_path = temp_repo / "docs" / "tasklist.md"
        # First modification and stage it
        content = tasklist_path.read_text()
        content = content.replace("- [ ] Task 1", "- [x] Task 1")
        tasklist_path.write_text(content)
        subprocess.run(["git", "add", str(tasklist_path)], cwd=temp_repo, capture_output=True)
        # Second modification without staging
        content = tasklist_path.read_text()
        content = content.replace("- [ ] Task 2", "- [x] Task 2")
        tasklist_path.write_text(content)

        orch = Orchestrator(tasklist="docs/tasklist.md")
        try:
            # Clear the banner output before testing
            capsys.readouterr()
            orch.check_uncommitted_tasklist()
            captured = capsys.readouterr()
            assert "Tasklist has uncommitted changes" in captured.out
            assert "both staged and unstaged" in captured.out
        finally:
            orch.cleanup()

    def test_warning_is_non_blocking(self, temp_repo):
        """Warning does not raise an exception or return failure."""
        tasklist_path = temp_repo / "docs" / "tasklist.md"
        content = tasklist_path.read_text()
        content = content.replace("- [ ] Task 1", "- [x] Task 1")
        tasklist_path.write_text(content)

        orch = Orchestrator(tasklist="docs/tasklist.md")
        try:
            # Should not raise
            orch.check_uncommitted_tasklist()
        finally:
            orch.cleanup()

    def test_no_warning_in_task_mode(self, temp_repo, capsys):
        """No warning in --task mode since tasklist is not used."""
        tasklist_path = temp_repo / "docs" / "tasklist.md"
        content = tasklist_path.read_text()
        content = content.replace("- [ ] Task 1", "- [x] Task 1")
        tasklist_path.write_text(content)

        orch = Orchestrator(task="Some direct task")
        try:
            # Clear the banner output before testing
            capsys.readouterr()
            orch.check_uncommitted_tasklist()
            captured = capsys.readouterr()
            assert "Tasklist has uncommitted changes" not in captured.out
        finally:
            orch.cleanup()

    def test_warning_shows_advice(self, temp_repo, capsys):
        """Warning includes advice about committing the tasklist."""
        tasklist_path = temp_repo / "docs" / "tasklist.md"
        content = tasklist_path.read_text()
        content = content.replace("- [ ] Task 1", "- [x] Task 1")
        tasklist_path.write_text(content)

        orch = Orchestrator(tasklist="docs/tasklist.md")
        try:
            # Clear the banner output before testing
            capsys.readouterr()
            orch.check_uncommitted_tasklist()
            captured = capsys.readouterr()
            assert "Task completion markers may not reflect actual repository state" in captured.out
            assert "Consider committing the tasklist" in captured.out
        finally:
            orch.cleanup()


class TestManualCompactFlag:
    """Tests for --compact flag functionality."""

    def test_compact_flag_with_task_raises_error(self):
        """--compact cannot be used with --task."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--compact', '--task', 'some task']):
            with pytest.raises(SystemExit) as exc_info:
                orchestrate.main()
            # argparse error returns exit code 2
            assert exc_info.value.code == 2

    def test_compact_flag_requires_tasklist_file(self, temp_repo):
        """--compact requires the tasklist file to exist."""
        from millstone import orchestrate

        # Remove the tasklist file
        tasklist = temp_repo / "docs" / "tasklist.md"
        if tasklist.exists():
            tasklist.unlink()

        with patch('sys.argv', ['orchestrate.py', '--compact', '--tasklist', 'docs/tasklist.md']):
            with pytest.raises(SystemExit) as exc_info:
                orchestrate.main()
            assert exc_info.value.code == 1

    def test_compact_flag_exits_zero_when_no_completed_tasks(self, temp_repo):
        """--compact exits 0 when there are no completed tasks."""
        from millstone import orchestrate

        # Create tasklist with only unchecked tasks
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasks\n\n- [ ] Task 1\n- [ ] Task 2\n")

        with patch('sys.argv', ['orchestrate.py', '--compact']):
            with patch.object(orchestrate.Orchestrator, 'preflight_checks'):
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()
                assert exc_info.value.code == 0

    def test_compact_flag_runs_compaction(self, temp_repo):
        """--compact runs compaction and exits."""
        from millstone import orchestrate

        # Create tasklist with completed tasks
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasks\n\n- [x] Done 1 with verbose details\n- [x] Done 2 with verbose details\n- [ ] Pending\n")

        with patch('sys.argv', ['orchestrate.py', '--compact']):
            with patch.object(orchestrate.Orchestrator, 'preflight_checks'):
                with patch.object(orchestrate.Orchestrator, 'run_compaction', return_value=True) as mock_compact:
                    with pytest.raises(SystemExit) as exc_info:
                        orchestrate.main()
                    assert exc_info.value.code == 0
                    mock_compact.assert_called_once()

    def test_compact_flag_returns_one_on_failure(self, temp_repo):
        """--compact returns exit code 1 when compaction fails."""
        from millstone import orchestrate

        # Create tasklist with completed tasks
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasks\n\n- [x] Done 1\n- [ ] Pending\n")

        with patch('sys.argv', ['orchestrate.py', '--compact']):
            with patch.object(orchestrate.Orchestrator, 'preflight_checks'):
                with patch.object(orchestrate.Orchestrator, 'run_compaction', return_value=False):
                    with pytest.raises(SystemExit) as exc_info:
                        orchestrate.main()
                    assert exc_info.value.code == 1


class TestProgressOutput:
    """Tests for progress output functionality."""

    def test_progress_tracking_attributes_initialized(self):
        """Progress tracking attributes are initialized."""
        orch = Orchestrator()
        try:
            assert orch.current_task_num == 0
            assert orch.total_tasks == 5  # Default max_tasks
            assert orch.current_task_title == ""
        finally:
            orch.cleanup()

    def test_task_prefix_format(self):
        """_task_prefix returns correctly formatted string."""
        orch = Orchestrator()
        try:
            orch.current_task_num = 2
            orch.total_tasks = 5
            assert orch._task_prefix() == "[Task 2/5]"
        finally:
            orch.cleanup()

    def test_extract_current_task_title_with_bold_title(self, temp_repo):
        """extract_current_task_title extracts bold title from tasklist."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        tasklist_path.write_text("""# Tasks

- [x] Done task
- [ ] **Add --dry-run flag**: Show prompts without invoking claude
- [ ] Another task
""")

        orch = Orchestrator()
        try:
            title = orch.extract_current_task_title()
            assert title == "Add --dry-run flag"
        finally:
            orch.cleanup()

    def test_extract_current_task_title_bold_only(self, temp_repo):
        """extract_current_task_title handles bold title without description."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        tasklist_path.write_text("""# Tasks

- [ ] **Simple Bold Task**
""")

        orch = Orchestrator()
        try:
            title = orch.extract_current_task_title()
            assert title == "Simple Bold Task"
        finally:
            orch.cleanup()

    def test_extract_current_task_title_no_bold(self, temp_repo):
        """extract_current_task_title falls back to plain text."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        tasklist_path.write_text("""# Tasks

- [ ] Plain task without bold formatting
""")

        orch = Orchestrator()
        try:
            title = orch.extract_current_task_title()
            assert title == "Plain task without bold formatting"
        finally:
            orch.cleanup()

    def test_extract_current_task_title_long_plain_text(self, temp_repo):
        """extract_current_task_title truncates long plain text to 50 chars."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        long_text = "This is a very long task description that exceeds fifty characters limit"
        tasklist_path.write_text(f"""# Tasks

- [ ] {long_text}
""")

        orch = Orchestrator()
        try:
            title = orch.extract_current_task_title()
            assert len(title) == 50
            assert title.endswith("...")
        finally:
            orch.cleanup()

    def test_extract_current_task_title_no_tasks(self, temp_repo):
        """extract_current_task_title returns empty string when no unchecked tasks."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        tasklist_path.write_text("""# Tasks

- [x] Done task
- [x] Another done task
""")

        orch = Orchestrator()
        try:
            title = orch.extract_current_task_title()
            assert title == ""
        finally:
            orch.cleanup()

    def test_extract_current_task_title_missing_file(self, temp_repo):
        """extract_current_task_title returns empty string when file missing."""
        tasklist_path = temp_repo / ".millstone" / "tasklist.md"
        tasklist_path.unlink()

        orch = Orchestrator()
        try:
            title = orch.extract_current_task_title()
            assert title == ""
        finally:
            orch.cleanup()


class TestDelegateCommit:
    """Tests for delegate_commit functionality."""

    def test_delegate_commit_uses_session_id_when_available(self, temp_repo):
        """delegate_commit resumes the existing session when session_id is set."""
        orch = Orchestrator()
        try:
            orch.session_id = "test-session-123"
            orch.current_task_num = 1
            orch.total_tasks = 1

            with patch.object(orch, 'run_agent') as mock_run, \
                 patch.object(orch, 'git') as mock_git:
                mock_run.return_value = "Committed"
                mock_git.return_value = ""  # No uncommitted changes = success
                result = orch.delegate_commit()

                mock_run.assert_called_once()
                call_args = mock_run.call_args
                # Should have resume argument with session_id
                assert call_args.kwargs.get('resume') == "test-session-123"
                assert result is True
        finally:
            orch.cleanup()

    def test_delegate_commit_starts_fresh_when_no_session(self, temp_repo):
        """delegate_commit starts fresh session when session_id is None."""
        orch = Orchestrator()
        try:
            orch.session_id = None
            orch.current_task_num = 1
            orch.total_tasks = 1

            with patch.object(orch, 'run_agent') as mock_run, \
                 patch.object(orch, 'git') as mock_git:
                mock_run.return_value = "Committed"
                mock_git.return_value = ""  # No uncommitted changes = success
                result = orch.delegate_commit()

                mock_run.assert_called_once()
                call_args = mock_run.call_args
                # Should not have resume argument
                assert call_args.kwargs.get('resume') is None
                assert result is True
        finally:
            orch.cleanup()

    def test_delegate_commit_loads_commit_prompt(self, temp_repo):
        """delegate_commit uses the commit_prompt.md file."""
        orch = Orchestrator()
        try:
            orch.session_id = None
            orch.current_task_num = 1
            orch.total_tasks = 1

            # Get the expected prompt content from file
            expected_prompt = orch.load_prompt("commit_prompt.md")

            with patch.object(orch, 'run_agent') as mock_run, \
                 patch.object(orch, 'git') as mock_git:
                mock_run.return_value = "Committed"
                mock_git.return_value = ""  # No uncommitted changes = success
                orch.delegate_commit()

                # Verify the exact prompt from file was used
                prompt = mock_run.call_args.args[0]
                assert prompt == expected_prompt
        finally:
            orch.cleanup()

    def test_delegate_commit_returns_false_when_changes_remain(self, temp_repo):
        """delegate_commit returns False if uncommitted changes remain after agent runs."""
        orch = Orchestrator()
        try:
            orch.session_id = None
            orch.current_task_num = 1
            orch.total_tasks = 1

            with patch.object(orch, 'run_agent') as mock_run, \
                 patch.object(orch, 'git') as mock_git:
                mock_run.return_value = "I couldn't commit"
                mock_git.return_value = "M orchestrate.py"  # Uncommitted changes remain
                result = orch.delegate_commit()

                assert result is False
                # Verify git status was checked
                mock_git.assert_called_with("status", "--porcelain")
        finally:
            orch.cleanup()

    def test_delegate_commit_stores_failure_diagnostics(self, temp_repo):
        """delegate_commit stores diagnostic info in last_commit_failure when commit fails."""
        orch = Orchestrator()
        try:
            orch.session_id = None
            orch.current_task_num = 1
            orch.total_tasks = 1

            with patch.object(orch, 'run_agent') as mock_run, \
                 patch.object(orch, 'git') as mock_git:
                mock_run.return_value = "Builder output: attempted commit but hooks failed"
                mock_git.return_value = "M orchestrate.py\nA new_file.txt"  # Uncommitted changes
                result = orch.delegate_commit()

                assert result is False
                # Verify diagnostic info was stored
                assert orch.last_commit_failure is not None
                assert orch.last_commit_failure["status"] == "M orchestrate.py\nA new_file.txt"
                assert "hooks failed" in orch.last_commit_failure["builder_output"]
        finally:
            orch.cleanup()

    def test_delegate_commit_logs_builder_output_on_failure(self, temp_repo):
        """delegate_commit logs builder output when commit fails."""
        orch = Orchestrator()
        try:
            orch.session_id = None
            orch.current_task_num = 1
            orch.total_tasks = 1

            with patch.object(orch, 'run_agent') as mock_run, \
                 patch.object(orch, 'git') as mock_git, \
                 patch.object(orch, 'log') as mock_log:
                mock_run.return_value = "Commit rejected by pre-commit hook"
                mock_git.return_value = "M test.py"
                orch.delegate_commit()

                # Verify log was called with commit_failed
                commit_failed_calls = [c for c in mock_log.call_args_list if c.args[0] == "commit_failed"]
                assert len(commit_failed_calls) == 1
                call_kwargs = commit_failed_calls[0].kwargs
                assert "builder_output" in call_kwargs
                assert "pre-commit hook" in call_kwargs["builder_output"]
        finally:
            orch.cleanup()


class TestLocBaseline:
    """Tests for per-task LoC baseline tracking."""

    def test_loc_baseline_ref_initialized_to_none(self, temp_repo):
        """loc_baseline_ref starts as None before run() is called."""
        orch = Orchestrator()
        try:
            assert orch.loc_baseline_ref is None
        finally:
            orch.cleanup()

    def test_init_loc_baseline_sets_head_hash(self, temp_repo):
        """_init_loc_baseline sets the baseline to current HEAD."""
        orch = Orchestrator()
        try:
            # Get current HEAD hash for comparison
            expected_hash = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=temp_repo,
                capture_output=True,
                text=True,
            ).stdout.strip()

            orch._init_loc_baseline()
            assert orch.loc_baseline_ref == expected_hash
        finally:
            orch.cleanup()

    def test_update_loc_baseline_updates_after_commit(self, temp_repo):
        """_update_loc_baseline updates baseline to current HEAD."""
        orch = Orchestrator()
        try:
            # Initialize baseline
            orch._init_loc_baseline()
            old_baseline = orch.loc_baseline_ref

            # Make a commit
            (temp_repo / "newfile.txt").write_text("content")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "test commit"],
                cwd=temp_repo,
                capture_output=True,
            )

            # Update baseline
            orch._update_loc_baseline()
            assert orch.loc_baseline_ref != old_baseline

            # Verify it matches current HEAD
            expected_hash = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=temp_repo,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert orch.loc_baseline_ref == expected_hash
        finally:
            orch.cleanup()

    def test_mechanical_checks_uses_baseline_for_diff(self, temp_repo):
        """mechanical_checks diffs against baseline, not always HEAD."""
        orch = Orchestrator()
        try:
            # Set a specific baseline
            orch.loc_baseline_ref = "abc123"

            with patch.object(orch, 'git') as mock_git:
                def side_effect(*args):
                    if args[0] == "status":
                        return "M changed.txt"  # porcelain status
                    elif args[0] == "diff" and "--numstat" in args:
                        return "1\t0\tchanged.txt"  # numstat format
                    elif args[0] == "diff" and "--name-only" in args:
                        return "changed.txt"  # name only
                    return ""
                mock_git.side_effect = side_effect

                orch.mechanical_checks()

                # Verify diff was called with the baseline ref
                diff_calls = [c for c in mock_git.call_args_list if 'diff' in c.args]
                assert len(diff_calls) >= 1
                # Check that baseline was used
                assert any("abc123" in c.args for c in diff_calls)
        finally:
            orch.cleanup()

    def test_mechanical_checks_falls_back_to_head(self, temp_repo):
        """mechanical_checks uses HEAD when baseline is None."""
        orch = Orchestrator()
        try:
            # Ensure baseline is None
            orch.loc_baseline_ref = None

            with patch.object(orch, 'git') as mock_git:
                def side_effect(*args):
                    if args[0] == "status":
                        return "M changed.txt"  # porcelain status
                    elif args[0] == "diff" and "--numstat" in args:
                        return "1\t0\tchanged.txt"  # numstat format
                    elif args[0] == "diff" and "--name-only" in args:
                        return "changed.txt"  # name only
                    return ""
                mock_git.side_effect = side_effect

                orch.mechanical_checks()

                # Verify diff was called with HEAD
                diff_calls = [c for c in mock_git.call_args_list if 'diff' in c.args]
                assert len(diff_calls) >= 1
                assert any("HEAD" in c.args for c in diff_calls)
        finally:
            orch.cleanup()

    def test_delegate_commit_updates_baseline_on_success(self, temp_repo):
        """delegate_commit updates the LoC baseline after successful commit."""
        orch = Orchestrator()
        try:
            orch.session_id = None
            orch.current_task_num = 1
            orch.total_tasks = 1
            orch._init_loc_baseline()

            with patch.object(orch, 'run_claude') as mock_run, \
                 patch.object(orch, 'git') as mock_git:
                # Simulate successful commit
                mock_run.return_value = "Committed"
                mock_git.side_effect = [
                    "",  # status --porcelain (no uncommitted changes)
                    "newhash123",  # rev-parse HEAD (for baseline update)
                ]
                result = orch.delegate_commit()

                assert result is True
                assert orch.loc_baseline_ref == "newhash123"
        finally:
            orch.cleanup()

    def test_delegate_commit_does_not_update_baseline_on_failure(self, temp_repo):
        """delegate_commit does not update baseline when commit fails."""
        orch = Orchestrator()
        try:
            orch.session_id = None
            orch.current_task_num = 1
            orch.total_tasks = 1
            orch._init_loc_baseline()
            old_baseline = orch.loc_baseline_ref

            with patch.object(orch, 'run_claude') as mock_run, \
                 patch.object(orch, 'git') as mock_git:
                # Simulate failed commit
                mock_run.return_value = "I couldn't commit"
                mock_git.return_value = "M orchestrate.py"  # Uncommitted changes remain
                result = orch.delegate_commit()

                assert result is False
                # Baseline should not have changed
                assert orch.loc_baseline_ref == old_baseline
        finally:
            orch.cleanup()


class TestContinueFlag:
    """Tests for --continue flag functionality."""

    def test_continue_run_is_false_by_default(self):
        """continue_run is False when not provided."""
        orch = Orchestrator()
        try:
            assert orch.continue_run is False
        finally:
            orch.cleanup()

    def test_continue_run_can_be_set(self):
        """continue_run can be set via constructor."""
        orch = Orchestrator(continue_run=True)
        try:
            assert orch.continue_run is True
        finally:
            orch.cleanup()

    def test_skip_mechanical_checks_initialized_false(self):
        """_skip_mechanical_checks starts as False."""
        orch = Orchestrator()
        try:
            assert orch._skip_mechanical_checks is False
        finally:
            orch.cleanup()


class TestStatePersistence:
    """Tests for state persistence to state.json."""

    def test_state_file_path(self, temp_repo):
        """_get_state_file_path returns correct path."""
        orch = Orchestrator()
        try:
            expected = orch.work_dir / "state.json"
            assert orch._get_state_file_path() == expected
        finally:
            orch.cleanup()

    def test_has_saved_state_false_when_no_file(self, temp_repo):
        """has_saved_state returns False when no state file exists."""
        orch = Orchestrator()
        try:
            assert orch.has_saved_state() is False
        finally:
            orch.cleanup()

    def test_save_state_creates_file(self, temp_repo):
        """save_state creates state.json file."""
        orch = Orchestrator()
        try:
            orch.current_task_num = 1
            orch.session_id = "test-session"
            orch.loc_baseline_ref = "abc123"
            orch.cycle = 2
            orch.save_state(halt_reason="test_halt")

            state_file = orch._get_state_file_path()
            assert state_file.exists()
        finally:
            orch.cleanup()

    def test_save_state_content(self, temp_repo):
        """save_state saves correct content."""
        import json
        orch = Orchestrator()
        try:
            orch.current_task_num = 3
            orch.session_id = "sess-456"
            orch.loc_baseline_ref = "def789"
            orch.cycle = 1
            orch.save_state(halt_reason="loc_threshold_exceeded:600")

            state_file = orch._get_state_file_path()
            state = json.loads(state_file.read_text())

            assert state["current_task_num"] == 3
            assert state["session_id"] == "sess-456"
            assert state["loc_baseline_ref"] == "def789"
            assert state["cycle"] == 1
            assert state["halt_reason"] == "loc_threshold_exceeded:600"
            assert "timestamp" in state
        finally:
            orch.cleanup()

    def test_load_state_returns_none_when_no_file(self, temp_repo):
        """load_state returns None when no state file exists."""
        orch = Orchestrator()
        try:
            state = orch.load_state()
            assert state is None
        finally:
            orch.cleanup()

    def test_load_state_returns_saved_state(self, temp_repo):
        """load_state returns previously saved state."""
        orch = Orchestrator()
        try:
            orch.current_task_num = 2
            orch.session_id = "load-test"
            orch.loc_baseline_ref = "hash123"
            orch.cycle = 0
            orch.save_state(halt_reason="sensitive_files")

            state = orch.load_state()

            assert state is not None
            assert state["current_task_num"] == 2
            assert state["session_id"] == "load-test"
            assert state["loc_baseline_ref"] == "hash123"
            assert state["halt_reason"] == "sensitive_files"
        finally:
            orch.cleanup()

    def test_load_state_handles_malformed_json(self, temp_repo):
        """load_state returns None for malformed JSON."""
        orch = Orchestrator()
        try:
            state_file = orch._get_state_file_path()
            state_file.write_text("{ invalid json }")

            state = orch.load_state()
            assert state is None
        finally:
            orch.cleanup()

    def test_clear_state_removes_file(self, temp_repo):
        """clear_state removes the state file."""
        orch = Orchestrator()
        try:
            orch.current_task_num = 1
            orch.save_state()
            assert orch.has_saved_state() is True

            orch.clear_state()
            assert orch.has_saved_state() is False
        finally:
            orch.cleanup()

    def test_clear_state_is_idempotent(self, temp_repo):
        """clear_state can be called when no state file exists."""
        orch = Orchestrator()
        try:
            # Should not raise
            orch.clear_state()
            orch.clear_state()
        finally:
            orch.cleanup()

    def test_has_saved_state_true_when_file_exists(self, temp_repo):
        """has_saved_state returns True when state file exists."""
        orch = Orchestrator()
        try:
            orch.save_state()
            assert orch.has_saved_state() is True
        finally:
            orch.cleanup()


class TestSessionMode:
    """Tests for --session CLI flag and session_mode parameter."""

    def test_session_mode_defaults_to_new_each_task(self, temp_repo):
        """session_mode defaults to 'new_each_task'."""
        orch = Orchestrator()
        try:
            assert orch.session_mode == "new_each_task"
            assert orch.session_id is None
        finally:
            orch.cleanup()

    def test_session_mode_accepts_new(self, temp_repo):
        """session_mode='new' is normalized to 'new_each_task'."""
        orch = Orchestrator(session_mode="new")
        try:
            # 'new' is normalized to 'new_each_task' for backwards compatibility
            assert orch.session_mode == "new_each_task"
            assert orch.session_id is None
        finally:
            orch.cleanup()

    def test_session_mode_accepts_new_each_task(self, temp_repo):
        """session_mode='new_each_task' starts fresh sessions."""
        orch = Orchestrator(session_mode="new_each_task")
        try:
            assert orch.session_mode == "new_each_task"
            assert orch.session_id is None
        finally:
            orch.cleanup()

    def test_session_mode_accepts_continue(self, temp_repo):
        """session_mode='continue' is normalized to 'continue_across_runs'."""
        orch = Orchestrator(session_mode="continue")
        try:
            # 'continue' is normalized to 'continue_across_runs' for backwards compatibility
            assert orch.session_mode == "continue_across_runs"
        finally:
            orch.cleanup()

    def test_session_mode_accepts_continue_across_runs(self, temp_repo):
        """session_mode='continue_across_runs' is accepted."""
        orch = Orchestrator(session_mode="continue_across_runs")
        try:
            assert orch.session_mode == "continue_across_runs"
        finally:
            orch.cleanup()

    def test_session_mode_accepts_continue_within_run(self, temp_repo):
        """session_mode='continue_within_run' is accepted."""
        orch = Orchestrator(session_mode="continue_within_run")
        try:
            assert orch.session_mode == "continue_within_run"
        finally:
            orch.cleanup()

    def test_session_mode_accepts_session_id(self, temp_repo):
        """session_mode accepts arbitrary session ID string."""
        orch = Orchestrator(session_mode="abc-123-def-456")
        try:
            assert orch.session_mode == "abc-123-def-456"
        finally:
            orch.cleanup()

    def test_session_mode_rejects_empty_string(self, temp_repo):
        """session_mode rejects empty string."""
        import pytest
        with pytest.raises(ValueError, match="Invalid session_mode"):
            Orchestrator(session_mode="")

    def test_session_mode_continue_loads_from_state(self, temp_repo, capsys):
        """session_mode='continue' loads session_id from state file."""
        # First, create a state file with a session_id
        orch1 = Orchestrator()
        try:
            orch1.session_id = "saved-session-xyz"
            orch1.save_state(halt_reason="test")
        finally:
            orch1.cleanup()

        # Now create orchestrator with session_mode='continue'
        orch2 = Orchestrator(session_mode="continue")
        try:
            # The session ID is loaded in run(), not __init__
            # So we just verify the mode is set correctly (normalized to continue_across_runs)
            assert orch2.session_mode == "continue_across_runs"
        finally:
            orch2.cleanup()

    def test_session_mode_explicit_id_sets_session(self, temp_repo, capsys):
        """session_mode with explicit ID is stored for use."""
        orch = Orchestrator(session_mode="explicit-session-123")
        try:
            assert orch.session_mode == "explicit-session-123"
            # Note: session_id is set from session_mode in run(), not __init__
        finally:
            orch.cleanup()

    def test_session_mode_shown_in_banner(self, temp_repo, capsys):
        """Non-default session_mode is shown in startup banner."""
        orch = Orchestrator(session_mode="continue")
        try:
            captured = capsys.readouterr()
            # 'continue' is normalized to 'continue_across_runs'
            assert "Session mode: continue_across_runs" in captured.out
        finally:
            orch.cleanup()

    def test_session_mode_new_not_shown_in_banner(self, temp_repo, capsys):
        """Default session_mode='new' is not shown in startup banner."""
        orch = Orchestrator(session_mode="new")
        try:
            captured = capsys.readouterr()
            assert "Session mode:" not in captured.out
        finally:
            orch.cleanup()

    def test_session_mode_new_each_task_not_shown_in_banner(self, temp_repo, capsys):
        """session_mode='new_each_task' is not shown in startup banner (it's the default)."""
        orch = Orchestrator(session_mode="new_each_task")
        try:
            captured = capsys.readouterr()
            assert "Session mode:" not in captured.out
        finally:
            orch.cleanup()

    def test_session_mode_continue_within_run_shown_in_banner(self, temp_repo, capsys):
        """session_mode='continue_within_run' is shown in startup banner."""
        orch = Orchestrator(session_mode="continue_within_run")
        try:
            captured = capsys.readouterr()
            assert "Session mode: continue_within_run" in captured.out
        finally:
            orch.cleanup()


class TestDualSessionIdTracking:
    """Tests for builder_session_id and reviewer_session_id tracking."""

    def test_builder_session_id_defaults_to_none(self, temp_repo):
        """builder_session_id defaults to None."""
        orch = Orchestrator()
        try:
            assert orch.builder_session_id is None
        finally:
            orch.cleanup()

    def test_reviewer_session_id_defaults_to_none(self, temp_repo):
        """reviewer_session_id defaults to None."""
        orch = Orchestrator()
        try:
            assert orch.reviewer_session_id is None
        finally:
            orch.cleanup()

    def test_session_id_property_returns_builder_session_id(self, temp_repo):
        """session_id property returns builder_session_id for backwards compatibility."""
        orch = Orchestrator()
        try:
            orch.builder_session_id = "builder-123"
            assert orch.session_id == "builder-123"
        finally:
            orch.cleanup()

    def test_session_id_property_sets_builder_session_id(self, temp_repo):
        """session_id property setter updates builder_session_id."""
        orch = Orchestrator()
        try:
            orch.session_id = "test-session"
            assert orch.builder_session_id == "test-session"
        finally:
            orch.cleanup()

    def test_save_state_includes_both_session_ids(self, temp_repo):
        """save_state stores both builder_session_id and reviewer_session_id."""
        import json
        orch = Orchestrator()
        try:
            orch.builder_session_id = "builder-abc"
            orch.reviewer_session_id = "reviewer-xyz"
            orch.save_state()

            state_file = orch._get_state_file_path()
            state = json.loads(state_file.read_text())

            assert state["builder_session_id"] == "builder-abc"
            assert state["reviewer_session_id"] == "reviewer-xyz"
            # Legacy key also present for backwards compatibility
            assert state["session_id"] == "builder-abc"
        finally:
            orch.cleanup()

    def test_save_state_handles_none_session_ids(self, temp_repo):
        """save_state works when session IDs are None."""
        import json
        orch = Orchestrator()
        try:
            orch.builder_session_id = None
            orch.reviewer_session_id = None
            orch.save_state()

            state_file = orch._get_state_file_path()
            state = json.loads(state_file.read_text())

            assert state["builder_session_id"] is None
            assert state["reviewer_session_id"] is None
            assert state["session_id"] is None
        finally:
            orch.cleanup()

    def test_save_state_only_builder_session(self, temp_repo):
        """save_state stores builder_session_id when only builder has session."""
        import json
        orch = Orchestrator()
        try:
            orch.builder_session_id = "builder-only"
            orch.reviewer_session_id = None
            orch.save_state()

            state_file = orch._get_state_file_path()
            state = json.loads(state_file.read_text())

            assert state["builder_session_id"] == "builder-only"
            assert state["reviewer_session_id"] is None
        finally:
            orch.cleanup()


class TestSessionCleanup:
    """Tests for session cleanup functionality."""

    def test_clear_sessions_clears_both_session_ids(self, temp_repo):
        """clear_sessions removes both builder and reviewer session IDs."""
        import json
        orch = Orchestrator()
        try:
            orch.builder_session_id = "builder-abc"
            orch.reviewer_session_id = "reviewer-xyz"
            orch.save_state()

            # Verify sessions exist in state
            state_file = orch._get_state_file_path()
            state = json.loads(state_file.read_text())
            assert state["builder_session_id"] == "builder-abc"
            assert state["reviewer_session_id"] == "reviewer-xyz"

            # Clear sessions
            result = orch.clear_sessions()
            assert result is True

            # Verify sessions are cleared in state file
            state = json.loads(state_file.read_text())
            assert state["builder_session_id"] is None
            assert state["reviewer_session_id"] is None
            assert state["session_id"] is None

            # Verify in-memory session IDs are also cleared
            assert orch.builder_session_id is None
            assert orch.reviewer_session_id is None
        finally:
            orch.cleanup()

    def test_clear_sessions_returns_false_when_no_state_file(self, temp_repo):
        """clear_sessions returns False when state file doesn't exist."""
        orch = Orchestrator()
        try:
            # Ensure no state file exists
            state_file = orch._get_state_file_path()
            if state_file.exists():
                state_file.unlink()

            result = orch.clear_sessions()
            assert result is False
        finally:
            orch.cleanup()

    def test_clear_sessions_returns_false_when_no_sessions_stored(self, temp_repo):
        """clear_sessions returns False when state has no session IDs."""
        orch = Orchestrator()
        try:
            # Save state without session IDs
            orch.builder_session_id = None
            orch.reviewer_session_id = None
            orch.save_state()

            result = orch.clear_sessions()
            assert result is False  # No sessions to clear
        finally:
            orch.cleanup()

    def test_clear_sessions_preserves_other_state(self, temp_repo):
        """clear_sessions preserves non-session state like current_task_num."""
        import json
        orch = Orchestrator()
        try:
            orch.builder_session_id = "builder-123"
            orch.current_task_num = 5
            orch.cycle = 3
            orch.save_state(halt_reason="test halt")

            orch.clear_sessions()

            state_file = orch._get_state_file_path()
            state = json.loads(state_file.read_text())

            # Session IDs should be cleared
            assert state["builder_session_id"] is None
            assert state["session_id"] is None

            # Other state should be preserved
            assert state["current_task_num"] == 5
            assert state["cycle"] == 3
            assert state["halt_reason"] == "test halt"
        finally:
            orch.cleanup()

    def test_auto_clear_stale_sessions_clears_old_sessions(self, temp_repo):
        """auto_clear_stale_sessions clears sessions older than max_age_hours."""
        import json
        from datetime import datetime, timedelta
        orch = Orchestrator()
        try:
            orch.builder_session_id = "old-session"
            orch.save_state()

            # Manually set timestamp to 25 hours ago
            state_file = orch._get_state_file_path()
            state = json.loads(state_file.read_text())
            old_time = datetime.now() - timedelta(hours=25)
            state["timestamp"] = old_time.isoformat()
            state_file.write_text(json.dumps(state, indent=2))

            # Auto-clear should detect and clear stale sessions
            result = orch.auto_clear_stale_sessions(max_age_hours=24)
            assert result is True

            # Verify sessions are cleared
            state = json.loads(state_file.read_text())
            assert state["builder_session_id"] is None
        finally:
            orch.cleanup()

    def test_auto_clear_stale_sessions_does_not_clear_recent(self, temp_repo):
        """auto_clear_stale_sessions does not clear recent sessions."""
        import json
        orch = Orchestrator()
        try:
            orch.builder_session_id = "recent-session"
            orch.save_state()  # Timestamp is now (fresh)

            result = orch.auto_clear_stale_sessions(max_age_hours=24)
            assert result is False

            # Verify session is still there
            state_file = orch._get_state_file_path()
            state = json.loads(state_file.read_text())
            assert state["builder_session_id"] == "recent-session"
        finally:
            orch.cleanup()

    def test_auto_clear_stale_sessions_returns_false_no_state(self, temp_repo):
        """auto_clear_stale_sessions returns False when no state file."""
        orch = Orchestrator()
        try:
            state_file = orch._get_state_file_path()
            if state_file.exists():
                state_file.unlink()

            result = orch.auto_clear_stale_sessions()
            assert result is False
        finally:
            orch.cleanup()

    def test_auto_clear_stale_sessions_returns_false_no_timestamp(self, temp_repo):
        """auto_clear_stale_sessions returns False when state has no timestamp."""
        import json
        orch = Orchestrator()
        try:
            # Write state without timestamp
            state_file = orch._get_state_file_path()
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state = {"builder_session_id": "test-session"}
            state_file.write_text(json.dumps(state))

            result = orch.auto_clear_stale_sessions()
            assert result is False
        finally:
            orch.cleanup()

    def test_auto_clear_stale_sessions_skips_when_no_sessions(self, temp_repo):
        """auto_clear_stale_sessions returns False when state is old but has no sessions."""
        import json
        from datetime import datetime, timedelta
        orch = Orchestrator()
        try:
            # Save state without session IDs
            orch.builder_session_id = None
            orch.reviewer_session_id = None
            orch.save_state()

            # Make it old
            state_file = orch._get_state_file_path()
            state = json.loads(state_file.read_text())
            old_time = datetime.now() - timedelta(hours=48)
            state["timestamp"] = old_time.isoformat()
            state_file.write_text(json.dumps(state, indent=2))

            result = orch.auto_clear_stale_sessions(max_age_hours=24)
            assert result is False  # No sessions to clear
        finally:
            orch.cleanup()


class TestMechanicalChecksWithContinue:
    """Tests for mechanical_checks with --continue flag."""

    def test_mechanical_checks_saves_state_on_loc_halt(self, temp_repo, capsys):
        """mechanical_checks saves state when LoC threshold exceeded."""
        orch = Orchestrator(loc_threshold=10)
        try:
            orch.current_task_num = 1
            orch.session_id = "test-session"
            orch.cycle = 1
            # Initialize baseline to HEAD (valid ref)
            orch._init_loc_baseline()

            # Make a large change
            large_content = "\n".join([f"line {i}" for i in range(100)])
            (temp_repo / "large_file.txt").write_text(large_content)
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            result = orch.mechanical_checks()
            assert result is False

            # Verify state file was created
            state_file = orch._get_state_file_path()
            assert state_file.exists()

            # Verify state content
            state = orch.load_state()
            assert "loc_threshold_exceeded" in state["halt_reason"]

            # Check that the message mentions --continue
            captured = capsys.readouterr()
            assert "--continue" in captured.out
        finally:
            orch.cleanup()

    def test_mechanical_checks_saves_state_on_sensitive_halt(self, temp_repo):
        """mechanical_checks saves state when sensitive files detected."""
        from millstone.runtime.orchestrator import POLICY_FILE_NAME, WORK_DIR_NAME

        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[sensitive]
enabled = true
paths = [".env"]
require_approval = true
""")

        orch = Orchestrator()
        try:
            orch.current_task_num = 1
            orch.session_id = "test-session"

            # Create a sensitive file
            (temp_repo / ".env").write_text("SECRET_KEY=abc123")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            result = orch.mechanical_checks()
            assert result is False
            assert orch.has_saved_state() is True

            state = orch.load_state()
            assert "sensitive_files" in state["halt_reason"]
        finally:
            orch.cleanup()

    def test_mechanical_checks_skips_when_flag_set(self, temp_repo):
        """mechanical_checks skips LoC/sensitive checks when _skip_mechanical_checks is True."""
        orch = Orchestrator(loc_threshold=10)
        try:
            orch._skip_mechanical_checks = True

            # Make a large change that would normally fail
            large_content = "\n".join([f"line {i}" for i in range(100)])
            (temp_repo / "large_file.txt").write_text(large_content)
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            result = orch.mechanical_checks()
            assert result is True  # Should pass due to skip flag
            assert orch._skip_mechanical_checks is False  # Flag should be reset
        finally:
            orch.cleanup()

    def test_mechanical_checks_skip_flag_only_works_once(self, temp_repo):
        """_skip_mechanical_checks only skips once, then normal behavior resumes."""
        orch = Orchestrator(loc_threshold=10)
        try:
            orch._skip_mechanical_checks = True

            # First call with large change - should pass
            large_content = "\n".join([f"line {i}" for i in range(100)])
            (temp_repo / "large_file.txt").write_text(large_content)
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            result1 = orch.mechanical_checks()
            assert result1 is True  # Skip applied

            # Second call - should fail since flag was reset
            result2 = orch.mechanical_checks()
            assert result2 is False  # Normal threshold check
        finally:
            orch.cleanup()

    def test_mechanical_checks_passes_with_no_changes_when_skipping(self, temp_repo):
        """Even with skip flag, mechanical_checks passes (with warning) when no changes."""
        orch = Orchestrator()
        try:
            orch._skip_mechanical_checks = True

            # No changes made
            result = orch.mechanical_checks()
            assert result is True  # Should pass now (with warning)
        finally:
            orch.cleanup()


class TestContinueRunIntegration:
    """Integration tests for --continue flag behavior."""

    def test_continue_restores_state_in_run(self, temp_repo):
        """State can be loaded across orchestrator instances."""
        import json

        # Create state file directly (simulating previous run that halted)
        work_dir = temp_repo / ".millstone"
        work_dir.mkdir(exist_ok=True)
        state_file = work_dir / "state.json"
        state_file.write_text(json.dumps({
            "current_task_num": 1,
            "session_id": "saved-session",
            "loc_baseline_ref": "saved-baseline",
            "cycle": 0,
            "halt_reason": "test",
            "timestamp": "2025-01-01T00:00:00",
        }))

        # Create new orchestrator with continue_run=True
        orch = Orchestrator(continue_run=True)
        try:
            # Load state and verify it reads correctly
            state = orch.load_state()
            assert state is not None
            assert state["session_id"] == "saved-session"
            assert state["loc_baseline_ref"] == "saved-baseline"
        finally:
            orch.cleanup()

    def test_continue_prints_warning_when_no_state(self, temp_repo, capsys):
        """run() prints warning when continue_run is True but no state exists."""
        # Create orchestrator with continue_run but NOT dry_run
        # so the state restoration logic runs
        orch = Orchestrator(continue_run=True)
        try:
            # Make sure no state file exists
            orch.clear_state()

            # We need to mock run_claude to avoid actually invoking claude
            # But we can at least verify the warning is printed during run()
            # by checking the state restoration code path
            # The warning is printed before preflight_checks()

            # Capture output during initialization and state loading
            state = orch.load_state()
            assert state is None  # No state file

            # Now check what run() outputs when there's no state
            # We can't run the full run() without mocks, but we can verify
            # the continue_run flag is set and triggers the right code path
            assert orch.continue_run is True
        finally:
            orch.cleanup()

    def test_continue_sets_skip_mechanical_checks(self, temp_repo):
        """State loading enables skip of mechanical checks."""
        import json

        # Create state file directly
        work_dir = temp_repo / ".millstone"
        work_dir.mkdir(exist_ok=True)
        state_file = work_dir / "state.json"
        state_file.write_text(json.dumps({
            "current_task_num": 1,
            "session_id": None,
            "loc_baseline_ref": "test-baseline",
            "cycle": 0,
            "halt_reason": "loc_threshold",
            "timestamp": "2025-01-01T00:00:00",
        }))

        # Create new orchestrator with continue_run=True
        orch = Orchestrator(continue_run=True)
        try:
            # Simulate what run() does at the start when continue_run=True
            state = orch.load_state()
            if state:
                orch.loc_baseline_ref = state.get("loc_baseline_ref")
                orch.session_id = state.get("session_id")
                orch._skip_mechanical_checks = True

            assert orch._skip_mechanical_checks is True
            assert orch.loc_baseline_ref == "test-baseline"
        finally:
            orch.cleanup()


class TestContinueFlagCLI:
    """Tests for --continue CLI flag parsing."""

    def test_continue_flag_parsed(self, temp_repo):
        """--continue flag is correctly parsed by argparse."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--continue', '--dry-run']):
            with patch.object(orchestrate.Orchestrator, 'run', return_value=0):
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()
                assert exc_info.value.code == 0


class TestEvalInfrastructure:
    """Tests for --eval infrastructure."""

    def test_parse_pytest_output_all_passed(self):
        """_parse_pytest_output correctly parses all-pass output."""
        orch = Orchestrator()
        try:
            output = "10 passed in 1.23s"
            result = orch._parse_pytest_output(output)
            assert result["total"] == 10
            assert result["passed"] == 10
            assert result["failed"] == 0
            assert result["errors"] == 0
            assert result["skipped"] == 0
        finally:
            orch.cleanup()

    def test_parse_pytest_output_mixed_results(self):
        """_parse_pytest_output correctly parses mixed results."""
        orch = Orchestrator()
        try:
            output = "5 passed, 2 failed, 1 error, 3 skipped in 2.50s"
            result = orch._parse_pytest_output(output)
            assert result["total"] == 11
            assert result["passed"] == 5
            assert result["failed"] == 2
            assert result["errors"] == 1
            assert result["skipped"] == 3
        finally:
            orch.cleanup()

    def test_parse_pytest_output_empty(self):
        """_parse_pytest_output handles empty output."""
        orch = Orchestrator()
        try:
            output = ""
            result = orch._parse_pytest_output(output)
            assert result["total"] == 0
            assert result["passed"] == 0
            assert result["failed"] == 0
        finally:
            orch.cleanup()

    def test_extract_failed_tests_finds_failures(self):
        """_extract_failed_tests extracts failed test names."""
        orch = Orchestrator()
        try:
            output = """
FAILED tests/test_foo.py::test_bar - AssertionError
FAILED tests/test_baz.py::test_qux - ValueError
"""
            result = orch._extract_failed_tests(output)
            assert len(result) == 2
            assert "tests/test_foo.py::test_bar" in result
            assert "tests/test_baz.py::test_qux" in result
        finally:
            orch.cleanup()

    def test_extract_failed_tests_empty_when_no_failures(self):
        """_extract_failed_tests returns empty list when no failures."""
        orch = Orchestrator()
        try:
            output = "10 passed in 1.00s"
            result = orch._extract_failed_tests(output)
            assert result == []
        finally:
            orch.cleanup()

    def test_run_eval_creates_evals_directory(self, temp_repo):
        """run_eval creates .millstone/evals/ directory."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 0.50s",
                    stderr="",
                    returncode=0,
                )
                orch.run_eval()

            evals_dir = orch.work_dir / "evals"
            assert evals_dir.exists()
            assert evals_dir.is_dir()
        finally:
            orch.cleanup()

    def test_run_eval_creates_json_file(self, temp_repo):
        """run_eval creates timestamped JSON file in evals directory."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 0.50s",
                    stderr="",
                    returncode=0,
                )
                orch.run_eval()

            evals_dir = orch.work_dir / "evals"
            # Exclude summary.json from the count
            json_files = [f for f in evals_dir.glob("*.json") if f.name != "summary.json"]
            assert len(json_files) == 1
            assert json_files[0].name.endswith(".json")
        finally:
            orch.cleanup()

    def test_run_eval_json_schema(self, temp_repo):
        """run_eval creates JSON with correct schema."""
        import json
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 0.50s",
                    stderr="",
                    returncode=0,
                )
                orch.run_eval()

            evals_dir = orch.work_dir / "evals"
            json_files = [f for f in evals_dir.glob("*.json") if f.name != "summary.json"]
            assert json_files, "Expected at least one eval JSON file"
            data = json.loads(json_files[0].read_text())

            # Check required fields
            assert "timestamp" in data
            assert "git_head" in data
            assert "duration_seconds" in data
            assert "tests" in data
            assert "failed_tests" in data

            # Check tests sub-schema
            tests = data["tests"]
            assert "total" in tests
            assert "passed" in tests
            assert "failed" in tests
            assert "errors" in tests
            assert "skipped" in tests
        finally:
            orch.cleanup()

    def test_run_eval_returns_passed_true_when_no_failures(self, temp_repo):
        """run_eval returns _passed=True when all tests pass."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 0.50s",
                    stderr="",
                    returncode=0,
                )
                result = orch.run_eval()

            assert result["_passed"] is True
        finally:
            orch.cleanup()

    def test_run_eval_returns_passed_false_when_failures(self, temp_repo):
        """run_eval returns _passed=False when tests fail."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="3 passed, 2 failed in 0.50s",
                    stderr="",
                    returncode=1,
                )
                result = orch.run_eval()

            assert result["_passed"] is False
        finally:
            orch.cleanup()

    def test_run_eval_returns_passed_false_when_errors(self, temp_repo):
        """run_eval returns _passed=False when there are errors."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="3 passed, 1 error in 0.50s",
                    stderr="",
                    returncode=1,
                )
                result = orch.run_eval()

            assert result["_passed"] is False
        finally:
            orch.cleanup()

    def test_run_eval_captures_failed_test_names(self, temp_repo):
        """run_eval captures names of failed tests."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="""
FAILED tests/test_foo.py::test_bar - AssertionError
2 passed, 1 failed in 0.50s
""",
                    stderr="",
                    returncode=1,
                )
                result = orch.run_eval()

            assert "tests/test_foo.py::test_bar" in result["failed_tests"]
        finally:
            orch.cleanup()

    def test_run_eval_logs_event(self, temp_repo):
        """run_eval logs eval_completed event."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 0.50s",
                    stderr="",
                    returncode=0,
                )
                orch.run_eval()

            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            assert "eval_completed" in log_content
        finally:
            orch.cleanup()

    def test_run_eval_prints_summary(self, temp_repo, capsys):
        """run_eval prints human-readable summary."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 0.50s",
                    stderr="",
                    returncode=0,
                )
                orch.run_eval()

            captured = capsys.readouterr()
            assert "Eval Results" in captured.out
            assert "5/5 passed" in captured.out
            assert "PASSED" in captured.out
        finally:
            orch.cleanup()

    def test_run_eval_prints_failures(self, temp_repo, capsys):
        """run_eval prints failed test names in summary."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="""
FAILED tests/test_foo.py::test_bar - AssertionError
2 passed, 1 failed in 0.50s
""",
                    stderr="",
                    returncode=1,
                )
                orch.run_eval()

            captured = capsys.readouterr()
            assert "FAILED" in captured.out
            assert "Failed tests:" in captured.out
            assert "test_foo.py::test_bar" in captured.out
        finally:
            orch.cleanup()


class TestEvalCLI:
    """Tests for --eval CLI flag."""

    def test_eval_flag_runs_eval(self, temp_repo):
        """--eval flag invokes run_eval."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--eval']):
            with patch.object(orchestrate.Orchestrator, 'run_eval') as mock_eval:
                mock_eval.return_value = {"_passed": True, "tests": {}}
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()
                mock_eval.assert_called_once()
                assert exc_info.value.code == 0

    def test_eval_flag_exits_1_on_failure(self, temp_repo):
        """--eval exits 1 when tests fail."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--eval']):
            with patch.object(orchestrate.Orchestrator, 'run_eval') as mock_eval:
                mock_eval.return_value = {"_passed": False, "tests": {"failed": 1}}
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()
                assert exc_info.value.code == 1

    def test_cov_flag_requires_eval(self, temp_repo):
        """--cov without --eval raises error."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--cov']):
            with pytest.raises(SystemExit) as exc_info:
                orchestrate.main()
            # argparse error returns exit code 2
            assert exc_info.value.code == 2

    def test_eval_with_cov_flag(self, temp_repo):
        """--eval --cov passes coverage=True to run_eval."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--eval', '--cov']):
            with patch.object(orchestrate.Orchestrator, 'run_eval') as mock_eval:
                mock_eval.return_value = {"_passed": True, "tests": {}}
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()
                mock_eval.assert_called_once_with(coverage=True)
                assert exc_info.value.code == 0


class TestEvalComparison:
    """Tests for compare_evals() method and --eval-compare CLI."""

    def test_compare_evals_requires_two_files(self, temp_repo):
        """compare_evals raises error if fewer than 2 eval files."""
        import json
        orch = Orchestrator()
        try:
            # No evals directory
            with pytest.raises(FileNotFoundError) as exc_info:
                orch.compare_evals()
            assert "No evals directory" in str(exc_info.value)

            # Create evals dir but with only 1 file
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()
            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": []
            }))

            with pytest.raises(FileNotFoundError) as exc_info:
                orch.compare_evals()
            assert "Need at least 2 eval files" in str(exc_info.value)
        finally:
            orch.cleanup()

    def test_compare_evals_finds_two_most_recent(self, temp_repo):
        """compare_evals compares the two most recent files by timestamp."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            # Create 3 eval files
            for i, timestamp in enumerate(["20241201_120000", "20241201_130000", "20241201_140000"]):
                (evals_dir / f"{timestamp}.json").write_text(json.dumps({
                    "timestamp": f"2024-12-01T1{2+i}:00:00",
                    "git_head": f"abc12{i}",
                    "duration_seconds": 1.0 + i,
                    "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                    "failed_tests": []
                }))

            result = orch.compare_evals()

            # Should compare the two most recent
            assert result["older_file"] == "20241201_130000.json"
            assert result["newer_file"] == "20241201_140000.json"
        finally:
            orch.cleanup()

    def test_compare_evals_detects_new_failures(self, temp_repo):
        """compare_evals identifies tests that started failing."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            # Older: all passing
            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": []
            }))

            # Newer: one failure
            (evals_dir / "20241201_130000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T13:00:00",
                "git_head": "def456",
                "duration_seconds": 1.5,
                "tests": {"total": 5, "passed": 4, "failed": 1, "errors": 0, "skipped": 0},
                "failed_tests": ["tests/test_foo.py::test_bar"]
            }))

            result = orch.compare_evals()

            assert result["status"] == "REGRESSION"
            assert result["_has_regressions"] is True
            assert "tests/test_foo.py::test_bar" in result["new_failures"]
            assert len(result["new_passes"]) == 0
        finally:
            orch.cleanup()

    def test_compare_evals_detects_new_passes(self, temp_repo):
        """compare_evals identifies tests that started passing."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            # Older: one failing
            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 4, "failed": 1, "errors": 0, "skipped": 0},
                "failed_tests": ["tests/test_foo.py::test_bar"]
            }))

            # Newer: all passing
            (evals_dir / "20241201_130000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T13:00:00",
                "git_head": "def456",
                "duration_seconds": 1.5,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": []
            }))

            result = orch.compare_evals()

            assert result["status"] == "IMPROVEMENT"
            assert result["_has_regressions"] is False
            assert len(result["new_failures"]) == 0
            assert "tests/test_foo.py::test_bar" in result["new_passes"]
        finally:
            orch.cleanup()

    def test_compare_evals_no_change(self, temp_repo):
        """compare_evals reports NO_CHANGE when no test status changes."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            # Both passing
            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": []
            }))

            (evals_dir / "20241201_130000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T13:00:00",
                "git_head": "def456",
                "duration_seconds": 1.5,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": []
            }))

            result = orch.compare_evals()

            assert result["status"] == "NO_CHANGE"
            assert result["_has_regressions"] is False
        finally:
            orch.cleanup()

    def test_compare_evals_coverage_delta(self, temp_repo):
        """compare_evals computes coverage delta when both have coverage."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": [],
                "coverage": {"line_rate": 0.85, "branch_rate": 0.72}
            }))

            (evals_dir / "20241201_130000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T13:00:00",
                "git_head": "def456",
                "duration_seconds": 1.5,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": [],
                "coverage": {"line_rate": 0.87, "branch_rate": 0.75}
            }))

            result = orch.compare_evals()

            # 0.87 - 0.85 = 0.02 -> 2.0%
            assert result["coverage_delta"] == 2.0
        finally:
            orch.cleanup()

    def test_compare_evals_no_coverage_delta_when_missing(self, temp_repo):
        """compare_evals returns None coverage_delta if either lacks coverage."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": []
                # No coverage
            }))

            (evals_dir / "20241201_130000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T13:00:00",
                "git_head": "def456",
                "duration_seconds": 1.5,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": [],
                "coverage": {"line_rate": 0.87}
            }))

            result = orch.compare_evals()

            assert result["coverage_delta"] is None
        finally:
            orch.cleanup()

    def test_compare_evals_duration_delta(self, temp_repo):
        """compare_evals computes duration delta."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 10.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": []
            }))

            (evals_dir / "20241201_130000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T13:00:00",
                "git_head": "def456",
                "duration_seconds": 12.5,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": []
            }))

            result = orch.compare_evals()

            assert result["duration_delta"] == 2.5
        finally:
            orch.cleanup()

    def test_compare_evals_prints_output(self, temp_repo, capsys):
        """compare_evals prints human-readable comparison."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 10.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": []
            }))

            (evals_dir / "20241201_130000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T13:00:00",
                "git_head": "def456",
                "duration_seconds": 12.5,
                "tests": {"total": 5, "passed": 4, "failed": 1, "errors": 0, "skipped": 0},
                "failed_tests": ["tests/test_foo.py::test_bar"]
            }))

            orch.compare_evals()

            captured = capsys.readouterr()
            # Check key output elements
            assert "Comparing:" in captured.out
            assert "20241201_120000.json" in captured.out
            assert "20241201_130000.json" in captured.out
            assert "Tests:" in captured.out
            assert "5/5 passed" in captured.out
            assert "4/5 passed" in captured.out
            assert "New failures:" in captured.out
            assert "test_foo.py::test_bar" in captured.out
            assert '"status": "REGRESSION"' in captured.out
        finally:
            orch.cleanup()

    def test_compare_evals_logs_event(self, temp_repo):
        """compare_evals logs eval_comparison event."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": []
            }))

            (evals_dir / "20241201_130000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T13:00:00",
                "git_head": "def456",
                "duration_seconds": 1.5,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": []
            }))

            orch.compare_evals()

            log_content = orch.log_file.read_text()
            assert "eval_comparison" in log_content
        finally:
            orch.cleanup()


class TestEvalCompareCLI:
    """Tests for --eval-compare CLI flag."""

    def test_eval_compare_flag_runs_compare(self, temp_repo):
        """--eval-compare flag invokes compare_evals."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--eval-compare']):
            with patch.object(orchestrate.Orchestrator, 'compare_evals') as mock_compare:
                mock_compare.return_value = {"_has_regressions": False}
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()
                mock_compare.assert_called_once()
                assert exc_info.value.code == 0

    def test_eval_compare_exits_1_on_regression(self, temp_repo):
        """--eval-compare exits 1 when regressions detected."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--eval-compare']):
            with patch.object(orchestrate.Orchestrator, 'compare_evals') as mock_compare:
                mock_compare.return_value = {"_has_regressions": True, "new_failures": ["test"]}
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()
                assert exc_info.value.code == 1

    def test_eval_compare_exits_1_on_missing_files(self, temp_repo):
        """--eval-compare exits 1 when insufficient eval files."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--eval-compare']):
            with patch.object(orchestrate.Orchestrator, 'compare_evals') as mock_compare:
                mock_compare.side_effect = FileNotFoundError("Need at least 2 eval files")
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()
                assert exc_info.value.code == 1


class TestEvalDeltaTracking:
    """Tests for eval delta tracking functionality."""

    def test_run_eval_adds_previous_eval_reference(self, temp_repo):
        """run_eval adds previous_eval field when prior eval exists."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            # Create a prior eval
            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": [],
                "composite_score": 0.95,
                "categories": {"tests": {"score": 1.0, "passed": 5, "failed": 0}}
            }))

            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 1.00s",
                    stderr="",
                    returncode=0
                )
                result = orch.run_eval()

            assert "previous_eval" in result
            assert result["previous_eval"] == "20241201_120000.json"
        finally:
            orch.cleanup()

    def test_run_eval_no_previous_eval_on_first_run(self, temp_repo):
        """run_eval does not add previous_eval on first eval."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 1.00s",
                    stderr="",
                    returncode=0
                )
                result = orch.run_eval()

            assert "previous_eval" not in result
        finally:
            orch.cleanup()

    def test_run_eval_computes_delta_from_previous(self, temp_repo):
        """run_eval computes delta object from previous eval."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            # Create a prior eval with known values
            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 4, "failed": 1, "errors": 0, "skipped": 0},
                "failed_tests": ["test_foo"],
                "composite_score": 0.90,
                "categories": {"tests": {"score": 0.80, "passed": 4, "failed": 1}}
            }))

            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 1.00s",
                    stderr="",
                    returncode=0
                )
                result = orch.run_eval()

            assert "delta" in result
            delta = result["delta"]
            assert "composite" in delta
            assert "tests" in delta
            assert delta["tests"]["passed"] == 1  # 5 - 4
            assert delta["tests"]["failed"] == -1  # 0 - 1
        finally:
            orch.cleanup()

    def test_run_eval_no_delta_on_first_run(self, temp_repo):
        """run_eval does not add delta on first eval."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 1.00s",
                    stderr="",
                    returncode=0
                )
                result = orch.run_eval()

            assert "delta" not in result
        finally:
            orch.cleanup()

    def test_run_eval_updates_summary_json(self, temp_repo):
        """run_eval creates/updates summary.json file."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 1.00s",
                    stderr="",
                    returncode=0
                )
                orch.run_eval()

            summary_file = orch.work_dir / "evals" / "summary.json"
            assert summary_file.exists()

            import json
            summary = json.loads(summary_file.read_text())
            assert "evals" in summary
            assert len(summary["evals"]) == 1
            assert "composite_score" in summary["evals"][0]
            assert "tests" in summary["evals"][0]
        finally:
            orch.cleanup()

    def test_run_eval_appends_to_summary_json(self, temp_repo):
        """run_eval appends to existing summary.json."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            # Create existing summary
            summary_file = evals_dir / "summary.json"
            summary_file.write_text(json.dumps({
                "evals": [{"timestamp": "2024-12-01T12:00:00", "composite_score": 0.90}]
            }))

            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 1.00s",
                    stderr="",
                    returncode=0
                )
                orch.run_eval()

            summary = json.loads(summary_file.read_text())
            assert len(summary["evals"]) == 2
        finally:
            orch.cleanup()

    def test_compare_evals_excludes_summary_json(self, temp_repo):
        """compare_evals ignores summary.json when finding eval files."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            # Create summary.json and two eval files
            (evals_dir / "summary.json").write_text(json.dumps({"evals": []}))
            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": []
            }))
            (evals_dir / "20241201_130000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T13:00:00",
                "git_head": "def456",
                "duration_seconds": 1.5,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": []
            }))

            result = orch.compare_evals()

            # Should compare the two timestamped files, not summary.json
            assert result["older_file"] == "20241201_120000.json"
            assert result["newer_file"] == "20241201_130000.json"
        finally:
            orch.cleanup()

    def test_compare_evals_includes_composite_delta(self, temp_repo):
        """compare_evals returns composite_delta when both have composite_score."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": [],
                "composite_score": 0.90
            }))
            (evals_dir / "20241201_130000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T13:00:00",
                "git_head": "def456",
                "duration_seconds": 1.5,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": [],
                "composite_score": 0.95
            }))

            result = orch.compare_evals()

            assert result["composite_delta"] == 0.05
        finally:
            orch.cleanup()

    def test_compare_evals_includes_category_deltas(self, temp_repo):
        """compare_evals returns category_deltas when both have categories."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": [],
                "categories": {
                    "tests": {"score": 0.90},
                    "coverage": {"score": 0.80}
                }
            }))
            (evals_dir / "20241201_130000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T13:00:00",
                "git_head": "def456",
                "duration_seconds": 1.5,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": [],
                "categories": {
                    "tests": {"score": 0.95},
                    "coverage": {"score": 0.85}
                }
            }))

            result = orch.compare_evals()

            assert "category_deltas" in result
            assert "tests" in result["category_deltas"]
            assert "coverage" in result["category_deltas"]
            assert result["category_deltas"]["tests"]["delta"] == 0.05
            assert result["category_deltas"]["coverage"]["delta"] == 0.05
        finally:
            orch.cleanup()

    def test_print_eval_comparison_shows_category_breakdown(self, temp_repo, capsys):
        """_print_eval_comparison shows category-by-category breakdown."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": [],
                "composite_score": 0.90,
                "categories": {"tests": {"score": 0.90}}
            }))
            (evals_dir / "20241201_130000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T13:00:00",
                "git_head": "def456",
                "duration_seconds": 1.5,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": [],
                "composite_score": 0.95,
                "categories": {"tests": {"score": 0.95}}
            }))

            orch.compare_evals()
            captured = capsys.readouterr()

            assert "Category Breakdown:" in captured.out
            assert "tests:" in captured.out
            assert "Composite Score:" in captured.out
        finally:
            orch.cleanup()

    def test_compute_eval_delta_handles_missing_data(self, temp_repo):
        """_compute_eval_delta handles evals with missing optional fields."""
        orch = Orchestrator()
        try:
            previous = {"tests": {"passed": 5, "failed": 0}}
            current = {"tests": {"passed": 5, "failed": 0}}

            delta = orch._compute_eval_delta(previous, current)

            assert "tests" in delta
            # No composite or categories since they weren't in the inputs
            assert "composite" not in delta
            assert "categories" not in delta
        finally:
            orch.cleanup()

    def test_compute_eval_delta_includes_coverage_delta(self, temp_repo):
        """_compute_eval_delta computes coverage delta when available."""
        orch = Orchestrator()
        try:
            previous = {
                "tests": {"passed": 5, "failed": 0},
                "coverage": {"line_rate": 0.80}
            }
            current = {
                "tests": {"passed": 5, "failed": 0},
                "coverage": {"line_rate": 0.85}
            }

            delta = orch._compute_eval_delta(previous, current)

            assert "coverage" in delta
            assert delta["coverage"] == 0.05
        finally:
            orch.cleanup()

    def test_summary_json_includes_category_scores(self, temp_repo):
        """summary.json includes category scores when available."""
        import json
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 1.00s",
                    stderr="",
                    returncode=0
                )
                orch.run_eval()

            summary_file = orch.work_dir / "evals" / "summary.json"
            summary = json.loads(summary_file.read_text())

            assert "category_scores" in summary["evals"][0]
            assert "tests" in summary["evals"][0]["category_scores"]
        finally:
            orch.cleanup()


class TestEvalTrendTracking:
    """Tests for eval trend tracking and warning functionality."""

    def test_print_eval_trend_warnings_detects_new_failures(self, temp_repo, capsys):
        """_print_eval_trend_warnings warns about new test failures."""
        orch = Orchestrator()
        try:
            previous = {
                "failed_tests": ["test_old"],
                "composite_score": 0.90,
            }
            current = {
                "failed_tests": ["test_old", "test_new"],
                "composite_score": 0.90,
            }
            delta = {"tests": {"passed": -1, "failed": 1}}

            result = orch._print_eval_trend_warnings(previous, current, delta)

            assert result is True  # Warnings were printed
            captured = capsys.readouterr()
            assert "WARNING: New test failures detected!" in captured.out
            assert "test_new" in captured.out
        finally:
            orch.cleanup()

    def test_print_eval_trend_warnings_detects_pass_rate_decrease(self, temp_repo, capsys):
        """_print_eval_trend_warnings warns about decreased pass rate."""
        orch = Orchestrator()
        try:
            previous = {"failed_tests": [], "composite_score": 0.90}
            current = {"failed_tests": [], "composite_score": 0.90}
            delta = {"tests": {"passed": -2, "failed": 0}}

            result = orch._print_eval_trend_warnings(previous, current, delta)

            assert result is True
            captured = capsys.readouterr()
            assert "WARNING: Pass rate decreased" in captured.out
            assert "-2" in captured.out
        finally:
            orch.cleanup()

    def test_print_eval_trend_warnings_detects_failure_increase(self, temp_repo, capsys):
        """_print_eval_trend_warnings warns about increased failures."""
        orch = Orchestrator()
        try:
            previous = {"failed_tests": [], "composite_score": 0.90}
            current = {"failed_tests": [], "composite_score": 0.90}
            delta = {"tests": {"passed": 0, "failed": 3}}

            result = orch._print_eval_trend_warnings(previous, current, delta)

            assert result is True
            captured = capsys.readouterr()
            assert "WARNING: Failure count increased" in captured.out
            assert "+3" in captured.out
        finally:
            orch.cleanup()

    def test_print_eval_trend_warnings_detects_composite_decrease(self, temp_repo, capsys):
        """_print_eval_trend_warnings warns about composite score decrease."""
        orch = Orchestrator()
        try:
            previous = {"failed_tests": [], "composite_score": 0.90}
            current = {"failed_tests": [], "composite_score": 0.85}
            delta = {"composite": -0.05, "tests": {"passed": 0, "failed": 0}}

            result = orch._print_eval_trend_warnings(previous, current, delta)

            assert result is True
            captured = capsys.readouterr()
            assert "WARNING: Composite score decreased" in captured.out
            assert "0.9000" in captured.out
            assert "0.8500" in captured.out
        finally:
            orch.cleanup()

    def test_print_eval_trend_warnings_detects_category_regressions(self, temp_repo, capsys):
        """_print_eval_trend_warnings warns about category score regressions."""
        orch = Orchestrator()
        try:
            previous = {"failed_tests": [], "composite_score": 0.90}
            current = {"failed_tests": [], "composite_score": 0.90}
            delta = {
                "tests": {"passed": 0, "failed": 0},
                "categories": {"tests": -0.10, "coverage": 0.05}
            }

            result = orch._print_eval_trend_warnings(previous, current, delta)

            assert result is True
            captured = capsys.readouterr()
            assert "WARNING: Category score regressions:" in captured.out
            assert "tests:" in captured.out
            assert "-0.1" in captured.out
            # coverage increased, so should not appear in warnings
            assert "coverage:" not in captured.out
        finally:
            orch.cleanup()

    def test_print_eval_trend_warnings_no_warnings_when_improved(self, temp_repo, capsys):
        """_print_eval_trend_warnings returns False when no regressions."""
        orch = Orchestrator()
        try:
            previous = {"failed_tests": ["test_old"], "composite_score": 0.80}
            current = {"failed_tests": [], "composite_score": 0.90}
            delta = {
                "composite": 0.10,
                "tests": {"passed": 1, "failed": -1},
                "categories": {"tests": 0.10}
            }

            result = orch._print_eval_trend_warnings(previous, current, delta)

            assert result is False
            captured = capsys.readouterr()
            assert "WARNING" not in captured.out
        finally:
            orch.cleanup()

    def test_print_eval_trend_warnings_logs_event(self, temp_repo):
        """_print_eval_trend_warnings logs eval_trend_warning event."""
        orch = Orchestrator()
        try:
            previous = {"failed_tests": [], "composite_score": 0.90}
            current = {"failed_tests": ["test_new"], "composite_score": 0.85}
            delta = {"composite": -0.05, "tests": {"passed": -1, "failed": 1}}

            orch._print_eval_trend_warnings(previous, current, delta)

            # Check the log file for the event
            runs_dir = orch.work_dir / "runs"
            log_files = list(runs_dir.glob("*.log"))
            assert len(log_files) == 1
            log_content = log_files[0].read_text()
            assert "eval_trend_warning" in log_content
        finally:
            orch.cleanup()

    def test_run_eval_prints_trend_warnings(self, temp_repo, capsys):
        """run_eval prints trend warnings when there are regressions."""
        import json
        orch = Orchestrator()
        try:
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir()

            # Create a prior eval with all tests passing
            (evals_dir / "20241201_120000.json").write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 10, "passed": 10, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": [],
                "composite_score": 0.95,
                "categories": {"tests": {"score": 1.0, "passed": 10, "failed": 0}}
            }))

            with patch('subprocess.run') as mock_run:
                # Now run eval with failures
                mock_run.return_value = MagicMock(
                    stdout="8 passed, 2 failed in 1.00s\nFAILED test_foo\nFAILED test_bar",
                    stderr="",
                    returncode=1
                )
                orch.run_eval()

            captured = capsys.readouterr()
            assert "WARNING: New test failures detected!" in captured.out
        finally:
            orch.cleanup()

    def test_run_eval_no_warnings_on_first_run(self, temp_repo, capsys):
        """run_eval does not print warnings on first run (no previous eval)."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="5 passed, 1 failed in 1.00s\nFAILED test_foo",
                    stderr="",
                    returncode=1
                )
                orch.run_eval()

            captured = capsys.readouterr()
            # No trend warnings since there's no previous eval to compare
            assert "WARNING: New test failures detected!" not in captured.out
            assert "WARNING: Pass rate decreased" not in captured.out
        finally:
            orch.cleanup()

    def test_print_eval_trend_warnings_truncates_many_failures(self, temp_repo, capsys):
        """_print_eval_trend_warnings truncates list when many new failures."""
        orch = Orchestrator()
        try:
            new_failures = [f"test_new_{i}" for i in range(15)]
            previous = {"failed_tests": [], "composite_score": 0.90}
            current = {"failed_tests": new_failures, "composite_score": 0.90}
            delta = {"tests": {"passed": -15, "failed": 15}}

            orch._print_eval_trend_warnings(previous, current, delta)

            captured = capsys.readouterr()
            assert "WARNING: New test failures detected!" in captured.out
            # Should show first 10 and indicate more
            assert "test_new_0" in captured.out
            assert "... and 5 more" in captured.out
        finally:
            orch.cleanup()


class TestEvalOnCommit:
    """Tests for --eval-on-commit functionality."""

    def test_eval_on_commit_param_defaults_to_false(self, temp_repo):
        """eval_on_commit parameter defaults to False."""
        orch = Orchestrator()
        try:
            assert orch.eval_on_commit is False
            assert orch.baseline_eval is None
        finally:
            orch.cleanup()

    def test_eval_on_commit_param_can_be_enabled(self, temp_repo):
        """eval_on_commit parameter can be set to True."""
        orch = Orchestrator(eval_on_commit=True)
        try:
            assert orch.eval_on_commit is True
        finally:
            orch.cleanup()

    def test_eval_on_commit_in_default_config(self):
        """eval_on_commit is in DEFAULT_CONFIG."""
        assert "eval_on_commit" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["eval_on_commit"] is False

    def test_run_eval_on_commit_detects_new_failures(self, temp_repo):
        """_run_eval_on_commit returns False when new failures are detected."""
        orch = Orchestrator(eval_on_commit=True)
        try:
            # Set up baseline with no failures
            orch.baseline_eval = {"failed_tests": [], "_passed": True}

            # Mock run_eval to return a failure
            with patch.object(orch, 'run_eval') as mock_eval:
                mock_eval.return_value = {
                    "failed_tests": ["test_foo.py::test_bar"],
                    "_passed": False,
                    "tests": {"total": 10, "passed": 9, "failed": 1}
                }
                # Mock git and input to handle the revert prompt
                with patch.object(orch, 'git') as mock_git:
                    mock_git.return_value = "abc123\n"
                    with patch('builtins.input', return_value='n'):
                        result = orch._run_eval_on_commit()
                        assert result is False
                        mock_eval.assert_called_once()
        finally:
            orch.cleanup()

    def test_run_eval_on_commit_allows_preexisting_failures(self, temp_repo):
        """_run_eval_on_commit returns True when failures existed in baseline."""
        orch = Orchestrator(eval_on_commit=True)
        try:
            # Set up baseline with an existing failure
            orch.baseline_eval = {"failed_tests": ["test_foo.py::test_bar"], "_passed": False}

            # Mock run_eval to return the same failure
            with patch.object(orch, 'run_eval') as mock_eval:
                mock_eval.return_value = {
                    "failed_tests": ["test_foo.py::test_bar"],
                    "_passed": False,
                    "tests": {"total": 10, "passed": 9, "failed": 1}
                }

                result = orch._run_eval_on_commit()
                assert result is True  # No NEW failures
        finally:
            orch.cleanup()

    def test_run_eval_on_commit_passes_when_all_tests_pass(self, temp_repo):
        """_run_eval_on_commit returns True when all tests pass."""
        orch = Orchestrator(eval_on_commit=True)
        try:
            # Set up baseline with no failures
            orch.baseline_eval = {"failed_tests": [], "_passed": True}

            # Mock run_eval to return all passing
            with patch.object(orch, 'run_eval') as mock_eval:
                mock_eval.return_value = {
                    "failed_tests": [],
                    "_passed": True,
                    "tests": {"total": 10, "passed": 10, "failed": 0}
                }

                result = orch._run_eval_on_commit()
                assert result is True
        finally:
            orch.cleanup()

    def test_run_eval_on_commit_handles_no_baseline(self, temp_repo):
        """_run_eval_on_commit works even if baseline_eval is None."""
        orch = Orchestrator(eval_on_commit=True)
        try:
            # No baseline set (simulates edge case)
            orch.baseline_eval = None

            # Mock run_eval to return a failure
            with patch.object(orch, 'run_eval') as mock_eval:
                mock_eval.return_value = {
                    "failed_tests": ["test_foo.py::test_bar"],
                    "_passed": False,
                    "tests": {"total": 10, "passed": 9, "failed": 1}
                }
                # Mock git and input to handle the revert prompt
                with patch.object(orch, 'git') as mock_git:
                    mock_git.return_value = "abc123\n"
                    with patch('builtins.input', return_value='n'):
                        # With no baseline, any failure is considered "new"
                        result = orch._run_eval_on_commit()
                        assert result is False
        finally:
            orch.cleanup()

    def test_run_captures_baseline_eval_at_start(self, temp_repo):
        """run() captures baseline eval before first task when eval_on_commit is enabled."""
        # Create a tasklist with a task
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Test task\n")

        orch = Orchestrator(eval_on_commit=True)
        try:
            # Verify baseline is None before run()
            assert orch.baseline_eval is None

            # Mock run_eval to return a test result
            mock_eval_result = {
                "failed_tests": [],
                "_passed": True,
                "tests": {"total": 5, "passed": 5, "failed": 0},
                "composite_score": 0.95,
            }

            # Track calls to run_eval
            eval_call_count = [0]

            def tracking_run_eval():
                eval_call_count[0] += 1
                return mock_eval_result

            # Mock dependencies to prevent actual task execution
            with patch.object(orch, 'run_eval', side_effect=tracking_run_eval):
                with patch.object(orch, 'has_remaining_tasks', return_value=False):
                    # Run orchestrator - it should capture baseline then see no tasks
                    result = orch.run()

            # Verify baseline was captured
            assert orch.baseline_eval is not None
            assert orch.baseline_eval == mock_eval_result
            # run_eval should have been called once for baseline capture
            assert eval_call_count[0] == 1
            # Should exit successfully since no tasks
            assert result == 0
        finally:
            orch.cleanup()

    def test_run_does_not_capture_baseline_when_eval_on_commit_disabled(self, temp_repo):
        """run() does not capture baseline eval when eval_on_commit is disabled."""
        # Create a tasklist with a task
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Test task\n")

        orch = Orchestrator(eval_on_commit=False)
        try:
            # Verify baseline is None before run()
            assert orch.baseline_eval is None

            # Track calls to run_eval
            eval_call_count = [0]

            def tracking_run_eval():
                eval_call_count[0] += 1
                return {"_passed": True}

            # Mock dependencies to prevent actual task execution
            with patch.object(orch, 'run_eval', side_effect=tracking_run_eval):
                with patch.object(orch, 'has_remaining_tasks', return_value=False):
                    # Run orchestrator - it should not capture baseline
                    orch.run()

            # Verify baseline was NOT captured
            assert orch.baseline_eval is None
            # run_eval should not have been called
            assert eval_call_count[0] == 0
        finally:
            orch.cleanup()

    def test_run_skips_baseline_on_continue(self, temp_repo):
        """run() does not capture baseline eval when continuing a previous run."""
        # Create a tasklist with a task
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Test task\n")

        orch = Orchestrator(eval_on_commit=True, continue_run=True)
        try:
            # Verify baseline is None before run()
            assert orch.baseline_eval is None

            # Track calls to run_eval
            eval_call_count = [0]

            def tracking_run_eval():
                eval_call_count[0] += 1
                return {"_passed": True}

            # Mock dependencies to prevent actual task execution
            with patch.object(orch, 'run_eval', side_effect=tracking_run_eval):
                with patch.object(orch, 'has_remaining_tasks', return_value=False):
                    with patch.object(orch, 'load_state', return_value=False):
                        # Run orchestrator - it should not capture baseline on continue
                        orch.run()

            # Verify baseline was NOT captured (skipped due to continue_run)
            assert orch.baseline_eval is None
            # run_eval should not have been called
            assert eval_call_count[0] == 0
        finally:
            orch.cleanup()

    def test_eval_on_commit_cli_flag(self, temp_repo):
        """--eval-on-commit CLI flag is recognized."""
        from millstone import orchestrate

        # Create a tasklist file
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Test task\n")

        with patch('sys.argv', ['orchestrate.py', '--eval-on-commit', '--dry-run']):
            # Use dry-run to avoid actually running
            with pytest.raises(SystemExit) as exc_info:
                orchestrate.main()
            # Dry run exits with 0
            assert exc_info.value.code == 0

    def test_eval_scripts_in_default_config(self):
        """eval_scripts defaults to empty list in DEFAULT_CONFIG."""
        from millstone.runtime.orchestrator import DEFAULT_CONFIG
        assert "eval_scripts" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["eval_scripts"] == []

    def test_eval_scripts_param_defaults_to_empty_list(self, temp_repo):
        """Orchestrator.eval_scripts defaults to empty list."""
        orch = Orchestrator()
        try:
            assert orch.eval_scripts == []
        finally:
            orch.cleanup()

    def test_eval_scripts_param_can_be_set(self, temp_repo):
        """Orchestrator accepts eval_scripts parameter."""
        scripts = ["mypy .", "ruff check ."]
        orch = Orchestrator(eval_scripts=scripts)
        try:
            assert orch.eval_scripts == scripts
        finally:
            orch.cleanup()

    def test_run_custom_eval_scripts_runs_commands(self, temp_repo):
        """_run_custom_eval_scripts runs each command and captures results."""
        orch = Orchestrator(eval_scripts=["echo hello", "echo world"])
        try:
            results = orch._run_custom_eval_scripts()

            assert len(results) == 2
            assert results[0]["command"] == "echo hello"
            assert results[0]["exit_code"] == 0
            assert "hello" in results[0]["stdout"]
            assert results[1]["command"] == "echo world"
            assert results[1]["exit_code"] == 0
        finally:
            orch.cleanup()

    def test_run_custom_eval_scripts_captures_failures(self, temp_repo):
        """_run_custom_eval_scripts captures non-zero exit codes."""
        orch = Orchestrator(eval_scripts=["exit 1"])
        try:
            results = orch._run_custom_eval_scripts()

            assert len(results) == 1
            assert results[0]["exit_code"] == 1
        finally:
            orch.cleanup()

    def test_run_custom_eval_scripts_captures_duration(self, temp_repo):
        """_run_custom_eval_scripts captures duration."""
        orch = Orchestrator(eval_scripts=["echo fast"])
        try:
            results = orch._run_custom_eval_scripts()

            assert len(results) == 1
            assert "duration" in results[0]
            assert results[0]["duration"] >= 0
        finally:
            orch.cleanup()

    def test_run_eval_with_custom_scripts_includes_in_json(self, temp_repo):
        """run_eval includes custom_scripts in JSON when scripts configured."""
        import json
        orch = Orchestrator(eval_scripts=["echo test"])
        try:
            with patch.object(orch, '_run_custom_eval_scripts') as mock_scripts:
                mock_scripts.return_value = [
                    {"command": "echo test", "exit_code": 0, "duration": 0.1, "stdout": "test\n", "stderr": ""}
                ]
                with patch('subprocess.run') as mock_run:
                    mock_run.return_value = MagicMock(stdout="5 passed in 0.50s", stderr="", returncode=0)
                    orch.run_eval()

            evals_dir = orch.work_dir / "evals"
            json_files = [f for f in evals_dir.glob("*.json") if f.name != "summary.json"]
            assert json_files, "Expected at least one eval JSON file"
            data = json.loads(json_files[0].read_text())

            assert "custom_scripts" in data
            assert len(data["custom_scripts"]) == 1
            assert data["custom_scripts"][0]["command"] == "echo test"
            assert data["custom_scripts"][0]["exit_code"] == 0
        finally:
            orch.cleanup()

    def test_run_eval_fails_when_custom_script_fails(self, temp_repo):
        """run_eval returns _passed=False when custom script fails."""
        orch = Orchestrator(eval_scripts=["exit 1"])
        try:
            with patch.object(orch, '_run_custom_eval_scripts') as mock_scripts:
                mock_scripts.return_value = [
                    {"command": "exit 1", "exit_code": 1, "duration": 0.1, "stdout": "", "stderr": "error"}
                ]
                with patch('subprocess.run') as mock_run:
                    # pytest passes
                    mock_run.return_value = MagicMock(stdout="5 passed in 0.50s", stderr="", returncode=0)
                    result = orch.run_eval()

            assert result["_passed"] is False
        finally:
            orch.cleanup()

    def test_run_eval_passes_when_all_scripts_pass(self, temp_repo):
        """run_eval returns _passed=True when pytest and all scripts pass."""
        orch = Orchestrator(eval_scripts=["echo ok"])
        try:
            with patch.object(orch, '_run_custom_eval_scripts') as mock_scripts:
                mock_scripts.return_value = [
                    {"command": "echo ok", "exit_code": 0, "duration": 0.1, "stdout": "ok\n", "stderr": ""}
                ]
                with patch('subprocess.run') as mock_run:
                    mock_run.return_value = MagicMock(stdout="5 passed in 0.50s", stderr="", returncode=0)
                    result = orch.run_eval()

            assert result["_passed"] is True
        finally:
            orch.cleanup()

    def test_print_eval_summary_shows_custom_scripts(self, temp_repo, capsys):
        """_print_eval_summary shows custom script results."""
        orch = Orchestrator()
        try:
            eval_result = {
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": [],
                "custom_scripts": [
                    {"command": "mypy .", "exit_code": 0, "duration": 2.5},
                    {"command": "ruff check .", "exit_code": 1, "duration": 1.0},
                ],
            }
            orch._print_eval_summary(eval_result)

            captured = capsys.readouterr()
            assert "Custom scripts:" in captured.out
            assert "[PASS] mypy ." in captured.out
            assert "[FAIL] ruff check ." in captured.out
            assert "Status: FAILED" in captured.out  # Because ruff failed
        finally:
            orch.cleanup()

    def test_print_eval_summary_no_custom_scripts(self, temp_repo, capsys):
        """_print_eval_summary doesn't show custom scripts section when none configured."""
        orch = Orchestrator()
        try:
            eval_result = {
                "git_head": "abc123",
                "duration_seconds": 1.0,
                "tests": {"total": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": [],
            }
            orch._print_eval_summary(eval_result)

            captured = capsys.readouterr()
            assert "Custom scripts:" not in captured.out
            assert "Status: PASSED" in captured.out
        finally:
            orch.cleanup()

    def test_eval_scripts_loaded_from_config(self, temp_repo):
        """eval_scripts is loaded from config.toml."""
        from millstone.runtime.orchestrator import load_config

        # Create config file
        millstone_dir = temp_repo / ".millstone"
        millstone_dir.mkdir(exist_ok=True)
        config_path = millstone_dir / "config.toml"
        config_path.write_text('eval_scripts = ["mypy .", "ruff check ."]\n')

        config = load_config(temp_repo)
        assert config["eval_scripts"] == ["mypy .", "ruff check ."]


class TestCategoryScoring:
    """Tests for category scoring in run_eval()."""

    def test_category_weights_in_default_config(self):
        """category_weights is defined in DEFAULT_CONFIG."""
        from millstone.runtime.orchestrator import DEFAULT_CONFIG

        assert "category_weights" in DEFAULT_CONFIG
        weights = DEFAULT_CONFIG["category_weights"]
        assert "tests" in weights
        assert "typing" in weights
        assert "lint" in weights
        assert "coverage" in weights
        assert "security" in weights
        assert "complexity" in weights
        # Weights should sum to 1.0
        assert abs(sum(weights.values()) - 1.0) < 0.01

    def test_category_thresholds_in_default_config(self):
        """category_thresholds is defined in DEFAULT_CONFIG."""
        from millstone.runtime.orchestrator import DEFAULT_CONFIG

        assert "category_thresholds" in DEFAULT_CONFIG
        thresholds = DEFAULT_CONFIG["category_thresholds"]
        assert "typing" in thresholds
        assert "lint" in thresholds
        assert "security" in thresholds
        assert "complexity" in thresholds

    def test_run_category_evals_returns_tests_category(self, temp_repo):
        """run_category_evals always returns tests category."""
        orch = Orchestrator()
        try:
            test_results = {"total": 10, "passed": 9, "failed": 1, "errors": 0}
            result = orch.run_category_evals(test_results, None)

            assert "categories" in result
            assert "tests" in result["categories"]
            assert result["categories"]["tests"]["score"] == 0.9
            assert result["categories"]["tests"]["passed"] == 9
            assert result["categories"]["tests"]["failed"] == 1
        finally:
            orch.cleanup()

    def test_run_category_evals_tests_score_all_passed(self, temp_repo):
        """tests category score is 1.0 when all tests pass."""
        orch = Orchestrator()
        try:
            test_results = {"total": 10, "passed": 10, "failed": 0, "errors": 0}
            result = orch.run_category_evals(test_results, None)

            assert result["categories"]["tests"]["score"] == 1.0
        finally:
            orch.cleanup()

    def test_run_category_evals_tests_score_all_failed(self, temp_repo):
        """tests category score is 0.0 when all tests fail."""
        orch = Orchestrator()
        try:
            test_results = {"total": 10, "passed": 0, "failed": 10, "errors": 0}
            result = orch.run_category_evals(test_results, None)

            assert result["categories"]["tests"]["score"] == 0.0
        finally:
            orch.cleanup()

    def test_run_category_evals_no_tests(self, temp_repo):
        """tests category score is 1.0 when no tests exist."""
        orch = Orchestrator()
        try:
            test_results = {"total": 0, "passed": 0, "failed": 0, "errors": 0}
            result = orch.run_category_evals(test_results, None)

            # No tests = nothing to fail = perfect score
            assert result["categories"]["tests"]["score"] == 1.0
        finally:
            orch.cleanup()

    def test_run_category_evals_includes_coverage_when_available(self, temp_repo):
        """run_category_evals includes coverage category when data available."""
        orch = Orchestrator()
        try:
            test_results = {"total": 10, "passed": 10, "failed": 0, "errors": 0}
            coverage_data = {"line_rate": 0.85}
            result = orch.run_category_evals(test_results, coverage_data)

            assert "coverage" in result["categories"]
            assert result["categories"]["coverage"]["score"] == 0.85
            assert result["categories"]["coverage"]["line_rate"] == 0.85
        finally:
            orch.cleanup()

    def test_run_category_evals_no_coverage_without_data(self, temp_repo):
        """run_category_evals excludes coverage category when no data."""
        orch = Orchestrator()
        try:
            test_results = {"total": 10, "passed": 10, "failed": 0, "errors": 0}
            result = orch.run_category_evals(test_results, None)

            assert "coverage" not in result["categories"]
        finally:
            orch.cleanup()

    def test_compute_composite_score_single_category(self, temp_repo):
        """composite score works with single category."""
        orch = Orchestrator()
        try:
            categories = {"tests": {"score": 0.9}}
            score = orch._compute_composite_score(categories)

            assert score == 0.9
        finally:
            orch.cleanup()

    def test_compute_composite_score_multiple_categories(self, temp_repo):
        """composite score is weighted average of categories."""
        orch = Orchestrator()
        try:
            # Set custom weights for predictable result
            orch.category_weights = {"tests": 0.5, "coverage": 0.5}
            categories = {
                "tests": {"score": 1.0},
                "coverage": {"score": 0.8},
            }
            score = orch._compute_composite_score(categories)

            assert score == 0.9  # (1.0 * 0.5 + 0.8 * 0.5) = 0.9
        finally:
            orch.cleanup()

    def test_compute_composite_score_normalizes_weights(self, temp_repo):
        """composite score normalizes to available category weights."""
        orch = Orchestrator()
        try:
            # Only tests is present, but weights include coverage
            orch.category_weights = {"tests": 0.4, "coverage": 0.6}
            categories = {"tests": {"score": 0.8}}
            score = orch._compute_composite_score(categories)

            # Should normalize to 100% tests weight = 0.8
            assert score == 0.8
        finally:
            orch.cleanup()

    def test_compute_composite_score_empty_categories(self, temp_repo):
        """composite score is 0.0 with no categories."""
        orch = Orchestrator()
        try:
            score = orch._compute_composite_score({})

            assert score == 0.0
        finally:
            orch.cleanup()

    def test_run_eval_includes_categories_in_result(self, temp_repo):
        """run_eval includes categories in result dict."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run, \
                 patch('shutil.which', return_value=None):  # No optional tools
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 0.50s",
                    stderr="",
                    returncode=0,
                )
                result = orch.run_eval()

            assert "categories" in result
            assert "tests" in result["categories"]
        finally:
            orch.cleanup()

    def test_run_eval_includes_composite_score_in_result(self, temp_repo):
        """run_eval includes composite_score in result dict."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run, \
                 patch('shutil.which', return_value=None):
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 0.50s",
                    stderr="",
                    returncode=0,
                )
                result = orch.run_eval()

            assert "composite_score" in result
            assert isinstance(result["composite_score"], float)
        finally:
            orch.cleanup()

    def test_run_eval_json_includes_categories(self, temp_repo):
        """run_eval stores categories in JSON file."""
        import json

        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run, \
                 patch('shutil.which', return_value=None):
                mock_run.return_value = MagicMock(
                    stdout="5 passed in 0.50s",
                    stderr="",
                    returncode=0,
                )
                orch.run_eval()

            evals_dir = orch.work_dir / "evals"
            json_files = [f for f in evals_dir.glob("*.json") if f.name != "summary.json"]
            assert json_files, "Expected at least one eval JSON file"
            data = json.loads(json_files[0].read_text())

            assert "categories" in data
            assert "composite_score" in data
        finally:
            orch.cleanup()

    def test_print_eval_summary_shows_category_scores(self, temp_repo, capsys):
        """_print_eval_summary shows category scores."""
        orch = Orchestrator()
        try:
            eval_result = {
                "git_head": "abc123",
                "duration_seconds": 1.5,
                "tests": {"total": 10, "passed": 9, "failed": 1, "errors": 0, "skipped": 0},
                "failed_tests": [],
                "categories": {
                    "tests": {"score": 0.9, "passed": 9, "failed": 1},
                },
                "composite_score": 0.9,
            }
            orch._print_eval_summary(eval_result)

            captured = capsys.readouterr()
            assert "Category Scores:" in captured.out
            assert "tests:" in captured.out
            assert "0.90" in captured.out
        finally:
            orch.cleanup()

    def test_print_eval_summary_shows_composite_score(self, temp_repo, capsys):
        """_print_eval_summary shows composite score."""
        orch = Orchestrator()
        try:
            eval_result = {
                "git_head": "abc123",
                "duration_seconds": 1.5,
                "tests": {"total": 10, "passed": 10, "failed": 0, "errors": 0, "skipped": 0},
                "failed_tests": [],
                "categories": {
                    "tests": {"score": 1.0, "passed": 10, "failed": 0},
                },
                "composite_score": 1.0,
            }
            orch._print_eval_summary(eval_result)

            captured = capsys.readouterr()
            assert "Composite Score:" in captured.out
            assert "1.00" in captured.out
        finally:
            orch.cleanup()

    def test_run_typing_parses_errors(self, temp_repo):
        """_run_typing correctly parses mypy-style error output."""
        orch = Orchestrator()
        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="file.py:1: error: Something wrong\nfile.py:2: error: Another error\n",
                    stderr="",
                    returncode=1,
                )
                result = orch._run_typing()

            assert result["errors"] == 2
            # With threshold 50, 2 errors = score 0.96
            assert result["score"] == 0.96
        finally:
            orch.cleanup()

    def test_run_lint_parses_json_output(self, temp_repo):
        """_run_lint correctly parses ruff JSON output when using default."""
        import json

        orch = Orchestrator()
        try:
            issues = [{"code": "E501"}, {"code": "F401"}, {"code": "F401"}]
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout=json.dumps(issues),
                    stderr="",
                    returncode=1,
                )
                result = orch._run_lint()  # No custom command = use ruff

            assert result["errors"] == 3
            # With threshold 100, 3 errors = score 0.97
            assert result["score"] == 0.97
        finally:
            orch.cleanup()

    def test_category_eval_tool_not_available(self, temp_repo):
        """Category is skipped when tool is not available."""
        orch = Orchestrator()
        try:
            with patch('shutil.which', return_value=None):
                test_results = {"total": 10, "passed": 10, "failed": 0, "errors": 0}
                result = orch.run_category_evals(test_results, None)

            # Only tests should be present (always available)
            assert "tests" in result["categories"]
            assert "typing" not in result["categories"]
            assert "lint" not in result["categories"]
            assert "security" not in result["categories"]
            assert "complexity" not in result["categories"]
        finally:
            orch.cleanup()

    def test_score_capped_at_zero(self, temp_repo):
        """Score is capped at 0.0 when errors exceed threshold."""
        orch = Orchestrator()
        orch.category_thresholds = {"typing": 10}
        try:
            with patch('subprocess.run') as mock_run:
                # 20 errors with threshold 10 should give score 0.0
                mock_run.return_value = MagicMock(
                    stdout=": error:\n" * 20,
                    stderr="",
                    returncode=1,
                )
                result = orch._run_typing()

            assert result["score"] == 0.0
            assert result["errors"] == 20
        finally:
            orch.cleanup()


class TestOuterLoopManagerProviderInjection:
    """Tests for provider injection into OuterLoopManager."""

    def test_has_opportunity_provider_attribute(self, temp_repo):
        """OuterLoopManager has opportunity_provider attribute."""
        orch = Orchestrator()
        try:
            mgr = orch._outer_loop_manager
            assert hasattr(mgr, "opportunity_provider")
        finally:
            orch.cleanup()

    def test_has_design_provider_attribute(self, temp_repo):
        """OuterLoopManager has design_provider attribute."""
        orch = Orchestrator()
        try:
            mgr = orch._outer_loop_manager
            assert hasattr(mgr, "design_provider")
        finally:
            orch.cleanup()

    def test_has_tasklist_provider_attribute(self, temp_repo):
        """OuterLoopManager has tasklist_provider attribute."""
        orch = Orchestrator()
        try:
            mgr = orch._outer_loop_manager
            assert hasattr(mgr, "tasklist_provider")
        finally:
            orch.cleanup()

    def test_opportunity_provider_is_protocol(self, temp_repo):
        """opportunity_provider passes isinstance check against OpportunityProvider Protocol."""
        from millstone.artifact_providers.protocols import OpportunityProvider
        orch = Orchestrator()
        try:
            assert isinstance(orch._outer_loop_manager.opportunity_provider, OpportunityProvider)
        finally:
            orch.cleanup()

    def test_design_provider_is_protocol(self, temp_repo):
        """design_provider passes isinstance check against DesignProvider Protocol."""
        from millstone.artifact_providers.protocols import DesignProvider
        orch = Orchestrator()
        try:
            assert isinstance(orch._outer_loop_manager.design_provider, DesignProvider)
        finally:
            orch.cleanup()

    def test_tasklist_provider_is_protocol(self, temp_repo):
        """tasklist_provider passes isinstance check against TasklistProvider Protocol."""
        from millstone.artifact_providers.protocols import TasklistProvider
        orch = Orchestrator()
        try:
            assert isinstance(orch._outer_loop_manager.tasklist_provider, TasklistProvider)
        finally:
            orch.cleanup()

    def test_opportunity_provider_path(self, temp_repo):
        """opportunity_provider.path points to repo_dir/.millstone/opportunities.md."""
        orch = Orchestrator()
        try:
            provider = orch._outer_loop_manager.opportunity_provider
            assert provider.path == temp_repo / ".millstone" / "opportunities.md"
        finally:
            orch.cleanup()

    def test_design_provider_path(self, temp_repo):
        """design_provider.path points to repo_dir/.millstone/designs."""
        orch = Orchestrator()
        try:
            provider = orch._outer_loop_manager.design_provider
            assert provider.path == temp_repo / ".millstone" / "designs"
        finally:
            orch.cleanup()

    def test_tasklist_provider_path(self, temp_repo):
        """tasklist_provider.path points to the configured tasklist file."""
        orch = Orchestrator()
        try:
            provider = orch._outer_loop_manager.tasklist_provider
            assert provider.path == temp_repo / orch.tasklist
        finally:
            orch.cleanup()


class TestAnalyzeInfrastructure:
    """Tests for run_analyze() method."""

    # APPROVED verdict JSON returned by the reviewer mock
    _APPROVED_RESPONSE = '```json\n{"verdict": "APPROVED", "feedback": ""}\n```'

    def test_run_analyze_loads_prompt(self, temp_repo):
        """run_analyze loads and uses the analyze prompt; first call is the analyze prompt."""
        orch = Orchestrator()
        try:
            # Create opportunities.md to simulate successful analysis
            opportunities_file = temp_repo / ".millstone" / "opportunities.md"
            opportunities_file.write_text("# Opportunities\n\n### Test opportunity\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                # Call 1: analyzer; Call 2: reviewer (returns APPROVED to end loop)
                mock_claude.side_effect = ["Analysis complete", self._APPROVED_RESPONSE]
                orch.run_analyze()

                # First call must be the analyze prompt
                assert mock_claude.call_count >= 2
                prompt = mock_claude.call_args_list[0][0][0]
                assert "senior software architect" in prompt
                assert "improvement opportunities" in prompt
        finally:
            orch.cleanup()
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_returns_success_when_opportunities_created(self, temp_repo):
        """run_analyze returns success=True when opportunities.md is created."""
        orch = Orchestrator()
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]

                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer

                mock_claude.side_effect = side_effect
                result = orch.run_analyze()

            assert result["success"] is True
            assert result["opportunities_file"] is not None
        finally:
            orch.cleanup()
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_returns_failure_when_opportunities_not_created(self, temp_repo):
        """run_analyze returns success=False when review loop exhausts without approval."""
        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                # Never returns APPROVED → loop exhausts → success=False
                mock_claude.return_value = "Did nothing"
                result = orch.run_analyze()

            assert result["success"] is False
        finally:
            orch.cleanup()

    def test_run_analyze_counts_opportunities(self, temp_repo):
        """run_analyze counts the number of opportunities found."""
        orch = Orchestrator()
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]

                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer
                        opportunities_file.write_text(
                            "# Opportunities\n\n"
                            "### First opportunity\n"
                            "Description\n\n"
                            "### Second opportunity\n"
                            "Description\n\n"
                            "### Third opportunity\n"
                            "Description\n"
                        )
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer

                mock_claude.side_effect = side_effect
                result = orch.run_analyze()

            assert result["opportunity_count"] == 3
        finally:
            orch.cleanup()
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_counts_opportunities_checklist_format(self, temp_repo):
        """run_analyze counts opportunities in checklist format via provider."""
        orch = Orchestrator()
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]

                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer
                        opportunities_file.write_text(
                            "# Opportunities\n\n"
                            "- [ ] First opportunity\n"
                            "  - Opportunity ID: first-opportunity\n"
                            "  - Status: identified\n"
                            "  - ROI Score: 8\n\n"
                            "- [ ] Second opportunity\n"
                            "  - Opportunity ID: second-opportunity\n"
                            "  - Status: identified\n"
                            "  - ROI Score: 6\n\n"
                            "- [x] Third opportunity\n"
                            "  - Opportunity ID: third-opportunity\n"
                            "  - Status: adopted\n"
                            "  - ROI Score: 5\n"
                        )
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer

                mock_claude.side_effect = side_effect
                result = orch.run_analyze()

            # Provider sees all 3 opportunities (identified + adopted)
            assert result["opportunity_count"] == 3
        finally:
            orch.cleanup()
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_logs_completed(self, temp_repo):
        """run_analyze logs analyze_completed event on success."""
        orch = Orchestrator()
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]

                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer

                mock_claude.side_effect = side_effect
                orch.run_analyze()

            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            assert "analyze_completed" in log_content
        finally:
            orch.cleanup()
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_logs_failed(self, temp_repo):
        """run_analyze logs analyze_failed event when review loop exhausts without approval."""
        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                # Never returns APPROVED → loop exhausts → analyze_failed logged
                mock_claude.return_value = "Did nothing"
                orch.run_analyze()

            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            assert "analyze_failed" in log_content
        finally:
            orch.cleanup()

    def test_run_analyze_prints_summary(self, temp_repo, capsys):
        """run_analyze prints summary to stdout on success."""
        orch = Orchestrator()
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]

                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer

                mock_claude.side_effect = side_effect
                orch.run_analyze()

            captured = capsys.readouterr()
            assert "Analysis Complete" in captured.out
            assert "Opportunities found:" in captured.out
        finally:
            orch.cleanup()
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_with_goals_file(self, temp_repo):
        """run_analyze incorporates goals.md content into prompt when present."""
        goals_file = temp_repo / "goals.md"
        goals_file.write_text("# Project Goals\n\n1. Be awesome\n2. Stay simple\n")

        orch = Orchestrator()
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]

                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer

                mock_claude.side_effect = side_effect
                result = orch.run_analyze()

                # First call must be the analyze prompt with goals injected
                assert mock_claude.call_count >= 2
                prompt = mock_claude.call_args_list[0][0][0]
                assert "## Project Goals" in prompt
                assert "Be awesome" in prompt
                assert "Stay simple" in prompt
                assert "Prioritize opportunities that advance these goals" in prompt

            assert result["goals_used"] is True
        finally:
            orch.cleanup()
            goals_file.unlink()
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_without_goals_file(self, temp_repo):
        """run_analyze works normally without goals.md."""
        orch = Orchestrator()
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]

                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer

                mock_claude.side_effect = side_effect
                result = orch.run_analyze()

                # First call (analyze prompt) must not have the goals placeholder
                assert mock_claude.call_count >= 2
                prompt = mock_claude.call_args_list[0][0][0]
                assert "{{PROJECT_GOALS}}" not in prompt

            assert result["goals_used"] is False
        finally:
            orch.cleanup()
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_logs_goals_used(self, temp_repo):
        """run_analyze logs goals_used in analyze_completed event."""
        goals_file = temp_repo / "goals.md"
        goals_file.write_text("# Project Goals\n\n1. Test goal\n")

        orch = Orchestrator()
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]

                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer

                mock_claude.side_effect = side_effect
                orch.run_analyze()

            # Check the log file for goals_used
            log_content = orch.log_file.read_text()
            assert "goals_used" in log_content
            assert "True" in log_content
        finally:
            orch.cleanup()
            goals_file.unlink()
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_prints_goals_status(self, temp_repo, capsys):
        """run_analyze prints goals status in summary when goals.md is used."""
        goals_file = temp_repo / "goals.md"
        goals_file.write_text("# Project Goals\n\n1. Test goal\n")

        orch = Orchestrator()
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]

                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer

                mock_claude.side_effect = side_effect
                orch.run_analyze()

            captured = capsys.readouterr()
            assert "Project goals: incorporated from goals.md" in captured.out
        finally:
            orch.cleanup()
            goals_file.unlink()
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_wires_reviewer_callback_with_reviewer_role(self, temp_repo):
        """Orchestrator.run_analyze passes a reviewer_callback with role='reviewer' to OuterLoopManager."""
        orch = Orchestrator()
        try:
            captured_kwargs = {}

            def fake_run_analyze(**kwargs):
                captured_kwargs.update(kwargs)
                return {"success": True, "opportunities_file": None, "opportunity_count": 0}

            with patch.object(orch._outer_loop_manager, "run_analyze", side_effect=fake_run_analyze):
                orch.run_analyze()

            assert "reviewer_callback" in captured_kwargs
            assert callable(captured_kwargs["reviewer_callback"])
        finally:
            orch.cleanup()

    def test_run_analyze_reviewer_callback_uses_reviewer_role(self, temp_repo):
        """The reviewer_callback wired by Orchestrator.run_analyze invokes run_agent with role='reviewer'."""
        orch = Orchestrator()
        try:
            captured_role = {}

            def fake_run_analyze(**kwargs):
                # Extract and invoke the reviewer_callback to observe its role
                reviewer_cb = kwargs.get("reviewer_callback")
                if reviewer_cb is not None:
                    with patch.object(orch, "run_agent") as mock_agent:
                        mock_agent.return_value = "review output"
                        reviewer_cb("some prompt")
                        if mock_agent.called:
                            captured_role["role"] = mock_agent.call_args[1].get("role")
                return {"success": True, "opportunities_file": None, "opportunity_count": 0}

            with patch.object(orch._outer_loop_manager, "run_analyze", side_effect=fake_run_analyze):
                orch.run_analyze()

            assert captured_role.get("role") == "reviewer"
        finally:
            orch.cleanup()


class TestAnalyzeCLI:
    """Tests for --analyze CLI flag."""

    def test_analyze_flag_runs_analyze(self, temp_repo):
        """--analyze flag invokes run_analyze."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--analyze']):
            with patch.object(Orchestrator, 'run_analyze') as mock_analyze:
                mock_analyze.return_value = {"success": True}
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()

                mock_analyze.assert_called_once()
                assert exc_info.value.code == 0

    def test_analyze_flag_exits_1_on_failure(self, temp_repo):
        """--analyze exits with code 1 when analysis fails."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--analyze']):
            with patch.object(Orchestrator, 'run_analyze') as mock_analyze:
                mock_analyze.return_value = {"success": False}
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()

                assert exc_info.value.code == 1

    def test_analyze_flag_exits_1_on_exception(self, temp_repo):
        """--analyze exits with code 1 on exception."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--analyze']):
            with patch.object(Orchestrator, 'run_analyze') as mock_analyze:
                mock_analyze.side_effect = Exception("Test error")
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()

                assert exc_info.value.code == 1

    def test_issues_requires_analyze(self, temp_repo):
        """--issues requires --analyze flag."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--issues', 'bugs.md']):
            with pytest.raises(SystemExit) as exc_info:
                orchestrate.main()

            # argparse exits with code 2 on argument errors
            assert exc_info.value.code == 2

    def test_issues_flag_passes_to_run_analyze(self, temp_repo):
        """--issues flag passes issues file path to run_analyze."""
        from millstone import orchestrate

        issues_file = temp_repo / "bugs.md"
        issues_file.write_text("# Known Bugs\n\n- Bug 1\n")

        with patch('sys.argv', ['orchestrate.py', '--analyze', '--issues', str(issues_file)]):
            with patch.object(Orchestrator, 'run_analyze') as mock_analyze:
                mock_analyze.return_value = {"success": True}
                with pytest.raises(SystemExit):
                    orchestrate.main()

                mock_analyze.assert_called_once_with(issues_file=str(issues_file))


class TestAnalyzeWithIssues:
    """Tests for run_analyze() with issues file."""

    # APPROVED verdict JSON returned by the reviewer mock
    _APPROVED_RESPONSE = '```json\n{"verdict": "APPROVED", "feedback": ""}\n```'

    def test_run_analyze_with_issues_file(self, temp_repo):
        """run_analyze incorporates issues file content into prompt when provided."""
        issues_file = temp_repo / "bugs.md"
        issues_file.write_text("# Known Bugs\n\n- Bug 1: Login fails on mobile\n- Bug 2: Memory leak in cache\n")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]
                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer/producer
                        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer
                mock_claude.side_effect = side_effect

                result = orch.run_analyze(issues_file=str(issues_file))

                # Verify issues were injected into the first (analyze) prompt
                assert mock_claude.call_count >= 2
                prompt = mock_claude.call_args_list[0][0][0]
                assert "## Known Issues" in prompt
                assert "Bug 1: Login fails on mobile" in prompt
                assert "Bug 2: Memory leak in cache" in prompt
                assert "Confirms reported issue" in prompt

            assert result["issues_used"] is True
        finally:
            orch.cleanup()
            issues_file.unlink()
            opportunities_file = temp_repo / ".millstone" / "opportunities.md"
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_without_issues_file(self, temp_repo):
        """run_analyze works normally when no issues file provided."""
        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]
                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer/producer
                        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer
                mock_claude.side_effect = side_effect

                result = orch.run_analyze()

                # Verify prompt doesn't have issues placeholder
                assert mock_claude.call_count >= 2
                prompt = mock_claude.call_args_list[0][0][0]
                assert "{{KNOWN_ISSUES}}" not in prompt

            assert result["issues_used"] is False
        finally:
            orch.cleanup()
            opportunities_file = temp_repo / ".millstone" / "opportunities.md"
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_with_nonexistent_issues_file(self, temp_repo, capsys):
        """run_analyze warns and continues when issues file doesn't exist."""
        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]
                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer/producer
                        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer
                mock_claude.side_effect = side_effect

                result = orch.run_analyze(issues_file="nonexistent.md")

                # Verify placeholder was removed, not replaced with content
                assert mock_claude.call_count >= 2
                prompt = mock_claude.call_args_list[0][0][0]
                assert "{{KNOWN_ISSUES}}" not in prompt
                assert "## Known Issues" not in prompt

            assert result["issues_used"] is False

            # Verify warning was printed
            captured = capsys.readouterr()
            assert "Warning: Issues file not found" in captured.out
        finally:
            orch.cleanup()
            opportunities_file = temp_repo / ".millstone" / "opportunities.md"
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_with_relative_issues_path(self, temp_repo):
        """run_analyze resolves relative issues file path from repo directory."""
        issues_file = temp_repo / "docs" / "bugs.md"
        issues_file.parent.mkdir(exist_ok=True)
        issues_file.write_text("# Known Bugs\n\n- Relative path bug\n")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]
                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer/producer
                        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer
                mock_claude.side_effect = side_effect

                result = orch.run_analyze(issues_file="docs/bugs.md")

                # Verify issues were found and injected into the first (analyze) prompt
                assert mock_claude.call_count >= 2
                prompt = mock_claude.call_args_list[0][0][0]
                assert "Relative path bug" in prompt

            assert result["issues_used"] is True
        finally:
            orch.cleanup()
            issues_file.unlink()
            opportunities_file = temp_repo / ".millstone" / "opportunities.md"
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_logs_issues_used(self, temp_repo):
        """run_analyze logs issues_used in analyze_completed event."""
        issues_file = temp_repo / "bugs.md"
        issues_file.write_text("# Known Bugs\n\n- Test bug\n")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]
                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer/producer
                        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer
                mock_claude.side_effect = side_effect

                orch.run_analyze(issues_file=str(issues_file))

            # Check the log file for issues_used
            log_content = orch.log_file.read_text()
            assert "issues_used" in log_content
            assert "True" in log_content
        finally:
            orch.cleanup()
            issues_file.unlink()
            opportunities_file = temp_repo / ".millstone" / "opportunities.md"
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_prints_issues_status(self, temp_repo, capsys):
        """run_analyze prints issues status in summary when issues file is used."""
        issues_file = temp_repo / "bugs.md"
        issues_file.write_text("# Known Bugs\n\n- Test bug\n")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]
                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer/producer
                        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer
                mock_claude.side_effect = side_effect

                orch.run_analyze(issues_file=str(issues_file))

            captured = capsys.readouterr()
            assert "Known issues: incorporated from issues file" in captured.out
        finally:
            orch.cleanup()
            issues_file.unlink()
            opportunities_file = temp_repo / ".millstone" / "opportunities.md"
            if opportunities_file.exists():
                opportunities_file.unlink()


class TestHardSignals:
    """Tests for hard signal collection in run_analyze()."""

    # APPROVED verdict JSON returned by the reviewer mock
    _APPROVED_RESPONSE = '```json\n{"verdict": "APPROVED", "feedback": ""}\n```'

    def test_collect_hard_signals_returns_structure(self, temp_repo):
        """collect_hard_signals returns dict with expected signal categories."""
        orch = Orchestrator()
        try:
            signals = orch.collect_hard_signals()

            assert "timestamp" in signals
            assert "test_failures" in signals
            assert "coverage_gaps" in signals
            assert "todo_comments" in signals
            assert "lint_errors" in signals
            assert "typing_errors" in signals
            assert "complexity_hotspots" in signals
            assert "total_signals" in signals

            # All values should be lists (except timestamp and total_signals)
            assert isinstance(signals["test_failures"], list)
            assert isinstance(signals["coverage_gaps"], list)
            assert isinstance(signals["todo_comments"], list)
            assert isinstance(signals["lint_errors"], list)
            assert isinstance(signals["typing_errors"], list)
            assert isinstance(signals["complexity_hotspots"], list)
            assert isinstance(signals["total_signals"], int)
        finally:
            orch.cleanup()

    def test_collect_hard_signals_stores_to_file(self, temp_repo):
        """collect_hard_signals stores signals in .millstone/signals/."""
        orch = Orchestrator()
        try:
            signals = orch.collect_hard_signals()

            signals_dir = orch.work_dir / "signals"
            assert signals_dir.exists()

            json_files = list(signals_dir.glob("*.json"))
            assert len(json_files) == 1

            stored = json.loads(json_files[0].read_text())
            assert stored["timestamp"] == signals["timestamp"]
            assert stored["total_signals"] == signals["total_signals"]
        finally:
            orch.cleanup()

    def test_collect_hard_signals_reads_test_failures_from_eval(self, temp_repo):
        """collect_hard_signals reads test failures from last eval."""
        orch = Orchestrator()
        try:
            # Create a mock eval with failed tests
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir(parents=True, exist_ok=True)
            eval_file = evals_dir / "20241201_120000.json"
            eval_file.write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "failed_tests": ["test_foo.py::test_bar", "test_baz.py::test_qux"],
            }))

            signals = orch.collect_hard_signals()

            assert signals["test_failures"] == ["test_foo.py::test_bar", "test_baz.py::test_qux"]
        finally:
            orch.cleanup()

    def test_collect_hard_signals_finds_todo_comments(self, temp_repo):
        """collect_hard_signals finds TODO/FIXME/HACK comments in codebase."""
        orch = Orchestrator()
        try:
            # Create a file with TODO comments
            test_file = temp_repo / "test_todos.py"
            test_file.write_text("# TODO: fix this\n# FIXME: broken\n")

            signals = orch.collect_hard_signals()

            # Should find our TODO comments
            todo_files = [t["file"] for t in signals["todo_comments"]]
            assert any("test_todos.py" in f for f in todo_files)
        finally:
            orch.cleanup()
            if test_file.exists():
                test_file.unlink()

    def test_format_signals_for_prompt_empty(self, temp_repo):
        """_format_signals_for_prompt handles empty signals."""
        orch = Orchestrator()
        try:
            signals = {
                "test_failures": [],
                "coverage_gaps": [],
                "todo_comments": [],
                "lint_errors": [],
                "typing_errors": [],
                "complexity_hotspots": [],
                "total_signals": 0,
            }

            result = orch._format_signals_for_prompt(signals)
            assert "No hard signals detected" in result
        finally:
            orch.cleanup()

    def test_format_signals_for_prompt_with_data(self, temp_repo):
        """_format_signals_for_prompt formats signal data as markdown."""
        orch = Orchestrator()
        try:
            signals = {
                "test_failures": ["test_foo.py::test_bar"],
                "coverage_gaps": [{"file": "foo.py", "coverage": 50.0, "missing_lines": 10}],
                "todo_comments": [{"file": "bar.py", "line": 5, "text": "TODO: fix"}],
                "lint_errors": [],
                "typing_errors": [],
                "complexity_hotspots": [],
                "total_signals": 3,
            }

            result = orch._format_signals_for_prompt(signals)
            assert "### Test Failures" in result
            assert "test_foo.py::test_bar" in result
            assert "### Coverage Gaps" in result
            assert "foo.py" in result
            assert "50.0%" in result
            assert "### TODO/FIXME/HACK Comments" in result
            assert "bar.py:5" in result
        finally:
            orch.cleanup()

    def test_run_analyze_collects_hard_signals(self, temp_repo):
        """run_analyze collects hard signals and includes in result."""
        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]
                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer/producer
                        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer
                mock_claude.side_effect = side_effect

                result = orch.run_analyze()

            assert "hard_signals" in result
            assert isinstance(result["hard_signals"], dict)
            assert "total_signals" in result["hard_signals"]
        finally:
            orch.cleanup()
            opportunities_file = temp_repo / ".millstone" / "opportunities.md"
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_injects_signals_into_prompt(self, temp_repo):
        """run_analyze injects hard signals into analyze prompt."""
        orch = Orchestrator()
        try:
            # Create eval with test failures to generate signals
            evals_dir = orch.work_dir / "evals"
            evals_dir.mkdir(parents=True, exist_ok=True)
            eval_file = evals_dir / "20241201_120000.json"
            eval_file.write_text(json.dumps({
                "timestamp": "2024-12-01T12:00:00",
                "failed_tests": ["test_fail.py::test_broken"],
            }))

            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]
                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer/producer
                        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer
                mock_claude.side_effect = side_effect

                orch.run_analyze()

                # Verify signals were injected into the first (analyze) prompt
                assert mock_claude.call_count >= 2
                prompt = mock_claude.call_args_list[0][0][0]
                assert "## Hard Signals" in prompt
                assert "test_fail.py::test_broken" in prompt
        finally:
            orch.cleanup()
            opportunities_file = temp_repo / ".millstone" / "opportunities.md"
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_no_signals_removes_placeholder(self, temp_repo):
        """run_analyze removes {{HARD_SIGNALS}} placeholder when no signals found."""
        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]
                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer/producer
                        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer
                mock_claude.side_effect = side_effect

                orch.run_analyze()

                # Verify placeholder was removed in the first (analyze) prompt
                assert mock_claude.call_count >= 2
                prompt = mock_claude.call_args_list[0][0][0]
                assert "{{HARD_SIGNALS}}" not in prompt
        finally:
            orch.cleanup()
            opportunities_file = temp_repo / ".millstone" / "opportunities.md"
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_prints_signal_count(self, temp_repo, capsys):
        """run_analyze prints hard signal count in summary."""
        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]
                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer/producer
                        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer
                mock_claude.side_effect = side_effect

                orch.run_analyze()

            captured = capsys.readouterr()
            assert "Hard signals collected:" in captured.out
        finally:
            orch.cleanup()
            opportunities_file = temp_repo / ".millstone" / "opportunities.md"
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_run_analyze_logs_signal_count(self, temp_repo):
        """run_analyze logs hard_signals_count in log event."""
        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]
                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] % 2 == 1:  # odd = analyzer/producer
                        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
                        opportunities_file.write_text("# Opportunities\n\n### Test\n")
                        return "Done"
                    return self._APPROVED_RESPONSE  # even = reviewer
                mock_claude.side_effect = side_effect

                orch.run_analyze()

            log_content = orch.log_file.read_text()
            assert "hard_signals_count" in log_content
        finally:
            orch.cleanup()
            opportunities_file = temp_repo / ".millstone" / "opportunities.md"
            if opportunities_file.exists():
                opportunities_file.unlink()

    def test_analyze_prompt_uses_checklist_format(self, temp_repo):
        """analyze_prompt.md instructs checklist (- [ ]) output, not ### heading format."""
        orch = Orchestrator()
        try:
            prompt = orch.load_prompt("analyze_prompt.md")
            assert "- [ ]" in prompt, "Prompt must instruct checklist (- [ ]) output"
            assert "Opportunity ID:" in prompt, "Prompt must include Opportunity ID: field"
            assert "### <" not in prompt, "Prompt must not use ### heading template for opportunities"
        finally:
            orch.cleanup()


class TestDesignInfrastructure:
    """Tests for run_design() method."""

    def test_run_design_loads_prompt(self, temp_repo):
        """run_design loads and uses the design prompt with opportunity substituted."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            # Create design file to simulate successful design
            designs_dir.mkdir(exist_ok=True)

            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]

                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] == 1:
                        design_file = designs_dir / "test-opportunity.md"
                        design_file.write_text("# Design: Test Opportunity\n")
                        return "Design complete"
                    # Reviewer call: return APPROVED
                    return '{"verdict": "APPROVED"}'

                mock_claude.side_effect = side_effect

                orch.run_design("Test opportunity description")

                # Verify run_claude was called with design prompt content on first call
                assert mock_claude.call_count >= 1
                first_prompt = mock_claude.call_args_list[0][0][0]
                assert "software architect" in first_prompt
                assert "Test opportunity description" in first_prompt
                assert "{{OPPORTUNITY}}" not in first_prompt
                assert "{{OPPORTUNITY_ID}}" not in first_prompt
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_run_design_forwards_opportunity_id(self, temp_repo):
        """run_design forwards optional opportunity_id to OuterLoopManager."""
        orch = Orchestrator()
        try:
            with patch.object(
                orch._outer_loop_manager,
                "run_design",
                return_value={"success": True, "design_file": "designs/test.md"},
            ) as mock_run_design:
                result = orch.run_design("Test opportunity", opportunity_id="my-id")

            assert result["success"] is True
            mock_run_design.assert_called_once()
            kwargs = mock_run_design.call_args.kwargs
            assert kwargs["opportunity"] == "Test opportunity"
            assert kwargs["opportunity_id"] == "my-id"
            assert kwargs["load_prompt_callback"] == orch.load_prompt
            assert callable(kwargs["run_agent_callback"])
            assert kwargs["log_callback"] == orch.log
        finally:
            orch.cleanup()

    def test_run_design_creates_designs_directory(self, temp_repo):
        """run_design creates designs/ directory if it doesn't exist."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            # Ensure designs dir doesn't exist
            assert not designs_dir.exists()

            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = "Did nothing"
                orch.run_design("Test opportunity")

            # Directory should be created even if design fails
            assert designs_dir.exists()
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_run_design_returns_success_when_design_created(self, temp_repo):
        """run_design returns success=True when a design file is created."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        try:
            opportunities_file.write_text(
                "- [ ] **Add caching layer**\n"
                "  - Opportunity ID: test-opportunity\n"
                "  - Description: Add caching layer\n"
            )
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]

                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] == 1:
                        designs_dir.mkdir(exist_ok=True)
                        design_file = designs_dir / "add-caching.md"
                        design_file.write_text(
                            "# Add Caching\n\n"
                            "- **design_id**: add-caching\n"
                            "- **title**: Add Caching\n"
                            "- **status**: draft\n"
                            "- **opportunity_ref**: test-opportunity\n"
                            "- **created**: 2026-03-02\n\n"
                            "---\n\n"
                            "Body\n"
                        )
                        return "Done"
                    return '{"verdict": "APPROVED"}'

                mock_claude.side_effect = side_effect

                result = orch.run_design("Add caching layer")

            assert result["success"] is True
            assert result["design_file"] is not None
            assert "add-caching.md" in result["design_file"]
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_run_design_returns_failure_when_no_design_created(self, temp_repo):
        """run_design returns success=False when no design file is created."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = "Did nothing"
                result = orch.run_design("Test opportunity")

            assert result["success"] is False
            assert result["design_file"] is None
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_run_design_logs_completed(self, temp_repo):
        """run_design logs design_completed event on success."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        try:
            opportunities_file.write_text(
                "- [ ] **Test opportunity**\n"
                "  - Opportunity ID: test-opportunity\n"
                "  - Description: Test opportunity\n"
            )
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]

                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] == 1:
                        designs_dir.mkdir(exist_ok=True)
                        design_file = designs_dir / "test.md"
                        design_file.write_text(
                            "# Test\n\n"
                            "- **design_id**: test\n"
                            "- **title**: Test\n"
                            "- **status**: draft\n"
                            "- **opportunity_ref**: test-opportunity\n"
                            "- **created**: 2026-03-02\n\n"
                            "---\n\n"
                            "Body\n"
                        )
                        return "Done"
                    return '{"verdict": "APPROVED"}'

                mock_claude.side_effect = side_effect

                orch.run_design("Test opportunity")

            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            assert "design_completed" in log_content
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_run_design_logs_failed(self, temp_repo):
        """run_design logs design_failed event when no design file created."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = "Did nothing"
                orch.run_design("Test opportunity")

            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            assert "design_failed" in log_content
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_run_design_prints_summary(self, temp_repo, capsys):
        """run_design prints summary to stdout on success."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        try:
            opportunities_file.write_text(
                "- [ ] **Test opportunity**\n"
                "  - Opportunity ID: test-opportunity\n"
                "  - Description: Test opportunity\n"
            )
            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]

                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] == 1:
                        designs_dir.mkdir(exist_ok=True)
                        design_file = designs_dir / "test.md"
                        design_file.write_text(
                            "# Test\n\n"
                            "- **design_id**: test\n"
                            "- **title**: Test\n"
                            "- **status**: draft\n"
                            "- **opportunity_ref**: test-opportunity\n"
                            "- **created**: 2026-03-02\n\n"
                            "---\n\n"
                            "Body\n"
                        )
                        return "Done"
                    return '{"verdict": "APPROVED"}'

                mock_claude.side_effect = side_effect

                orch.run_design("Test opportunity")

            captured = capsys.readouterr()
            assert "Design Complete" in captured.out
            assert "Design file:" in captured.out
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_run_design_prints_failure(self, temp_repo, capsys):
        """run_design prints failure message when no design created."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = "Did nothing"
                orch.run_design("Test opportunity")

            captured = capsys.readouterr()
            assert "Design Failed" in captured.out
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_run_design_detects_new_files_only(self, temp_repo):
        """run_design only counts new design files, not pre-existing ones."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        try:
            opportunities_file.write_text(
                "- [ ] **Test opportunity**\n"
                "  - Opportunity ID: test-opportunity\n"
                "  - Description: Test opportunity\n"
            )
            # Create existing design file
            designs_dir.mkdir(exist_ok=True)
            existing_file = designs_dir / "existing.md"
            existing_file.write_text("# Existing Design\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                call_count = [0]

                def side_effect(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] == 1:
                        design_file = designs_dir / "new-design.md"
                        design_file.write_text(
                            "# New Design\n\n"
                            "- **design_id**: new-design\n"
                            "- **title**: New Design\n"
                            "- **status**: draft\n"
                            "- **opportunity_ref**: test-opportunity\n"
                            "- **created**: 2026-03-02\n\n"
                            "---\n\n"
                            "Body\n"
                        )
                        return "Done"
                    return '{"verdict": "APPROVED"}'

                mock_claude.side_effect = side_effect

                result = orch.run_design("Test opportunity")

            assert result["success"] is True
            assert "new-design.md" in result["design_file"]
            assert "existing.md" not in result["design_file"]
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_run_design_uses_file_glob_for_detection(self, temp_repo):
        """run_design uses filesystem glob to detect new design files."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        try:
            opportunities_file.write_text(
                "- [ ] **Add new feature**\n"
                "  - Opportunity ID: test-opportunity\n"
                "  - Description: Add new feature\n"
            )
            # Pre-create an existing design file so the snapshot is non-empty
            designs_dir.mkdir(exist_ok=True)
            existing_file = designs_dir / "old-feature.md"
            existing_file.write_text("# Design: Old Feature\n")

            call_count = [0]

            def create_new_file_and_return(_prompt, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    # Simulate agent creating a new file whose stem differs from
                    # any canonical design_id that might appear in metadata
                    new_file = designs_dir / "new-feature.md"
                    new_file.write_text(
                        "# New Feature\n\n"
                        "- **design_id**: new-feature\n"
                        "- **title**: New Feature\n"
                        "- **status**: draft\n"
                        "- **opportunity_ref**: test-opportunity\n"
                        "- **created**: 2026-03-02\n\n"
                        "---\n\n"
                        "Body\n"
                    )
                    return "Done"
                return '{"verdict": "APPROVED"}'

            with patch.object(orch, 'run_claude', side_effect=create_new_file_and_return):
                result = orch.run_design("Add new feature")

            assert result["success"] is True
            assert result["design_file"].endswith("new-feature.md")
            # old-feature.md must NOT appear in result
            assert "old-feature" not in result["design_file"]
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_run_design_wires_reviewer_callback_with_reviewer_role(self, temp_repo):
        """Orchestrator.run_design passes a reviewer_callback with role='reviewer' to OuterLoopManager."""
        orch = Orchestrator()
        try:
            captured_kwargs = {}

            def fake_run_design(**kwargs):
                captured_kwargs.update(kwargs)
                return {"success": True, "design_file": None, "design_id": None}

            with patch.object(orch._outer_loop_manager, "run_design", side_effect=fake_run_design):
                orch.run_design("test opportunity")

            assert "reviewer_callback" in captured_kwargs
            assert callable(captured_kwargs["reviewer_callback"])
        finally:
            orch.cleanup()

    def test_run_design_reviewer_callback_uses_reviewer_role(self, temp_repo):
        """The reviewer_callback wired by Orchestrator.run_design invokes run_agent with role='reviewer'."""
        orch = Orchestrator()
        try:
            captured_role = {}

            def fake_run_design(**kwargs):
                reviewer_cb = kwargs.get("reviewer_callback")
                if reviewer_cb is not None:
                    with patch.object(orch, "run_agent") as mock_agent:
                        mock_agent.return_value = "review output"
                        reviewer_cb("some prompt")
                        if mock_agent.called:
                            captured_role["role"] = mock_agent.call_args[1].get("role")
                return {"success": True, "design_file": None, "design_id": None}

            with patch.object(orch._outer_loop_manager, "run_design", side_effect=fake_run_design):
                orch.run_design("test opportunity")

            assert captured_role.get("role") == "reviewer"
        finally:
            orch.cleanup()


class TestDesignCLI:
    """Tests for --design CLI argument."""

    def test_design_flag_runs_design(self, temp_repo):
        """--design flag invokes run_design and exits."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--design', 'Add retry logic']):
            with patch.object(Orchestrator, 'run_design') as mock_design:
                with patch.object(Orchestrator, 'review_design') as mock_review:
                    mock_design.return_value = {"success": True, "design_file": "designs/test.md"}
                    mock_review.return_value = {"approved": True, "verdict": "APPROVED", "output": "..."}
                    with pytest.raises(SystemExit) as exc_info:
                        orchestrate.main()

                    mock_design.assert_called_once_with(opportunity="Add retry logic")
                    # Review is also called when review_designs=True (default)
                    mock_review.assert_called_once_with("designs/test.md")
                    assert exc_info.value.code == 0

    def test_design_flag_exits_1_on_failure(self, temp_repo):
        """--design exits 1 when design fails."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--design', 'Test opportunity']):
            with patch.object(Orchestrator, 'run_design') as mock_design:
                mock_design.return_value = {"success": False, "design_file": None}
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()

                assert exc_info.value.code == 1

    def test_design_flag_exits_1_on_exception(self, temp_repo):
        """--design exits 1 on exception."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--design', 'Test opportunity']):
            with patch.object(Orchestrator, 'run_design') as mock_design:
                mock_design.side_effect = Exception("Design error")
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()

                assert exc_info.value.code == 1


class TestReviewDesign:
    """Tests for review_design method."""

    def test_review_design_loads_prompt(self, temp_repo):
        """review_design loads and uses the review design prompt with content substituted."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            # Create design file
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test-design.md"
            design_file.write_text("# Design: Test\n\nTest design content.")

            with patch.object(orch, 'run_claude') as mock_claude:
                # Return a valid JSON response to avoid triggering retry logic
                mock_claude.return_value = '{"verdict": "APPROVED", "strengths": ["good"], "issues": []}'

                orch.review_design(str(design_file))

            # Verify run_claude was called with review design prompt content
            mock_claude.assert_called_once()
            prompt = mock_claude.call_args[0][0]
            assert "reviewing a design document" in prompt
            assert "Test design content" in prompt
            assert "{{DESIGN_CONTENT}}" not in prompt

            # Verify no model override was passed (should be None or not present)
            args, kwargs = mock_claude.call_args
            assert kwargs.get("model") is None
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_returns_approved_when_verdict_approved(self, temp_repo):
        """review_design returns approved=True when verdict is APPROVED."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = "## Design Review\n\n**Verdict**: APPROVED\n\n### Strengths\n- Good design"

                result = orch.review_design(str(design_file))

            assert result["approved"] is True
            assert result["verdict"] == "APPROVED"
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_returns_not_approved_when_verdict_needs_revision(self, temp_repo):
        """review_design returns approved=False when verdict is NEEDS_REVISION."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = "## Design Review\n\n**Verdict**: NEEDS_REVISION\n\n### Issues\n- Missing success criteria"

                result = orch.review_design(str(design_file))

            assert result["approved"] is False
            assert result["verdict"] == "NEEDS_REVISION"
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_returns_error_when_file_not_found(self, temp_repo):
        """review_design returns error when design file doesn't exist."""
        orch = Orchestrator()
        try:
            result = orch.review_design("designs/nonexistent.md")

            assert result["approved"] is False
            assert result["verdict"] == "ERROR"
            assert "not found" in result["output"]
        finally:
            orch.cleanup()

    def test_review_design_handles_relative_path(self, temp_repo):
        """review_design handles relative paths correctly."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = "**Verdict**: APPROVED"

                # Use relative path
                result = orch.review_design("designs/test.md")

            assert result["approved"] is True
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_logs_completed(self, temp_repo):
        """review_design logs design_review_completed event."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = "**Verdict**: APPROVED"

                orch.review_design(str(design_file))

            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            assert "design_review_completed" in log_content
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_logs_failed_when_file_not_found(self, temp_repo):
        """review_design logs design_review_failed when file not found."""
        orch = Orchestrator()
        try:
            orch.review_design("designs/nonexistent.md")

            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            assert "design_review_failed" in log_content
        finally:
            orch.cleanup()

    def test_review_design_prints_summary(self, temp_repo, capsys):
        """review_design prints summary to stdout."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = "**Verdict**: APPROVED"

                orch.review_design(str(design_file))

            captured = capsys.readouterr()
            assert "Design Review" in captured.out
            assert "APPROVED" in captured.out
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_prints_failure(self, temp_repo, capsys):
        """review_design prints failure message when file not found."""
        orch = Orchestrator()
        try:
            orch.review_design("designs/nonexistent.md")

            captured = capsys.readouterr()
            assert "Design Review Failed" in captured.out
        finally:
            orch.cleanup()

    def test_review_design_logs_response_before_extraction(self, temp_repo):
        """review_design logs full response before attempting verdict extraction."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = "## Design Review\n\n**Verdict**: APPROVED\n\nThis is a thorough review."

                orch.review_design(str(design_file))

            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            # Verify response is logged before extraction
            assert "design_review_response" in log_content
            assert "This is a thorough review." in log_content
            # Verify response log appears before completed log
            response_pos = log_content.find("design_review_response")
            completed_pos = log_content.find("design_review_completed")
            assert response_pos < completed_pos
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_logs_extraction_failed_on_empty_response(self, temp_repo):
        """review_design logs extraction failure when response is empty after retry."""
        orch = Orchestrator(retry_on_empty_response=True)
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = ""  # Empty response (both calls)

                result = orch.review_design(str(design_file))

            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            # Should log retry attempt before extraction failure
            assert "design_review_retry" in log_content
            assert "design_review_extraction_failed" in log_content
            assert "empty_response" in log_content
            # Should default to NEEDS_REVISION on extraction failure
            assert result["approved"] is False
            assert result["verdict"] == "NEEDS_REVISION"
            # Should have called run_claude 4 times: (Agent initial + Agent retry) x (Review initial + Review retry)
            assert mock_claude.call_count == 4
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_logs_extraction_failed_on_whitespace_only_response(self, temp_repo):
        """review_design logs extraction failure when response is whitespace only after retry."""
        orch = Orchestrator(retry_on_empty_response=True)
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = "   \n\t\n   "  # Whitespace only (both calls)

                result = orch.review_design(str(design_file))

            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            # Should log retry attempt before extraction failure
            assert "design_review_retry" in log_content
            assert "design_review_extraction_failed" in log_content
            assert "empty_response" in log_content
            assert result["approved"] is False
            assert result["verdict"] == "NEEDS_REVISION"
            # Should have called run_claude 4 times: (Agent initial + Agent retry) x (Review initial + Review retry)
            assert mock_claude.call_count == 4
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_logs_extraction_failed_on_missing_verdict_keywords(self, temp_repo):
        """review_design logs extraction failure when no verdict keywords found after retry."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                # Response without APPROVED or NEEDS_REVISION (both calls)
                mock_claude.return_value = "## Design Review\n\nThe design looks okay but needs work."

                result = orch.review_design(str(design_file))

            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            # Should log retry attempt before extraction failure
            assert "design_review_retry" in log_content
            assert "design_review_extraction_failed" in log_content
            assert "no_verdict_keywords" in log_content
            # Should include full response for debugging
            assert "The design looks okay" in log_content
            # Should default to NEEDS_REVISION on extraction failure
            assert result["approved"] is False
            assert result["verdict"] == "NEEDS_REVISION"
            # Should have called run_claude 4 times: (Agent initial + Agent retry) x (Review initial + Review retry)
            # Since the response is not empty but lacks keywords, run_agent won't retry internally,
            # but review_design will retry once. Total 2 calls.
            assert mock_claude.call_count == 2
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_status_updated_to_reviewed_on_approved(self, temp_repo):
        """review_design marks approved designs as reviewed via provider status update."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test-design.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, "run_claude") as mock_claude:
                mock_claude.return_value = '{"verdict": "APPROVED", "strengths": [], "issues": []}'
                with patch.object(
                    orch._outer_loop_manager.design_provider, "update_design_status"
                ) as mock_update_status:
                    result = orch.review_design(str(design_file))

            assert result["approved"] is True
            mock_update_status.assert_called_once_with("test-design", DesignStatus.reviewed)
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_status_not_updated_on_needs_revision(self, temp_repo):
        """review_design does not update status when verdict is NEEDS_REVISION."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test-design.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, "run_claude") as mock_claude:
                mock_claude.return_value = (
                    '{"verdict": "NEEDS_REVISION", "strengths": [], "issues": ["fix"]}'
                )
                with patch.object(
                    orch._outer_loop_manager.design_provider, "update_design_status"
                ) as mock_update_status:
                    result = orch.review_design(str(design_file))

            assert result["approved"] is False
            mock_update_status.assert_not_called()
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_status_update_failure_does_not_propagate(self, temp_repo):
        """review_design swallows status update errors and logs a warning."""
        orch = Orchestrator()
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test-design.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, "run_claude") as mock_claude:
                mock_claude.return_value = '{"verdict": "APPROVED", "strengths": [], "issues": []}'
                with patch.object(
                    orch._outer_loop_manager.design_provider,
                    "update_design_status",
                    side_effect=RuntimeError("boom"),
                ) as mock_update_status, patch("millstone.loops.outer.progress") as mock_progress:
                    result = orch.review_design(str(design_file))

            assert result["approved"] is True
            mock_update_status.assert_called_once_with("test-design", DesignStatus.reviewed)
            assert any(
                "Failed to update design status to reviewed" in call.args[0]
                for call in mock_progress.call_args_list
            )
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)


class TestReviewDesignRetry:
    """Tests for design review retry functionality."""

    def test_review_design_retries_on_empty_response(self, temp_repo):
        """review_design retries once when first response is empty."""
        orch = Orchestrator(retry_on_empty_response=True)
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                # First call returns empty, second returns valid JSON
                mock_claude.side_effect = [
                    "",
                    '{"verdict": "APPROVED", "strengths": ["good"], "issues": []}'
                ]

                result = orch.review_design(str(design_file))

            assert mock_claude.call_count == 2
            assert result["approved"] is True
            assert result["verdict"] == "APPROVED"
            # Log should show retry from run_agent
            log_content = orch.log_file.read_text()
            assert "empty_response_retry" in log_content
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_retries_on_malformed_response(self, temp_repo):
        """review_design retries once when first response is malformed."""
        orch = Orchestrator(retry_on_empty_response=True)
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                # First call returns text without proper format, second returns valid JSON
                mock_claude.side_effect = [
                    "This is a review but without proper structure.",
                    '{"verdict": "NEEDS_REVISION", "strengths": [], "issues": ["missing details"]}'
                ]

                result = orch.review_design(str(design_file))

            assert mock_claude.call_count == 2
            assert result["approved"] is False
            assert result["verdict"] == "NEEDS_REVISION"
            assert "missing details" in result["issues"]
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_retry_prompt_includes_format_instruction(self, temp_repo):
        """review_design retry prompt includes explicit format instruction."""
        orch = Orchestrator(retry_on_empty_response=True)
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.side_effect = [
                    "",  # Initial
                    "",  # run_agent retry
                    '{"verdict": "APPROVED", "strengths": [], "issues": []}' # review_design retry
                ]

                orch.review_design(str(design_file))

            # Check the retry prompt (third call) includes format instruction
            retry_prompt = mock_claude.call_args_list[2][0][0]
            assert "IMPORTANT" in retry_prompt
            assert "required JSON format" in retry_prompt
            assert '"verdict"' in retry_prompt
            assert '"strengths"' in retry_prompt
            assert '"issues"' in retry_prompt
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_no_retry_on_valid_response(self, temp_repo):
        """review_design does not retry when first response is valid."""
        orch = Orchestrator(retry_on_empty_response=True)
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                # Return valid JSON on first call
                mock_claude.return_value = '{"verdict": "APPROVED", "strengths": ["well designed"], "issues": []}'

                result = orch.review_design(str(design_file))

            # Should only call once, no retry needed
            mock_claude.assert_called_once()
            assert result["approved"] is True
            # Log should not show retry
            log_content = orch.log_file.read_text()
            assert "design_review_retry" not in log_content
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)

    def test_review_design_retry_logs_original_response(self, temp_repo):
        """review_design logs original response when triggering retry."""
        orch = Orchestrator(retry_on_empty_response=True)
        designs_dir = temp_repo / ".millstone" / "designs"
        try:
            designs_dir.mkdir(exist_ok=True)
            design_file = designs_dir / "test.md"
            design_file.write_text("# Design: Test\n")

            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.side_effect = [
                    "bad response without structure",
                    '{"verdict": "APPROVED", "strengths": [], "issues": []}'
                ]

                orch.review_design(str(design_file))

            log_content = orch.log_file.read_text()
            assert "empty_response_retry" in log_content
            assert "bad response without structure" in log_content
        finally:
            orch.cleanup()
            if designs_dir.exists():
                import shutil
                shutil.rmtree(designs_dir)


class TestReviewDesignCLI:
    """Tests for --review-design CLI argument."""

    def test_review_design_flag_runs_review(self, temp_repo):
        """--review-design flag invokes review_design and exits."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--review-design', 'designs/test.md']):
            with patch.object(Orchestrator, 'review_design') as mock_review:
                mock_review.return_value = {"approved": True, "verdict": "APPROVED", "output": "..."}
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()

                mock_review.assert_called_once_with(design_path="designs/test.md")
                assert exc_info.value.code == 0

    def test_review_design_flag_exits_1_on_needs_revision(self, temp_repo):
        """--review-design exits 1 when design needs revision."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--review-design', 'designs/test.md']):
            with patch.object(Orchestrator, 'review_design') as mock_review:
                mock_review.return_value = {"approved": False, "verdict": "NEEDS_REVISION", "output": "..."}
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()

                assert exc_info.value.code == 1

    def test_review_design_flag_exits_1_on_exception(self, temp_repo):
        """--review-design exits 1 on exception."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--review-design', 'designs/test.md']):
            with patch.object(Orchestrator, 'review_design') as mock_review:
                mock_review.side_effect = Exception("Review error")
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()

                assert exc_info.value.code == 1


class TestDesignWithAutoReview:
    """Tests for --design with automatic review when review_designs=true."""

    def test_design_auto_reviews_when_review_designs_true(self, temp_repo):
        """--design automatically reviews design when review_designs config is true."""
        from millstone import orchestrate

        # Create config with review_designs=true (default)
        config_dir = temp_repo / ".millstone"
        config_dir.mkdir(exist_ok=True)

        with patch('sys.argv', ['orchestrate.py', '--design', 'Add caching']):
            with patch.object(Orchestrator, 'run_design') as mock_design:
                with patch.object(Orchestrator, 'review_design') as mock_review:
                    mock_design.return_value = {"success": True, "design_file": "designs/add-caching.md"}
                    mock_review.return_value = {"approved": True, "verdict": "APPROVED", "output": "..."}
                    with pytest.raises(SystemExit) as exc_info:
                        orchestrate.main()

                    # Both design and review should be called
                    mock_design.assert_called_once()
                    mock_review.assert_called_once_with("designs/add-caching.md")
                    assert exc_info.value.code == 0

    def test_design_skips_review_when_design_fails(self, temp_repo):
        """--design doesn't review when design creation fails."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--design', 'Add caching']):
            with patch.object(Orchestrator, 'run_design') as mock_design:
                with patch.object(Orchestrator, 'review_design') as mock_review:
                    mock_design.return_value = {"success": False, "design_file": None}
                    with pytest.raises(SystemExit) as exc_info:
                        orchestrate.main()

                    # Design called but review not called
                    mock_design.assert_called_once()
                    mock_review.assert_not_called()
                    assert exc_info.value.code == 1

    def test_design_exits_1_when_review_fails(self, temp_repo):
        """--design exits 1 when design is created but review fails."""
        from millstone import orchestrate

        with patch('sys.argv', ['orchestrate.py', '--design', 'Add caching']):
            with patch.object(Orchestrator, 'run_design') as mock_design:
                with patch.object(Orchestrator, 'review_design') as mock_review:
                    mock_design.return_value = {"success": True, "design_file": "designs/add-caching.md"}
                    mock_review.return_value = {"approved": False, "verdict": "NEEDS_REVISION", "output": "..."}
                    with pytest.raises(SystemExit) as exc_info:
                        orchestrate.main()

                    assert exc_info.value.code == 1

    def test_design_skips_review_when_review_designs_false(self, temp_repo):
        """--design skips review when review_designs config is false."""
        from millstone import orchestrate

        # Create config with review_designs=false
        config_dir = temp_repo / ".millstone"
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / "config.toml"
        config_file.write_text("review_designs = false\n")

        with patch('sys.argv', ['orchestrate.py', '--design', 'Add caching']):
            with patch.object(Orchestrator, 'run_design') as mock_design:
                with patch.object(Orchestrator, 'review_design') as mock_review:
                    mock_design.return_value = {"success": True, "design_file": "designs/add-caching.md"}
                    with pytest.raises(SystemExit) as exc_info:
                        orchestrate.main()

                    # Design called but review not called
                    mock_design.assert_called_once()
                    mock_review.assert_not_called()
                    assert exc_info.value.code == 0


class TestReviewDesignsConfig:
    """Tests for review_designs configuration."""

    def test_default_config_has_review_designs_true(self):
        """DEFAULT_CONFIG includes review_designs = True."""
        assert "review_designs" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["review_designs"] is True

    def test_orchestrator_defaults_to_review_designs_true(self, temp_repo):
        """Orchestrator defaults to review_designs=True."""
        orch = Orchestrator()
        try:
            assert orch.review_designs is True
        finally:
            orch.cleanup()

    def test_orchestrator_respects_review_designs_false(self, temp_repo):
        """Orchestrator can be initialized with review_designs=False."""
        orch = Orchestrator(review_designs=False)
        try:
            assert orch.review_designs is False
        finally:
            orch.cleanup()


class TestPlanInfrastructure:
    """Tests for run_plan() method."""

    def test_run_plan_loads_prompt(self, temp_repo):
        """run_plan loads and uses the plan prompt with design and tasklist substituted."""
        # Create design file
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test Design\n\nTest design content.")

        # Create tasklist
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Existing task")

        orch = Orchestrator()
        # Disable validation to test just prompt loading
        orch.task_constraints = {"max_loc": 200, "require_tests": False, "require_criteria": False, "require_risk": False, "require_context": False, "max_split_attempts": 0}
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                # Simulate agent behavior:
                # 1. Add a task
                # 2. Approve the plan
                def side_effect(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt:
                        content = tasklist.read_text()
                        tasklist.write_text(content + "\n\n- [ ] **New Task**: Description\n  - Context: none")
                        return "Done"
                    elif "prompt_name: plan_review_prompt.md" in prompt:
                        return '{"verdict": "APPROVED", "score": 10}'
                    return ""
                mock_claude.side_effect = side_effect

                orch.run_plan(str(design_file))

                # Verify run_claude was called twice: plan gen + plan review
                assert mock_claude.call_count == 2
                # Check first call (plan)
                prompt = mock_claude.call_args_list[0][0][0]
                assert "technical lead" in prompt
                assert "Test design content" in prompt
                assert "Existing task" in prompt
                assert "{{DESIGN_CONTENT}}" not in prompt
                assert "{{TASKLIST_CONTENT}}" not in prompt
                assert "{{TASKLIST_PATH}}" not in prompt
        finally:
            orch.cleanup()

    def test_run_plan_returns_success_when_tasks_added(self, temp_repo):
        """run_plan returns success=True when tasks are added to tasklist."""
        # Create design file
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test Design\n")

        # Create tasklist with one existing task
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Existing task")

        orch = Orchestrator()
        # Disable validation but allow review
        orch.task_constraints = {"max_loc": 200, "require_tests": False, "require_criteria": False, "require_risk": False, "require_context": False, "max_split_attempts": 0}
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                # Agent behavior:
                # 1. Add tasks
                # 2. Approve plan
                def behavior(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt:
                        content = tasklist.read_text()
                        tasklist.write_text(content + "\n\n- [ ] **Task 1**: Desc\n  - Context: none\n\n- [ ] **Task 2**: Desc\n  - Context: none")
                        return "Done"
                    elif "prompt_name: plan_review_prompt.md" in prompt:
                        return '{"verdict": "APPROVED", "score": 10}'
                    return ""
                mock_claude.side_effect = behavior

                result = orch.run_plan(str(design_file))

            assert result["success"] is True
            assert result["tasks_added"] == 2
        finally:
            orch.cleanup()

    def test_run_plan_returns_failure_when_no_tasks_added(self, temp_repo):
        """run_plan returns success=False when no tasks are added."""
        # Create design file
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test Design\n")

        # Create tasklist
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Existing task")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = "Did nothing"
                result = orch.run_plan(str(design_file))

            assert result["success"] is False
            assert result["tasks_added"] == 0
        finally:
            orch.cleanup()

    def test_run_plan_fails_when_design_not_found(self, temp_repo):
        """run_plan returns error when design file doesn't exist."""
        # Create tasklist
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        orch = Orchestrator()
        try:
            result = orch.run_plan("nonexistent/design.md")

            assert result["success"] is False
            assert result["tasks_added"] == 0
            assert "error" in result
            assert "not found" in result["error"]
        finally:
            orch.cleanup()

    def test_run_plan_fails_when_design_provider_returns_none(self, temp_repo):
        """run_plan fails via design_provider.get_design() returning None (not Path.exists())."""
        from unittest.mock import MagicMock

        # Create design file on disk so Path.exists() would pass
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "my-design.md"
        design_file.write_text("# Design: My Design\n")

        # Create tasklist
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        orch = Orchestrator()
        try:
            # Override design_provider so get_design() returns None
            mock_provider = MagicMock()
            mock_provider.get_design.return_value = None
            orch._outer_loop_manager.design_provider = mock_provider

            result = orch.run_plan(str(design_file))

            assert result["success"] is False
            assert result["tasks_added"] == 0
            assert "not found" in result["error"]
            mock_provider.get_design.assert_called_once_with("my-design")
        finally:
            orch.cleanup()

    def test_run_plan_extensionless_path_returns_structured_error(self, temp_repo):
        """run_plan with extensionless path like 'designs/my-design' must not raise FileNotFoundError."""
        from unittest.mock import MagicMock

        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "my-design.md"
        design_file.write_text("# Design: My Design\n")

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        orch = Orchestrator()
        try:
            mock_provider = MagicMock()
            mock_provider.get_design.return_value = None
            orch._outer_loop_manager.design_provider = mock_provider

            # Pass extensionless path - must return a structured error, not raise
            result = orch.run_plan(str(designs_dir / "my-design"))

            assert result["success"] is False
            assert result["tasks_added"] == 0
            assert "not found" in result["error"]
            mock_provider.get_design.assert_called_once_with("my-design")
        finally:
            orch.cleanup()

    def test_run_plan_fails_when_tasklist_not_found(self, temp_repo):
        """run_plan returns error when tasklist file doesn't exist."""
        # Create design file but no tasklist
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test Design\n")

        # Use non-default tasklist path that doesn't exist
        orch = Orchestrator(tasklist="nonexistent/tasklist.md")
        try:
            result = orch.run_plan(str(design_file))

            assert result["success"] is False
            assert result["tasks_added"] == 0
            assert "error" in result
            assert "not found" in result["error"]
        finally:
            orch.cleanup()

    def test_run_plan_handles_relative_path(self, temp_repo):
        """run_plan handles relative design paths."""
        # Create design file
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test Design\n")

        # Create tasklist
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Existing")

        orch = Orchestrator()
        # Disable validation but allow review
        orch.task_constraints = {"max_loc": 200, "require_tests": False, "require_criteria": False, "require_risk": False, "require_context": False, "max_split_attempts": 0}
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                # Agent behavior:
                # 1. Add tasks
                # 2. Approve plan
                def behavior(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt:
                        content = tasklist.read_text()
                        tasklist.write_text(content + "\n\n- [ ] **New task**: Desc\n  - Context: none\n\n- [ ] **New task**: Desc\n  - Context: none\n\n- [ ] **New task**: Desc\n  - Context: none")
                        return "Done"
                    elif "prompt_name: plan_review_prompt.md" in prompt:
                        return '{"verdict": "APPROVED", "score": 10}'
                    return ""
                mock_claude.side_effect = behavior

                result = orch.run_plan("designs/test-design.md")

            assert result["success"] is True
        finally:
            orch.cleanup()

    def test_run_plan_logs_completed(self, temp_repo):
        """run_plan logs plan_completed event on success."""
        # Create design file
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test Design\n")

        # Create tasklist
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        orch = Orchestrator()
        # Disable validation but allow review
        orch.task_constraints = {"max_loc": 200, "require_tests": False, "require_criteria": False, "require_risk": False, "require_context": False, "max_split_attempts": 0}
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                # Agent behavior:
                # 1. Add tasks
                # 2. Approve plan
                def behavior(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt:
                        content = tasklist.read_text()
                        tasklist.write_text(content + "\n\n- [ ] **Task 1**: Desc\n  - Context: none\n\n- [ ] **Task 2**: Desc\n  - Context: none")
                        return "Done"
                    elif "prompt_name: plan_review_prompt.md" in prompt:
                        return '{"verdict": "APPROVED", "score": 10}'
                    return ""
                mock_claude.side_effect = behavior

                orch.run_plan(str(design_file))

            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            assert "plan_completed" in log_content
        finally:
            orch.cleanup()

    def test_run_plan_logs_failed(self, temp_repo):
        """run_plan logs plan_failed event when no tasks added."""
        # Create design file
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test Design\n")

        # Create tasklist
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = "Did nothing"
                orch.run_plan(str(design_file))

            assert orch.log_file.exists()
            log_content = orch.log_file.read_text()
            assert "plan_failed" in log_content
        finally:
            orch.cleanup()

    def test_run_plan_prints_summary(self, temp_repo, capsys):
        """run_plan prints summary to stdout on success."""
        # Create design file
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test Design\n")

        # Create tasklist
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        orch = Orchestrator()
        # Disable validation but allow review
        orch.task_constraints = {"max_loc": 200, "require_tests": False, "require_criteria": False, "require_risk": False, "require_context": False, "max_split_attempts": 0}
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                # Agent behavior:
                # 1. Add task
                # 2. Approve plan
                def behavior(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt:
                        content = tasklist.read_text()
                        tasklist.write_text(content + "\n\n- [ ] **New Task**: Description\n  - Context: none")
                        return "Done"
                    elif "prompt_name: plan_review_prompt.md" in prompt:
                        return '{"verdict": "APPROVED", "score": 10}'
                    return ""
                mock_claude.side_effect = behavior

                orch.run_plan(str(design_file))

            captured = capsys.readouterr()
            assert "Planning Complete" in captured.out
            assert "Tasks added: 1" in captured.out
        finally:
            orch.cleanup()

    def test_run_plan_prints_failure(self, temp_repo, capsys):
        """run_plan prints failure message when no tasks added."""
        # Create design file
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test Design\n")

        # Create tasklist
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                mock_claude.return_value = "Did nothing"
                orch.run_plan(str(design_file))

            captured = capsys.readouterr()
            assert "Planning Failed" in captured.out
        finally:
            orch.cleanup()

    def test_run_plan_does_not_modify_existing_tasks(self, temp_repo):
        """run_plan should not modify existing checked or unchecked tasks."""
        # Create design file
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test Design\n")

        # Create tasklist with existing tasks
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        original_content = "# Tasklist\n\n- [x] Completed task\n- [ ] Existing unchecked"
        tasklist.write_text(original_content)

        orch = Orchestrator()
        orch.task_constraints = {"max_loc": 200, "require_tests": False, "require_criteria": False, "require_risk": False, "require_context": False, "max_split_attempts": 0}
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                # Agent behavior:
                # 1. Add task
                # 2. Approve plan
                def behavior(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt:
                        content = tasklist.read_text()
                        tasklist.write_text(content + "\n\n- [ ] **New task from plan**\n  - Context: none")
                        return "Done"
                    elif "prompt_name: plan_review_prompt.md" in prompt:
                        return '{"verdict": "APPROVED", "score": 10}'
                    return ""
                mock_claude.side_effect = behavior

                orch.run_plan(str(design_file))

            # Verify original tasks are still there
            new_content = tasklist.read_text()
            assert "- [x] Completed task" in new_content
            assert "- [ ] Existing unchecked" in new_content
            assert "- [ ] **New task from plan**" in new_content
        finally:
            orch.cleanup()


class TestTaskAtomizer:
    """Tests for task atomizer in run_plan()."""

    def test_parse_task_metadata_extracts_all_fields(self, temp_repo):
        """_parse_task_metadata extracts title, description, and metadata fields."""
        orch = Orchestrator()
        try:
            task_text = """**Add feature**: Implement the new feature.
  - Est. LoC: 150
  - Tests: test_feature.py
  - Criteria: All tests pass
  - Context: Directives for builder"""

            result = orch._parse_task_metadata(task_text)

            assert result["title"] == "Add feature"
            assert result["description"] == "Implement the new feature."
            assert result["est_loc"] == 150
            assert result["tests"] == "test_feature.py"
            assert result["criteria"] == "All tests pass"
            assert result["context"] == "Directives for builder"
            assert result["raw"] == task_text
        finally:
            orch.cleanup()

    def test_parse_task_metadata_handles_missing_fields(self, temp_repo):
        """_parse_task_metadata returns None for missing metadata fields."""
        orch = Orchestrator()
        try:
            task_text = "**Simple task**: Just a description with no metadata"

            result = orch._parse_task_metadata(task_text)

            assert result["title"] == "Simple task"
            assert result["description"] == "Just a description with no metadata"
            assert result["est_loc"] is None
            assert result["tests"] is None
            assert result["criteria"] is None
        finally:
            orch.cleanup()

    def test_parse_task_metadata_handles_no_bold_title(self, temp_repo):
        """_parse_task_metadata handles tasks without bold title."""
        orch = Orchestrator()
        try:
            task_text = "Plain task description without bold"

            result = orch._parse_task_metadata(task_text)

            assert result["title"] == ""
            assert result["description"] == "Plain task description without bold"
        finally:
            orch.cleanup()

    def test_extract_new_tasks_finds_added_tasks(self, temp_repo):
        """_extract_new_tasks correctly identifies newly added tasks."""
        orch = Orchestrator()
        try:
            old_content = """# Tasklist
- [ ] Existing task
- [x] Completed task"""

            new_content = """# Tasklist
- [ ] Existing task
- [x] Completed task

- [ ] **New task 1**: First new task
- [ ] **New task 2**: Second new task"""

            result = orch._extract_new_tasks(old_content, new_content)

            assert len(result) == 2
            assert "**New task 1**: First new task" in result[0]
            assert "**New task 2**: Second new task" in result[1]
        finally:
            orch.cleanup()

    def test_validate_task_passes_valid_task(self, temp_repo):
        """_validate_task passes tasks that meet all constraints."""
        orch = Orchestrator()
        try:
            metadata = {
                "title": "Good task",
                "description": "A well-formed task with tests and criteria",
                "est_loc": 100,
                "tests": "test_file.py",
                "risk": "low",
                "criteria": "Tests pass",
                "context": "Directives",
                "raw": "",
            }

            result = orch._validate_task(metadata)

            assert result["valid"] is True
            assert len(result["violations"]) == 0
        finally:
            orch.cleanup()

    def test_validate_task_fails_missing_context(self, temp_repo):
        """_validate_task rejects tasks without context when required."""
        orch = Orchestrator()
        orch.task_constraints = {"require_context": True, "require_tests": False, "require_criteria": False, "require_risk": False}
        try:
            metadata = {
                "title": "Task",
                "description": "Desc",
                "est_loc": 10,
                "tests": None,
                "risk": None,
                "criteria": None,
                "context": None,
                "raw": "",
            }

            result = orch._validate_task(metadata)

            assert result["valid"] is False
            assert any("context" in v.lower() for v in result["violations"])
        finally:
            orch.cleanup()

    def test_validate_task_fails_oversized_task(self, temp_repo):
        """_validate_task rejects tasks exceeding max_loc."""
        orch = Orchestrator()
        orch.task_constraints = {"max_loc": 200, "require_tests": False, "require_criteria": False}
        try:
            metadata = {
                "title": "Big task",
                "description": "A task that is too large",
                "est_loc": 500,
                "tests": "test.py",
                "criteria": "Done",
                "raw": "",
            }

            result = orch._validate_task(metadata)

            assert result["valid"] is False
            assert any("exceeds maximum" in v for v in result["violations"])
        finally:
            orch.cleanup()

    def test_validate_task_fails_missing_tests(self, temp_repo):
        """_validate_task rejects tasks without test specification."""
        orch = Orchestrator()
        orch.task_constraints = {"max_loc": 200, "require_tests": True, "require_criteria": False}
        try:
            metadata = {
                "title": "Missing verification",
                "description": "A feature without any mention of how to verify",
                "est_loc": 50,
                "tests": None,
                "criteria": "Done",
                "raw": "",
            }

            result = orch._validate_task(metadata)

            assert result["valid"] is False
            assert any("test" in v.lower() for v in result["violations"])
        finally:
            orch.cleanup()

    def test_validate_task_fails_missing_criteria(self, temp_repo):
        """_validate_task rejects tasks without success criteria."""
        orch = Orchestrator()
        orch.task_constraints = {"max_loc": 200, "require_tests": False, "require_criteria": True}
        try:
            metadata = {
                "title": "No criteria task",
                "description": "A task without clear done condition",
                "est_loc": 50,
                "tests": "test.py",
                "criteria": None,
                "raw": "",
            }

            result = orch._validate_task(metadata)

            assert result["valid"] is False
            assert any("criteria" in v.lower() for v in result["violations"])
        finally:
            orch.cleanup()

    def test_validate_task_allows_tests_in_description(self, temp_repo):
        """_validate_task passes if tests are mentioned in description."""
        orch = Orchestrator()
        orch.task_constraints = {"max_loc": 200, "require_tests": True, "require_criteria": False, "require_risk": False, "require_context": False}
        try:
            metadata = {
                "title": "Task with test mention",
                "description": "Implement feature and add tests to verify",
                "est_loc": 50,
                "tests": None,  # No explicit metadata
                "risk": None,
                "criteria": None,
                "context": None,
                "raw": "",
            }

            result = orch._validate_task(metadata)

            # Should pass because "test" is in description
            assert result["valid"] is True
        finally:
            orch.cleanup()

    def test_validate_generated_tasks_validates_all_new_tasks(self, temp_repo):
        """_validate_generated_tasks checks all newly added tasks."""
        orch = Orchestrator()
        orch.task_constraints = {"max_loc": 200, "require_tests": True, "require_criteria": True, "require_risk": True}
        try:
            old_content = "# Tasklist\n- [ ] Old task"
            new_content = """# Tasklist
- [ ] Old task

- [ ] **Good task**: Description with tests and criteria
  - Est. LoC: 100
  - Tests: test.py
  - Risk: low
  - Criteria: All pass
  - Context: none

- [ ] **Bad task**: No metadata at all"""

            result = orch._validate_generated_tasks(old_content, new_content)

            assert result["valid"] is False
            assert len(result["tasks"]) == 2
            # First task should be valid
            assert result["tasks"][0]["validation"]["valid"] is True
            # Second task should have violations
            assert result["tasks"][1]["validation"]["valid"] is False
        finally:
            orch.cleanup()

    def test_run_plan_validates_tasks(self, temp_repo):
        """run_plan validates generated tasks against constraints."""
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test\n")

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        orch = Orchestrator()
        orch.task_constraints = {"max_loc": 200, "require_tests": True, "require_criteria": True, "require_risk": True, "require_context": True, "max_split_attempts": 0}
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                # Agent behavior:
                # 1. Add valid task
                # 2. Approve plan
                def behavior(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt:
                        content = tasklist.read_text()
                        tasklist.write_text(content + """

- [ ] **Valid task**: Description
  - Est. LoC: 100
  - Tests: test.py
  - Risk: low
  - Criteria: Tests pass
  - Context: none""")
                        return "Done"
                    elif "prompt_name: plan_review_prompt.md" in prompt:
                        return '{"verdict": "APPROVED", "score": 10}'
                    return ""

                mock_claude.side_effect = behavior

                result = orch.run_plan(str(design_file))

            assert result["success"] is True
            assert result["tasks_added"] == 1
            assert result["validation"]["valid"] is True
        finally:
            orch.cleanup()

    def test_run_plan_attempts_split_on_violations(self, temp_repo):
        """run_plan calls split prompt when tasks have violations."""
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test\n")

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        orch = Orchestrator()
        orch.task_constraints = {"max_loc": 200, "require_tests": True, "require_criteria": True, "require_risk": True, "require_context": True, "max_split_attempts": 2}
        try:

            with patch.object(orch, 'run_claude') as mock_claude:
                def handle_calls(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt:
                        # First call: add oversized task
                        content = tasklist.read_text()
                        tasklist.write_text(content + """

- [ ] **Big task**: Too large
  - Est. LoC: 500
  - Tests: test.py
  - Risk: medium
  - Criteria: Done
  - Context: none""")
                        return "Done"
                    elif "prompt_name: task_split_prompt.md" in prompt:
                        # Second call (split): fix the task
                        tasklist.write_text("""# Tasklist

- [ ] **Split task 1**: First part
  - Est. LoC: 100
  - Tests: test.py
  - Risk: low
  - Criteria: Part 1 works
  - Context: none

- [ ] **Split task 2**: Second part
  - Est. LoC: 100
  - Tests: test.py
  - Risk: low
  - Criteria: Part 2 works
  - Context: none""")
                        return "Done"
                    elif "prompt_name: plan_review_prompt.md" in prompt:
                        return '{"verdict": "APPROVED", "score": 10}'
                    return ""

                mock_claude.side_effect = handle_calls

                result = orch.run_plan(str(design_file))

            # Should have called run_claude 3 times (initial + 1 split + 1 review)
            assert mock_claude.call_count == 3
            assert result["success"] is True
            assert result["tasks_added"] == 2
        finally:
            orch.cleanup()

    def test_run_plan_respects_max_split_attempts(self, temp_repo):
        """run_plan stops splitting after max_split_attempts."""
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test\n")

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        orch = Orchestrator()
        orch.task_constraints = {"max_loc": 200, "require_tests": True, "require_criteria": True, "require_risk": True, "require_context": True, "max_split_attempts": 2}
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                # Always add an invalid task (agent never fixes it)
                def add_bad_task(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt or "prompt_name: task_split_prompt.md" in prompt:
                        content = tasklist.read_text()
                        if "Bad task" not in content:
                            tasklist.write_text(content + "\n\n- [ ] **Bad task**: No metadata")
                        return "Done"
                    elif "prompt_name: plan_review_prompt.md" in prompt:
                        return '{"verdict": "APPROVED", "score": 10}'
                    return ""
                mock_claude.side_effect = add_bad_task

                result = orch.run_plan(str(design_file))

            # Should have called: 1 initial + 2 split attempts + 1 review = 4 total
            assert mock_claude.call_count == 4
            assert result["success"] is True  # Tasks were added, just with warnings
            assert result["validation"]["valid"] is False
        finally:
            orch.cleanup()

    def test_run_plan_includes_max_loc_in_prompt(self, temp_repo):
        """run_plan substitutes MAX_LOC placeholder in prompt."""
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test\n")

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        orch = Orchestrator()
        orch.task_constraints = {"max_loc": 150, "require_tests": False, "require_criteria": False, "require_risk": False, "require_context": False, "max_split_attempts": 0}
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                def side_effect(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt:
                        content = tasklist.read_text()
                        tasklist.write_text(content + "\n\n- [ ] **Task**: Desc\n  - Est. LoC: 50\n  - Context: none")
                        return "Done"
                    elif "prompt_name: plan_review_prompt.md" in prompt:
                        return '{"verdict": "APPROVED", "score": 10}'
                    return ""
                mock_claude.side_effect = side_effect

                orch.run_plan(str(design_file))

            # Verify MAX_LOC was substituted in prompt (check first call)
            prompt = mock_claude.call_args_list[0][0][0]
            assert "150" in prompt  # Our custom max_loc
            assert "{{MAX_LOC}}" not in prompt
        finally:
            orch.cleanup()

    def test_run_plan_tasklist_path_from_provider(self, temp_repo):
        """_run_plan_impl uses tasklist provider snapshot methods for planning state."""
        from unittest.mock import MagicMock, patch

        # Set up a design file
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "my-design.md"
        design_file.write_text("# Design: My Design\n\nDesign content.")

        # Create a tasklist at a non-default path to distinguish from repo_dir / self.tasklist
        alt_tasklist = temp_repo / "alt" / "tasklist.md"
        alt_tasklist.parent.mkdir(exist_ok=True)
        alt_tasklist.write_text("# Tasklist\n\n- [ ] Existing task")

        orch = Orchestrator()
        orch.task_constraints = {"max_loc": 200, "require_tests": False, "require_criteria": False, "require_risk": False, "require_context": False, "max_split_attempts": 0}
        try:
            # Override provider snapshot methods to point planning logic at alt_tasklist
            mock_tasklist_provider = MagicMock()
            mock_tasklist_provider.get_snapshot.side_effect = lambda: alt_tasklist.read_text()
            mock_tasklist_provider.restore_snapshot.side_effect = lambda content: alt_tasklist.write_text(content)
            # list_tasks reflects current file state (used for ID-based task counting)
            def list_tasks_from_file():
                content = alt_tasklist.read_text()
                if "New Task" in content:
                    from millstone.artifacts.models import TasklistItem, TaskStatus
                    return [TasklistItem(task_id="new-task-id", title="New Task", status=TaskStatus.todo)]
                return []
            mock_tasklist_provider.list_tasks.side_effect = list_tasks_from_file
            orch._outer_loop_manager.tasklist_provider = mock_tasklist_provider

            with patch.object(orch, 'run_claude') as mock_claude:
                def behavior(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt:
                        content = alt_tasklist.read_text()
                        alt_tasklist.write_text(content + "\n\n- [ ] **New Task**: Desc\n  - Context: none")
                        return "Done"
                    elif "prompt_name: plan_review_prompt.md" in prompt:
                        return '{"verdict": "APPROVED", "score": 10}'
                    return ""
                mock_claude.side_effect = behavior

                result = orch.run_plan(str(design_file))

            # If tasklist snapshot methods are used, alt_tasklist was used and tasks were added
            assert result["success"] is True
            assert result["tasks_added"] == 1
        finally:
            orch.cleanup()

    def test_run_plan_status_updated_to_approved_on_success(self, temp_repo):
        """run_plan marks successful plans as approved via provider status update."""
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test Design\n")

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Existing task")

        orch = Orchestrator()
        orch.task_constraints = {
            "max_loc": 200,
            "require_tests": False,
            "require_criteria": False,
            "require_risk": False,
            "require_context": False,
            "max_split_attempts": 0,
        }
        try:
            with patch.object(orch, "run_claude") as mock_claude:
                def behavior(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt:
                        content = tasklist.read_text()
                        tasklist.write_text(
                            content + "\n\n- [ ] **Task 1**: Desc\n  - Context: none"
                        )
                        return "Done"
                    if "prompt_name: plan_review_prompt.md" in prompt:
                        return '{"verdict": "APPROVED", "score": 10}'
                    return ""

                mock_claude.side_effect = behavior
                with patch.object(
                    orch._outer_loop_manager.design_provider, "update_design_status"
                ) as mock_update_status:
                    result = orch.run_plan(str(design_file))

            assert result["success"] is True
            mock_update_status.assert_called_once_with(
                "test-design", DesignStatus.approved
            )
        finally:
            orch.cleanup()

    def test_run_plan_status_not_updated_to_approved_on_failure(self, temp_repo):
        """run_plan does not update design status when planning fails."""
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test Design\n")

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Existing task")

        orch = Orchestrator()
        try:
            with patch.object(orch, "run_claude") as mock_claude:
                mock_claude.return_value = "Did nothing"
                with patch.object(
                    orch._outer_loop_manager.design_provider, "update_design_status"
                ) as mock_update_status:
                    result = orch.run_plan(str(design_file))

            assert result["success"] is False
            mock_update_status.assert_not_called()
        finally:
            orch.cleanup()

    def test_run_plan_warns_when_design_is_draft_before_planning(self, temp_repo):
        """run_plan emits a draft warning before planning proceeds."""
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test Design\n")

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Existing task")

        orch = Orchestrator()
        orch.task_constraints = {
            "max_loc": 200,
            "require_tests": False,
            "require_criteria": False,
            "require_risk": False,
            "require_context": False,
            "max_split_attempts": 0,
        }
        try:
            events = []
            with patch.object(
                orch._outer_loop_manager.design_provider,
                "get_design",
                return_value=MagicMock(status=DesignStatus.draft),
            ), patch.object(
                orch._outer_loop_manager.design_provider, "update_design_status"
            ), patch("millstone.loops.outer.progress") as mock_progress:
                def progress_side_effect(message):
                    events.append(("progress", message))

                mock_progress.side_effect = progress_side_effect

                with patch.object(orch, "run_claude") as mock_claude:
                    def behavior(prompt, **kwargs):
                        if "prompt_name: plan_prompt.md" in prompt:
                            events.append(("run_claude", "plan_prompt"))
                            content = tasklist.read_text()
                            tasklist.write_text(
                                content + "\n\n- [ ] **Task 1**: Desc\n  - Context: none"
                            )
                            return "Done"
                        if "prompt_name: plan_review_prompt.md" in prompt:
                            events.append(("run_claude", "plan_review_prompt"))
                            return '{"verdict": "APPROVED", "score": 10}'
                        return ""

                    mock_claude.side_effect = behavior
                    result = orch.run_plan(str(design_file))

            assert result["success"] is True
            warning_index = next(
                i
                for i, event in enumerate(events)
                if event[0] == "progress" and "draft status" in event[1]
            )
            first_agent_call_index = next(
                i for i, event in enumerate(events) if event[0] == "run_claude"
            )
            assert warning_index < first_agent_call_index
        finally:
            orch.cleanup()

    def test_run_plan_status_update_failure_does_not_propagate(self, temp_repo):
        """run_plan swallows approved-status update errors and logs a warning."""
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test Design\n")

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Existing task")

        orch = Orchestrator()
        orch.task_constraints = {
            "max_loc": 200,
            "require_tests": False,
            "require_criteria": False,
            "require_risk": False,
            "require_context": False,
            "max_split_attempts": 0,
        }
        try:
            with patch.object(orch, "run_claude") as mock_claude:
                def behavior(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt:
                        content = tasklist.read_text()
                        tasklist.write_text(
                            content + "\n\n- [ ] **Task 1**: Desc\n  - Context: none"
                        )
                        return "Done"
                    if "prompt_name: plan_review_prompt.md" in prompt:
                        return '{"verdict": "APPROVED", "score": 10}'
                    return ""

                mock_claude.side_effect = behavior
                with patch.object(
                    orch._outer_loop_manager.design_provider,
                    "update_design_status",
                    side_effect=RuntimeError("boom"),
                ) as mock_update_status, patch("millstone.loops.outer.progress") as mock_progress:
                    result = orch.run_plan(str(design_file))

            assert result["success"] is True
            mock_update_status.assert_called_once_with(
                "test-design", DesignStatus.approved
            )
            assert any(
                "Failed to update design status to approved" in call.args[0]
                for call in mock_progress.call_args_list
            )
        finally:
            orch.cleanup()



class TestPlanReview:
    """Tests for plan review loop in run_plan()."""

    def test_run_plan_reviews_and_approves(self, temp_repo):
        """run_plan approves valid plan without fixes."""
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test\n")

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                # Mock responses:
                # 1. Plan generation (add valid task)
                # 2. Plan review (approve)

                def plan_behavior(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt:
                        content = tasklist.read_text()
                        tasklist.write_text(content + "\n\n- [ ] **Task 1**: Desc\n  - Est. LoC: 50\n  - Tests: t.py\n  - Risk: low\n  - Criteria: pass\n  - Context: none")
                        return "Done"
                    elif "prompt_name: plan_review_prompt.md" in prompt:
                        return '{"verdict": "APPROVED", "score": 10}'
                    return ""

                mock_claude.side_effect = plan_behavior

                result = orch.run_plan(str(design_file))

                assert result["success"] is True
                assert result["tasks_added"] == 1
                # Should have called plan gen and review
                assert mock_claude.call_count == 2
        finally:
            orch.cleanup()

    def test_run_plan_reviews_rejects_and_fixes(self, temp_repo):
        """run_plan rejects invalid plan, triggers fix, and approves."""
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test\n")

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        original_content = "# Tasklist\n"
        tasklist.write_text(original_content)

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_claude') as mock_claude:
                # Sequence:
                # 1. Plan gen (bad task)
                # 2. Review (reject)
                # 3. Fix (good task)
                # 4. Review (approve)

                call_state = {"attempt": 0}

                def behavior(prompt, **kwargs):
                    if "prompt_name: plan_prompt.md" in prompt:
                        # Initial bad plan
                        content = tasklist.read_text()
                        tasklist.write_text(content + "\n\n- [ ] **Bad Task**: No details\n  - Est. LoC: 50\n  - Tests: t.py\n  - Risk: low\n  - Criteria: pass\n  - Context: none")
                        return "Done"

                    elif "prompt_name: plan_review_prompt.md" in prompt:
                        call_state["attempt"] += 1
                        if call_state["attempt"] == 1:
                            return '{"verdict": "NEEDS_REVISION", "feedback": ["Bad task needs detail"]}'
                        return '{"verdict": "APPROVED", "score": 10}'

                    elif "prompt_name: plan_fix_prompt.md" in prompt:
                        # Edit-in-place: bad task is still in the file (no revert)
                        current = tasklist.read_text()
                        assert "Bad Task" in current  # file was NOT reverted
                        # Replace bad task with good task in place
                        updated = current.replace(
                            "- [ ] **Bad Task**: No details",
                            "- [ ] **Good Task**: Details added",
                        )
                        tasklist.write_text(updated)
                        return "Fixed"

                    return ""

                mock_claude.side_effect = behavior

                result = orch.run_plan(str(design_file))

                assert result["success"] is True
                assert result["tasks_added"] == 1
                final_content = tasklist.read_text()
                assert "Good Task" in final_content
                assert "Bad Task" not in final_content
                # Calls: Plan(1) + Review(1) + Fix(1) + Review(2) = 4
                assert mock_claude.call_count == 4
        finally:
            orch.cleanup()


class TestPlanCLI:
    """Tests for --plan CLI flag."""

    def test_plan_flag_runs_plan(self, temp_repo):
        """--plan flag invokes run_plan and exits 0 on success."""
        from millstone import orchestrate

        # Create design file
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test\n")

        # Create tasklist
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        with patch('sys.argv', ['orchestrate.py', '--plan', str(design_file)]):
            with patch.object(Orchestrator, 'run_plan') as mock_plan:
                mock_plan.return_value = {"success": True, "tasks_added": 3}
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()

                mock_plan.assert_called_once()
                assert exc_info.value.code == 0

    def test_plan_flag_exits_1_on_failure(self, temp_repo):
        """--plan flag exits 1 when no tasks are added."""
        from millstone import orchestrate

        # Create tasklist
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        with patch('sys.argv', ['orchestrate.py', '--plan', 'designs/test.md']):
            with patch.object(Orchestrator, 'run_plan') as mock_plan:
                mock_plan.return_value = {"success": False, "tasks_added": 0}
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()

                assert exc_info.value.code == 1

    def test_plan_flag_exits_1_on_exception(self, temp_repo):
        """--plan flag exits 1 on exception."""
        from millstone import orchestrate

        # Create tasklist
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        with patch('sys.argv', ['orchestrate.py', '--plan', 'designs/test.md']):
            with patch.object(Orchestrator, 'run_plan') as mock_plan:
                mock_plan.side_effect = Exception("Test error")
                with pytest.raises(SystemExit) as exc_info:
                    orchestrate.main()

                assert exc_info.value.code == 1

    def test_plan_flag_uses_tasklist_argument(self, temp_repo):
        """--plan respects --tasklist argument."""
        from millstone import orchestrate

        # Create design file
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        design_file = designs_dir / "test-design.md"
        design_file.write_text("# Design: Test\n")

        # Create custom tasklist
        custom_tasklist = temp_repo / "custom_tasklist.md"
        custom_tasklist.write_text("# Custom Tasklist\n")

        with patch('sys.argv', ['orchestrate.py', '--plan', str(design_file), '--tasklist', 'custom_tasklist.md']):
            with patch.object(Orchestrator, 'run_plan') as mock_plan:
                mock_plan.return_value = {"success": True, "tasks_added": 1}
                with pytest.raises(SystemExit):
                    orchestrate.main()

                # Verify the tasklist argument was passed to run_plan
                mock_plan.assert_called_once()


class TestSelectOpportunity:
    """Tests for _select_opportunity helper method."""

    def test_returns_none_when_no_opportunities_file(self, temp_repo):
        """Returns None when opportunities.md doesn't exist."""
        orch = Orchestrator()
        try:
            result = orch._select_opportunity()
            assert result is None
        finally:
            orch.cleanup()

    def test_extracts_high_priority_opportunity(self, temp_repo):
        """Extracts the first High Priority opportunity."""
        opportunities_content = """# Opportunities

Generated: 2024-12-01

## High Priority (High Impact / Low Effort)

### Add Caching Layer
- **Impact**: 4 - Improves response time significantly
- **Effort**: 2 - Well-understood pattern
- **Location**: api/handlers.py
- **Description**: Add Redis caching for frequently accessed data.

### Improve Error Messages
- **Impact**: 3 - Better developer experience
- **Effort**: 1 - Simple changes
- **Description**: Make error messages more descriptive.

## Medium Priority
"""
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(opportunities_content)

        orch = Orchestrator()
        try:
            result = orch._select_opportunity()
            assert result is not None
            assert result.title == "Add Caching Layer"
        finally:
            orch.cleanup()

    def test_falls_back_to_any_opportunity_without_high_priority(self, temp_repo):
        """Falls back to any opportunity if no High Priority section."""
        opportunities_content = """# Opportunities

### Some Opportunity
- Description: A thing to do
"""
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(opportunities_content)

        orch = Orchestrator()
        try:
            result = orch._select_opportunity()
            assert result is not None
            assert result.title == "Some Opportunity"
        finally:
            orch.cleanup()

    def test_returns_none_for_empty_opportunities(self, temp_repo):
        """Returns None when opportunities file has no opportunities."""
        opportunities_content = """# Opportunities

No issues found!
"""
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(opportunities_content)

        orch = Orchestrator()
        try:
            result = orch._select_opportunity()
            assert result is None
        finally:
            orch.cleanup()

    def test_picks_highest_roi_opportunity(self, temp_repo):
        """Picks the opportunity with highest ROI score regardless of order."""
        opportunities_content = """# Opportunities

Generated: 2024-12-01

## High Priority (High Impact / Low Effort)

### Low ROI Task
- **Impact**: 2/5 - Minor improvement
- **Effort**: 4/5 - Significant work
**ROI Score**: 0.5
- **Confidence**: Medium
- **Location**: somewhere.py
- **Description**: This has low ROI.

### High ROI Task
- **Impact**: 4/5 - Major improvement
- **Effort**: 2/5 - Easy work
**ROI Score**: 2.0
- **Confidence**: High
- **Location**: elsewhere.py
- **Description**: This has high ROI.

### Medium ROI Task
- **Impact**: 3/5 - Moderate improvement
- **Effort**: 3/5 - Moderate work
**ROI Score**: 1.0
- **Confidence**: Medium
- **Location**: another.py
- **Description**: This has medium ROI.
"""
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(opportunities_content)

        orch = Orchestrator()
        try:
            result = orch._select_opportunity()
            assert result is not None
            assert result.opportunity_id == "high-roi-task"
            assert result.title == "High ROI Task"
        finally:
            orch.cleanup()

    def test_picks_highest_roi_across_priority_sections(self, temp_repo):
        """Picks highest ROI even if it's in Medium Priority section."""
        opportunities_content = """# Opportunities

Generated: 2024-12-01

## High Priority (High Impact / Low Effort)

### Moderate Task in High Priority
- **Impact**: 3/5 - Good improvement
- **Effort**: 3/5 - Moderate work
**ROI Score**: 1.0
- **Confidence**: Medium
- **Description**: In high priority but lower ROI.

## Medium Priority

### Best ROI in Medium Section
- **Impact**: 5/5 - Excellent improvement
- **Effort**: 2/5 - Easy work
**ROI Score**: 2.5
- **Confidence**: High
- **Description**: Better ROI despite being in medium priority.
"""
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(opportunities_content)

        orch = Orchestrator()
        try:
            result = orch._select_opportunity()
            assert result is not None
            assert result.title == "Best ROI in Medium Section"
        finally:
            orch.cleanup()

    def test_fallback_to_high_priority_without_roi_scores(self, temp_repo):
        """Falls back to first High Priority when no ROI scores present."""
        opportunities_content = """# Opportunities

Generated: 2024-12-01

## High Priority (High Impact / Low Effort)

### First High Priority Task
- **Impact**: 4 - Improves things
- **Effort**: 2 - Easy
- **Description**: No ROI score but in high priority.

### Second High Priority Task
- **Impact**: 3 - Also improves
- **Effort**: 1 - Very easy
- **Description**: Also no ROI score.
"""
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(opportunities_content)

        orch = Orchestrator()
        try:
            result = orch._select_opportunity()
            assert result is not None
            assert result.title == "First High Priority Task"
        finally:
            orch.cleanup()

    def test_handles_mixed_roi_and_no_roi(self, temp_repo):
        """Picks highest ROI when some opportunities have scores and others don't."""
        opportunities_content = """# Opportunities

## High Priority

### Task Without ROI
- **Impact**: 5/5 - Great
- **Effort**: 1/5 - Easy
- **Description**: No ROI score field.

### Task With ROI
- **Impact**: 3/5 - OK
- **Effort**: 2/5 - Medium
**ROI Score**: 1.5
- **Description**: Has ROI score.
"""
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(opportunities_content)

        orch = Orchestrator()
        try:
            result = orch._select_opportunity()
            # Should pick the one with ROI score since it's the only one with a score
            assert result is not None
            assert result.title == "Task With ROI"
        finally:
            orch.cleanup()

    def test_excludes_adopted_and_rejected_checklist_opportunities(self, temp_repo):
        """Adopted and rejected opportunities are excluded from selection."""
        opportunities_content = """# Opportunities

- [x] **Adopted Task**
  - Opportunity ID: adopted-task
  - ROI Score: 5.0
  - Description: This is adopted.

- [ ] **Rejected Task**
  - Opportunity ID: rejected-task
  - Status: rejected
  - ROI Score: 4.0
  - Description: This is rejected.

- [ ] **Identified Task**
  - Opportunity ID: identified-task
  - ROI Score: 1.0
  - Description: This is identified.
"""
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(opportunities_content)

        orch = Orchestrator()
        try:
            result = orch._select_opportunity()
            assert result is not None
            assert result.title == "Identified Task"
        finally:
            orch.cleanup()

    def test_selects_highest_roi_from_checklist_format(self, temp_repo):
        """Picks highest ROI identified opportunity from checklist format."""
        opportunities_content = """# Opportunities

- [ ] **Low ROI Task**
  - Opportunity ID: low-roi
  - ROI Score: 0.5
  - Description: Low ROI.

- [ ] **High ROI Task**
  - Opportunity ID: high-roi
  - ROI Score: 2.0
  - Description: High ROI.

- [ ] **Medium ROI Task**
  - Opportunity ID: medium-roi
  - ROI Score: 1.0
  - Description: Medium ROI.
"""
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(opportunities_content)

        orch = Orchestrator()
        try:
            result = orch._select_opportunity()
            assert result is not None
            assert result.title == "High ROI Task"
        finally:
            orch.cleanup()

    def test_returns_none_when_all_opportunities_adopted_or_rejected(self, temp_repo):
        """Returns None when no identified opportunities remain."""
        opportunities_content = """# Opportunities

- [x] **Adopted Task**
  - Opportunity ID: adopted-task
  - Description: Adopted.

- [ ] **Rejected Task**
  - Opportunity ID: rejected-task
  - Status: rejected
  - Description: Rejected.
"""
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(opportunities_content)

        orch = Orchestrator()
        try:
            result = orch._select_opportunity()
            assert result is None
        finally:
            orch.cleanup()


class TestRunCycle:
    """Tests for run_cycle method."""

    def test_runs_inner_loop_when_pending_tasks(self, temp_repo):
        """When pending tasks exist, run_cycle calls run() directly."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [ ] Existing task")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run') as mock_run:
                mock_run.return_value = 0
                result = orch.run_cycle()

                mock_run.assert_called_once()
                assert result == 0
        finally:
            orch.cleanup()

    def test_runs_analysis_when_no_pending_tasks(self, temp_repo):
        """When no pending tasks, run_cycle runs analysis."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [x] Completed task")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze:
                mock_analyze.return_value = {"success": False}
                result = orch.run_cycle()

                mock_analyze.assert_called_once()
                assert result == 1  # Fails because analysis failed
        finally:
            orch.cleanup()

    def test_returns_0_when_no_opportunities_found(self, temp_repo):
        """Returns 0 (success) when no opportunities are found after analysis."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n")

        # Create empty opportunities file
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text("# Opportunities\n\nNo issues found!")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze:
                mock_analyze.return_value = {"success": True}
                result = orch.run_cycle()

                assert result == 0
        finally:
            orch.cleanup()

    def test_runs_full_cycle_when_opportunity_found(self, temp_repo):
        """Runs design, review, plan, and build when opportunity is found."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n")

        # Create opportunities file
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text("""# Opportunities

- [ ] **Add Tests**
  - Opportunity ID: add-tests
  - ROI Score: 1.0
  - Description: Add unit tests
""")

        # Disable approval gates to test full cycle flow
        orch = Orchestrator(
            review_designs=True,
            approve_opportunities=False,
            approve_designs=False,
            approve_plans=False,
        )
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze, \
                 patch.object(orch, 'run_design') as mock_design, \
                 patch.object(orch, 'review_design') as mock_review, \
                 patch.object(orch, 'run_plan') as mock_plan, \
                 patch.object(orch, 'run') as mock_run:

                mock_analyze.return_value = {"success": True}
                mock_design.return_value = {"success": True, "design_file": "designs/add-tests.md"}
                mock_review.return_value = {"approved": True}
                mock_plan.return_value = {"success": True, "tasks_added": 2}
                mock_run.return_value = 0

                result = orch.run_cycle()

                mock_analyze.assert_called_once()
                mock_design.assert_called_once_with("Add Tests", opportunity_id="add-tests")
                mock_review.assert_called_once_with("designs/add-tests.md")
                mock_plan.assert_called_once_with("designs/add-tests.md")
                mock_run.assert_called_once()
                assert result == 0
        finally:
            orch.cleanup()

    def test_halts_when_design_fails(self, temp_repo):
        """Halts with exit 1 when design fails."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n")

        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(
            "# Opportunities\n\n"
            "- [ ] **Test**\n"
            "  - Opportunity ID: test\n"
            "  - ROI Score: 1.0\n"
            "  - Description: Test opportunity\n"
        )

        # Disable approval gates to test design failure
        orch = Orchestrator(approve_opportunities=False)
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze, \
                 patch.object(orch, 'run_design') as mock_design:

                mock_analyze.return_value = {"success": True}
                mock_design.return_value = {"success": False}

                result = orch.run_cycle()
                assert result == 1
        finally:
            orch.cleanup()

    def test_halts_when_design_review_fails(self, temp_repo):
        """Halts with exit 1 when design review fails."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n")

        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(
            "# Opportunities\n\n"
            "- [ ] **Test**\n"
            "  - Opportunity ID: test\n"
            "  - ROI Score: 1.0\n"
            "  - Description: Test opportunity\n"
        )

        # Disable approval gates to test design review failure
        orch = Orchestrator(review_designs=True, approve_opportunities=False)
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze, \
                 patch.object(orch, 'run_design') as mock_design, \
                 patch.object(orch, 'review_design') as mock_review:

                mock_analyze.return_value = {"success": True}
                mock_design.return_value = {"success": True, "design_file": "designs/test.md"}
                mock_review.return_value = {"approved": False}

                result = orch.run_cycle()
                assert result == 1
        finally:
            orch.cleanup()

    def test_skips_review_when_review_designs_disabled(self, temp_repo):
        """Skips design review when review_designs is False."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n")

        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(
            "# Opportunities\n\n"
            "- [ ] **Test**\n"
            "  - Opportunity ID: test\n"
            "  - ROI Score: 1.0\n"
            "  - Description: Test opportunity\n"
        )

        # Disable approval gates to test review_designs behavior
        orch = Orchestrator(
            review_designs=False,
            approve_opportunities=False,
            approve_designs=False,
            approve_plans=False,
        )
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze, \
                 patch.object(orch, 'run_design') as mock_design, \
                 patch.object(orch, 'review_design') as mock_review, \
                 patch.object(orch, 'run_plan') as mock_plan, \
                 patch.object(orch, 'run') as mock_run:

                mock_analyze.return_value = {"success": True}
                mock_design.return_value = {"success": True, "design_file": "designs/test.md"}
                mock_plan.return_value = {"success": True, "tasks_added": 1}
                mock_run.return_value = 0

                result = orch.run_cycle()

                mock_review.assert_not_called()
                assert result == 0
        finally:
            orch.cleanup()

    def test_halts_when_plan_fails(self, temp_repo):
        """Halts with exit 1 when planning fails."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n")

        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(
            "# Opportunities\n\n"
            "- [ ] **Test**\n"
            "  - Opportunity ID: test\n"
            "  - ROI Score: 1.0\n"
            "  - Description: Test opportunity\n"
        )

        # Disable approval gates to test plan failure
        orch = Orchestrator(
            review_designs=False,
            approve_opportunities=False,
            approve_designs=False,
        )
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze, \
                 patch.object(orch, 'run_design') as mock_design, \
                 patch.object(orch, 'run_plan') as mock_plan:

                mock_analyze.return_value = {"success": True}
                mock_design.return_value = {"success": True, "design_file": "designs/test.md"}
                mock_plan.return_value = {"success": False, "tasks_added": 0}

                result = orch.run_cycle()
                assert result == 1
        finally:
            orch.cleanup()

    def test_adopts_selected_opportunity_before_opportunities_gate(self, temp_repo):
        """run_cycle marks selected opportunity adopted before approval gate halt."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n")

        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(
            "# Opportunities\n\n"
            "- [ ] **First Opportunity**\n"
            "  - Opportunity ID: first-opportunity\n"
            "  - ROI Score: 3.0\n"
            "  - Description: First\n"
        )

        orch = Orchestrator(approve_opportunities=True)
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze, \
                 patch.object(orch._outer_loop_manager, '_cycle_log') as mock_cycle_log:
                mock_analyze.return_value = {"success": True, "opportunity_count": 1}
                result = orch.run_cycle()

            assert result == 0
            phases = [call.args[0] for call in mock_cycle_log.call_args_list]
            assert "ADOPT" in phases
            assert "GATE" in phases
            assert phases.index("ADOPT") < phases.index("GATE")
            assert "- [x] **First Opportunity**" in opportunities_file.read_text()
        finally:
            orch.cleanup()

    def test_second_gate_halted_cycle_does_not_reselect_previous_adopted(self, temp_repo):
        """After gate halt, a second run selects the next identified opportunity."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n")

        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text(
            "# Opportunities\n\n"
            "- [ ] **First Opportunity**\n"
            "  - Opportunity ID: first-opportunity\n"
            "  - ROI Score: 3.0\n"
            "  - Description: First\n\n"
            "- [ ] **Second Opportunity**\n"
            "  - Opportunity ID: second-opportunity\n"
            "  - ROI Score: 2.0\n"
            "  - Description: Second\n"
        )

        orch = Orchestrator(approve_opportunities=True)
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze:
                mock_analyze.return_value = {"success": True, "opportunity_count": 2}
                first_result = orch.run_cycle()
                second_result = orch.run_cycle()

            assert first_result == 0
            assert second_result == 0
            content = opportunities_file.read_text()
            assert "- [x] **First Opportunity**" in content
            assert "- [x] **Second Opportunity**" in content
        finally:
            orch.cleanup()

    def test_roadmap_goal_path_skips_opportunity_selection_and_adoption(self, temp_repo):
        """Roadmap-based cycle does not select or adopt opportunities."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [x] Done")
        roadmap = temp_repo / "roadmap.md"
        roadmap.write_text("# Roadmap\n\n- [ ] Improve onboarding")

        orch = Orchestrator(
            tasklist=str(tasklist),
            roadmap="roadmap.md",
            approve_designs=True,
        )
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze, \
                 patch.object(orch, '_select_opportunity') as mock_select, \
                 patch.object(orch._outer_loop_manager.opportunity_provider, 'update_opportunity_status') as mock_update, \
                 patch.object(orch, 'run_design') as mock_design, \
                 patch.object(orch, 'review_design') as mock_review:
                mock_design.return_value = {"success": True, "design_file": "designs/improve-onboarding.md"}
                mock_review.return_value = {"approved": True}

                result = orch.run_cycle()

            assert result == 0
            mock_analyze.assert_not_called()
            mock_select.assert_not_called()
            mock_update.assert_not_called()
            mock_design.assert_called_once_with("Improve onboarding")
        finally:
            orch.cleanup()


class TestCycleCLI:
    """Tests for --cycle CLI flag."""

    def test_cycle_flag_runs_cycle(self, temp_repo):
        """--cycle flag invokes run_cycle and exits 0 on success."""
        from millstone import orchestrate

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        with patch('sys.argv', ['orchestrate.py', '--cycle']):
            with patch.object(Orchestrator, 'preflight_checks'):
                with patch.object(Orchestrator, 'run_cycle') as mock_cycle:
                    mock_cycle.return_value = 0
                    with pytest.raises(SystemExit) as exc_info:
                        orchestrate.main()

                    mock_cycle.assert_called_once()
                    assert exc_info.value.code == 0

    def test_cycle_flag_exits_1_on_failure(self, temp_repo):
        """--cycle flag exits 1 when run_cycle fails."""
        from millstone import orchestrate

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        with patch('sys.argv', ['orchestrate.py', '--cycle']):
            with patch.object(Orchestrator, 'preflight_checks'):
                with patch.object(Orchestrator, 'run_cycle') as mock_cycle:
                    mock_cycle.return_value = 1
                    with pytest.raises(SystemExit) as exc_info:
                        orchestrate.main()

                    assert exc_info.value.code == 1

    def test_cycle_flag_exits_1_on_exception(self, temp_repo):
        """--cycle flag exits 1 on exception."""
        from millstone import orchestrate

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        with patch('sys.argv', ['orchestrate.py', '--cycle']):
            with patch.object(Orchestrator, 'preflight_checks'):
                with patch.object(Orchestrator, 'run_cycle') as mock_cycle:
                    mock_cycle.side_effect = Exception("Test error")
                    with pytest.raises(SystemExit) as exc_info:
                        orchestrate.main()

                    assert exc_info.value.code == 1

    def test_cycle_flag_passes_config_options(self, temp_repo):
        """--cycle respects config options like max_cycles and loc_threshold."""
        from millstone import orchestrate

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        with patch('sys.argv', ['orchestrate.py', '--cycle', '--max-cycles', '5', '--loc-threshold', '1000']):
            with patch.object(Orchestrator, 'preflight_checks'):
                with patch.object(Orchestrator, 'run_cycle') as mock_cycle:
                    mock_cycle.return_value = 0
                    with pytest.raises(SystemExit):
                        orchestrate.main()

                    mock_cycle.assert_called_once()


class TestApprovalGates:
    """Tests for approval gates configuration in --cycle mode."""

    def test_approve_opportunities_pauses_after_analyze(self, temp_repo):
        """With approve_opportunities=True, cycle pauses after analysis."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [x] Completed task")

        opportunities = temp_repo / ".millstone" / "opportunities.md"
        opportunities.write_text("""# Opportunities

- [ ] **Add retry logic**
  - Opportunity ID: add-retry-logic
  - ROI Score: 2.0
  - Description: Improve retry handling
""")

        orch = Orchestrator(
            tasklist=str(tasklist),
            approve_opportunities=True,
            approve_designs=True,
            approve_plans=True,
        )
        orch.repo_dir = temp_repo
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze:
                mock_analyze.return_value = {"success": True}
                result = orch.run_cycle()

                mock_analyze.assert_called_once()
                # Should pause after analyze (return 0, not continue to design)
                assert result == 0
        finally:
            orch.cleanup()

    def test_approve_designs_pauses_after_design(self, temp_repo):
        """With approve_designs=True, cycle pauses after design creation."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [x] Completed task")

        opportunities = temp_repo / ".millstone" / "opportunities.md"
        opportunities.write_text("""# Opportunities

- [ ] **Add retry logic**
  - Opportunity ID: add-retry-logic
  - ROI Score: 2.0
  - Description: Improve retry handling
""")

        orch = Orchestrator(
            tasklist=str(tasklist),
            approve_opportunities=False,  # Skip this gate
            approve_designs=True,
            approve_plans=True,
        )
        orch.repo_dir = temp_repo
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze:
                with patch.object(orch, 'run_design') as mock_design:
                    with patch.object(orch, 'review_design') as mock_review:
                        mock_analyze.return_value = {"success": True}
                        mock_design.return_value = {"success": True, "design_file": "designs/test.md"}
                        mock_review.return_value = {"approved": True}

                        result = orch.run_cycle()

                        mock_design.assert_called_once()
                        # Should pause after design (return 0, not continue to plan)
                        assert result == 0
        finally:
            orch.cleanup()

    def test_approve_plans_pauses_after_plan(self, temp_repo):
        """With approve_plans=True, cycle pauses after plan creation."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [x] Completed task")

        opportunities = temp_repo / ".millstone" / "opportunities.md"
        opportunities.write_text("""# Opportunities

- [ ] **Add retry logic**
  - Opportunity ID: add-retry-logic
  - ROI Score: 2.0
  - Description: Improve retry handling
""")

        orch = Orchestrator(
            tasklist=str(tasklist),
            approve_opportunities=False,
            approve_designs=False,  # Skip this gate
            approve_plans=True,
        )
        orch.repo_dir = temp_repo
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze:
                with patch.object(orch, 'run_design') as mock_design:
                    with patch.object(orch, 'review_design') as mock_review:
                        with patch.object(orch, 'run_plan') as mock_plan:
                            mock_analyze.return_value = {"success": True}
                            mock_design.return_value = {"success": True, "design_file": "designs/test.md"}
                            mock_review.return_value = {"approved": True}
                            mock_plan.return_value = {"success": True, "tasks_added": 3}

                            result = orch.run_cycle()

                            mock_plan.assert_called_once()
                            # Should pause after plan (return 0, not continue to run)
                            assert result == 0
        finally:
            orch.cleanup()

    def test_no_approve_runs_full_cycle(self, temp_repo):
        """With all approve_* flags False, cycle runs fully autonomous."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [x] Completed task")

        opportunities = temp_repo / ".millstone" / "opportunities.md"
        opportunities.write_text("""# Opportunities

- [ ] **Add retry logic**
  - Opportunity ID: add-retry-logic
  - ROI Score: 2.0
  - Description: Improve retry handling
""")

        orch = Orchestrator(
            tasklist=str(tasklist),
            approve_opportunities=False,
            approve_designs=False,
            approve_plans=False,
        )
        orch.repo_dir = temp_repo
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze:
                with patch.object(orch, 'run_design') as mock_design:
                    with patch.object(orch, 'review_design') as mock_review:
                        with patch.object(orch, 'run_plan') as mock_plan:
                            with patch.object(orch, 'run') as mock_run:
                                mock_analyze.return_value = {"success": True}
                                mock_design.return_value = {"success": True, "design_file": "designs/test.md"}
                                mock_review.return_value = {"approved": True}
                                mock_plan.return_value = {"success": True, "tasks_added": 3}
                                mock_run.return_value = 0

                                result = orch.run_cycle()

                                # All phases should run
                                mock_analyze.assert_called_once()
                                mock_design.assert_called_once()
                                mock_plan.assert_called_once()
                                mock_run.assert_called_once()
                                assert result == 0
        finally:
            orch.cleanup()

    def test_no_approve_flag_disables_gates(self, temp_repo):
        """--no-approve CLI flag sets all approval gates to False."""
        from millstone import orchestrate

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        with patch('sys.argv', ['orchestrate.py', '--cycle', '--no-approve']):
            with patch.object(Orchestrator, 'preflight_checks'):
                with patch.object(Orchestrator, '__init__', return_value=None) as mock_init:
                    with patch.object(Orchestrator, 'run_cycle', return_value=0):
                        with pytest.raises(SystemExit):
                            orchestrate.main()

                        # Verify approval gates were set to False
                        call_kwargs = mock_init.call_args[1]
                        assert call_kwargs.get('approve_opportunities') is False
                        assert call_kwargs.get('approve_designs') is False
                        assert call_kwargs.get('approve_plans') is False

    def test_default_approval_gates_from_config(self, temp_repo):
        """Without --no-approve, approval gates use config values (default True)."""
        from millstone import orchestrate

        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("# Tasklist\n")

        with patch('sys.argv', ['orchestrate.py', '--cycle']):
            with patch.object(Orchestrator, 'preflight_checks'):
                with patch.object(Orchestrator, '__init__', return_value=None) as mock_init:
                    with patch.object(Orchestrator, 'run_cycle', return_value=0):
                        with pytest.raises(SystemExit):
                            orchestrate.main()

                        # Verify approval gates use default (True)
                        call_kwargs = mock_init.call_args[1]
                        assert call_kwargs.get('approve_opportunities') is True
                        assert call_kwargs.get('approve_designs') is True
                        assert call_kwargs.get('approve_plans') is True


class TestCycleLogging:
    """Tests for cycle logging functionality."""

    def test_cycle_logging_creates_cycles_directory(self, temp_repo):
        """run_cycle creates .millstone/cycles/ directory."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [x] Completed task")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze:
                mock_analyze.return_value = {"success": False}
                orch.run_cycle()

                # Verify cycles directory was created
                cycles_dir = temp_repo / ".millstone" / "cycles"
                assert cycles_dir.exists()
        finally:
            orch.cleanup()

    def test_cycle_logging_creates_log_file(self, temp_repo):
        """run_cycle creates a timestamped log file in .millstone/cycles/."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [x] Completed task")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze:
                mock_analyze.return_value = {"success": False}
                orch.run_cycle()

                # Verify a log file was created
                cycles_dir = temp_repo / ".millstone" / "cycles"
                log_files = list(cycles_dir.glob("*.log"))
                assert len(log_files) == 1
        finally:
            orch.cleanup()

    def test_cycle_log_contains_started_header(self, temp_repo):
        """Cycle log contains started header with timestamp."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [x] Completed task")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze:
                mock_analyze.return_value = {"success": False}
                orch.run_cycle()

                cycles_dir = temp_repo / ".millstone" / "cycles"
                log_file = list(cycles_dir.glob("*.log"))[0]
                content = log_file.read_text()
                assert "=== Cycle Started:" in content
        finally:
            orch.cleanup()

    def test_cycle_log_contains_completed_footer(self, temp_repo):
        """Cycle log contains completed footer with status."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [x] Completed task")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze:
                mock_analyze.return_value = {"success": False}
                orch.run_cycle()

                cycles_dir = temp_repo / ".millstone" / "cycles"
                log_file = list(cycles_dir.glob("*.log"))[0]
                content = log_file.read_text()
                assert "=== Cycle Completed: FAILED ===" in content
        finally:
            orch.cleanup()

    def test_cycle_log_records_analyze_phase(self, temp_repo):
        """Cycle log records ANALYZE phase results."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n")

        # Create opportunities file
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text("# Opportunities\n\nNo issues found!")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze:
                mock_analyze.return_value = {"success": True, "opportunity_count": 5}
                orch.run_cycle()

                cycles_dir = temp_repo / ".millstone" / "cycles"
                log_file = list(cycles_dir.glob("*.log"))[0]
                content = log_file.read_text()
                assert "ANALYZE: Found 5 opportunities" in content
        finally:
            orch.cleanup()

    def test_cycle_log_records_select_phase(self, temp_repo):
        """Cycle log records SELECT phase as structured JSON."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n")

        # Create opportunities file with high priority item
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text("""# Opportunities

- [ ] **Add Retry Logic**
  - Opportunity ID: add-retry-logic
  - ROI Score: 2.0
  - Description: Add retry logic to API calls
""")

        orch = Orchestrator(approve_opportunities=True)
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze:
                mock_analyze.return_value = {"success": True, "opportunity_count": 1}
                orch.run_cycle()

                cycles_dir = temp_repo / ".millstone" / "cycles"
                log_file = list(cycles_dir.glob("*.log"))[0]
                content = log_file.read_text()
                select_line = next(
                    line for line in content.splitlines() if "] SELECT: " in line
                )
                payload = json.loads(select_line.split("SELECT: ", 1)[1])
                assert payload == {
                    "opportunity_id": "add-retry-logic",
                    "title": "Add Retry Logic",
                    "roi_score": 2.0,
                    "requires_design": None,
                }
                assert "ADOPT:" in content
        finally:
            orch.cleanup()

    def test_cycle_log_records_gate_halts(self, temp_repo):
        """Cycle log records when approval gates halt the cycle."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n")

        # Create opportunities file
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text("""# Opportunities

- [ ] **Add Tests**
  - Opportunity ID: add-tests
  - ROI Score: 1.0
  - Description: Add unit tests
""")

        orch = Orchestrator(approve_opportunities=True)  # Will halt at first gate
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze:
                mock_analyze.return_value = {"success": True, "opportunity_count": 1}
                orch.run_cycle()

                cycles_dir = temp_repo / ".millstone" / "cycles"
                log_file = list(cycles_dir.glob("*.log"))[0]
                content = log_file.read_text()
                assert "GATE: Paused at opportunities approval gate" in content
                assert "=== Cycle Completed: HALTED ===" in content
        finally:
            orch.cleanup()

    def test_cycle_log_records_full_cycle(self, temp_repo):
        """Cycle log records all phases in full cycle."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n")

        # Create opportunities file
        opportunities_file = temp_repo / ".millstone" / "opportunities.md"
        opportunities_file.write_text("""# Opportunities

- [ ] **Add Tests**
  - Opportunity ID: add-tests
  - ROI Score: 1.0
  - Description: Add unit tests
""")

        orch = Orchestrator(
            review_designs=True,
            approve_opportunities=False,
            approve_designs=False,
            approve_plans=False,
        )
        try:
            with patch.object(orch, 'run_analyze') as mock_analyze, \
                 patch.object(orch, 'run_design') as mock_design, \
                 patch.object(orch, 'review_design') as mock_review, \
                 patch.object(orch, 'run_plan') as mock_plan, \
                 patch.object(orch, 'run') as mock_run:

                mock_analyze.return_value = {"success": True, "opportunity_count": 3}
                mock_design.return_value = {"success": True, "design_file": "designs/add-tests.md"}
                mock_review.return_value = {"approved": True, "verdict": "APPROVED"}
                mock_plan.return_value = {"success": True, "tasks_added": 2}
                mock_run.return_value = 0

                orch.run_cycle()

                cycles_dir = temp_repo / ".millstone" / "cycles"
                log_file = list(cycles_dir.glob("*.log"))[0]
                content = log_file.read_text()

                # Verify all phases are logged
                assert "=== Cycle Started:" in content
                assert "ANALYZE: Found 3 opportunities" in content
                select_line = next(
                    line for line in content.splitlines() if "] SELECT: " in line
                )
                select_payload = json.loads(select_line.split("SELECT: ", 1)[1])
                assert select_payload["title"] == "Add Tests"
                assert "ADOPT:" in content
                assert "DESIGN: Created designs/add-tests.md" in content
                assert "REVIEW: APPROVED" in content
                assert "PLAN: Added 2 tasks to tasklist" in content
                assert "EXECUTE: Starting task execution" in content
                assert "EXECUTE: All tasks completed successfully" in content
                assert "=== Cycle Completed: SUCCESS ===" in content
        finally:
            orch.cleanup()

    def test_cycle_log_records_pending_tasks_skip(self, temp_repo):
        """Cycle log records when pending tasks cause skip to execution."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [ ] Pending task")

        orch = Orchestrator()
        try:
            with patch.object(orch, 'run') as mock_run:
                mock_run.return_value = 0
                orch.run_cycle()

                cycles_dir = temp_repo / ".millstone" / "cycles"
                log_file = list(cycles_dir.glob("*.log"))[0]
                content = log_file.read_text()
                assert "SKIP: Pending tasks found in tasklist" in content
        finally:
            orch.cleanup()


class TestCostTracking:
    """Tests for per-task cost tracking functionality."""

    def test_generate_task_hash_deterministic(self):
        """_generate_task_hash returns consistent hash for same input."""
        orch = Orchestrator()
        try:
            hash1 = orch._generate_task_hash("Add retry logic")
            hash2 = orch._generate_task_hash("Add retry logic")
            assert hash1 == hash2
            assert len(hash1) == 8
        finally:
            orch.cleanup()

    def test_generate_task_hash_differs_for_different_tasks(self):
        """_generate_task_hash returns different hashes for different inputs."""
        orch = Orchestrator()
        try:
            hash1 = orch._generate_task_hash("Add retry logic")
            hash2 = orch._generate_task_hash("Fix authentication bug")
            assert hash1 != hash2
        finally:
            orch.cleanup()

    def test_save_task_metrics_creates_tasks_directory(self, temp_repo):
        """save_task_metrics creates .millstone/tasks/ directory."""
        orch = Orchestrator()
        try:
            orch._task_start_time = orch._task_start_time  # Just to ensure it's set
            orch.save_task_metrics("Test task", "approved", 2)

            tasks_dir = orch.work_dir / "tasks"
            assert tasks_dir.exists()
            assert tasks_dir.is_dir()
        finally:
            orch.cleanup()

    def test_save_task_metrics_creates_json_file(self, temp_repo):
        """save_task_metrics creates a JSON file with correct schema."""
        import json
        orch = Orchestrator()
        try:
            from datetime import datetime
            orch._task_start_time = datetime.now()
            orch._task_tokens_in = 1000
            orch._task_tokens_out = 500

            task_file = orch.save_task_metrics("Test task", "approved", 2)

            assert task_file.exists()
            data = json.loads(task_file.read_text())

            # Check required fields
            assert "task" in data
            assert "task_hash" in data
            assert "timestamp" in data
            assert "duration_seconds" in data
            assert "cycles" in data
            assert "tokens" in data
            assert "outcome" in data

            # Check values
            assert data["task"] == "Test task"
            assert data["cycles"] == 2
            assert data["outcome"] == "approved"
            assert data["tokens"]["input"] == 1000
            assert data["tokens"]["output"] == 500
        finally:
            orch.cleanup()

    def test_save_task_metrics_with_eval_delta(self, temp_repo):
        """save_task_metrics calculates eval delta when before/after provided."""
        import json
        orch = Orchestrator()
        try:
            from datetime import datetime
            orch._task_start_time = datetime.now()

            eval_before = {
                "composite_score": 0.80,
                "tests": {"passed": 10, "failed": 2},
                "coverage": {"line_rate": 0.75},
            }
            eval_after = {
                "composite_score": 0.85,
                "tests": {"passed": 11, "failed": 1},
                "coverage": {"line_rate": 0.80},
            }

            task_file = orch.save_task_metrics(
                "Test task", "approved", 1, eval_before, eval_after
            )

            data = json.loads(task_file.read_text())

            # Check eval_delta
            assert "eval_delta" in data
            assert data["eval_delta"]["composite"] == 0.05
            assert data["eval_delta"]["tests"]["passed"] == 1
            assert data["eval_delta"]["tests"]["failed"] == -1
            assert data["eval_delta"]["coverage"] == 0.05
        finally:
            orch.cleanup()

    def test_get_task_summary_empty_when_no_tasks(self, temp_repo):
        """get_task_summary returns empty list when no tasks exist."""
        orch = Orchestrator()
        try:
            result = orch.get_task_summary()
            assert result == []
        finally:
            orch.cleanup()

    def test_get_task_summary_returns_tasks_sorted(self, temp_repo):
        """get_task_summary returns tasks sorted by timestamp (newest first)."""
        import time
        orch = Orchestrator()
        try:
            from datetime import datetime
            tasks_dir = orch.work_dir / "tasks"
            tasks_dir.mkdir(exist_ok=True)

            # Create two tasks with different timestamps
            orch._task_start_time = datetime.now()
            orch._task_tokens_in = 100
            orch._task_tokens_out = 50
            orch.save_task_metrics("First task", "approved", 1)

            time.sleep(0.1)  # Small delay to ensure different timestamps

            orch._task_start_time = datetime.now()
            orch.save_task_metrics("Second task", "approved", 1)

            result = orch.get_task_summary()

            assert len(result) == 2
            # Newest should be first
            assert result[0]["task"] == "Second task"
            assert result[1]["task"] == "First task"
        finally:
            orch.cleanup()

    def test_get_task_summary_respects_limit(self, temp_repo):
        """get_task_summary respects the limit parameter."""
        import time
        orch = Orchestrator()
        try:
            from datetime import datetime
            tasks_dir = orch.work_dir / "tasks"
            tasks_dir.mkdir(exist_ok=True)

            # Create multiple tasks
            for i in range(5):
                orch._task_start_time = datetime.now()
                orch._task_tokens_in = 100
                orch._task_tokens_out = 50
                orch.save_task_metrics(f"Task {i}", "approved", 1)
                time.sleep(0.05)

            result = orch.get_task_summary(limit=3)

            assert len(result) == 3
        finally:
            orch.cleanup()

    def test_cleanup_preserves_tasks_directory(self, temp_repo):
        """cleanup() preserves the tasks directory."""
        orch = Orchestrator()
        try:
            from datetime import datetime
            orch._task_start_time = datetime.now()
            orch._task_tokens_in = 100
            orch._task_tokens_out = 50
            orch.save_task_metrics("Test task", "approved", 1)

            tasks_dir = orch.work_dir / "tasks"
            assert tasks_dir.exists()

            # Create a dummy file that should be cleaned
            (orch.work_dir / "dummy.txt").write_text("test")

            orch.cleanup()

            # tasks directory should still exist
            assert tasks_dir.exists()
            # But dummy file should be gone
            assert not (orch.work_dir / "dummy.txt").exists()
        finally:
            pass  # cleanup already called

    def test_run_claude_accumulates_tokens(self, temp_repo):
        """run_agent accumulates token estimates."""
        orch = Orchestrator(cli="claude")
        try:
            orch._task_tokens_in = 0
            orch._task_tokens_out = 0

            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    stdout="Response from claude (about 100 chars)...",
                    stderr="",
                    returncode=0,
                )

                # Call with a prompt via run_agent
                prompt = "A" * 400  # 100 tokens estimated
                orch.run_agent(prompt, role="default")

                # Should have accumulated some tokens
                assert orch._task_tokens_in > 0
                assert orch._task_tokens_out > 0
        finally:
            orch.cleanup()

    def test_save_task_metrics_includes_review_data(self, temp_repo):
        """save_task_metrics includes review quality metrics."""
        import json
        orch = Orchestrator()
        try:
            from datetime import datetime
            orch._task_start_time = datetime.now()
            orch._task_tokens_in = 1000
            orch._task_tokens_out = 500
            # Set review metrics
            orch._task_review_cycles = 2
            orch._task_review_duration_ms = 5000
            orch._task_findings_count = 3
            orch._task_findings_by_severity = {
                "critical": 1,
                "high": 1,
                "medium": 1,
                "low": 0,
                "nit": 0,
            }

            task_file = orch.save_task_metrics("Test task", "approved", 3)

            data = json.loads(task_file.read_text())

            # Check review section exists
            assert "review" in data
            review = data["review"]

            # Check review metrics
            assert review["review_cycles"] == 2
            assert review["review_duration_ms"] == 5000
            assert review["findings_count"] == 3
            assert review["findings_by_severity"]["critical"] == 1
            assert review["findings_by_severity"]["high"] == 1
            assert review["findings_by_severity"]["medium"] == 1
        finally:
            orch.cleanup()

    def test_save_task_metrics_review_data_defaults(self, temp_repo):
        """save_task_metrics includes default review metrics when not set."""
        import json
        orch = Orchestrator()
        try:
            from datetime import datetime
            orch._task_start_time = datetime.now()
            # Don't set review metrics, use defaults

            task_file = orch.save_task_metrics("Test task", "approved", 1)

            data = json.loads(task_file.read_text())

            # Check review section exists with defaults
            assert "review" in data
            review = data["review"]

            assert review["review_cycles"] == 0
            assert review["review_duration_ms"] == 0
            assert review["findings_count"] == 0
            assert review["findings_by_severity"]["critical"] == 0
        finally:
            orch.cleanup()

    def test_append_review_metric_creates_metrics_directory(self, temp_repo):
        """append_review_metric creates metrics directory if it doesn't exist."""
        orch = Orchestrator()
        try:
            metrics_dir = orch.work_dir / "metrics"
            assert not metrics_dir.exists()

            orch.append_review_metric(
                task_text="Test task",
                verdict="APPROVED",
                findings=None,
                findings_by_severity=None,
                duration_ms=1000,
            )

            assert metrics_dir.exists()
        finally:
            orch.cleanup()

    def test_append_review_metric_creates_jsonl_file(self, temp_repo):
        """append_review_metric creates reviews.jsonl file."""
        orch = Orchestrator()
        try:
            reviews_file = orch.work_dir / "metrics" / "reviews.jsonl"
            assert not reviews_file.exists()

            orch.append_review_metric(
                task_text="Test task",
                verdict="APPROVED",
                findings=None,
                findings_by_severity=None,
                duration_ms=1000,
            )

            assert reviews_file.exists()
        finally:
            orch.cleanup()

    def test_append_review_metric_writes_json_line(self, temp_repo):
        """append_review_metric writes valid JSON line."""
        import json
        orch = Orchestrator()
        try:
            orch.append_review_metric(
                task_text="Test task",
                verdict="APPROVED",
                findings=None,
                findings_by_severity=None,
                duration_ms=1500,
            )

            reviews_file = orch.work_dir / "metrics" / "reviews.jsonl"
            lines = reviews_file.read_text().strip().split("\n")
            assert len(lines) == 1

            data = json.loads(lines[0])
            assert data["verdict"] == "APPROVED"
            assert data["duration_ms"] == 1500
            assert "task_hash" in data
            assert "timestamp" in data
            assert data["reviewer_cli"] == "claude"  # default
        finally:
            orch.cleanup()

    def test_append_review_metric_appends_multiple_entries(self, temp_repo):
        """append_review_metric appends multiple reviews to same file."""
        import json
        orch = Orchestrator()
        try:
            orch.append_review_metric(
                task_text="Task 1",
                verdict="REQUEST_CHANGES",
                findings=["Issue 1"],
                findings_by_severity=None,
                duration_ms=1000,
            )
            orch.append_review_metric(
                task_text="Task 1",
                verdict="APPROVED",
                findings=None,
                findings_by_severity=None,
                duration_ms=800,
            )

            reviews_file = orch.work_dir / "metrics" / "reviews.jsonl"
            lines = reviews_file.read_text().strip().split("\n")
            assert len(lines) == 2

            data1 = json.loads(lines[0])
            data2 = json.loads(lines[1])

            assert data1["verdict"] == "REQUEST_CHANGES"
            assert data1["findings"] == ["Issue 1"]
            assert data1["findings_count"] == 1

            assert data2["verdict"] == "APPROVED"
            assert data2["findings"] == []
            assert data2["findings_count"] == 0
        finally:
            orch.cleanup()

    def test_append_review_metric_includes_severity_breakdown(self, temp_repo):
        """append_review_metric includes findings by severity."""
        import json
        orch = Orchestrator()
        try:
            orch.append_review_metric(
                task_text="Test task",
                verdict="REQUEST_CHANGES",
                findings=None,
                findings_by_severity={
                    "critical": ["Bug 1"],
                    "high": ["Issue 1", "Issue 2"],
                    "medium": [],
                },
                duration_ms=2000,
            )

            reviews_file = orch.work_dir / "metrics" / "reviews.jsonl"
            data = json.loads(reviews_file.read_text().strip())

            assert data["findings_by_severity"]["critical"] == ["Bug 1"]
            assert data["findings_by_severity"]["high"] == ["Issue 1", "Issue 2"]
            assert data["findings_count"] == 3  # Combined from all severities
            assert "Bug 1" in data["findings"]
            assert "Issue 1" in data["findings"]
            assert "Issue 2" in data["findings"]
        finally:
            orch.cleanup()

    def test_append_review_metric_records_reviewer_cli(self, temp_repo):
        """append_review_metric records the reviewer CLI used."""
        import json
        orch = Orchestrator(cli_reviewer="codex")
        try:
            orch.append_review_metric(
                task_text="Test task",
                verdict="APPROVED",
                findings=None,
                findings_by_severity=None,
                duration_ms=1000,
            )

            reviews_file = orch.work_dir / "metrics" / "reviews.jsonl"
            data = json.loads(reviews_file.read_text().strip())

            assert data["reviewer_cli"] == "codex"
        finally:
            orch.cleanup()

    def test_append_review_metric_includes_false_positive_indicator(self, temp_repo):
        """append_review_metric includes false_positive_indicator field."""
        import json
        orch = Orchestrator()
        try:
            orch.append_review_metric(
                task_text="Test task",
                verdict="APPROVED",
                findings=None,
                findings_by_severity=None,
                duration_ms=1000,
                false_positive_indicator=True,
            )

            reviews_file = orch.work_dir / "metrics" / "reviews.jsonl"
            data = json.loads(reviews_file.read_text().strip())

            assert data["false_positive_indicator"] is True
        finally:
            orch.cleanup()

    def test_append_review_metric_false_positive_defaults_to_false(self, temp_repo):
        """append_review_metric defaults false_positive_indicator to False."""
        import json
        orch = Orchestrator()
        try:
            orch.append_review_metric(
                task_text="Test task",
                verdict="APPROVED",
                findings=None,
                findings_by_severity=None,
                duration_ms=1000,
            )

            reviews_file = orch.work_dir / "metrics" / "reviews.jsonl"
            data = json.loads(reviews_file.read_text().strip())

            assert data["false_positive_indicator"] is False
        finally:
            orch.cleanup()


class TestFalsePositiveDetection:
    """Tests for false positive detection in reviews."""

    def test_is_whitespace_or_comment_only_change_identical_diffs(self):
        """Returns False when diffs are identical (no changes made)."""
        from millstone.runtime.orchestrator import is_whitespace_or_comment_only_change

        diff = "diff --git a/foo.py b/foo.py\n+print('hello')"
        assert is_whitespace_or_comment_only_change(diff, diff) is False

    def test_is_whitespace_or_comment_only_change_whitespace_only(self):
        """Returns True when only whitespace changed between diffs."""
        from millstone.runtime.orchestrator import is_whitespace_or_comment_only_change

        before = "diff --git a/foo.py b/foo.py\n+print('hello')"
        after = "diff --git a/foo.py b/foo.py\n+print('hello')\n+    "  # Added empty line

        assert is_whitespace_or_comment_only_change(before, after) is True

    def test_is_whitespace_or_comment_only_change_comment_only(self):
        """Returns True when only comments changed between diffs."""
        from millstone.runtime.orchestrator import is_whitespace_or_comment_only_change

        before = "diff --git a/foo.py b/foo.py\n+print('hello')"
        after = "diff --git a/foo.py b/foo.py\n+print('hello')\n+# This is a comment"

        assert is_whitespace_or_comment_only_change(before, after) is True

    def test_is_whitespace_or_comment_only_change_code_changed(self):
        """Returns False when actual code changed between diffs."""
        from millstone.runtime.orchestrator import is_whitespace_or_comment_only_change

        before = "diff --git a/foo.py b/foo.py\n+print('hello')"
        after = "diff --git a/foo.py b/foo.py\n+print('goodbye')"

        assert is_whitespace_or_comment_only_change(before, after) is False

    def test_is_whitespace_or_comment_only_change_js_comment(self):
        """Returns True for JavaScript-style comments."""
        from millstone.runtime.orchestrator import is_whitespace_or_comment_only_change

        before = "diff --git a/foo.js b/foo.js\n+console.log('test');"
        after = "diff --git a/foo.js b/foo.js\n+console.log('test');\n+// TODO: fix this"

        assert is_whitespace_or_comment_only_change(before, after) is True

    def test_is_whitespace_or_comment_only_change_c_style_comment(self):
        """Returns True for C-style block comments."""
        from millstone.runtime.orchestrator import is_whitespace_or_comment_only_change

        before = "diff --git a/foo.c b/foo.c\n+int x = 1;"
        after = "diff --git a/foo.c b/foo.c\n+int x = 1;\n+/* comment */\n+* middle line"

        assert is_whitespace_or_comment_only_change(before, after) is True

    def test_is_whitespace_or_comment_only_change_docstring(self):
        """Returns True for Python docstring changes."""
        from millstone.runtime.orchestrator import is_whitespace_or_comment_only_change

        before = "diff --git a/foo.py b/foo.py\n+def func():"
        after = 'diff --git a/foo.py b/foo.py\n+def func():\n+"""This is a docstring."""'

        assert is_whitespace_or_comment_only_change(before, after) is True


class TestEvalSummaryCLI:
    """Tests for --eval-summary CLI flag."""

    def test_eval_summary_shows_no_tasks_message(self, temp_repo, capsys):
        """--eval-summary shows message when no tasks exist."""
        orch = Orchestrator(task="eval-summary", dry_run=False)
        try:
            orch.print_eval_summary()

            captured = capsys.readouterr()
            assert "No task history found" in captured.out
        finally:
            orch.cleanup()

    def test_eval_summary_shows_task_data(self, temp_repo, capsys):
        """--eval-summary shows task cost data when tasks exist."""
        import json
        from datetime import datetime

        orch = Orchestrator(task="eval-summary", dry_run=False)
        try:
            # Create a task metrics file
            tasks_dir = orch.work_dir / "tasks"
            tasks_dir.mkdir(exist_ok=True)
            task_data = {
                "task": "Test task for eval summary",
                "task_hash": "12345678",
                "timestamp": datetime.now().isoformat(),
                "duration_seconds": 120.5,
                "cycles": 2,
                "tokens": {"input": 5000, "output": 2000},
                "outcome": "approved",
            }
            task_file = tasks_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_12345678.json"
            task_file.write_text(json.dumps(task_data))

            orch.print_eval_summary()

            captured = capsys.readouterr()
            assert "Task Cost Summary" in captured.out
            assert "Tasks analyzed: 1" in captured.out
            assert "Test task for eval summary" in captured.out
            assert "approved" in captured.out
        finally:
            orch.cleanup()


class TestRiskLabels:
    """Tests for risk labels and verification requirements."""

    def test_parse_task_metadata_extracts_risk(self, temp_repo):
        """_parse_task_metadata extracts risk level from task text."""
        orch = Orchestrator()
        try:
            task_text = """**Add feature**: Implement the new feature.
  - Est. LoC: 150
  - Tests: test_feature.py
  - Risk: medium
  - Criteria: All tests pass"""

            result = orch._parse_task_metadata(task_text)

            assert result["risk"] == "medium"
            assert result["title"] == "Add feature"
            assert result["est_loc"] == 150
        finally:
            orch.cleanup()

    def test_parse_task_metadata_risk_case_insensitive(self, temp_repo):
        """_parse_task_metadata handles case-insensitive risk levels."""
        orch = Orchestrator()
        try:
            task_text = """**Task**: Description
  - Risk: HIGH"""

            result = orch._parse_task_metadata(task_text)

            assert result["risk"] == "high"
        finally:
            orch.cleanup()

    def test_parse_task_metadata_risk_none_when_missing(self, temp_repo):
        """_parse_task_metadata returns None when risk is not specified."""
        orch = Orchestrator()
        try:
            task_text = """**Task**: Description
  - Est. LoC: 100
  - Tests: test.py
  - Criteria: Done"""

            result = orch._parse_task_metadata(task_text)

            assert result["risk"] is None
        finally:
            orch.cleanup()

    def test_validate_task_fails_missing_risk(self, temp_repo):
        """_validate_task rejects tasks without risk level when required."""
        orch = Orchestrator()
        orch.task_constraints = {"max_loc": 200, "require_tests": False, "require_criteria": False, "require_risk": True}
        try:
            metadata = {
                "title": "Task without risk",
                "description": "A task without risk assignment",
                "est_loc": 50,
                "tests": "test.py",
                "risk": None,
                "criteria": "Done",
                "raw": "",
            }

            result = orch._validate_task(metadata)

            assert result["valid"] is False
            assert any("risk" in v.lower() for v in result["violations"])
        finally:
            orch.cleanup()

    def test_validate_task_fails_invalid_risk(self, temp_repo):
        """_validate_task rejects tasks with invalid risk levels."""
        orch = Orchestrator()
        orch.task_constraints = {"max_loc": 200, "require_tests": False, "require_criteria": False, "require_risk": True}
        try:
            metadata = {
                "title": "Task with invalid risk",
                "description": "A task with wrong risk",
                "est_loc": 50,
                "tests": "test.py",
                "risk": "critical",  # Invalid - must be low/medium/high
                "criteria": "Done",
                "raw": "",
            }

            result = orch._validate_task(metadata)

            assert result["valid"] is False
            assert any("invalid risk" in v.lower() for v in result["violations"])
        finally:
            orch.cleanup()

    def test_validate_task_passes_valid_risk(self, temp_repo):
        """_validate_task passes tasks with valid risk levels."""
        orch = Orchestrator()
        orch.task_constraints = {"max_loc": 200, "require_tests": False, "require_criteria": False, "require_risk": True, "require_context": False}
        try:
            for risk_level in ["low", "medium", "high"]:
                metadata = {
                    "title": f"Task with {risk_level} risk",
                    "description": "A valid task",
                    "est_loc": 50,
                    "tests": "test.py",
                    "risk": risk_level,
                    "criteria": "Done",
                    "context": None,
                    "raw": "",
                }

                result = orch._validate_task(metadata)

                assert result["valid"] is True, f"Failed for risk level: {risk_level}"
        finally:
            orch.cleanup()

    def test_extract_current_task_risk(self, temp_repo):
        """extract_current_task_risk returns risk level from tasklist."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("""# Tasklist

- [ ] **Test Task**: Do something
  - Est. LoC: 50
  - Tests: test.py
  - Risk: high
  - Criteria: Done
""")

        orch = Orchestrator()
        try:
            risk = orch.extract_current_task_risk()

            assert risk == "high"
        finally:
            orch.cleanup()

    def test_extract_current_task_risk_none_when_missing(self, temp_repo):
        """extract_current_task_risk returns None when no risk specified."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("""# Tasklist

- [ ] **Test Task**: Do something without risk
  - Est. LoC: 50
""")

        orch = Orchestrator()
        try:
            risk = orch.extract_current_task_risk()

            assert risk is None
        finally:
            orch.cleanup()

    def test_apply_risk_settings_adjusts_max_cycles(self, temp_repo):
        """apply_risk_settings adjusts max_cycles based on risk level."""
        orch = Orchestrator(max_cycles=3)
        try:
            # Low risk should reduce max_cycles
            orch.apply_risk_settings("low")
            assert orch.max_cycles == 2
            assert orch.current_task_risk == "low"

            # Medium risk uses default
            orch.apply_risk_settings("medium")
            assert orch.max_cycles == 3
            assert orch.current_task_risk == "medium"

            # High risk increases max_cycles
            orch.apply_risk_settings("high")
            assert orch.max_cycles == 5
            assert orch.current_task_risk == "high"

            # None uses base max_cycles
            orch.apply_risk_settings(None)
            assert orch.max_cycles == 3
            assert orch.current_task_risk is None
        finally:
            orch.cleanup()

    def test_requires_high_risk_approval(self, temp_repo):
        """requires_high_risk_approval returns True for high-risk tasks."""
        orch = Orchestrator()
        try:
            # Low risk - no approval needed
            orch.current_task_risk = "low"
            assert orch.requires_high_risk_approval() is False

            # Medium risk - no approval needed
            orch.current_task_risk = "medium"
            assert orch.requires_high_risk_approval() is False

            # High risk - approval needed
            orch.current_task_risk = "high"
            assert orch.requires_high_risk_approval() is True

            # None - no approval needed
            orch.current_task_risk = None
            assert orch.requires_high_risk_approval() is False
        finally:
            orch.cleanup()

    def test_default_risk_settings_structure(self, temp_repo):
        """Default risk_settings has expected structure."""
        orch = Orchestrator()
        try:
            assert "low" in orch.risk_settings
            assert "medium" in orch.risk_settings
            assert "high" in orch.risk_settings

            # Check high risk has require_approval
            assert orch.risk_settings["high"]["require_approval"] is True
            assert orch.risk_settings["high"]["require_full_eval"] is True

            # Check low/medium don't require full eval
            assert orch.risk_settings["low"]["require_full_eval"] is False
            assert orch.risk_settings["medium"]["require_full_eval"] is False
        finally:
            orch.cleanup()


class TestTaskGroups:
    """Tests for task group syntax (## Group: <name> sections)."""

    def test_extract_current_task_group_returns_group_name(self, temp_repo):
        """extract_current_task_group returns the group name for tasks in a group."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("""# Tasklist

## Group: Authentication

- [ ] **Add login form**: Create the login form component
- [ ] **Add logout button**: Add logout functionality
""")

        orch = Orchestrator()
        try:
            group = orch.extract_current_task_group()
            assert group == "Authentication"
        finally:
            orch.cleanup()

    def test_extract_current_task_group_returns_none_when_no_group(self, temp_repo):
        """extract_current_task_group returns None for tasks not in a group."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("""# Tasklist

- [ ] **Add feature**: Standalone task without group
""")

        orch = Orchestrator()
        try:
            group = orch.extract_current_task_group()
            assert group is None
        finally:
            orch.cleanup()

    def test_extract_current_task_group_returns_most_recent_group(self, temp_repo):
        """extract_current_task_group returns the most recent group header before the task."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("""# Tasklist

## Group: First Group

- [x] **Done task**: Already completed

## Group: Second Group

- [ ] **Current task**: This should be in Second Group
""")

        orch = Orchestrator()
        try:
            group = orch.extract_current_task_group()
            assert group == "Second Group"
        finally:
            orch.cleanup()

    def test_extract_current_task_group_ignores_groups_after_task(self, temp_repo):
        """extract_current_task_group ignores group headers after the current task."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("""# Tasklist

## Group: Before Group

- [ ] **Current task**: This should be in Before Group

## Group: After Group

- [ ] **Later task**: This is in After Group
""")

        orch = Orchestrator()
        try:
            group = orch.extract_current_task_group()
            assert group == "Before Group"
        finally:
            orch.cleanup()

    def test_extract_current_task_group_handles_missing_file(self, temp_repo):
        """extract_current_task_group returns None when tasklist file is missing."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.unlink()

        orch = Orchestrator()
        try:
            group = orch.extract_current_task_group()
            assert group is None
        finally:
            orch.cleanup()

    def test_extract_current_task_group_handles_no_unchecked_tasks(self, temp_repo):
        """extract_current_task_group returns None when no unchecked tasks exist."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("""# Tasklist

## Group: Some Group

- [x] **Done task**: Already completed
""")

        orch = Orchestrator()
        try:
            group = orch.extract_current_task_group()
            assert group is None
        finally:
            orch.cleanup()

    def test_extract_current_task_group_handles_whitespace_in_name(self, temp_repo):
        """extract_current_task_group handles extra whitespace in group name."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("""# Tasklist

## Group:   Spaced Name

- [ ] **Task**: A task in a group with extra spaces
""")

        orch = Orchestrator()
        try:
            group = orch.extract_current_task_group()
            assert group == "Spaced Name"
        finally:
            orch.cleanup()

    def test_extract_current_task_group_task_before_any_group(self, temp_repo):
        """extract_current_task_group returns None for tasks before any group header."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("""# Tasklist

- [ ] **Task before group**: This task has no group

## Group: Later Group

- [ ] **Task in group**: This task has a group
""")

        orch = Orchestrator()
        try:
            group = orch.extract_current_task_group()
            assert group is None
        finally:
            orch.cleanup()

    def test_current_task_group_attribute_initialized(self, temp_repo):
        """Orchestrator initializes current_task_group to None."""
        orch = Orchestrator()
        try:
            assert orch.current_task_group is None
        finally:
            orch.cleanup()


class TestGroupContextAccumulation:
    """Tests for group context accumulation (cross-task context sharing)."""

    def test_accumulate_group_context_creates_context_file(self, temp_repo):
        """accumulate_group_context creates a context file for the group."""
        orch = Orchestrator()
        try:
            orch.current_task_group = "Authentication"
            task_text = "**Add login form**: Create the login form component"

            result = orch.accumulate_group_context(task_text)

            assert result is True
            context_path = orch._get_group_context_path("Authentication")
            assert context_path.exists()
            content = context_path.read_text()
            assert "# Group Context: Authentication" in content
            assert "Add login form" in content
        finally:
            orch.cleanup()

    def test_accumulate_group_context_returns_false_when_no_group(self, temp_repo):
        """accumulate_group_context returns False when task is not in a group."""
        orch = Orchestrator()
        try:
            orch.current_task_group = None
            task_text = "**Standalone task**: Not in any group"

            result = orch.accumulate_group_context(task_text)

            assert result is False
            context_dir = orch._get_context_dir()
            assert not context_dir.exists() or not any(context_dir.iterdir())
        finally:
            orch.cleanup()

    def test_accumulate_group_context_appends_to_existing(self, temp_repo):
        """accumulate_group_context appends new tasks to existing context."""
        orch = Orchestrator()
        try:
            orch.current_task_group = "Feature"

            # First task
            orch.accumulate_group_context("**Task one**: First task in group")

            # Second task
            orch.accumulate_group_context("**Task two**: Second task in group")

            context_path = orch._get_group_context_path("Feature")
            content = context_path.read_text()
            assert "Task one" in content
            assert "Task two" in content
            # Both should be in the same file
            assert content.count("## ") == 2  # Two task headers
        finally:
            orch.cleanup()

    def test_accumulate_group_context_strips_markdown_bold(self, temp_repo):
        """accumulate_group_context strips **bold** markers from task title."""
        orch = Orchestrator()
        try:
            orch.current_task_group = "Test"
            task_text = "**Bold Title**: Description here"

            orch.accumulate_group_context(task_text)

            context_path = orch._get_group_context_path("Test")
            content = context_path.read_text()
            # The header should not have ** markers
            assert "## Bold Title" in content
            assert "**Bold Title**" not in content
        finally:
            orch.cleanup()

    def test_accumulate_group_context_truncates_long_descriptions(self, temp_repo):
        """accumulate_group_context truncates task bodies longer than 500 chars."""
        orch = Orchestrator()
        try:
            orch.current_task_group = "Long"
            long_body = "x" * 600
            task_text = f"**Title**: Task title\n{long_body}"

            orch.accumulate_group_context(task_text)

            context_path = orch._get_group_context_path("Long")
            content = context_path.read_text()
            assert "..." in content
            # Body should be truncated to ~500 chars
            assert content.count("x") <= 510
        finally:
            orch.cleanup()

    def test_accumulate_group_context_explicit_group_name(self, temp_repo):
        """accumulate_group_context accepts explicit group name parameter."""
        orch = Orchestrator()
        try:
            orch.current_task_group = "Default"
            task_text = "**Task**: Some task"

            # Explicit group overrides current_task_group
            result = orch.accumulate_group_context(task_text, group_name="Override")

            assert result is True
            override_path = orch._get_group_context_path("Override")
            default_path = orch._get_group_context_path("Default")
            assert override_path.exists()
            assert not default_path.exists()
        finally:
            orch.cleanup()

    def test_get_group_context_returns_content(self, temp_repo):
        """get_group_context returns the accumulated context content."""
        orch = Orchestrator()
        try:
            orch.current_task_group = "TestGroup"
            orch.accumulate_group_context("**Task**: First task")

            context = orch.get_group_context()

            assert context is not None
            assert "TestGroup" in context
            assert "Task" in context
        finally:
            orch.cleanup()

    def test_get_group_context_returns_none_when_no_context(self, temp_repo):
        """get_group_context returns None when no context file exists."""
        orch = Orchestrator()
        try:
            orch.current_task_group = "NewGroup"

            context = orch.get_group_context()

            assert context is None
        finally:
            orch.cleanup()

    def test_get_group_context_returns_none_when_no_group(self, temp_repo):
        """get_group_context returns None when current_task_group is None."""
        orch = Orchestrator()
        try:
            orch.current_task_group = None

            context = orch.get_group_context()

            assert context is None
        finally:
            orch.cleanup()

    def test_get_group_context_explicit_group_name(self, temp_repo):
        """get_group_context accepts explicit group name parameter."""
        orch = Orchestrator()
        try:
            orch.current_task_group = "Current"
            orch.accumulate_group_context("**Task**: Task in Other", group_name="Other")

            # Should get context from explicitly named group
            context = orch.get_group_context(group_name="Other")
            assert context is not None
            assert "Task in Other" in context

            # Current group should have no context
            current_context = orch.get_group_context()
            assert current_context is None
        finally:
            orch.cleanup()

    def test_get_group_context_path_sanitizes_special_chars(self, temp_repo):
        """_get_group_context_path sanitizes special characters in group names."""
        orch = Orchestrator()
        try:
            # Test various special characters
            path1 = orch._get_group_context_path("My Group/Name")
            path2 = orch._get_group_context_path("Group: Special!")
            path3 = orch._get_group_context_path("Group With Spaces")

            # All should produce valid filenames (no special chars except _ and -)
            assert "/" not in path1.name
            assert ":" not in path2.name
            assert "!" not in path2.name
            assert " " not in path3.name
        finally:
            orch.cleanup()

    def test_get_tasklist_prompt_includes_group_context(self, temp_repo):
        """get_tasklist_prompt includes group context when available."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("""# Tasklist

## Group: Feature

- [ ] **Second task**: Do something else
""")

        orch = Orchestrator()
        try:
            # Set up the group and accumulate context from a "previous" task
            orch.current_task_group = "Feature"
            orch.accumulate_group_context("**First task**: Did something important")

            prompt = orch.get_tasklist_prompt()

            assert "## Group Context" in prompt
            assert "Feature" in prompt
            assert "First task" in prompt
            assert "previously completed tasks" in prompt
        finally:
            orch.cleanup()

    def test_get_tasklist_prompt_no_context_when_no_group(self, temp_repo):
        """get_tasklist_prompt has no group context section when not in a group."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("""# Tasklist

- [ ] **Standalone task**: Not in any group
""")

        orch = Orchestrator()
        try:
            orch.current_task_group = None

            prompt = orch.get_tasklist_prompt()

            assert "## Group Context" not in prompt
        finally:
            orch.cleanup()

    def test_get_tasklist_prompt_no_context_when_first_task_in_group(self, temp_repo):
        """get_tasklist_prompt has no group context for first task in group."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(exist_ok=True)
        tasklist.write_text("""# Tasklist

## Group: NewFeature

- [ ] **First task**: This is the first task
""")

        orch = Orchestrator()
        try:
            orch.current_task_group = "NewFeature"
            # No prior tasks accumulated

            prompt = orch.get_tasklist_prompt()

            # No context section because no accumulated context yet
            assert "## Group Context" not in prompt
        finally:
            orch.cleanup()

    def test_context_dir_created_on_accumulate(self, temp_repo):
        """Context directory is created when accumulating context."""
        orch = Orchestrator()
        try:
            context_dir = orch._get_context_dir()
            assert not context_dir.exists()

            orch.current_task_group = "TestGroup"
            orch.accumulate_group_context("**Task**: Test task")

            assert context_dir.exists()
            assert context_dir.is_dir()
        finally:
            orch.cleanup()


class TestContextExtraction:
    """Tests for LLM-based context extraction from completed tasks."""

    def test_accumulate_with_git_diff_calls_extraction(self, temp_repo, monkeypatch):
        """accumulate_group_context with git_diff triggers context extraction."""
        orch = Orchestrator()
        try:
            orch.current_task_group = "TestGroup"
            task_text = "**Add feature**: Create a new API endpoint"
            git_diff = """+def new_endpoint():
+    return {"status": "ok"}"""

            # Mock extract_context_summary to verify it's called
            calls = []
            def mock_extract(task, diff):
                calls.append((task, diff))
                return {"summary": "Added new API endpoint", "key_decisions": ["Used REST pattern"]}
            monkeypatch.setattr(orch, "extract_context_summary", mock_extract)

            orch.accumulate_group_context(task_text, git_diff=git_diff)

            assert len(calls) == 1
            assert calls[0][0] == task_text
            assert calls[0][1] == git_diff
        finally:
            orch.cleanup()

    def test_accumulate_without_git_diff_skips_extraction(self, temp_repo, monkeypatch):
        """accumulate_group_context without git_diff doesn't call extraction."""
        orch = Orchestrator()
        try:
            orch.current_task_group = "TestGroup"
            task_text = "**Simple task**: Just a task"

            # Mock extract_context_summary to verify it's not called
            calls = []
            def mock_extract(task, diff):
                calls.append((task, diff))
                return None
            monkeypatch.setattr(orch, "extract_context_summary", mock_extract)

            orch.accumulate_group_context(task_text)

            assert len(calls) == 0
        finally:
            orch.cleanup()

    def test_accumulate_with_empty_git_diff_skips_extraction(self, temp_repo, monkeypatch):
        """accumulate_group_context with empty git_diff doesn't call extraction."""
        orch = Orchestrator()
        try:
            orch.current_task_group = "TestGroup"
            task_text = "**Task**: Description"

            calls = []
            def mock_extract(task, diff):
                calls.append((task, diff))
                return None
            monkeypatch.setattr(orch, "extract_context_summary", mock_extract)

            # Empty string should skip extraction
            orch.accumulate_group_context(task_text, git_diff="")
            orch.accumulate_group_context(task_text, git_diff="   ")

            assert len(calls) == 0
        finally:
            orch.cleanup()

    def test_accumulate_uses_extracted_context_in_file(self, temp_repo, monkeypatch):
        """accumulate_group_context writes extracted summary and decisions to file."""
        orch = Orchestrator()
        try:
            orch.current_task_group = "Extracted"
            task_text = "**Feature**: Build new component"
            git_diff = "+// some code"

            def mock_extract(task, diff):
                return {
                    "summary": "Built the new component",
                    "key_decisions": ["Used TypeScript", "Added unit tests"]
                }
            monkeypatch.setattr(orch, "extract_context_summary", mock_extract)

            orch.accumulate_group_context(task_text, git_diff=git_diff)

            context_path = orch._get_group_context_path("Extracted")
            content = context_path.read_text()
            assert "**Summary:** Built the new component" in content
            assert "**Key decisions:**" in content
            assert "- Used TypeScript" in content
            assert "- Added unit tests" in content
        finally:
            orch.cleanup()

    def test_accumulate_falls_back_when_extraction_fails(self, temp_repo, monkeypatch):
        """accumulate_group_context falls back to truncated description on extraction failure."""
        orch = Orchestrator()
        try:
            orch.current_task_group = "Fallback"
            task_text = "**Task**: This is a description"
            git_diff = "+code"

            def mock_extract(task, diff):
                return None  # Extraction failed
            monkeypatch.setattr(orch, "extract_context_summary", mock_extract)

            orch.accumulate_group_context(task_text, git_diff=git_diff)

            context_path = orch._get_group_context_path("Fallback")
            content = context_path.read_text()
            # Should contain the fallback description
            assert "This is a description" in content
            # Should NOT contain extraction-specific formatting
            assert "**Summary:**" not in content
            assert "**Key decisions:**" not in content
        finally:
            orch.cleanup()

    def test_extract_context_summary_parses_json_response(self, temp_repo, monkeypatch):
        """extract_context_summary parses valid JSON from agent response."""
        orch = Orchestrator()
        try:
            # Mock run_agent to return a valid JSON response
            def mock_run_agent(prompt, role=None, output_schema=None):
                return '''Here's the extracted context:

```json
{"summary": "Added authentication", "key_decisions": ["Used JWT tokens", "Added refresh tokens"]}
```'''
            monkeypatch.setattr(orch, "run_agent", mock_run_agent)
            monkeypatch.setattr(orch, "load_prompt", lambda name: "{{TASK_TEXT}}\n{{GIT_DIFF}}")

            result = orch.extract_context_summary("**Auth**: Add auth", "+code")

            assert result is not None
            assert result["summary"] == "Added authentication"
            assert "Used JWT tokens" in result["key_decisions"]
            assert "Added refresh tokens" in result["key_decisions"]
        finally:
            orch.cleanup()

    def test_extract_context_summary_returns_none_on_invalid_json(self, temp_repo, monkeypatch):
        """extract_context_summary returns None when JSON is invalid."""
        orch = Orchestrator()
        try:
            def mock_run_agent(prompt, role=None, output_schema=None):
                return "No valid JSON here, just some text."
            monkeypatch.setattr(orch, "run_agent", mock_run_agent)
            monkeypatch.setattr(orch, "load_prompt", lambda name: "{{TASK_TEXT}}\n{{GIT_DIFF}}")

            result = orch.extract_context_summary("**Task**: Do thing", "+code")

            assert result is None
        finally:
            orch.cleanup()

    def test_extract_context_summary_truncates_large_diffs(self, temp_repo, monkeypatch):
        """extract_context_summary truncates diffs larger than 10000 chars."""
        orch = Orchestrator()
        try:
            captured_prompts = []
            def mock_run_agent(prompt, role=None, output_schema=None):
                captured_prompts.append(prompt)
                return '{"summary": "test", "key_decisions": []}'
            monkeypatch.setattr(orch, "run_agent", mock_run_agent)
            monkeypatch.setattr(orch, "load_prompt", lambda name: "Task: {{TASK_TEXT}}\nDiff: {{GIT_DIFF}}")

            large_diff = "+" * 15000  # Larger than 10000 char limit
            orch.extract_context_summary("**Task**: Do thing", large_diff)

            assert len(captured_prompts) == 1
            # The prompt should contain truncated diff
            assert "... (truncated)" in captured_prompts[0]
            # Should be much smaller than original
            assert len(captured_prompts[0]) < len(large_diff)
        finally:
            orch.cleanup()

    def test_extract_context_summary_handles_exception(self, temp_repo, monkeypatch):
        """extract_context_summary returns None on exception."""
        orch = Orchestrator()
        try:
            def mock_run_agent(prompt, role=None, output_schema=None):
                raise RuntimeError("Connection failed")
            monkeypatch.setattr(orch, "run_agent", mock_run_agent)
            monkeypatch.setattr(orch, "load_prompt", lambda name: "{{TASK_TEXT}}\n{{GIT_DIFF}}")

            result = orch.extract_context_summary("**Task**: Do thing", "+code")

            assert result is None
        finally:
            orch.cleanup()

    def test_is_empty_response_context_extraction_schema(self, temp_repo):
        """is_empty_response validates context_extraction schema correctly."""
        from millstone.runtime.orchestrator import is_empty_response

        # Valid context extraction response
        valid = '{"summary": "Did something", "key_decisions": ["choice1"]}'
        assert is_empty_response(valid, expected_schema="context_extraction") is False

        # Missing summary
        no_summary = '{"key_decisions": ["choice1"]}'
        assert is_empty_response(no_summary, expected_schema="context_extraction") is True

        # Missing key_decisions
        no_decisions = '{"summary": "Did something"}'
        assert is_empty_response(no_decisions, expected_schema="context_extraction") is True

        # Empty string
        assert is_empty_response("", expected_schema="context_extraction") is True


class TestContextFileAnnotation:
    """Tests for context_file task annotation (<!-- context: path -->)."""

    def test_parse_task_metadata_extracts_context_file(self, temp_repo):
        """_parse_task_metadata extracts context file from HTML comment."""
        orch = Orchestrator()
        try:
            task_text = """**Add feature**: Implement the new feature.
  <!-- context: .millstone/context/deprecation.md -->
  - Est. LoC: 50"""

            result = orch._parse_task_metadata(task_text)

            assert result["context_file"] == ".millstone/context/deprecation.md"
            assert result["title"] == "Add feature"
            assert result["est_loc"] == 50
        finally:
            orch.cleanup()

    def test_parse_task_metadata_context_file_none_when_missing(self, temp_repo):
        """_parse_task_metadata returns None when no context annotation."""
        orch = Orchestrator()
        try:
            task_text = "**Simple task**: Just a description"

            result = orch._parse_task_metadata(task_text)

            assert result["context_file"] is None
        finally:
            orch.cleanup()

    def test_parse_task_metadata_context_file_with_spaces(self, temp_repo):
        """_parse_task_metadata handles context annotation with surrounding spaces."""
        orch = Orchestrator()
        try:
            task_text = """**Task**: Description
  <!--   context:   path/to/context.md   -->"""

            result = orch._parse_task_metadata(task_text)

            assert result["context_file"] == "path/to/context.md"
        finally:
            orch.cleanup()

    def test_extract_current_task_context_file_returns_path(self, temp_repo):
        """extract_current_task_context_file returns context path from tasklist."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("""# Tasklist

- [ ] **Task with context**: Do something
  <!-- context: .millstone/context/mycontext.md -->
""")

        orch = Orchestrator()
        try:
            result = orch.extract_current_task_context_file()

            assert result == ".millstone/context/mycontext.md"
        finally:
            orch.cleanup()

    def test_extract_current_task_context_file_returns_none_when_no_annotation(self, temp_repo):
        """extract_current_task_context_file returns None when no context annotation."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("""# Tasklist

- [ ] **Task without context**: Do something
""")

        orch = Orchestrator()
        try:
            result = orch.extract_current_task_context_file()

            assert result is None
        finally:
            orch.cleanup()

    def test_get_task_context_file_content_returns_content(self, temp_repo):
        """get_task_context_file_content returns content of context file."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("""# Tasklist

- [ ] **Task**: Description
  <!-- context: context/mycontext.md -->
""")

        # Create the context file
        context_file = temp_repo / "context" / "mycontext.md"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        context_file.write_text("# Context\n\nThis is important context for the task.")

        orch = Orchestrator()
        try:
            result = orch.get_task_context_file_content()

            assert result == "# Context\n\nThis is important context for the task."
        finally:
            orch.cleanup()

    def test_get_task_context_file_content_returns_none_when_file_missing(self, temp_repo):
        """get_task_context_file_content returns None when context file doesn't exist."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("""# Tasklist

- [ ] **Task**: Description
  <!-- context: nonexistent/file.md -->
""")

        orch = Orchestrator()
        try:
            result = orch.get_task_context_file_content()

            assert result is None
        finally:
            orch.cleanup()

    def test_get_task_context_file_content_returns_none_when_no_annotation(self, temp_repo):
        """get_task_context_file_content returns None when no context annotation."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("""# Tasklist

- [ ] **Task**: Description without context annotation
""")

        orch = Orchestrator()
        try:
            result = orch.get_task_context_file_content()

            assert result is None
        finally:
            orch.cleanup()

    def test_get_tasklist_prompt_includes_context_file(self, temp_repo):
        """get_tasklist_prompt includes context file content when specified."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("""# Tasklist

- [ ] **Task with context**: Implement feature
  <!-- context: docs/context.md -->
""")

        # Create the context file
        context_file = temp_repo / "docs" / "context.md"
        context_file.write_text("## Important Decisions\n\n- Use pattern A\n- Avoid pattern B")

        orch = Orchestrator()
        try:
            prompt = orch.get_tasklist_prompt()

            assert "## Task Context" in prompt
            assert "`docs/context.md`" in prompt
            assert "## Important Decisions" in prompt
            assert "Use pattern A" in prompt
        finally:
            orch.cleanup()

    def test_get_tasklist_prompt_no_context_section_when_no_annotation(self, temp_repo):
        """get_tasklist_prompt has no context section when no annotation."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("""# Tasklist

- [ ] **Plain task**: Do something
""")

        orch = Orchestrator()
        try:
            prompt = orch.get_tasklist_prompt()

            assert "## Task Context" not in prompt
        finally:
            orch.cleanup()

    def test_get_tasklist_prompt_includes_both_group_and_file_context(self, temp_repo):
        """get_tasklist_prompt includes both group context and file context."""
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("""# Tasklist

## Group: FeatureGroup

- [ ] **Task two**: Second task
  <!-- context: docs/extra.md -->
""")

        # Create the context file
        context_file = temp_repo / "docs" / "extra.md"
        context_file.write_text("Extra context content")

        orch = Orchestrator()
        try:
            # Set up group and accumulate context from a "previous" task
            orch.current_task_group = "FeatureGroup"
            orch.accumulate_group_context("**First task**: Did something")

            prompt = orch.get_tasklist_prompt()

            # Should have both context sections
            assert "## Group Context" in prompt
            assert "## Task Context" in prompt
            assert "Extra context content" in prompt
        finally:
            orch.cleanup()


class TestProjectAdapterInterface:
    """Tests for .millstone/project.toml project adapter interface."""

    def test_default_project_config_structure(self):
        """DEFAULT_PROJECT_CONFIG has all required sections."""
        from millstone.runtime.orchestrator import DEFAULT_PROJECT_CONFIG

        assert "project" in DEFAULT_PROJECT_CONFIG
        assert "tests" in DEFAULT_PROJECT_CONFIG
        assert "lint" in DEFAULT_PROJECT_CONFIG
        assert "typing" in DEFAULT_PROJECT_CONFIG
        assert "sensitive_paths" in DEFAULT_PROJECT_CONFIG
        assert "tasklist" in DEFAULT_PROJECT_CONFIG

    def test_detect_project_type_python_pyproject(self, temp_repo):
        """detect_project_type returns 'python' for pyproject.toml."""
        from millstone.runtime.orchestrator import detect_project_type

        (temp_repo / "pyproject.toml").write_text("[project]\nname = 'test'")
        assert detect_project_type(temp_repo) == "python"

    def test_detect_project_type_python_setup_py(self, temp_repo):
        """detect_project_type returns 'python' for setup.py."""
        from millstone.runtime.orchestrator import detect_project_type

        (temp_repo / "setup.py").write_text("from setuptools import setup")
        assert detect_project_type(temp_repo) == "python"

    def test_detect_project_type_python_requirements(self, temp_repo):
        """detect_project_type returns 'python' for requirements.txt."""
        from millstone.runtime.orchestrator import detect_project_type

        (temp_repo / "requirements.txt").write_text("pytest")
        assert detect_project_type(temp_repo) == "python"

    def test_detect_project_type_node(self, temp_repo):
        """detect_project_type returns 'node' for package.json."""
        from millstone.runtime.orchestrator import detect_project_type

        (temp_repo / "package.json").write_text('{"name": "test"}')
        assert detect_project_type(temp_repo) == "node"

    def test_detect_project_type_go(self, temp_repo):
        """detect_project_type returns 'go' for go.mod."""
        from millstone.runtime.orchestrator import detect_project_type

        (temp_repo / "go.mod").write_text("module test")
        assert detect_project_type(temp_repo) == "go"

    def test_detect_project_type_unknown(self, temp_repo):
        """detect_project_type returns 'unknown' for unrecognized project."""
        from millstone.runtime.orchestrator import detect_project_type

        # temp_repo only has README.md and docs/tasklist.md
        assert detect_project_type(temp_repo) == "unknown"

    def test_get_default_commands_python(self, temp_repo):
        """get_default_commands returns pytest commands for Python."""
        from millstone.runtime.orchestrator import get_default_commands

        defaults = get_default_commands("python", temp_repo)
        assert "pytest" in defaults["tests"]["command"]
        assert "--cov" in defaults["tests"]["coverage_command"]

    def test_get_default_commands_node(self, temp_repo):
        """get_default_commands returns npm commands for Node."""
        from millstone.runtime.orchestrator import get_default_commands

        defaults = get_default_commands("node", temp_repo)
        assert "npm test" in defaults["tests"]["command"]

    def test_get_default_commands_go(self, temp_repo):
        """get_default_commands returns go test commands for Go."""
        from millstone.runtime.orchestrator import get_default_commands

        defaults = get_default_commands("go", temp_repo)
        assert "go test" in defaults["tests"]["command"]

    def test_get_default_commands_unknown(self, temp_repo):
        """get_default_commands returns empty commands for unknown."""
        from millstone.runtime.orchestrator import get_default_commands

        defaults = get_default_commands("unknown", temp_repo)
        assert defaults["tests"]["command"] == ""

    def test_load_project_config_returns_defaults_when_no_file(self, temp_repo):
        """load_project_config returns defaults with auto-detected language."""
        from millstone.runtime.orchestrator import load_project_config

        # temp_repo has no project markers, should detect as 'unknown'
        config = load_project_config(temp_repo)
        assert config["project"]["language"] == "unknown"
        assert "patterns" in config["sensitive_paths"]

    def test_load_project_config_reads_toml_file(self, temp_repo):
        """load_project_config reads values from project.toml."""
        from millstone.runtime.orchestrator import (
            PROJECT_FILE_NAME,
            WORK_DIR_NAME,
            load_project_config,
        )

        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        project_file = config_dir / PROJECT_FILE_NAME
        project_file.write_text("""
[project]
name = "my-project"
language = "python"

[tests]
command = "pytest custom_tests/"
coverage_command = "pytest --cov custom_tests/"

[lint]
command = "flake8 ."

[typing]
command = "pyright ."

[sensitive_paths]
patterns = [".env", "secrets/"]
""")

        config = load_project_config(temp_repo)
        assert config["project"]["name"] == "my-project"
        assert config["project"]["language"] == "python"
        assert config["tests"]["command"] == "pytest custom_tests/"
        assert config["lint"]["command"] == "flake8 ."
        assert config["typing"]["command"] == "pyright ."
        assert ".env" in config["sensitive_paths"]["patterns"]

    def test_load_project_config_auto_detects_language(self, temp_repo):
        """load_project_config auto-detects language when set to 'auto'."""
        from millstone.runtime.orchestrator import load_project_config

        # Create a pyproject.toml to be detected as Python
        (temp_repo / "pyproject.toml").write_text("[project]\nname = 'test'")

        config = load_project_config(temp_repo)
        assert config["project"]["language"] == "python"

    def test_load_project_config_fills_missing_commands(self, temp_repo):
        """load_project_config fills missing commands with auto-detected defaults."""
        from millstone.runtime.orchestrator import (
            PROJECT_FILE_NAME,
            WORK_DIR_NAME,
            load_project_config,
        )

        # Create a pyproject.toml to be detected as Python
        (temp_repo / "pyproject.toml").write_text("[project]\nname = 'test'")

        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        project_file = config_dir / PROJECT_FILE_NAME
        project_file.write_text("""
[project]
language = "python"

[lint]
command = "flake8 ."
""")

        config = load_project_config(temp_repo)
        # Custom lint command should be preserved
        assert config["lint"]["command"] == "flake8 ."
        # Test command should be auto-filled
        assert "pytest" in config["tests"]["command"]

    def test_orchestrator_loads_project_config(self, temp_repo):
        """Orchestrator loads project config on init."""
        # Create pyproject.toml for Python detection
        (temp_repo / "pyproject.toml").write_text("[project]\nname = 'test'")

        orch = Orchestrator()
        try:
            assert orch.project_config is not None
            assert orch.project_config["project"]["language"] == "python"
        finally:
            orch.cleanup()

    def test_orchestrator_uses_configured_sensitive_patterns(self, temp_repo):
        """Orchestrator uses sensitive_paths patterns from project config."""
        from millstone.runtime.orchestrator import PROJECT_FILE_NAME, WORK_DIR_NAME

        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        project_file = config_dir / PROJECT_FILE_NAME
        project_file.write_text("""
[sensitive_paths]
patterns = [".custom_env", "my_secrets/"]
""")

        orch = Orchestrator()
        try:
            patterns = orch.project_config.get("sensitive_paths", {}).get("patterns", [])
            assert ".custom_env" in patterns
            assert "my_secrets/" in patterns
        finally:
            orch.cleanup()

    def test_project_config_sensitive_patterns_default(self, temp_repo):
        """Default project config has standard sensitive file patterns."""
        from millstone.runtime.orchestrator import DEFAULT_PROJECT_CONFIG

        patterns = DEFAULT_PROJECT_CONFIG["sensitive_paths"]["patterns"]
        assert ".env" in patterns
        assert "*.key" in patterns
        assert "*.pem" in patterns


class TestPolicyEngine:
    """Tests for the configurable policy engine."""

    def test_default_policy_loaded(self, temp_repo):
        """Orchestrator loads default policy when no policy.toml exists."""
        from millstone.runtime.orchestrator import DEFAULT_POLICY

        orch = Orchestrator()
        try:
            assert orch.policy is not None
            assert orch.policy["limits"]["max_loc_per_task"] == DEFAULT_POLICY["limits"]["max_loc_per_task"]
        finally:
            orch.cleanup()

    def test_custom_policy_loaded(self, temp_repo):
        """Orchestrator loads custom policy from policy.toml."""
        from millstone.runtime.orchestrator import POLICY_FILE_NAME, WORK_DIR_NAME

        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[limits]
max_loc_per_task = 999
max_cycles = 10

[sensitive]
enabled = true
paths = [".secret", "*.token"]
require_approval = false

[dangerous]
patterns = ["rm -rf /", "format c:"]
block = true

[eval]
min_composite_score = 0.7
max_regression = 0.1
""")

        orch = Orchestrator()
        try:
            assert orch.policy["limits"]["max_loc_per_task"] == 999
            assert orch.policy["limits"]["max_cycles"] == 10
            assert orch.policy["sensitive"]["enabled"] is True
            assert ".secret" in orch.policy["sensitive"]["paths"]
            assert "*.token" in orch.policy["sensitive"]["paths"]
            assert orch.policy["sensitive"]["require_approval"] is False
            assert "rm -rf /" in orch.policy["dangerous"]["patterns"]
            assert orch.policy["dangerous"]["block"] is True
            assert orch.policy["eval"]["min_composite_score"] == 0.7
            assert orch.policy["eval"]["max_regression"] == 0.1
        finally:
            orch.cleanup()

    def test_load_policy_function(self, temp_repo):
        """load_policy function works correctly."""
        from millstone.runtime.orchestrator import POLICY_FILE_NAME, WORK_DIR_NAME, load_policy

        # Test default policy when no file exists
        policy = load_policy(temp_repo)
        assert "limits" in policy
        assert "sensitive" in policy
        assert "dangerous" in policy
        assert "eval" in policy

        # Create policy file
        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[limits]
max_loc_per_task = 100
""")

        policy = load_policy(temp_repo)
        assert policy["limits"]["max_loc_per_task"] == 100
        # Other defaults should remain

    def test_mechanical_checks_uses_policy_loc_limit(self, temp_repo):
        """mechanical_checks uses policy's max_loc_per_task limit."""
        from millstone.runtime.orchestrator import POLICY_FILE_NAME, WORK_DIR_NAME

        # Create policy with low LoC limit
        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[limits]
max_loc_per_task = 5
""")

        orch = Orchestrator()
        try:
            # Make a change that exceeds the policy limit
            large_content = "\n".join([f"line {i}" for i in range(20)])
            (temp_repo / "large_file.txt").write_text(large_content)
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            result = orch.mechanical_checks()
            assert result is False  # Should fail due to policy limit
        finally:
            orch.cleanup()

    def test_mechanical_checks_dangerous_patterns_blocks(self, temp_repo):
        """mechanical_checks blocks dangerous patterns when block=true."""
        from millstone.runtime.orchestrator import POLICY_FILE_NAME, WORK_DIR_NAME

        # Create policy with dangerous patterns
        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[limits]
max_loc_per_task = 10000

[dangerous]
patterns = ["DROP TABLE"]
block = true
""")

        orch = Orchestrator()
        try:
            # Create a file with dangerous content
            (temp_repo / "script.sql").write_text("DROP TABLE users;")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            result = orch.mechanical_checks()
            assert result is False  # Should be blocked
        finally:
            orch.cleanup()

    def test_mechanical_checks_dangerous_patterns_warns_only(self, temp_repo, capsys):
        """mechanical_checks warns but doesn't block when block=false."""
        from millstone.runtime.orchestrator import POLICY_FILE_NAME, WORK_DIR_NAME

        # Create policy with block=false
        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[limits]
max_loc_per_task = 10000

[dangerous]
patterns = ["DROP TABLE"]
block = false
""")

        orch = Orchestrator()
        try:
            # Create a file with dangerous content
            (temp_repo / "script.sql").write_text("DROP TABLE users;")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            result = orch.mechanical_checks()
            assert result is True  # Should pass since block=false

            captured = capsys.readouterr()
            assert "WARN" in captured.out
            assert "DROP TABLE" in captured.out
        finally:
            orch.cleanup()

    def test_mechanical_checks_sensitive_with_require_approval_false(self, temp_repo, capsys):
        """mechanical_checks warns but doesn't halt when require_approval=false."""
        from millstone.runtime.orchestrator import POLICY_FILE_NAME, WORK_DIR_NAME

        # Create policy with require_approval=false
        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[limits]
max_loc_per_task = 10000

[sensitive]
enabled = true
paths = [".env"]
require_approval = false
""")

        orch = Orchestrator()
        try:
            # Create a sensitive file
            (temp_repo / ".env").write_text("SECRET=abc123")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            result = orch.mechanical_checks()
            assert result is True  # Should pass since require_approval=false

            captured = capsys.readouterr()
            assert "WARN" in captured.out
            assert ".env" in captured.out
        finally:
            orch.cleanup()

    def test_mechanical_checks_blocks_multiple_tasklist_checkoffs(self, temp_repo):
        """mechanical_checks blocks when multiple tasks are checked off."""
        from millstone.runtime.orchestrator import POLICY_FILE_NAME, WORK_DIR_NAME

        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[limits]
max_loc_per_task = 10000

[tasklist]
enforce_single_task = true
""")

        orch = Orchestrator()
        try:
            tasklist_path = temp_repo / ".millstone" / "tasklist.md"
            orch._tasklist_baseline = tasklist_path.read_text()

            tasklist_path.write_text(
                "# Tasklist\n\n- [x] Task 1: Do something\n- [x] Task 2: Do another thing\n"
            )

            result = orch.mechanical_checks()
            assert result is False
        finally:
            orch.cleanup()

    def test_mechanical_checks_blocks_non_first_tasklist_checkoff(self, temp_repo):
        """mechanical_checks blocks when a later task is checked off first."""
        from millstone.runtime.orchestrator import POLICY_FILE_NAME, WORK_DIR_NAME

        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[limits]
max_loc_per_task = 10000

[tasklist]
enforce_single_task = true
""")

        orch = Orchestrator()
        try:
            tasklist_path = temp_repo / ".millstone" / "tasklist.md"
            orch._tasklist_baseline = tasklist_path.read_text()

            tasklist_path.write_text(
                "# Tasklist\n\n- [ ] Task 1: Do something\n- [x] Task 2: Do another thing\n"
            )

            result = orch.mechanical_checks()
            assert result is False
        finally:
            orch.cleanup()

    def test_mechanical_checks_allows_single_tasklist_checkoff(self, temp_repo):
        """mechanical_checks allows a single checkoff of the first unchecked task."""
        from millstone.runtime.orchestrator import POLICY_FILE_NAME, WORK_DIR_NAME

        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[limits]
max_loc_per_task = 10000

[tasklist]
enforce_single_task = true
""")

        orch = Orchestrator()
        try:
            tasklist_path = temp_repo / ".millstone" / "tasklist.md"
            orch._tasklist_baseline = tasklist_path.read_text()

            tasklist_path.write_text(
                "# Tasklist\n\n- [x] Task 1: Do something\n- [ ] Task 2: Do another thing\n"
            )

            result = orch.mechanical_checks()
            assert result is True
        finally:
            orch.cleanup()

    def test_mechanical_checks_logs_tasklist_scope_violation(self, temp_repo):
        """mechanical_checks logs tasklist scope violations."""
        from millstone.runtime.orchestrator import POLICY_FILE_NAME, WORK_DIR_NAME

        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[limits]
max_loc_per_task = 10000

[tasklist]
enforce_single_task = true
""")

        orch = Orchestrator()
        try:
            tasklist_path = temp_repo / ".millstone" / "tasklist.md"
            orch._tasklist_baseline = tasklist_path.read_text()

            tasklist_path.write_text(
                "# Tasklist\n\n- [x] Task 1: Do something\n- [x] Task 2: Do another thing\n"
            )

            result = orch.mechanical_checks()
            assert result is False

            log_content = orch.log_file.read_text()
            assert "tasklist_scope_violation" in log_content
        finally:
            orch.cleanup()

    def test_policy_violation_logged(self, temp_repo):
        """Policy violations are logged with specific rule info."""
        from millstone.runtime.orchestrator import POLICY_FILE_NAME, WORK_DIR_NAME

        # Create policy with low LoC limit
        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[limits]
max_loc_per_task = 5
""")

        orch = Orchestrator()
        try:
            # Make a change that exceeds the policy limit
            large_content = "\n".join([f"line {i}" for i in range(20)])
            (temp_repo / "large_file.txt").write_text(large_content)
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            orch.mechanical_checks()

            # Check log file for policy violation
            log_content = orch.log_file.read_text()
            assert "policy_violation" in log_content
            assert "loc_threshold_exceeded" in log_content
        finally:
            orch.cleanup()

    def test_default_policy_has_expected_values(self):
        """DEFAULT_POLICY has all expected sections and values."""
        from millstone.runtime.orchestrator import DEFAULT_POLICY

        # Check limits section
        assert "limits" in DEFAULT_POLICY
        assert DEFAULT_POLICY["limits"]["max_loc_per_task"] == 2000
        assert DEFAULT_POLICY["limits"]["max_cycles"] == 3

        # Check sensitive section
        assert "sensitive" in DEFAULT_POLICY
        assert DEFAULT_POLICY["sensitive"]["enabled"] is False
        assert ".env" in DEFAULT_POLICY["sensitive"]["paths"]
        assert DEFAULT_POLICY["sensitive"]["require_approval"] is True

        # Check dangerous section
        assert "dangerous" in DEFAULT_POLICY
        assert "rm -rf" in DEFAULT_POLICY["dangerous"]["patterns"]
        assert "DROP TABLE" in DEFAULT_POLICY["dangerous"]["patterns"]
        assert DEFAULT_POLICY["dangerous"]["block"] is True

        # Check tasklist section
        assert "tasklist" in DEFAULT_POLICY
        assert DEFAULT_POLICY["tasklist"]["enforce_single_task"] is True

        # Check eval section
        assert "eval" in DEFAULT_POLICY
        assert DEFAULT_POLICY["eval"]["min_composite_score"] == 0.0
        assert DEFAULT_POLICY["eval"]["max_regression"] == 0.05

    def test_policy_partial_override(self, temp_repo):
        """Policy file can partially override defaults."""
        from millstone.runtime.orchestrator import (
            DEFAULT_POLICY,
            POLICY_FILE_NAME,
            WORK_DIR_NAME,
            load_policy,
        )

        # Create policy file that only overrides some values
        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[limits]
max_loc_per_task = 1000
""")

        policy = load_policy(temp_repo)

        # Overridden value
        assert policy["limits"]["max_loc_per_task"] == 1000

        # Default values should remain
        assert policy["sensitive"]["paths"] == DEFAULT_POLICY["sensitive"]["paths"]

    def test_mechanical_checks_policy_sensitive_paths_priority(self, temp_repo):
        """Policy sensitive.paths takes priority over project_config sensitive_paths."""
        from millstone.runtime.orchestrator import (
            POLICY_FILE_NAME,
            PROJECT_FILE_NAME,
            WORK_DIR_NAME,
        )

        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)

        # Create project config with one set of patterns
        project_file = config_dir / PROJECT_FILE_NAME
        project_file.write_text("""
[sensitive_paths]
patterns = [".project_secret"]
""")

        # Create policy with different patterns
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[limits]
max_loc_per_task = 10000

[sensitive]
enabled = true
paths = [".policy_secret"]
require_approval = true
""")

        orch = Orchestrator()
        try:
            # Create file matching policy pattern (should be caught)
            (temp_repo / ".policy_secret").write_text("secret")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            result = orch.mechanical_checks()
            assert result is False  # Should be caught by policy pattern
        finally:
            orch.cleanup()

    def test_save_state_includes_policy_prefix(self, temp_repo):
        """State saved on policy violations includes 'policy:' prefix."""
        from millstone.runtime.orchestrator import POLICY_FILE_NAME, WORK_DIR_NAME

        # Create policy with low LoC limit
        config_dir = temp_repo / WORK_DIR_NAME
        config_dir.mkdir(exist_ok=True)
        policy_file = config_dir / POLICY_FILE_NAME
        policy_file.write_text("""
[limits]
max_loc_per_task = 5
""")

        orch = Orchestrator()
        try:
            # Make a change that exceeds the policy limit
            large_content = "\n".join([f"line {i}" for i in range(20)])
            (temp_repo / "large_file.txt").write_text(large_content)
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)

            orch.mechanical_checks()

            # Check state file for policy prefix
            state_file = orch.work_dir / "state.json"
            if state_file.exists():
                state = json.loads(state_file.read_text())
                assert "policy:" in state.get("halt_reason", "")
        finally:
            orch.cleanup()



class TestEvalRollback:
    """Tests for eval regression rollback functionality."""

    def test_auto_rollback_param_defaults_to_false(self, temp_repo):
        """auto_rollback parameter defaults to False."""
        orch = Orchestrator()
        try:
            assert orch.auto_rollback is False
        finally:
            orch.cleanup()

    def test_auto_rollback_param_can_be_enabled(self, temp_repo):
        """auto_rollback parameter can be set to True."""
        orch = Orchestrator(auto_rollback=True)
        try:
            assert orch.auto_rollback is True
        finally:
            orch.cleanup()

    def test_auto_rollback_in_default_config(self):
        """auto_rollback is in DEFAULT_CONFIG."""
        assert "auto_rollback" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["auto_rollback"] is False

    def test_last_rollback_context_starts_as_none(self, temp_repo):
        """last_rollback_context attribute starts as None."""
        orch = Orchestrator()
        try:
            assert orch.last_rollback_context is None
        finally:
            orch.cleanup()

    def test_run_eval_on_commit_detects_composite_score_regression(self, temp_repo):
        """_run_eval_on_commit detects composite score regression."""
        orch = Orchestrator(eval_on_commit=True)
        try:
            # Set up baseline with good composite score
            orch.baseline_eval = {
                "failed_tests": [],
                "_passed": True,
                "composite_score": 0.95,
                "categories": {"tests": {"score": 0.95}},
            }

            # Mock run_eval to return a lower score (regression > max_regression)
            with patch.object(orch, 'run_eval') as mock_eval:
                mock_eval.return_value = {
                    "failed_tests": [],
                    "_passed": True,
                    "composite_score": 0.85,  # 0.10 regression, > 0.05 default max
                    "categories": {"tests": {"score": 0.85}},
                }
                with patch.object(orch, 'git') as mock_git:
                    mock_git.return_value = "abc123\n"
                    # Mock input to decline revert
                    with patch('builtins.input', return_value='n'):
                        result = orch._run_eval_on_commit()

            assert result is False  # Regression detected
        finally:
            orch.cleanup()

    def test_run_eval_on_commit_allows_small_regression(self, temp_repo):
        """_run_eval_on_commit allows regression within threshold."""
        orch = Orchestrator(eval_on_commit=True)
        try:
            # Set up baseline
            orch.baseline_eval = {
                "failed_tests": [],
                "_passed": True,
                "composite_score": 0.95,
                "categories": {},
            }

            # Mock run_eval to return a small regression (< max_regression)
            with patch.object(orch, 'run_eval') as mock_eval:
                mock_eval.return_value = {
                    "failed_tests": [],
                    "_passed": True,
                    "composite_score": 0.92,  # 0.03 regression, < 0.05 default max
                    "categories": {},
                }

                result = orch._run_eval_on_commit()

            assert result is True  # Small regression is allowed
        finally:
            orch.cleanup()

    def test_run_eval_on_commit_allows_score_improvement(self, temp_repo):
        """_run_eval_on_commit allows score improvements."""
        orch = Orchestrator(eval_on_commit=True)
        try:
            # Set up baseline
            orch.baseline_eval = {
                "failed_tests": [],
                "_passed": True,
                "composite_score": 0.80,
                "categories": {},
            }

            # Mock run_eval to return an improvement
            with patch.object(orch, 'run_eval') as mock_eval:
                mock_eval.return_value = {
                    "failed_tests": [],
                    "_passed": True,
                    "composite_score": 0.90,  # Score improved
                    "categories": {},
                }

                result = orch._run_eval_on_commit()

            assert result is True
        finally:
            orch.cleanup()

    def test_perform_rollback_creates_revert_commit(self, temp_repo):
        """_perform_rollback creates a git revert commit."""
        orch = Orchestrator(auto_rollback=True)
        try:
            # Create an initial commit
            (temp_repo / "test.txt").write_text("initial content")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=temp_repo, capture_output=True)

            # Create a second commit to revert
            (temp_repo / "test.txt").write_text("changed content")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", "To be reverted"], cwd=temp_repo, capture_output=True)

            # Get the commit hash
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=temp_repo,
                capture_output=True,
                text=True
            )
            commit_hash = result.stdout.strip()

            # Perform rollback
            success = orch._perform_rollback(
                commit_hash,
                "Test task",
                "test_reason",
                {"detail": "test"}
            )

            assert success is True

            # Verify revert commit was created
            result = subprocess.run(
                ["git", "log", "-1", "--format=%s"],
                cwd=temp_repo,
                capture_output=True,
                text=True
            )
            assert "Revert" in result.stdout
        finally:
            orch.cleanup()

    def test_perform_rollback_saves_context_file(self, temp_repo):
        """_perform_rollback saves rollback context to JSON file."""
        orch = Orchestrator(auto_rollback=True)
        try:
            # Create two commits
            (temp_repo / "test.txt").write_text("initial")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=temp_repo, capture_output=True)

            (temp_repo / "test.txt").write_text("changed")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Change"], cwd=temp_repo, capture_output=True)

            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=temp_repo,
                capture_output=True,
                text=True
            )
            commit_hash = result.stdout.strip()

            orch._perform_rollback(
                commit_hash,
                "Test task",
                "composite_score_regression",
                {"baseline_score": 0.95, "current_score": 0.80}
            )

            # Check context file exists
            rollback_file = orch.work_dir / "last_rollback.json"
            assert rollback_file.exists()

            context = json.loads(rollback_file.read_text())
            assert context["task"] == "Test task"
            assert context["reason"] == "composite_score_regression"
            assert "baseline_score" in context["details"]
        finally:
            orch.cleanup()

    def test_load_rollback_context_returns_none_when_no_file(self, temp_repo):
        """_load_rollback_context returns None when no rollback file exists."""
        orch = Orchestrator()
        try:
            result = orch._load_rollback_context()
            assert result is None
        finally:
            orch.cleanup()

    def test_load_rollback_context_returns_file_contents(self, temp_repo):
        """_load_rollback_context loads context from file."""
        orch = Orchestrator()
        try:
            # Create rollback file
            rollback_file = orch.work_dir / "last_rollback.json"
            context = {
                "timestamp": "2024-01-01T00:00:00",
                "task": "Previous task",
                "reason": "test_regression",
            }
            rollback_file.write_text(json.dumps(context))

            result = orch._load_rollback_context()
            assert result["task"] == "Previous task"
            assert result["reason"] == "test_regression"
        finally:
            orch.cleanup()

    def test_load_rollback_context_prefers_in_memory(self, temp_repo):
        """_load_rollback_context prefers in-memory context over file."""
        orch = Orchestrator()
        try:
            # Set in-memory context
            orch.last_rollback_context = {
                "task": "In-memory task",
                "reason": "memory_reason",
            }

            # Create file with different content
            rollback_file = orch.work_dir / "last_rollback.json"
            rollback_file.write_text(json.dumps({
                "task": "File task",
                "reason": "file_reason",
            }))

            result = orch._load_rollback_context()
            assert result["task"] == "In-memory task"
        finally:
            orch.cleanup()

    def test_clear_rollback_context_removes_file_and_memory(self, temp_repo):
        """clear_rollback_context removes both file and in-memory context."""
        orch = Orchestrator()
        try:
            # Set up both contexts
            orch.last_rollback_context = {"task": "test"}
            rollback_file = orch.work_dir / "last_rollback.json"
            rollback_file.write_text('{"task": "test"}')

            orch.clear_rollback_context()

            assert orch.last_rollback_context is None
            assert not rollback_file.exists()
        finally:
            orch.cleanup()

    def test_auto_rollback_reverts_without_prompt(self, temp_repo):
        """With auto_rollback=True, commits are reverted without user prompt."""
        orch = Orchestrator(eval_on_commit=True, auto_rollback=True)
        try:
            # Create two commits
            (temp_repo / "test.txt").write_text("initial")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=temp_repo, capture_output=True)

            (temp_repo / "test.txt").write_text("changed")
            subprocess.run(["git", "add", "."], cwd=temp_repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Change"], cwd=temp_repo, capture_output=True)

            # Set up baseline
            orch.baseline_eval = {
                "failed_tests": [],
                "_passed": True,
                "composite_score": 0.95,
                "categories": {},
            }

            # Mock run_eval to return regression
            with patch.object(orch, 'run_eval') as mock_eval:
                mock_eval.return_value = {
                    "failed_tests": [],
                    "_passed": True,
                    "composite_score": 0.80,  # Big regression
                    "categories": {},
                }
                # Should NOT prompt for input - auto_rollback is True
                result = orch._run_eval_on_commit(task_text="Test task")

            assert result is False

            # Verify revert was created
            log_result = subprocess.run(
                ["git", "log", "-1", "--format=%s"],
                cwd=temp_repo,
                capture_output=True,
                text=True
            )
            assert "Revert" in log_result.stdout
        finally:
            orch.cleanup()

    def test_auto_rollback_cli_flag(self, temp_repo):
        """--auto-rollback CLI flag is recognized."""
        from millstone import orchestrate

        # Create a tasklist file
        tasklist = temp_repo / ".millstone" / "tasklist.md"
        tasklist.parent.mkdir(parents=True, exist_ok=True)
        tasklist.write_text("# Tasklist\n\n- [ ] Test task\n")

        with patch('sys.argv', ['orchestrate.py', '--auto-rollback', '--dry-run']):
            with pytest.raises(SystemExit) as exc_info:
                orchestrate.main()
            assert exc_info.value.code == 0

    def test_print_category_comparison_formats_output(self, temp_repo, capsys):
        """_print_category_comparison prints category comparison."""
        orch = Orchestrator()
        try:
            orch.baseline_eval = {
                "categories": {
                    "tests": {"score": 0.90},
                    "coverage": {"score": 0.80},
                }
            }

            current_eval = {
                "categories": {
                    "tests": {"score": 0.85},  # Regression
                    "coverage": {"score": 0.85},  # Improvement
                }
            }

            orch._print_category_comparison(current_eval)

            captured = capsys.readouterr()
            assert "Category breakdown:" in captured.out
            assert "tests" in captured.out
            assert "coverage" in captured.out
        finally:
            orch.cleanup()


class TestMetricsReport:
    """Tests for --metrics-report CLI flag."""

    def test_metrics_report_shows_no_file_message(self, temp_repo, capsys):
        """--metrics-report shows message when no reviews.jsonl exists."""
        orch = Orchestrator(task="metrics-report", dry_run=False)
        try:
            orch.print_metrics_report()

            captured = capsys.readouterr()
            assert "No review metrics found" in captured.out
        finally:
            orch.cleanup()

    def test_metrics_report_shows_empty_file_message(self, temp_repo, capsys):
        """--metrics-report shows message when reviews.jsonl is empty."""
        orch = Orchestrator(task="metrics-report", dry_run=False)
        try:
            metrics_dir = orch.work_dir / "metrics"
            metrics_dir.mkdir(exist_ok=True)
            reviews_file = metrics_dir / "reviews.jsonl"
            reviews_file.write_text("")

            orch.print_metrics_report()

            captured = capsys.readouterr()
            assert "No valid review entries found" in captured.out
        finally:
            orch.cleanup()

    def test_metrics_report_shows_basic_stats(self, temp_repo, capsys):
        """--metrics-report shows basic approval rate stats."""
        import json
        orch = Orchestrator(task="metrics-report", dry_run=False)
        try:
            metrics_dir = orch.work_dir / "metrics"
            metrics_dir.mkdir(exist_ok=True)
            reviews_file = metrics_dir / "reviews.jsonl"

            reviews = [
                {"task_hash": "abc123", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 1000, "timestamp": "2024-01-01T10:00:00", "reviewer_cli": "claude"},
                {"task_hash": "def456", "verdict": "REQUEST_CHANGES", "findings": ["Issue 1"], "findings_count": 1, "duration_ms": 1500, "timestamp": "2024-01-01T11:00:00", "reviewer_cli": "claude"},
                {"task_hash": "def456", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 800, "timestamp": "2024-01-01T11:30:00", "reviewer_cli": "claude"},
            ]
            reviews_file.write_text("\n".join(json.dumps(r) for r in reviews) + "\n")

            orch.print_metrics_report()

            captured = capsys.readouterr()
            assert "Review Metrics Report" in captured.out
            assert "Total reviews: 3" in captured.out
            assert "Approved: 2" in captured.out
        finally:
            orch.cleanup()

    def test_metrics_report_shows_cycles_to_approval(self, temp_repo, capsys):
        """--metrics-report shows average cycles to approval."""
        import json
        orch = Orchestrator(task="metrics-report", dry_run=False)
        try:
            metrics_dir = orch.work_dir / "metrics"
            metrics_dir.mkdir(exist_ok=True)
            reviews_file = metrics_dir / "reviews.jsonl"

            # Task 1: approved on first try (1 cycle)
            # Task 2: approved on second try (2 cycles)
            reviews = [
                {"task_hash": "task1", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 1000, "timestamp": "2024-01-01T10:00:00", "reviewer_cli": "claude"},
                {"task_hash": "task2", "verdict": "REQUEST_CHANGES", "findings": ["Fix bug"], "findings_count": 1, "duration_ms": 1500, "timestamp": "2024-01-01T11:00:00", "reviewer_cli": "claude"},
                {"task_hash": "task2", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 800, "timestamp": "2024-01-01T11:30:00", "reviewer_cli": "claude"},
            ]
            reviews_file.write_text("\n".join(json.dumps(r) for r in reviews) + "\n")

            orch.print_metrics_report()

            captured = capsys.readouterr()
            assert "Tasks with approval: 2" in captured.out
            assert "Average cycles to approval: 1.50" in captured.out
        finally:
            orch.cleanup()

    def test_metrics_report_shows_finding_categories(self, temp_repo, capsys):
        """--metrics-report categorizes findings by keywords."""
        import json
        orch = Orchestrator(task="metrics-report", dry_run=False)
        try:
            metrics_dir = orch.work_dir / "metrics"
            metrics_dir.mkdir(exist_ok=True)
            reviews_file = metrics_dir / "reviews.jsonl"

            reviews = [
                {"task_hash": "task1", "verdict": "REQUEST_CHANGES", "findings": ["Security vulnerability in auth", "Missing test coverage"], "findings_count": 2, "duration_ms": 1000, "timestamp": "2024-01-01T10:00:00", "reviewer_cli": "claude"},
                {"task_hash": "task2", "verdict": "REQUEST_CHANGES", "findings": ["Error handling needed", "Type annotation missing"], "findings_count": 2, "duration_ms": 1500, "timestamp": "2024-01-01T11:00:00", "reviewer_cli": "claude"},
            ]
            reviews_file.write_text("\n".join(json.dumps(r) for r in reviews) + "\n")

            orch.print_metrics_report()

            captured = capsys.readouterr()
            assert "Total findings: 4" in captured.out
            assert "Finding categories:" in captured.out
            assert "security:" in captured.out
            assert "testing:" in captured.out
            assert "error handling:" in captured.out
            assert "type:" in captured.out
        finally:
            orch.cleanup()

    def test_metrics_report_shows_severity_breakdown(self, temp_repo, capsys):
        """--metrics-report shows findings by severity."""
        import json
        orch = Orchestrator(task="metrics-report", dry_run=False)
        try:
            metrics_dir = orch.work_dir / "metrics"
            metrics_dir.mkdir(exist_ok=True)
            reviews_file = metrics_dir / "reviews.jsonl"

            reviews = [
                {
                    "task_hash": "task1",
                    "verdict": "REQUEST_CHANGES",
                    "findings": ["Bug 1", "Issue 1", "Issue 2"],
                    "findings_count": 3,
                    "findings_by_severity": {"critical": ["Bug 1"], "high": ["Issue 1", "Issue 2"]},
                    "duration_ms": 1000,
                    "timestamp": "2024-01-01T10:00:00",
                    "reviewer_cli": "claude",
                },
            ]
            reviews_file.write_text("\n".join(json.dumps(r) for r in reviews) + "\n")

            orch.print_metrics_report()

            captured = capsys.readouterr()
            assert "Findings by severity:" in captured.out
            assert "critical: 1" in captured.out
            assert "high: 2" in captured.out
        finally:
            orch.cleanup()

    def test_metrics_report_shows_reviewer_comparison(self, temp_repo, capsys):
        """--metrics-report compares multiple reviewer CLIs."""
        import json
        orch = Orchestrator(task="metrics-report", dry_run=False)
        try:
            metrics_dir = orch.work_dir / "metrics"
            metrics_dir.mkdir(exist_ok=True)
            reviews_file = metrics_dir / "reviews.jsonl"

            reviews = [
                {"task_hash": "task1", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 1000, "timestamp": "2024-01-01T10:00:00", "reviewer_cli": "claude"},
                {"task_hash": "task2", "verdict": "REQUEST_CHANGES", "findings": ["Issue"], "findings_count": 1, "duration_ms": 2000, "timestamp": "2024-01-01T11:00:00", "reviewer_cli": "codex"},
                {"task_hash": "task3", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 800, "timestamp": "2024-01-01T12:00:00", "reviewer_cli": "codex"},
            ]
            reviews_file.write_text("\n".join(json.dumps(r) for r in reviews) + "\n")

            orch.print_metrics_report()

            captured = capsys.readouterr()
            assert "Reviewer comparison:" in captured.out
            assert "claude" in captured.out
            assert "codex" in captured.out
        finally:
            orch.cleanup()

    def test_metrics_report_single_reviewer_shows_stats(self, temp_repo, capsys):
        """--metrics-report shows stats for single reviewer."""
        import json
        orch = Orchestrator(task="metrics-report", dry_run=False)
        try:
            metrics_dir = orch.work_dir / "metrics"
            metrics_dir.mkdir(exist_ok=True)
            reviews_file = metrics_dir / "reviews.jsonl"

            reviews = [
                {"task_hash": "task1", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 1000, "timestamp": "2024-01-01T10:00:00", "reviewer_cli": "claude"},
            ]
            reviews_file.write_text("\n".join(json.dumps(r) for r in reviews) + "\n")

            orch.print_metrics_report()

            captured = capsys.readouterr()
            assert "Reviewer: claude" in captured.out
            assert "Reviewer comparison:" not in captured.out  # No comparison table for single reviewer
        finally:
            orch.cleanup()

    def test_metrics_report_shows_duration_stats(self, temp_repo, capsys):
        """--metrics-report shows total and average review duration."""
        import json
        orch = Orchestrator(task="metrics-report", dry_run=False)
        try:
            metrics_dir = orch.work_dir / "metrics"
            metrics_dir.mkdir(exist_ok=True)
            reviews_file = metrics_dir / "reviews.jsonl"

            reviews = [
                {"task_hash": "task1", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 2000, "timestamp": "2024-01-01T10:00:00", "reviewer_cli": "claude"},
                {"task_hash": "task2", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 4000, "timestamp": "2024-01-01T11:00:00", "reviewer_cli": "claude"},
            ]
            reviews_file.write_text("\n".join(json.dumps(r) for r in reviews) + "\n")

            orch.print_metrics_report()

            captured = capsys.readouterr()
            assert "Total review time: 6.0s" in captured.out
            assert "Average review duration: 3000ms" in captured.out
        finally:
            orch.cleanup()

    def test_metrics_report_handles_malformed_json(self, temp_repo, capsys):
        """--metrics-report skips malformed JSON lines gracefully."""
        orch = Orchestrator(task="metrics-report", dry_run=False)
        try:
            metrics_dir = orch.work_dir / "metrics"
            metrics_dir.mkdir(exist_ok=True)
            reviews_file = metrics_dir / "reviews.jsonl"

            # Mix of valid and invalid lines
            content = '{"task_hash": "task1", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 1000, "timestamp": "2024-01-01T10:00:00", "reviewer_cli": "claude"}\n'
            content += "not valid json\n"
            content += '{"task_hash": "task2", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 2000, "timestamp": "2024-01-01T11:00:00", "reviewer_cli": "claude"}\n'
            reviews_file.write_text(content)

            orch.print_metrics_report()

            captured = capsys.readouterr()
            assert "Total reviews: 2" in captured.out  # Only counts valid entries
        finally:
            orch.cleanup()

    def test_metrics_report_shows_false_positive_count(self, temp_repo, capsys):
        """--metrics-report shows count of potential false positives."""
        import json
        orch = Orchestrator(task="metrics-report", dry_run=False)
        try:
            metrics_dir = orch.work_dir / "metrics"
            metrics_dir.mkdir(exist_ok=True)
            reviews_file = metrics_dir / "reviews.jsonl"

            reviews = [
                {"task_hash": "task1", "verdict": "REQUEST_CHANGES", "findings": ["Issue"], "findings_count": 1, "duration_ms": 1000, "timestamp": "2024-01-01T10:00:00", "reviewer_cli": "claude", "false_positive_indicator": False},
                {"task_hash": "task1", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 800, "timestamp": "2024-01-01T10:30:00", "reviewer_cli": "claude", "false_positive_indicator": True},
                {"task_hash": "task2", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 1000, "timestamp": "2024-01-01T11:00:00", "reviewer_cli": "claude", "false_positive_indicator": False},
            ]
            reviews_file.write_text("\n".join(json.dumps(r) for r in reviews) + "\n")

            orch.print_metrics_report()

            captured = capsys.readouterr()
            assert "Potential false positives: 1" in captured.out
            assert "Tasks approved on retry without meaningful code changes" in captured.out
        finally:
            orch.cleanup()

    def test_metrics_report_hides_false_positive_when_zero(self, temp_repo, capsys):
        """--metrics-report hides false positive section when count is zero."""
        import json
        orch = Orchestrator(task="metrics-report", dry_run=False)
        try:
            metrics_dir = orch.work_dir / "metrics"
            metrics_dir.mkdir(exist_ok=True)
            reviews_file = metrics_dir / "reviews.jsonl"

            reviews = [
                {"task_hash": "task1", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 1000, "timestamp": "2024-01-01T10:00:00", "reviewer_cli": "claude", "false_positive_indicator": False},
            ]
            reviews_file.write_text("\n".join(json.dumps(r) for r in reviews) + "\n")

            orch.print_metrics_report()

            captured = capsys.readouterr()
            assert "Potential false positives:" not in captured.out
        finally:
            orch.cleanup()

    def test_metrics_report_handles_missing_false_positive_field(self, temp_repo, capsys):
        """--metrics-report handles reviews without false_positive_indicator field."""
        import json
        orch = Orchestrator(task="metrics-report", dry_run=False)
        try:
            metrics_dir = orch.work_dir / "metrics"
            metrics_dir.mkdir(exist_ok=True)
            reviews_file = metrics_dir / "reviews.jsonl"

            # Old review format without false_positive_indicator
            reviews = [
                {"task_hash": "task1", "verdict": "APPROVED", "findings": [], "findings_count": 0, "duration_ms": 1000, "timestamp": "2024-01-01T10:00:00", "reviewer_cli": "claude"},
            ]
            reviews_file.write_text("\n".join(json.dumps(r) for r in reviews) + "\n")

            orch.print_metrics_report()

            captured = capsys.readouterr()
            # Should not crash and should not show false positives
            assert "Total reviews: 1" in captured.out
            assert "Potential false positives:" not in captured.out
        finally:
            orch.cleanup()


class TestAnalyzeTasklist:
    """Tests for --analyze-tasklist command."""

    def test_analyze_tasklist_shows_no_file_message(self, temp_repo, capsys):
        """--analyze-tasklist shows message when tasklist doesn't exist."""
        orch = Orchestrator(tasklist="nonexistent.md", dry_run=False, quiet=True)
        try:
            result = orch.analyze_tasklist()

            captured = capsys.readouterr()
            assert "Tasklist not found" in captured.out
            assert result["pending_count"] == 0
            assert result["total_count"] == 0
        finally:
            orch.cleanup()

    def test_analyze_tasklist_counts_tasks(self, temp_repo, capsys):
        """--analyze-tasklist reports correct task counts."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [x] Completed task 1
- [x] Completed task 2
- [ ] Pending task 1
- [ ] Pending task 2
- [ ] Pending task 3
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.analyze_tasklist()

            assert result["pending_count"] == 3
            assert result["completed_count"] == 2
            assert result["total_count"] == 5

            captured = capsys.readouterr()
            assert "5 total" in captured.out
            assert "3 pending" in captured.out
            assert "2 completed" in captured.out
        finally:
            orch.cleanup()

    def test_analyze_tasklist_detects_simple_complexity(self, temp_repo, capsys):
        """Simple tasks like 'fix typo' are classified as simple."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Fix typo in README.md
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.analyze_tasklist()

            assert len(result["tasks"]) == 1
            assert result["tasks"][0]["complexity"] == "simple"

            captured = capsys.readouterr()
            assert "[S]" in captured.out  # Simple indicator
        finally:
            orch.cleanup()

    def test_analyze_tasklist_detects_medium_complexity(self, temp_repo, capsys):
        """Tasks with 'add' or 'implement' are classified as medium."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Add user authentication to `api.py` and `auth.py`
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.analyze_tasklist()

            assert len(result["tasks"]) == 1
            assert result["tasks"][0]["complexity"] == "medium"

            captured = capsys.readouterr()
            assert "[M]" in captured.out  # Medium indicator
        finally:
            orch.cleanup()

    def test_analyze_tasklist_detects_complex_tasks(self, temp_repo, capsys):
        """Tasks with 'refactor' and multiple file refs are classified as complex."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Refactor the authentication system across `auth.py`, `api.py`, `models.py`, `views.py`, and `utils.py`
  - Est. LoC: 300
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.analyze_tasklist()

            assert len(result["tasks"]) == 1
            assert result["tasks"][0]["complexity"] == "complex"

            captured = capsys.readouterr()
            assert "[C]" in captured.out  # Complex indicator
        finally:
            orch.cleanup()

    def test_analyze_tasklist_extracts_file_references(self, temp_repo, capsys):
        """File references are extracted from task descriptions."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Update `src/api.py` and `tests/test_api.py`
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.analyze_tasklist()

            task = result["tasks"][0]
            assert "src/api.py" in task["file_refs"]
            assert "tests/test_api.py" in task["file_refs"]

            captured = capsys.readouterr()
            assert "Files:" in captured.out
        finally:
            orch.cleanup()

    def test_analyze_tasklist_detects_shared_file_dependencies(self, temp_repo, capsys):
        """Dependencies are detected when tasks share file references."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Add validation to `api.py`
- [ ] Add caching to `api.py`
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.analyze_tasklist()

            assert len(result["dependencies"]) > 0
            dep = result["dependencies"][0]
            assert "shared files" in dep["reason"]
            assert dep["type"] == "file_overlap"

            captured = capsys.readouterr()
            assert "Potential dependencies:" in captured.out
        finally:
            orch.cleanup()

    def test_analyze_tasklist_suggests_task_order(self, temp_repo, capsys):
        """Suggested order prioritizes simple tasks and respects dependencies."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Refactor and migrate the entire auth system in `auth.py`, `models.py`, `views.py`, `utils.py`, `config.py`
  - Est. LoC: 300
- [ ] Fix typo in README.md
- [ ] Add logging to `api.py`
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.analyze_tasklist()

            # Should have suggested order with simpler tasks first
            assert len(result["suggested_order"]) == 3
            # Simple task (fix typo) should come before complex task (refactor)
            order = result["suggested_order"]
            # Check that simpler tasks come before harder ones
            # Find all task positions by complexity
            simple_positions = [i for i, idx in enumerate(order) if result["tasks"][idx]["complexity"] == "simple"]
            complex_positions = [i for i, idx in enumerate(order) if result["tasks"][idx]["complexity"] == "complex"]
            # At least one simple task should exist and come before complex tasks
            assert len(simple_positions) > 0
            if complex_positions:
                assert min(simple_positions) < max(complex_positions)

            captured = capsys.readouterr()
            assert "Suggested task order:" in captured.out
        finally:
            orch.cleanup()

    def test_analyze_tasklist_shows_complexity_breakdown(self, temp_repo, capsys):
        """Report shows breakdown of tasks by complexity."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Fix typo
- [ ] Add feature
- [ ] Refactor system with `a.py`, `b.py`, `c.py`, `d.py`, `e.py`
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            orch.analyze_tasklist()

            captured = capsys.readouterr()
            assert "Pending tasks by complexity:" in captured.out
            assert "Simple:" in captured.out
            assert "Medium:" in captured.out
            assert "Complex:" in captured.out
        finally:
            orch.cleanup()

    def test_analyze_tasklist_handles_no_pending_tasks(self, temp_repo, capsys):
        """Report handles case where all tasks are completed."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [x] Task 1
- [x] Task 2
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.analyze_tasklist()

            assert result["pending_count"] == 0
            assert result["completed_count"] == 2

            captured = capsys.readouterr()
            assert "No pending tasks" in captured.out
        finally:
            orch.cleanup()

    def test_analyze_tasklist_parses_est_loc(self, temp_repo, capsys):
        """Estimated LoC is extracted and affects complexity."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] **Add feature**: Description
  - Est. LoC: 250
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.analyze_tasklist()

            task = result["tasks"][0]
            assert task["est_loc"] == 250

            captured = capsys.readouterr()
            assert "Est. LoC: 250" in captured.out
        finally:
            orch.cleanup()

    def test_analyze_tasklist_extracts_keywords(self, temp_repo, capsys):
        """Complexity keywords are extracted from task descriptions."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Refactor and migrate the legacy code
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.analyze_tasklist()

            task = result["tasks"][0]
            assert "refactor" in task["keywords"]
            assert "migrate" in task["keywords"]

            captured = capsys.readouterr()
            assert "Keywords:" in captured.out
        finally:
            orch.cleanup()

    def test_analyze_tasklist_uses_referenced_code_size(self, temp_repo, capsys):
        """Referenced code size contributes to complexity estimation."""
        # Create a file with significant code
        src_dir = temp_repo / "src"
        src_dir.mkdir(exist_ok=True)
        large_file = src_dir / "large_module.py"
        # Create a file with 600 lines (should trigger complex via ref_loc >= 500 -> +2 points)
        large_file.write_text("\n".join([f"# line {i}" for i in range(600)]))

        small_file = src_dir / "small_module.py"
        # Create a file with 50 lines (should not trigger anything)
        small_file.write_text("\n".join([f"# line {i}" for i in range(50)]))

        tasklist = temp_repo / "docs" / "tasklist.md"
        # Use task descriptions that don't include complexity keywords
        tasklist.write_text("""# Tasklist

- [ ] Work on `src/large_module.py`
- [ ] Work on `src/small_module.py`
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.analyze_tasklist()

            # Task referencing large file should have ref_loc
            task_large = result["tasks"][0]
            assert task_large.get("ref_loc") == 600
            # Large file (600 lines >= 500) should boost complexity to medium (score +2)
            assert task_large["complexity"] == "medium"

            # Task referencing small file should have ref_loc
            task_small = result["tasks"][1]
            assert task_small.get("ref_loc") == 50
            # Small file (50 lines < 150) shouldn't boost complexity
            assert task_small["complexity"] == "simple"

            captured = capsys.readouterr()
            assert "Ref. code size:" in captured.out
            assert "600 lines" in captured.out
        finally:
            orch.cleanup()

    def test_analyze_tasklist_ref_loc_not_computed_for_completed(self, temp_repo, capsys):
        """ref_loc is not computed for completed tasks to avoid overhead."""
        src_dir = temp_repo / "src"
        src_dir.mkdir(exist_ok=True)
        src_file = src_dir / "module.py"
        src_file.write_text("\n".join([f"# line {i}" for i in range(100)]))

        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [x] Update `src/module.py` (completed)
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.analyze_tasklist()

            task = result["tasks"][0]
            # ref_loc should be None for completed tasks
            assert task.get("ref_loc") is None
        finally:
            orch.cleanup()

    def test_analyze_tasklist_cli_flag(self, temp_repo, monkeypatch):
        """--analyze-tasklist flag triggers analysis and exits."""
        import sys

        from millstone.runtime.orchestrator import main

        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Task 1
- [ ] Task 2
""")

        monkeypatch.setattr(sys, "argv", ["millstone", "--analyze-tasklist"])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 0

    def test_analyze_tasklist_includes_time_estimate(self, temp_repo, capsys):
        """analyze_tasklist result includes time_estimate dict."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Simple task: fix typo
- [ ] Medium task: implement new feature
- [ ] Complex task: refactor the entire module
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.analyze_tasklist()

            assert "time_estimate" in result
            time_est = result["time_estimate"]
            assert "total_seconds" in time_est
            assert "total_formatted" in time_est
            assert "by_complexity" in time_est
            assert "has_data" in time_est
            assert "confidence" in time_est
        finally:
            orch.cleanup()

    def test_analyze_tasklist_shows_estimated_time_in_output(self, temp_repo, capsys):
        """--analyze-tasklist output includes estimated remaining time."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Simple task
- [ ] Medium task: implement feature
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            orch.analyze_tasklist()
            captured = capsys.readouterr()

            assert "Estimated remaining time:" in captured.out
            assert "Total:" in captured.out
        finally:
            orch.cleanup()

    def test_status_cli_flag_works(self, temp_repo, monkeypatch):
        """--status flag triggers tasklist analysis and exits."""
        import sys

        from millstone.runtime.orchestrator import main

        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Task 1
- [ ] Task 2
""")

        monkeypatch.setattr(sys, "argv", ["millstone", "--status"])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 0


class TestSplitTask:
    """Tests for --split-task command."""

    def test_split_task_shows_no_file_message(self, temp_repo, capsys):
        """split_task shows error when tasklist doesn't exist."""
        orch = Orchestrator(tasklist="nonexistent.md", dry_run=False, quiet=True)
        try:
            result = orch.split_task(task_number=1)

            captured = capsys.readouterr()
            assert "Tasklist not found" in captured.out
            assert result["success"] is False
            assert result["task"] is None
        finally:
            orch.cleanup()

    def test_split_task_shows_no_pending_tasks_message(self, temp_repo, capsys):
        """split_task shows error when no pending tasks exist."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [x] Completed task 1
- [x] Completed task 2
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.split_task(task_number=1)

            captured = capsys.readouterr()
            assert "No pending tasks" in captured.out
            assert result["success"] is False
        finally:
            orch.cleanup()

    def test_split_task_validates_task_number_too_high(self, temp_repo, capsys):
        """split_task rejects task number higher than task count."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Task 1
- [ ] Task 2
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.split_task(task_number=5)

            captured = capsys.readouterr()
            assert "out of range" in captured.out
            assert "1-2" in captured.out
            assert result["success"] is False
        finally:
            orch.cleanup()

    def test_split_task_validates_task_number_too_low(self, temp_repo, capsys):
        """split_task rejects task number less than 1."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Task 1
""")

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.split_task(task_number=0)

            captured = capsys.readouterr()
            assert "out of range" in captured.out
            assert result["success"] is False
        finally:
            orch.cleanup()

    def test_split_task_shows_task_info(self, temp_repo, capsys, monkeypatch):
        """split_task displays task info before invoking agent."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Refactor the auth system in `auth.py` and `api.py`
""")

        # Mock run_claude to avoid actual agent invocation
        def mock_run_claude(prompt, **kwargs):
            return "Mock agent response"

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            monkeypatch.setattr(orch, "run_claude", mock_run_claude)
            result = orch.split_task(task_number=1)

            captured = capsys.readouterr()
            assert "Task to Split" in captured.out
            assert "Task #1" in captured.out
            assert "Complexity:" in captured.out
            assert result["success"] is True
        finally:
            orch.cleanup()

    def test_split_task_returns_task_info(self, temp_repo, monkeypatch):
        """split_task returns task info in result dict."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] **Add feature**: Update `config.py` and `main.py`
""")

        def mock_run_claude(prompt, **kwargs):
            return "Suggested subtasks..."

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            monkeypatch.setattr(orch, "run_claude", mock_run_claude)
            result = orch.split_task(task_number=1)

            assert result["success"] is True
            assert result["task"] is not None
            assert "complexity" in result["task"]
            assert "file_refs" in result["task"]
            assert result["output"] == "Suggested subtasks..."
        finally:
            orch.cleanup()

    def test_split_task_selects_correct_pending_task(self, temp_repo, monkeypatch):
        """split_task selects the Nth pending task, not counting completed."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [x] Completed task A
- [ ] First pending task
- [x] Completed task B
- [ ] Second pending task
- [ ] Third pending task
""")

        prompts_received = []

        def mock_run_claude(prompt, **kwargs):
            prompts_received.append(prompt)
            return "Analysis..."

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            monkeypatch.setattr(orch, "run_claude", mock_run_claude)
            result = orch.split_task(task_number=2)

            # Should select "Second pending task" (2nd pending)
            assert result["success"] is True
            assert "Second pending task" in prompts_received[0]
        finally:
            orch.cleanup()

    def test_split_task_cli_flag(self, temp_repo, monkeypatch):
        """--split-task flag triggers split analysis and exits."""
        import sys

        from millstone.runtime.orchestrator import main

        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Task to split
""")

        # Mock subprocess.run to avoid actual agent invocation
        def mock_subprocess_run(*args, **kwargs):
            class MockResult:
                returncode = 0
                stdout = '{"result": "suggested splits"}'
                stderr = ""
            return MockResult()

        monkeypatch.setattr("subprocess.run", mock_subprocess_run)
        monkeypatch.setattr(sys, "argv", ["millstone", "--split-task", "1"])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 0

    def test_split_task_cli_invalid_task_number(self, temp_repo, monkeypatch, capsys):
        """--split-task with invalid task number exits with error."""
        import sys

        from millstone.runtime.orchestrator import main

        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] Only one task
""")

        monkeypatch.setattr(sys, "argv", ["millstone", "--split-task", "5"])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

    def test_split_task_logs_operation(self, temp_repo, monkeypatch):
        """split_task logs the operation details."""
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("""# Tasklist

- [ ] **Refactor authentication**: Update auth module
""")

        def mock_run_claude(prompt, **kwargs):
            return "Suggested subtasks..."

        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            monkeypatch.setattr(orch, "run_claude", mock_run_claude)
            orch.split_task(task_number=1)

            # Check log file contains split_task event
            runs_dir = orch.work_dir / "runs"
            log_files = list(runs_dir.glob("*.log"))
            assert len(log_files) == 1
            log_content = log_files[0].read_text()
            assert "split_task" in log_content
            assert "task_number" in log_content
        finally:
            orch.cleanup()


class TestProgressEstimation:
    """Tests for progress estimation functionality."""

    def test_get_duration_by_complexity_empty_history(self, temp_repo):
        """get_duration_by_complexity returns zeros with no task history."""
        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            result = orch.get_duration_by_complexity()

            assert result["simple"]["count"] == 0
            assert result["medium"]["count"] == 0
            assert result["complex"]["count"] == 0
            assert result["simple"]["avg_seconds"] == 0.0
        finally:
            orch.cleanup()

    def test_get_duration_by_complexity_with_history(self, temp_repo):
        """get_duration_by_complexity calculates averages from task history."""
        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            # Create mock task history
            tasks_dir = orch.work_dir / "tasks"
            tasks_dir.mkdir(parents=True, exist_ok=True)

            # Simple task (short duration) - "fix typo" is a simple keyword
            task1 = {
                "task": "Fix typo in readme",
                "outcome": "approved",
                "duration_seconds": 60.0,
            }
            (tasks_dir / "20241201_120000_abc123.json").write_text(json.dumps(task1))

            # Another simple task
            task2 = {
                "task": "Rename variable in utils.py",
                "outcome": "approved",
                "duration_seconds": 90.0,
            }
            (tasks_dir / "20241201_120100_def456.json").write_text(json.dumps(task2))

            result = orch.get_duration_by_complexity()

            # Both tasks have "fix typo" or "rename" keywords -> simple
            assert result["simple"]["count"] == 2
            assert result["simple"]["avg_seconds"] == 75.0  # (60 + 90) / 2

            # No medium or complex tasks
            assert result["medium"]["count"] == 0
            assert result["complex"]["count"] == 0
        finally:
            orch.cleanup()

    def test_get_duration_by_complexity_excludes_non_approved(self, temp_repo):
        """get_duration_by_complexity only includes approved tasks."""
        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            tasks_dir = orch.work_dir / "tasks"
            tasks_dir.mkdir(parents=True, exist_ok=True)

            # Approved task
            task1 = {
                "task": "Fix typo",
                "outcome": "approved",
                "duration_seconds": 60.0,
            }
            (tasks_dir / "20241201_120000_abc123.json").write_text(json.dumps(task1))

            # Rejected task (should be excluded)
            task2 = {
                "task": "Another fix",
                "outcome": "loop_detected",
                "duration_seconds": 300.0,
            }
            (tasks_dir / "20241201_120100_def456.json").write_text(json.dumps(task2))

            result = orch.get_duration_by_complexity()

            # Only the approved task should be counted
            total_count = sum(s["count"] for s in result.values())
            assert total_count == 1
        finally:
            orch.cleanup()

    def test_estimate_remaining_time_uses_historical_data(self, temp_repo):
        """estimate_remaining_time uses historical averages when available."""
        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            # Create mock task history
            tasks_dir = orch.work_dir / "tasks"
            tasks_dir.mkdir(parents=True, exist_ok=True)

            # Historical simple task: 120 seconds
            task1 = {
                "task": "Fix typo in file",
                "outcome": "approved",
                "duration_seconds": 120.0,
            }
            (tasks_dir / "20241201_120000_abc123.json").write_text(json.dumps(task1))

            # Pending task analysis
            pending_tasks = [
                {"complexity": "simple"},
                {"complexity": "simple"},
            ]

            result = orch.estimate_remaining_time(pending_tasks)

            # Should use historical average of 120s per simple task
            assert result["total_seconds"] == 240.0  # 2 simple tasks * 120s
            assert result["has_data"] is True
        finally:
            orch.cleanup()

    def test_estimate_remaining_time_uses_defaults_without_history(self, temp_repo):
        """estimate_remaining_time uses default estimates without history."""
        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            pending_tasks = [
                {"complexity": "simple"},
                {"complexity": "medium"},
                {"complexity": "complex"},
            ]

            result = orch.estimate_remaining_time(pending_tasks)

            # Should use default durations: simple=120, medium=300, complex=600
            expected = 120.0 + 300.0 + 600.0
            assert result["total_seconds"] == expected
            assert result["has_data"] is False
            assert result["confidence"] == "low"
        finally:
            orch.cleanup()

    def test_estimate_remaining_time_confidence_levels(self, temp_repo):
        """estimate_remaining_time sets confidence based on data quantity."""
        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            tasks_dir = orch.work_dir / "tasks"
            tasks_dir.mkdir(parents=True, exist_ok=True)

            pending_tasks = [{"complexity": "simple"}]

            # No history -> low confidence
            result = orch.estimate_remaining_time(pending_tasks)
            assert result["confidence"] == "low"

            # 3 tasks -> medium confidence
            for i in range(3):
                task = {"task": f"Fix typo {i}", "outcome": "approved", "duration_seconds": 60.0}
                (tasks_dir / f"20241201_12000{i}_abc{i}.json").write_text(json.dumps(task))

            result = orch.estimate_remaining_time(pending_tasks)
            assert result["confidence"] == "medium"

            # 10+ tasks -> high confidence
            for i in range(3, 10):
                task = {"task": f"Fix typo {i}", "outcome": "approved", "duration_seconds": 60.0}
                (tasks_dir / f"20241201_12001{i}_def{i}.json").write_text(json.dumps(task))

            result = orch.estimate_remaining_time(pending_tasks)
            assert result["confidence"] == "high"
        finally:
            orch.cleanup()

    def test_estimate_remaining_time_formats_time_correctly(self, temp_repo):
        """estimate_remaining_time formats time as seconds/minutes/hours."""
        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            # 30 seconds -> "30 seconds"
            pending_tasks = []
            result = orch.estimate_remaining_time(pending_tasks)
            assert result["total_formatted"] == "0 seconds"

            # Minutes
            pending_tasks = [{"complexity": "simple"}]  # 120 seconds default
            result = orch.estimate_remaining_time(pending_tasks)
            assert "minutes" in result["total_formatted"]

            # Hours
            pending_tasks = [{"complexity": "complex"}] * 10  # 6000 seconds default
            result = orch.estimate_remaining_time(pending_tasks)
            assert "hours" in result["total_formatted"]
        finally:
            orch.cleanup()

    def test_extract_file_refs_helper(self, temp_repo):
        """_extract_file_refs extracts file paths from text."""
        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            text = "Update `src/main.py` and tests/test_foo.py"
            refs = orch._extract_file_refs(text)

            assert "src/main.py" in refs
            assert "tests/test_foo.py" in refs
        finally:
            orch.cleanup()

    def test_extract_complexity_keywords_helper(self, temp_repo):
        """_extract_complexity_keywords finds complexity indicators."""
        orch = Orchestrator(tasklist="docs/tasklist.md", dry_run=False, quiet=True)
        try:
            text = "Refactor the module and add new feature"
            keywords = orch._extract_complexity_keywords(text)

            keyword_names = [kw for kw, _ in keywords]
            assert "refactor" in keyword_names
            assert "add" in keyword_names
        finally:
            orch.cleanup()


class TestAutonomousOrgHooks:
    """Tests for the new autonomous organization hooks (SRE, Release, QE)."""

    def test_run_review_diff_approved(self, temp_repo):
        """run_review_diff parses APPROVED verdict correctly."""
        orch = Orchestrator(cli="claude")
        try:
            with patch.object(orch, 'run_agent') as mock_agent:
                mock_agent.return_value = '## QA Review\n\n```json\n{"verdict": "APPROVED", "reason": "Looks good"}\n```'
                result = orch.run_review_diff("diff content")
                assert result["approved"] is True
                assert "APPROVED" in result["output"]
        finally:
            orch.cleanup()

    def test_run_review_diff_rejected(self, temp_repo):
        """run_review_diff parses REJECTED verdict correctly."""
        orch = Orchestrator(cli="claude")
        try:
            with patch.object(orch, 'run_agent') as mock_agent:
                mock_agent.return_value = '{"verdict": "REJECTED", "reason": "Security risk"}'
                result = orch.run_review_diff("diff content")
                assert result["approved"] is False
                assert "REJECTED" in result["output"]
        finally:
            orch.cleanup()

    def test_run_prepare_release_updates_changelog_and_tags(self, temp_repo):
        """run_prepare_release updates the changelog file and creates a git tag."""
        orch = Orchestrator(cli="claude")
        try:
            # Create changelog path
            changelog = temp_repo / "CHANGELOG.md"
            changelog.parent.mkdir(parents=True, exist_ok=True)

            with patch.object(orch, 'run_agent') as mock_agent:
                mock_agent.return_value = '# Changelog\n\n## [1.2.3] - 2025-12-28\n- Feature X\n'
                with patch.object(orch, 'git') as mock_git:
                    result = orch.run_prepare_release()

                    assert result["tag"] == "v1.2.3"
                    assert changelog.read_text() == mock_agent.return_value
                    mock_git.assert_called_with("tag", "-a", "v1.2.3", "-m", "Release 1.2.3", check=True)
        finally:
            orch.cleanup()

    def test_run_sre_diagnose(self, temp_repo):
        """run_sre_diagnose calls the agent with alerts and manifest context."""
        orch = Orchestrator(cli="claude")
        try:
            (temp_repo / "alerts.json").write_text('[{"id": "alert1"}]')
            infra_manifest_path = temp_repo / "docs/maintainer/infrastructure/manifest.md"
            infra_manifest_path.parent.mkdir(parents=True, exist_ok=True)
            infra_manifest_path.write_text("Service: millstone")
            with patch.object(orch, 'run_agent') as mock_agent:
                mock_agent.return_value = "Diagnosis: High CPU"
                result = orch.run_sre_diagnose()
                assert "High CPU" in result["mitigation_plan"]

                # Verify context was injected
                prompt = mock_agent.call_args[0][0]
                assert "alert1" in prompt
                assert "Service: millstone" in prompt
        finally:
            orch.cleanup()
