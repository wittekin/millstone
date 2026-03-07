"""Tests for MCP deferred writes (staging mode) on opportunity, design, and tasklist providers."""

from pathlib import Path

import pytest

from millstone.artifact_providers.mcp import (
    MCPDesignProvider,
    MCPOpportunityProvider,
    MCPTasklistProvider,
)

# ---------------------------------------------------------------------------
# staging() context manager
# ---------------------------------------------------------------------------


class TestStagingContextManager:
    """Tests for the staging() context manager on MCPOpportunityProvider."""

    def test_staging_mode_true_inside_context(self):
        provider = MCPOpportunityProvider(mcp_server="github")
        assert provider._staging_mode is False
        staging_path = Path("/tmp/test_opportunities.md")
        with provider.staging(staging_path):
            assert provider._staging_mode is True
            assert provider._staging_path == staging_path
        assert provider._staging_mode is False
        assert provider._staging_path is None

    def test_staging_mode_false_after_exception(self):
        provider = MCPOpportunityProvider(mcp_server="github")
        staging_path = Path("/tmp/test_opportunities.md")
        with pytest.raises(RuntimeError, match="boom"), provider.staging(staging_path):
            assert provider._staging_mode is True
            raise RuntimeError("boom")
        assert provider._staging_mode is False
        assert provider._staging_path is None


# ---------------------------------------------------------------------------
# get_prompt_placeholders — staging vs normal mode
# ---------------------------------------------------------------------------


class TestStagingPlaceholders:
    """Tests that get_prompt_placeholders() returns correct instructions based on staging mode."""

    def test_normal_mode_returns_mcp_instructions(self):
        provider = MCPOpportunityProvider(mcp_server="github")
        placeholders = provider.get_prompt_placeholders()
        assert "github" in placeholders["OPPORTUNITY_WRITE_INSTRUCTIONS"].lower()
        assert "MCP" in placeholders["OPPORTUNITY_WRITE_INSTRUCTIONS"]

    def test_staging_mode_returns_file_instructions(self):
        provider = MCPOpportunityProvider(mcp_server="github")
        staging_path = Path("/tmp/staging/opportunities.md")
        with provider.staging(staging_path):
            placeholders = provider.get_prompt_placeholders()
        # Outside the context manager, check what was captured inside
        # Re-enter to assert
        with provider.staging(staging_path):
            placeholders = provider.get_prompt_placeholders()
            assert "Write your findings to" in placeholders["OPPORTUNITY_WRITE_INSTRUCTIONS"]
            assert str(staging_path) in placeholders["OPPORTUNITY_WRITE_INSTRUCTIONS"]
            assert "MCP" not in placeholders["OPPORTUNITY_WRITE_INSTRUCTIONS"]
            assert str(staging_path) in placeholders["OPPORTUNITY_READ_INSTRUCTIONS"]

    def test_staging_mode_with_projects(self):
        """Staging mode overrides project clause — instructions are file-based."""
        provider = MCPOpportunityProvider(mcp_server="linear", projects=["MyProject"])
        with provider.staging(Path("/tmp/opps.md")):
            placeholders = provider.get_prompt_placeholders()
            # File-based instructions should NOT mention MCP or project
            assert "MCP" not in placeholders["OPPORTUNITY_WRITE_INSTRUCTIONS"]
            assert "Write your findings to" in placeholders["OPPORTUNITY_WRITE_INSTRUCTIONS"]

    def test_normal_mode_after_staging_returns_mcp_instructions(self):
        """After exiting staging, placeholders revert to MCP instructions."""
        provider = MCPOpportunityProvider(mcp_server="github")
        with provider.staging(Path("/tmp/opps.md")):
            pass
        placeholders = provider.get_prompt_placeholders()
        assert "MCP" in placeholders["OPPORTUNITY_WRITE_INSTRUCTIONS"]


# ---------------------------------------------------------------------------
# _should_stage_opportunities — OuterLoopManager helper
# ---------------------------------------------------------------------------


class TestShouldStageOpportunities:
    """Tests for OuterLoopManager._should_stage_opportunities()."""

    def _make_olm(self, tmp_path, *, opportunity_provider=None, approve_opportunities=True):
        from millstone.loops.outer import OuterLoopManager

        work_dir = tmp_path / ".millstone"
        work_dir.mkdir(parents=True, exist_ok=True)
        tasklist = tmp_path / ".millstone" / "tasklist.md"
        tasklist.write_text("- [ ] task\n")

        return OuterLoopManager(
            work_dir=work_dir,
            repo_dir=tmp_path,
            tasklist=str(tasklist),
            task_constraints={},
            approve_opportunities=approve_opportunities,
            opportunity_provider=opportunity_provider,
        )

    def test_mcp_provider_with_approve_true(self, tmp_path):
        provider = MCPOpportunityProvider(mcp_server="github")
        olm = self._make_olm(tmp_path, opportunity_provider=provider, approve_opportunities=True)
        assert olm._should_stage_opportunities() is True

    def test_mcp_provider_with_approve_false(self, tmp_path):
        provider = MCPOpportunityProvider(mcp_server="github")
        olm = self._make_olm(tmp_path, opportunity_provider=provider, approve_opportunities=False)
        assert olm._should_stage_opportunities() is False

    def test_file_provider_with_approve_true(self, tmp_path):
        """File providers are never staged."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        provider = FileOpportunityProvider(tmp_path / "opportunities.md")
        olm = self._make_olm(tmp_path, opportunity_provider=provider, approve_opportunities=True)
        assert olm._should_stage_opportunities() is False


# ---------------------------------------------------------------------------
# Exception safety: staging mode resets on run_analyze failure
# ---------------------------------------------------------------------------


class TestRunAnalyzeStagingExceptionSafety:
    """Verify that staging mode is reset even when run_analyze raises."""

    def _make_olm(self, tmp_path, *, opportunity_provider=None, approve_opportunities=True):
        from millstone.loops.outer import OuterLoopManager

        work_dir = tmp_path / ".millstone"
        work_dir.mkdir(parents=True, exist_ok=True)
        tasklist = work_dir / "tasklist.md"
        tasklist.write_text("- [ ] task\n")

        return OuterLoopManager(
            work_dir=work_dir,
            repo_dir=tmp_path,
            tasklist=str(tasklist),
            task_constraints={},
            approve_opportunities=approve_opportunities,
            opportunity_provider=opportunity_provider,
        )

    def test_staging_mode_resets_on_agent_exception(self, tmp_path):
        """If the agent callback raises during run_analyze, staging mode is cleared."""
        provider = MCPOpportunityProvider(mcp_server="github")
        olm = self._make_olm(tmp_path, opportunity_provider=provider, approve_opportunities=True)

        def _exploding_agent(prompt, **kw):
            raise RuntimeError("agent crashed")

        with pytest.raises(RuntimeError, match="agent crashed"):
            olm.run_analyze(
                load_prompt_callback=lambda name: (
                    "Analyze {{OPPORTUNITY_WRITE_INSTRUCTIONS}} "
                    "{{HARD_SIGNALS}} {{PROJECT_GOALS}} {{KNOWN_ISSUES}} {{ROLLBACK_CONTEXT}}"
                ),
                run_agent_callback=_exploding_agent,
                log_callback=lambda *_, **kw: None,
            )

        assert provider._staging_mode is False, (
            "Staging mode should be reset after exception in run_analyze"
        )
        assert provider._staging_path is None


# ===========================================================================
# MCPDesignProvider staging tests
# ===========================================================================


class TestDesignStagingContextManager:
    """Tests for the staging() context manager on MCPDesignProvider."""

    def test_staging_mode_true_inside_context(self):
        provider = MCPDesignProvider(mcp_server="github")
        assert provider._staging_mode is False
        staging_path = Path("/tmp/test_designs")
        with provider.staging(staging_path):
            assert provider._staging_mode is True
            assert provider._staging_path == staging_path
        assert provider._staging_mode is False
        assert provider._staging_path is None

    def test_staging_mode_false_after_exception(self):
        provider = MCPDesignProvider(mcp_server="github")
        staging_path = Path("/tmp/test_designs")
        with pytest.raises(RuntimeError, match="boom"), provider.staging(staging_path):
            assert provider._staging_mode is True
            raise RuntimeError("boom")
        assert provider._staging_mode is False
        assert provider._staging_path is None


class TestDesignStagingPlaceholders:
    """Tests that get_prompt_placeholders() returns correct instructions based on staging mode."""

    def test_normal_mode_returns_mcp_instructions(self):
        provider = MCPDesignProvider(mcp_server="github")
        placeholders = provider.get_prompt_placeholders()
        assert "MCP" in placeholders["DESIGN_WRITE_INSTRUCTIONS"]

    def test_staging_mode_returns_file_instructions(self):
        provider = MCPDesignProvider(mcp_server="github")
        staging_path = Path("/tmp/staging/designs")
        with provider.staging(staging_path):
            placeholders = provider.get_prompt_placeholders()
            assert "Write the design document to" in placeholders["DESIGN_WRITE_INSTRUCTIONS"]
            assert str(staging_path) in placeholders["DESIGN_WRITE_INSTRUCTIONS"]
            assert "MCP" not in placeholders["DESIGN_WRITE_INSTRUCTIONS"]

    def test_staging_mode_with_projects(self):
        """Staging mode overrides project clause — instructions are file-based."""
        provider = MCPDesignProvider(mcp_server="linear", projects=["MyProject"])
        with provider.staging(Path("/tmp/designs")):
            placeholders = provider.get_prompt_placeholders()
            assert "MCP" not in placeholders["DESIGN_WRITE_INSTRUCTIONS"]
            assert "Write the design document to" in placeholders["DESIGN_WRITE_INSTRUCTIONS"]

    def test_normal_mode_after_staging_returns_mcp_instructions(self):
        """After exiting staging, placeholders revert to MCP instructions."""
        provider = MCPDesignProvider(mcp_server="github")
        with provider.staging(Path("/tmp/designs")):
            pass
        placeholders = provider.get_prompt_placeholders()
        assert "MCP" in placeholders["DESIGN_WRITE_INSTRUCTIONS"]


class TestShouldStageDesigns:
    """Tests for OuterLoopManager._should_stage_designs()."""

    def _make_olm(self, tmp_path, *, design_provider=None, approve_designs=True):
        from millstone.loops.outer import OuterLoopManager

        work_dir = tmp_path / ".millstone"
        work_dir.mkdir(parents=True, exist_ok=True)
        tasklist = tmp_path / ".millstone" / "tasklist.md"
        tasklist.write_text("- [ ] task\n")

        return OuterLoopManager(
            work_dir=work_dir,
            repo_dir=tmp_path,
            tasklist=str(tasklist),
            task_constraints={},
            approve_designs=approve_designs,
            design_provider=design_provider,
        )

    def test_mcp_provider_with_approve_true(self, tmp_path):
        provider = MCPDesignProvider(mcp_server="github")
        olm = self._make_olm(tmp_path, design_provider=provider, approve_designs=True)
        assert olm._should_stage_designs() is True

    def test_mcp_provider_with_approve_false(self, tmp_path):
        provider = MCPDesignProvider(mcp_server="github")
        olm = self._make_olm(tmp_path, design_provider=provider, approve_designs=False)
        assert olm._should_stage_designs() is False

    def test_file_provider_with_approve_true(self, tmp_path):
        """File providers are never staged."""
        from millstone.artifact_providers.file import FileDesignProvider

        provider = FileDesignProvider(tmp_path / "designs")
        olm = self._make_olm(tmp_path, design_provider=provider, approve_designs=True)
        assert olm._should_stage_designs() is False


class TestRunDesignStagingExceptionSafety:
    """Verify that staging mode is reset even when run_design raises."""

    def _make_olm(self, tmp_path, *, design_provider=None, approve_designs=True):
        from millstone.loops.outer import OuterLoopManager

        work_dir = tmp_path / ".millstone"
        work_dir.mkdir(parents=True, exist_ok=True)
        tasklist = work_dir / "tasklist.md"
        tasklist.write_text("- [ ] task\n")

        return OuterLoopManager(
            work_dir=work_dir,
            repo_dir=tmp_path,
            tasklist=str(tasklist),
            task_constraints={},
            approve_designs=approve_designs,
            design_provider=design_provider,
        )

    def test_staging_mode_resets_on_agent_exception(self, tmp_path):
        """If the agent callback raises during run_design, staging mode is cleared."""
        provider = MCPDesignProvider(mcp_server="github")
        olm = self._make_olm(tmp_path, design_provider=provider, approve_designs=True)

        def _exploding_agent(prompt, **kw):
            raise RuntimeError("agent crashed")

        with pytest.raises(RuntimeError, match="agent crashed"):
            olm.run_design(
                opportunity="Test opportunity",
                load_prompt_callback=lambda name: (
                    "Design {{OPPORTUNITY}} {{OPPORTUNITY_ID}} {{DESIGN_WRITE_INSTRUCTIONS}}"
                ),
                run_agent_callback=_exploding_agent,
                log_callback=lambda *_, **kw: None,
            )

        assert provider._staging_mode is False, (
            "Staging mode should be reset after exception in run_design"
        )
        assert provider._staging_path is None


# ===========================================================================
# MCPTasklistProvider staging tests
# ===========================================================================


class TestTasklistStagingContextManager:
    """Tests for the staging() context manager on MCPTasklistProvider."""

    def test_staging_mode_true_inside_context(self, tmp_path):
        provider = MCPTasklistProvider(mcp_server="github")
        assert provider._staging_mode is False
        staging_path = tmp_path / "tasklist-staged.md"
        with provider.staging(staging_path):
            assert provider._staging_mode is True
            assert provider._staging_path == staging_path
            assert provider._staging_provider is not None
        assert provider._staging_mode is False
        assert provider._staging_path is None
        assert provider._staging_provider is None

    def test_staging_mode_false_after_exception(self, tmp_path):
        provider = MCPTasklistProvider(mcp_server="github")
        staging_path = tmp_path / "tasklist-staged.md"
        with pytest.raises(RuntimeError, match="boom"), provider.staging(staging_path):
            assert provider._staging_mode is True
            raise RuntimeError("boom")
        assert provider._staging_mode is False
        assert provider._staging_path is None
        assert provider._staging_provider is None


class TestTasklistStagingPlaceholders:
    """Tests that get_prompt_placeholders() returns correct instructions based on staging mode."""

    def test_normal_mode_returns_mcp_instructions(self):
        provider = MCPTasklistProvider(mcp_server="github")
        placeholders = provider.get_prompt_placeholders()
        assert "MCP" in placeholders["TASKLIST_APPEND_INSTRUCTIONS"]
        assert "github" in placeholders["TASKLIST_READ_INSTRUCTIONS"].lower()

    def test_staging_mode_returns_file_instructions(self, tmp_path):
        provider = MCPTasklistProvider(mcp_server="github")
        staging_path = tmp_path / "tasklist-staged.md"
        with provider.staging(staging_path):
            placeholders = provider.get_prompt_placeholders()
            assert "MCP" not in placeholders["TASKLIST_APPEND_INSTRUCTIONS"]
            assert str(staging_path) in placeholders["TASKLIST_APPEND_INSTRUCTIONS"]
            assert str(staging_path) in placeholders["TASKLIST_READ_INSTRUCTIONS"]

    def test_staging_mode_with_labels(self, tmp_path):
        """Staging mode overrides label clause — instructions are file-based."""
        provider = MCPTasklistProvider(mcp_server="linear", labels=["millstone"])
        with provider.staging(tmp_path / "tasks.md"):
            placeholders = provider.get_prompt_placeholders()
            assert "MCP" not in placeholders["TASKLIST_APPEND_INSTRUCTIONS"]

    def test_normal_mode_after_staging_returns_mcp_instructions(self, tmp_path):
        """After exiting staging, placeholders revert to MCP instructions."""
        provider = MCPTasklistProvider(mcp_server="github")
        with provider.staging(tmp_path / "tasks.md"):
            pass
        placeholders = provider.get_prompt_placeholders()
        assert "MCP" in placeholders["TASKLIST_APPEND_INSTRUCTIONS"]


class TestTasklistStagingDelegation:
    """Tests that read methods delegate to local file during staging."""

    def test_list_tasks_reads_from_local_file(self, tmp_path):
        staging_path = tmp_path / "tasklist-staged.md"
        staging_path.write_text("- [ ] **Task A**: Do something\n- [ ] **Task B**: Do another\n")

        provider = MCPTasklistProvider(mcp_server="github")
        with provider.staging(staging_path):
            tasks = provider.list_tasks()
        assert len(tasks) == 2
        assert tasks[0].title == "Task A"
        assert tasks[1].title == "Task B"

    def test_get_snapshot_reads_from_local_file(self, tmp_path):
        staging_path = tmp_path / "tasklist-staged.md"
        content = "- [ ] **Task A**: Do something\n"
        staging_path.write_text(content)

        provider = MCPTasklistProvider(mcp_server="github")
        with provider.staging(staging_path):
            snapshot = provider.get_snapshot()
        assert "Task A" in snapshot

    def test_get_task_reads_from_local_file(self, tmp_path):
        staging_path = tmp_path / "tasklist-staged.md"
        staging_path.write_text("- [ ] **Task A**: Do something\n")

        provider = MCPTasklistProvider(mcp_server="github")
        with provider.staging(staging_path):
            tasks = provider.list_tasks()
            task = provider.get_task(tasks[0].task_id)
        assert task is not None
        assert task.title == "Task A"

    def test_list_tasks_empty_when_no_staging_file(self, tmp_path):
        staging_path = tmp_path / "tasklist-staged.md"
        provider = MCPTasklistProvider(mcp_server="github")
        with provider.staging(staging_path):
            tasks = provider.list_tasks()
        assert tasks == []


class TestShouldStageTasks:
    """Tests for OuterLoopManager._should_stage_tasks()."""

    def _make_olm(self, tmp_path, *, tasklist_provider=None, approve_plans=True):
        from millstone.loops.outer import OuterLoopManager

        work_dir = tmp_path / ".millstone"
        work_dir.mkdir(parents=True, exist_ok=True)
        tasklist = tmp_path / ".millstone" / "tasklist.md"
        tasklist.write_text("- [ ] task\n")

        return OuterLoopManager(
            work_dir=work_dir,
            repo_dir=tmp_path,
            tasklist=str(tasklist),
            task_constraints={},
            approve_plans=approve_plans,
            tasklist_provider=tasklist_provider,
        )

    def test_mcp_provider_with_approve_true(self, tmp_path):
        provider = MCPTasklistProvider(mcp_server="github")
        olm = self._make_olm(tmp_path, tasklist_provider=provider, approve_plans=True)
        assert olm._should_stage_tasks() is True

    def test_mcp_provider_with_approve_false(self, tmp_path):
        provider = MCPTasklistProvider(mcp_server="github")
        olm = self._make_olm(tmp_path, tasklist_provider=provider, approve_plans=False)
        assert olm._should_stage_tasks() is False

    def test_file_provider_with_approve_true(self, tmp_path):
        """File providers are never staged."""
        from millstone.artifact_providers.file import FileTasklistProvider

        provider = FileTasklistProvider(tmp_path / "tasklist.md")
        olm = self._make_olm(tmp_path, tasklist_provider=provider, approve_plans=True)
        assert olm._should_stage_tasks() is False


class TestIsMcpProviderStagingAware:
    """Tests that _is_mcp_provider() returns False during staging."""

    def _make_olm(self, tmp_path, *, tasklist_provider=None):
        from millstone.loops.outer import OuterLoopManager

        work_dir = tmp_path / ".millstone"
        work_dir.mkdir(parents=True, exist_ok=True)
        tasklist = tmp_path / ".millstone" / "tasklist.md"
        tasklist.write_text("- [ ] task\n")

        return OuterLoopManager(
            work_dir=work_dir,
            repo_dir=tmp_path,
            tasklist=str(tasklist),
            task_constraints={},
            tasklist_provider=tasklist_provider,
        )

    def test_mcp_provider_normal_mode(self, tmp_path):
        provider = MCPTasklistProvider(mcp_server="github")
        olm = self._make_olm(tmp_path, tasklist_provider=provider)
        assert olm._is_mcp_provider() is True

    def test_mcp_provider_staging_mode(self, tmp_path):
        provider = MCPTasklistProvider(mcp_server="github")
        olm = self._make_olm(tmp_path, tasklist_provider=provider)
        staging_path = tmp_path / "tasklist-staged.md"
        with provider.staging(staging_path):
            assert olm._is_mcp_provider() is False

    def test_mcp_provider_after_staging(self, tmp_path):
        provider = MCPTasklistProvider(mcp_server="github")
        olm = self._make_olm(tmp_path, tasklist_provider=provider)
        with provider.staging(tmp_path / "tasks.md"):
            pass
        assert olm._is_mcp_provider() is True
