"""E2E tests for MCP deferred writes (staging mode) during analyze, design, and plan phases.

Verifies that when the opportunity/design/tasklist provider is MCP-backed and
approve_opportunities/approve_designs/approve_plans=True, the agent writes to a
local staging file instead of MCP.  On re-run (--continue), the staged artifacts
are synced to MCP before the next phase begins.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from millstone.artifact_providers.mcp import (
    MCPDesignProvider,
    MCPOpportunityProvider,
    MCPTasklistProvider,
)
from millstone.runtime.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OPP_STAGING_CONTENT = """\
- [ ] **Improve caching**
  - Opportunity ID: improve-caching
  - Description: Add LRU cache to hot paths
  - ROI: 8.0
"""


def _make_orchestrator(
    repo_dir: Path,
    *,
    mcp_server: str = "github",
    approve_opportunities: bool = True,
    **kwargs,
) -> Orchestrator:
    """Return an Orchestrator with an MCPOpportunityProvider injected."""
    millstone_dir = repo_dir / ".millstone"
    millstone_dir.mkdir(exist_ok=True)
    tasklist = millstone_dir / "tasklist.md"
    if not tasklist.exists():
        tasklist.write_text("# Tasklist\n\n")

    orch = Orchestrator(
        repo_dir=repo_dir,
        tasklist=str(tasklist),
        review_designs=False,
        approve_opportunities=approve_opportunities,
        task_constraints={
            "require_tests": False,
            "require_criteria": False,
            "require_risk": False,
            "require_context": False,
        },
        **kwargs,
    )
    # Inject MCP opportunity provider after construction
    orch._outer_loop_manager.opportunity_provider = MCPOpportunityProvider(mcp_server=mcp_server)
    return orch


def _side_effect_write_staging(repo: Path) -> None:
    """Side effect that writes opportunities to the staging file."""
    staging_path = repo / ".millstone" / "opportunities.md"
    staging_path.write_text(_OPP_STAGING_CONTENT)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAnalyzeStaging:
    """Analyze phase with MCP + approve_opportunities writes to staging file."""

    def test_analyze_writes_to_staging_file_not_mcp(self, temp_repo: Path) -> None:
        """When staging is active, the agent receives file-based write instructions
        and opportunities are written to a local staging file, not MCP."""
        orch = _make_orchestrator(temp_repo, approve_opportunities=True)
        opp_provider = orch._outer_loop_manager.opportunity_provider
        assert isinstance(opp_provider, MCPOpportunityProvider)

        # Track whether write_opportunity was called on the MCP provider
        mcp_write_called = False
        original_write = opp_provider.write_opportunity

        def _tracking_write(opp):
            nonlocal mcp_write_called
            mcp_write_called = True
            return original_write(opp)

        opp_provider.write_opportunity = _tracking_write

        # The analyzer agent will write to the staging file via side_effect
        result = orch._outer_loop_manager.run_analyze(
            load_prompt_callback=lambda name: (
                "Analyze {{OPPORTUNITY_WRITE_INSTRUCTIONS}} {{HARD_SIGNALS}} {{PROJECT_GOALS}} {{KNOWN_ISSUES}} {{ROLLBACK_CONTEXT}}"
            ),
            run_agent_callback=lambda prompt, **kw: (
                _side_effect_write_staging(temp_repo),
                "done",
            )[1],
            log_callback=lambda *_, **kw: None,
        )

        assert result["success"] is True
        assert result.get("staged") is True
        assert result.get("staging_file") is not None
        assert not mcp_write_called, "MCP write_opportunity should NOT have been called"

        # Verify staging file exists with content
        staging_path = Path(result["staging_file"])
        assert staging_path.exists()
        content = staging_path.read_text()
        assert "Improve caching" in content

    def test_analyze_no_staging_when_approve_false(self, temp_repo: Path) -> None:
        """When approve_opportunities=False, no staging occurs — direct MCP write."""
        orch = _make_orchestrator(temp_repo, approve_opportunities=False)
        opp_provider = orch._outer_loop_manager.opportunity_provider
        assert isinstance(opp_provider, MCPOpportunityProvider)

        prompts_captured = []

        def _capture_prompt(prompt, **kw):
            prompts_captured.append(prompt)
            return "done"

        orch._outer_loop_manager.run_analyze(
            load_prompt_callback=lambda name: (
                "Analyze {{OPPORTUNITY_WRITE_INSTRUCTIONS}} {{HARD_SIGNALS}} {{PROJECT_GOALS}} {{KNOWN_ISSUES}} {{ROLLBACK_CONTEXT}}"
            ),
            run_agent_callback=_capture_prompt,
            log_callback=lambda *_, **kw: None,
        )

        # The prompt should contain MCP instructions, not file-based ones
        assert len(prompts_captured) > 0
        assert "MCP" in prompts_captured[0], (
            f"Expected MCP write instructions in prompt, got: {prompts_captured[0][:200]}"
        )

    def test_run_cycle_gate_persists_pending_sync(self, temp_repo: Path) -> None:
        """run_cycle() with MCP+approve_opportunities saves pending_mcp_syncs in state.json."""
        orch = _make_orchestrator(temp_repo, approve_opportunities=True)

        mock_opp = MagicMock()
        mock_opp.title = "Improve caching"
        mock_opp.opportunity_id = "improve-caching"
        mock_opp.roi_score = 8.0
        mock_opp.requires_design = True

        # run_analyze returns staged result
        staged_file = str(temp_repo / ".millstone" / "opportunities.md")
        analyze_result = {
            "success": True,
            "opportunity_count": 1,
            "staged": True,
            "staging_file": staged_file,
        }

        # Write the staging file so _select_opportunity can read it
        (temp_repo / ".millstone" / "opportunities.md").write_text(_OPP_STAGING_CONTENT)

        with (
            patch.object(orch, "has_remaining_tasks", return_value=False),
            patch.object(orch, "run_analyze", return_value=analyze_result),
        ):
            exit_code = orch.run_cycle()
        orch.cleanup()

        assert exit_code == 0

        state_file = temp_repo / ".millstone" / "state.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        outer = state.get("outer_loop", {})
        assert outer.get("stage") == "analyze_complete"

        pending = outer.get("pending_mcp_syncs")
        assert pending is not None, "Expected pending_mcp_syncs in state.json"
        assert len(pending) == 1
        assert pending[0]["type"] == "opportunities"
        assert pending[0]["staging_file"] == staged_file

    def test_run_cycle_rerun_resumes_from_analyze_complete(self, temp_repo: Path) -> None:
        """Plain --cycle rerun with analyze_complete checkpoint syncs MCP writes,
        then resumes from design phase instead of re-running analysis.
        With approve_designs=True (default), it halts at the design gate."""
        orch = _make_orchestrator(temp_repo, approve_opportunities=True)
        opp_provider = orch._outer_loop_manager.opportunity_provider
        assert isinstance(opp_provider, MCPOpportunityProvider)

        # Write staging file with opportunities
        staging_path = temp_repo / ".millstone" / "opportunities.md"
        staging_path.write_text(_OPP_STAGING_CONTENT)

        # Pre-populate state.json as if a previous --cycle halted at analyze gate
        state_file = temp_repo / ".millstone" / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "outer_loop": {
                        "stage": "analyze_complete",
                        "opportunity": "Improve caching",
                        "pending_mcp_syncs": [
                            {
                                "type": "opportunities",
                                "staging_file": str(staging_path),
                                "last_synced_index": 0,
                            }
                        ],
                    }
                }
            )
        )

        # Track calls to verify design is called and analyze is NOT re-called
        design_called = False
        analyze_called = False

        def _mock_design(opportunity, **kw):
            nonlocal design_called
            design_called = True
            return {
                "success": True,
                "design_file": str(temp_repo / ".millstone" / "designs" / "test.md"),
            }

        def _mock_analyze(*_, **kw):
            nonlocal analyze_called
            analyze_called = True
            return {"success": True}

        # Mock write_opportunity to track MCP sync
        synced_titles = []
        opp_provider.set_agent_callback(lambda p, **k: "ok")
        opp_provider.write_opportunity = lambda opp: synced_titles.append(opp.title)

        with (
            patch.object(orch, "run_design", side_effect=_mock_design),
            patch.object(orch, "run_analyze", side_effect=_mock_analyze),
        ):
            exit_code = orch.run_cycle()
        orch.cleanup()

        assert exit_code == 0
        assert design_called, "run_design should have been called (resume from analyze_complete)"
        assert not analyze_called, "run_analyze should NOT have been re-called on cycle rerun"
        assert "Improve caching" in synced_titles, (
            "Pending opportunities should have been synced to MCP"
        )

        # With approve_designs=True, state should show design_complete (halted at gate)
        assert state_file.exists(), "state.json should exist (halted at design gate)"
        state = json.loads(state_file.read_text())
        assert state["outer_loop"]["stage"] == "design_complete"


# ---------------------------------------------------------------------------
# Multi-opportunity staging content for partial-sync tests
# ---------------------------------------------------------------------------

_MULTI_OPP_STAGING_CONTENT = """\
- [ ] **Improve caching**
  - Opportunity ID: improve-caching
  - Description: Add LRU cache to hot paths
  - ROI: 8.0
- [ ] **Add retries**
  - Opportunity ID: add-retries
  - Description: Add retry logic to API calls
  - ROI: 6.0
- [ ] **Reduce allocations**
  - Opportunity ID: reduce-allocs
  - Description: Pool frequently allocated objects
  - ROI: 5.0
"""


class TestSyncPartialFailure:
    """Tests for partial-sync recovery in _sync_pending_mcp_writes."""

    def test_partial_sync_persists_last_synced_index(self, temp_repo: Path) -> None:
        """If write_opportunity fails mid-sync, last_synced_index is persisted
        so retry skips already-synced items."""
        orch = _make_orchestrator(temp_repo, approve_opportunities=True)
        opp_provider = orch._outer_loop_manager.opportunity_provider
        assert isinstance(opp_provider, MCPOpportunityProvider)

        # Write multi-opp staging file
        staging_path = temp_repo / ".millstone" / "opportunities.md"
        staging_path.write_text(_MULTI_OPP_STAGING_CONTENT)

        # Set up state.json with pending sync
        state_file = temp_repo / ".millstone" / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "outer_loop": {
                        "stage": "analyze_complete",
                        "pending_mcp_syncs": [
                            {
                                "type": "opportunities",
                                "staging_file": str(staging_path),
                                "last_synced_index": 0,
                            }
                        ],
                    }
                }
            )
        )

        # Mock write_opportunity to succeed once then fail
        write_call_count = 0

        def _failing_write(opp):
            nonlocal write_call_count
            write_call_count += 1
            if write_call_count == 2:
                raise RuntimeError("MCP write failed")

        opp_provider.set_agent_callback(lambda p, **k: "ok")
        opp_provider.write_opportunity = _failing_write

        pending_syncs = [
            {
                "type": "opportunities",
                "staging_file": str(staging_path),
                "last_synced_index": 0,
            }
        ]

        # Sync should raise on the second item
        with contextlib.suppress(RuntimeError):
            orch._sync_pending_mcp_writes(pending_syncs)

        # Verify last_synced_index was persisted as 1 (first item succeeded)
        state = json.loads(state_file.read_text())
        syncs = state["outer_loop"]["pending_mcp_syncs"]
        assert syncs[0]["last_synced_index"] == 1, (
            f"Expected last_synced_index=1 after first item succeeded, got {syncs[0]['last_synced_index']}"
        )
        # Staging file should still exist (not archived)
        assert staging_path.exists()

    def test_retry_after_partial_failure_skips_synced(self, temp_repo: Path) -> None:
        """Retry after partial failure starts from last_synced_index, not 0."""
        orch = _make_orchestrator(temp_repo, approve_opportunities=True)
        opp_provider = orch._outer_loop_manager.opportunity_provider
        assert isinstance(opp_provider, MCPOpportunityProvider)

        # Write multi-opp staging file
        staging_path = temp_repo / ".millstone" / "opportunities.md"
        staging_path.write_text(_MULTI_OPP_STAGING_CONTENT)

        # State shows first item already synced
        state_file = temp_repo / ".millstone" / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "outer_loop": {
                        "stage": "analyze_complete",
                        "pending_mcp_syncs": [
                            {
                                "type": "opportunities",
                                "staging_file": str(staging_path),
                                "last_synced_index": 1,
                            }
                        ],
                    }
                }
            )
        )

        written_titles = []

        def _tracking_write(opp):
            written_titles.append(opp.title)

        opp_provider.set_agent_callback(lambda p, **k: "ok")
        opp_provider.write_opportunity = _tracking_write

        pending_syncs = [
            {
                "type": "opportunities",
                "staging_file": str(staging_path),
                "last_synced_index": 1,
            }
        ]

        orch._sync_pending_mcp_writes(pending_syncs)

        # Should have skipped "Improve caching" (index 0) and written the remaining 2
        assert "Improve caching" not in written_titles
        assert "Add retries" in written_titles
        assert "Reduce allocations" in written_titles
        assert len(written_titles) == 2

        # Staging file should be archived
        assert not staging_path.exists()
        assert Path(str(staging_path) + ".synced").exists()

        # pending_mcp_syncs should be cleared from state
        state = json.loads(state_file.read_text())
        assert "pending_mcp_syncs" not in state.get("outer_loop", {})


# ===========================================================================
# Design staging tests
# ===========================================================================

_DESIGN_CONTENT = """\
- **design_id**: test-design
- **title**: Test Design
- **status**: draft
- **opportunity_ref**: improve-caching
---

## Summary

This is a test design document.
"""


def _make_design_orchestrator(
    repo_dir: Path,
    *,
    mcp_server: str = "github",
    approve_designs: bool = True,
    approve_opportunities: bool = False,
    **kwargs,
) -> Orchestrator:
    """Return an Orchestrator with an MCPDesignProvider injected."""
    millstone_dir = repo_dir / ".millstone"
    millstone_dir.mkdir(exist_ok=True)
    tasklist = millstone_dir / "tasklist.md"
    if not tasklist.exists():
        tasklist.write_text("# Tasklist\n\n")

    orch = Orchestrator(
        repo_dir=repo_dir,
        tasklist=str(tasklist),
        review_designs=False,
        approve_designs=approve_designs,
        approve_opportunities=approve_opportunities,
        task_constraints={
            "require_tests": False,
            "require_criteria": False,
            "require_risk": False,
            "require_context": False,
        },
        **kwargs,
    )
    # Inject MCP design provider
    orch._outer_loop_manager.design_provider = MCPDesignProvider(mcp_server=mcp_server)
    return orch


def _side_effect_write_design(repo: Path) -> None:
    """Side effect that writes a design to the staging directory."""
    staging_dir = repo / ".millstone" / "designs"
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "test-design.md").write_text(_DESIGN_CONTENT)


class TestDesignStaging:
    """Design phase with MCP + approve_designs writes to staging directory."""

    def test_design_writes_to_staging_not_mcp(self, temp_repo: Path) -> None:
        """When staging is active, the agent receives file-based write instructions
        and designs are written to a local staging dir, not MCP."""
        orch = _make_design_orchestrator(temp_repo, approve_designs=True)
        design_provider = orch._outer_loop_manager.design_provider
        assert isinstance(design_provider, MCPDesignProvider)

        # Provide an opportunity so reference integrity check passes
        opp_file = temp_repo / ".millstone" / "opportunities.md"
        opp_file.write_text(
            "- [ ] **Improve caching**\n"
            "  - Opportunity ID: improve-caching\n"
            "  - Description: Add caching\n"
        )

        # Track whether write_design was called on the MCP provider
        mcp_write_called = False
        original_write = design_provider.write_design

        def _tracking_write(design):
            nonlocal mcp_write_called
            mcp_write_called = True
            return original_write(design)

        design_provider.write_design = _tracking_write

        result = orch._outer_loop_manager.run_design(
            opportunity="Improve caching",
            opportunity_id="improve-caching",
            load_prompt_callback=lambda name: (
                "Design {{OPPORTUNITY}} {{OPPORTUNITY_ID}} {{DESIGN_WRITE_INSTRUCTIONS}}"
            ),
            run_agent_callback=lambda prompt, **kw: (
                _side_effect_write_design(temp_repo),
                "done",
            )[1],
            log_callback=lambda *_, **kw: None,
        )

        assert result["success"] is True
        assert result.get("staged") is True
        assert result.get("staging_file") is not None
        assert not mcp_write_called, "MCP write_design should NOT have been called"

        # Verify staging directory has the design
        staging_path = Path(result["staging_file"])
        assert staging_path.exists()

    def test_design_no_staging_when_approve_false(self, temp_repo: Path) -> None:
        """When approve_designs=False, no staging occurs — direct MCP write."""
        orch = _make_design_orchestrator(temp_repo, approve_designs=False)
        design_provider = orch._outer_loop_manager.design_provider
        assert isinstance(design_provider, MCPDesignProvider)

        prompts_captured = []

        def _capture_prompt(prompt, **kw):
            prompts_captured.append(prompt)
            # Write design to the default location so detection works
            _side_effect_write_design(temp_repo)
            return "done"

        orch._outer_loop_manager.run_design(
            opportunity="Improve caching",
            load_prompt_callback=lambda name: (
                "Design {{OPPORTUNITY}} {{OPPORTUNITY_ID}} {{DESIGN_WRITE_INSTRUCTIONS}}"
            ),
            run_agent_callback=_capture_prompt,
            log_callback=lambda *_, **kw: None,
        )

        assert len(prompts_captured) > 0
        assert "MCP" in prompts_captured[0], (
            f"Expected MCP write instructions in prompt, got: {prompts_captured[0][:200]}"
        )


class TestCycleResumeApprovalGates:
    """Verify that run_cycle() resume path respects approval gates."""

    def test_resume_from_analyze_complete_halts_at_design_gate(self, temp_repo: Path) -> None:
        """When resuming from analyze_complete with approve_designs=True,
        the cycle should halt at the design approval gate after run_design()."""
        orch = _make_design_orchestrator(
            temp_repo,
            approve_designs=True,
            approve_opportunities=False,
        )
        opp_provider = orch._outer_loop_manager.opportunity_provider

        # Write a staging file so pending_mcp_syncs has something to sync
        staging_path = temp_repo / ".millstone" / "opportunities.md"
        staging_path.write_text(_OPP_STAGING_CONTENT)

        # Make the opportunity provider MCP-backed for sync
        orch._outer_loop_manager.opportunity_provider = MCPOpportunityProvider(mcp_server="github")
        opp_provider = orch._outer_loop_manager.opportunity_provider
        opp_provider.set_agent_callback(lambda p, **k: "ok")
        opp_provider.write_opportunity = lambda opp: None  # no-op

        # Pre-populate state.json as if a previous --cycle halted at analyze gate
        state_file = temp_repo / ".millstone" / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "outer_loop": {
                        "stage": "analyze_complete",
                        "opportunity": "Improve caching",
                        "pending_mcp_syncs": [
                            {
                                "type": "opportunities",
                                "staging_file": str(staging_path),
                                "last_synced_index": 0,
                            }
                        ],
                    }
                }
            )
        )

        design_called = False
        plan_called = False

        def _mock_design(opportunity, **kw):
            nonlocal design_called
            design_called = True
            return {
                "success": True,
                "design_file": str(temp_repo / ".millstone" / "designs" / "test.md"),
            }

        def _mock_plan(design_path, **kw):
            nonlocal plan_called
            plan_called = True
            return {"success": True, "tasks_added": 1}

        with (
            patch.object(orch, "run_design", side_effect=_mock_design),
            patch.object(orch, "run_plan", side_effect=_mock_plan),
            patch.object(orch, "run", return_value=0),
        ):
            exit_code = orch.run_cycle()
        orch.cleanup()

        assert exit_code == 0
        assert design_called, "run_design should have been called"
        assert not plan_called, "run_plan should NOT have been called (halted at design gate)"

        # State should show design_complete checkpoint
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert state["outer_loop"]["stage"] == "design_complete"

    def test_resume_from_design_complete_halts_at_plan_gate(self, temp_repo: Path) -> None:
        """When resuming from design_complete with approve_plans=True,
        the cycle should halt at the plan approval gate after run_plan()."""
        orch = _make_design_orchestrator(
            temp_repo,
            approve_designs=False,
            approve_opportunities=False,
        )
        orch.approve_plans = True

        # Inject MCP design provider so pending_mcp_syncs triggers resume
        orch._outer_loop_manager.design_provider = MCPDesignProvider(mcp_server="github")
        design_provider = orch._outer_loop_manager.design_provider
        design_provider.set_agent_callback(lambda p, **k: "ok")
        design_provider.write_design = lambda d: None

        staging_dir = temp_repo / ".millstone" / "designs"
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "test-design.md").write_text(_DESIGN_CONTENT)

        state_file = temp_repo / ".millstone" / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "outer_loop": {
                        "stage": "design_complete",
                        "design_path": str(temp_repo / ".millstone" / "designs" / "test.md"),
                        "opportunity": "Improve caching",
                        "pending_mcp_syncs": [
                            {
                                "type": "designs",
                                "staging_file": str(staging_dir),
                                "last_synced_index": 0,
                            }
                        ],
                    }
                }
            )
        )

        plan_called = False
        run_called = False

        def _mock_plan(design_path, **kw):
            nonlocal plan_called
            plan_called = True
            return {"success": True, "tasks_added": 2}

        def _mock_run():
            nonlocal run_called
            run_called = True
            return 0

        with (
            patch.object(orch, "run_plan", side_effect=_mock_plan),
            patch.object(orch, "run", side_effect=_mock_run),
        ):
            exit_code = orch.run_cycle()
        orch.cleanup()

        assert exit_code == 0
        assert plan_called, "run_plan should have been called"
        assert not run_called, "run() should NOT have been called (halted at plan gate)"

        # State should show plan_complete checkpoint
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert state["outer_loop"]["stage"] == "plan_complete"

    def test_resume_no_gates_runs_through(self, temp_repo: Path) -> None:
        """When both approve_designs and approve_plans are False,
        resume from analyze_complete runs all the way through."""
        orch = _make_design_orchestrator(
            temp_repo,
            approve_designs=False,
            approve_opportunities=False,
        )
        orch.approve_plans = False

        # Set up pending_mcp_syncs so run_cycle triggers resume path
        staging_path = temp_repo / ".millstone" / "opportunities.md"
        staging_path.write_text(_OPP_STAGING_CONTENT)
        orch._outer_loop_manager.opportunity_provider = MCPOpportunityProvider(mcp_server="github")
        opp_provider = orch._outer_loop_manager.opportunity_provider
        opp_provider.set_agent_callback(lambda p, **k: "ok")
        opp_provider.write_opportunity = lambda opp: None

        state_file = temp_repo / ".millstone" / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "outer_loop": {
                        "stage": "analyze_complete",
                        "opportunity": "Improve caching",
                        "pending_mcp_syncs": [
                            {
                                "type": "opportunities",
                                "staging_file": str(staging_path),
                                "last_synced_index": 0,
                            }
                        ],
                    }
                }
            )
        )

        def _mock_design(opportunity, **kw):
            return {
                "success": True,
                "design_file": str(temp_repo / ".millstone" / "designs" / "test.md"),
            }

        def _mock_plan(design_path, **kw):
            return {"success": True, "tasks_added": 1}

        with (
            patch.object(orch, "run_design", side_effect=_mock_design),
            patch.object(orch, "run_plan", side_effect=_mock_plan),
            patch.object(orch, "run", return_value=0),
        ):
            exit_code = orch.run_cycle()
        orch.cleanup()

        assert exit_code == 0
        # State should be cleared after successful completion
        assert not state_file.exists()


# ===========================================================================
# Tasklist staging tests
# ===========================================================================

_STAGED_TASKS_CONTENT = """\
- [ ] **Add caching**: Add LRU cache to hot paths
- [ ] **Add retries**: Add retry logic to API calls
"""


def _make_tasklist_orchestrator(
    repo_dir: Path,
    *,
    mcp_server: str = "github",
    approve_plans: bool = True,
    **kwargs,
) -> Orchestrator:
    """Return an Orchestrator with an MCPTasklistProvider injected."""
    millstone_dir = repo_dir / ".millstone"
    millstone_dir.mkdir(exist_ok=True)
    tasklist = millstone_dir / "tasklist.md"
    if not tasklist.exists():
        tasklist.write_text("# Tasklist\n\n")

    orch = Orchestrator(
        repo_dir=repo_dir,
        tasklist=str(tasklist),
        review_designs=False,
        approve_plans=approve_plans,
        task_constraints={
            "require_tests": False,
            "require_criteria": False,
            "require_risk": False,
            "require_context": False,
        },
        **kwargs,
    )
    # Inject MCP tasklist provider after construction
    orch._outer_loop_manager.tasklist_provider = MCPTasklistProvider(mcp_server=mcp_server)
    return orch


class TestPlanStaging:
    """Plan phase with MCP + approve_plans writes to staging file."""

    def test_run_cycle_plan_gate_persists_pending_sync(self, temp_repo: Path) -> None:
        """run_cycle() with MCP tasklist + approve_plans saves pending_mcp_syncs."""
        orch = _make_tasklist_orchestrator(
            temp_repo,
            approve_plans=True,
            approve_designs=False,
            approve_opportunities=False,
        )

        # Mock run_analyze, run_design, and run_plan
        staged_file = str(temp_repo / ".millstone" / "tasklist-staged.md")

        def _mock_plan(design_path, **kw):
            # Write the staged tasks file
            Path(staged_file).write_text(_STAGED_TASKS_CONTENT)
            return {
                "success": True,
                "tasks_added": 2,
                "staged": True,
                "staging_file": staged_file,
            }

        # Write opportunities file so update_opportunity_status doesn't fail
        opp_file = temp_repo / ".millstone" / "opportunities.md"
        opp_file.write_text(
            "- [ ] **Add caching**\n"
            "  - Opportunity ID: add-caching\n"
            "  - Description: Add caching\n"
            "  - ROI: 8.0\n"
        )

        with (
            patch.object(orch, "has_remaining_tasks", return_value=False),
            patch.object(
                orch,
                "run_analyze",
                return_value={
                    "success": True,
                    "opportunity_count": 1,
                },
            ),
            patch.object(
                orch._outer_loop_manager,
                "_select_opportunity",
                return_value=MagicMock(
                    title="Add caching",
                    opportunity_id="add-caching",
                    roi_score=8.0,
                    requires_design=True,
                ),
            ),
            patch.object(
                orch,
                "run_design",
                return_value={
                    "success": True,
                    "design_file": str(temp_repo / ".millstone" / "designs" / "test.md"),
                },
            ),
            patch.object(orch, "run_plan", side_effect=_mock_plan),
        ):
            exit_code = orch.run_cycle()
        orch.cleanup()

        assert exit_code == 0

        state_file = temp_repo / ".millstone" / "state.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        outer = state.get("outer_loop", {})
        assert outer.get("stage") == "plan_complete"

        pending = outer.get("pending_mcp_syncs")
        assert pending is not None, "Expected pending_mcp_syncs in state.json"
        assert len(pending) == 1
        assert pending[0]["type"] == "tasks"
        assert pending[0]["staging_file"] == staged_file

    def test_task_sync_writes_to_mcp_on_resume(self, temp_repo: Path) -> None:
        """Resuming from plan_complete syncs staged tasks to MCP."""
        orch = _make_tasklist_orchestrator(temp_repo, approve_plans=True)
        task_provider = orch._outer_loop_manager.tasklist_provider
        assert isinstance(task_provider, MCPTasklistProvider)

        # Write staging file
        staging_path = temp_repo / ".millstone" / "tasklist-staged.md"
        staging_path.write_text(_STAGED_TASKS_CONTENT)

        # Track append_tasks calls
        appended_tasks = []
        task_provider.set_agent_callback(lambda p, **k: "ok")

        def _tracking_append(tasks):
            appended_tasks.extend(tasks)

        task_provider.append_tasks = _tracking_append

        pending_syncs = [
            {
                "type": "tasks",
                "staging_file": str(staging_path),
                "last_synced_index": 0,
            }
        ]

        # Set up state.json
        state_file = temp_repo / ".millstone" / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "outer_loop": {
                        "stage": "plan_complete",
                        "pending_mcp_syncs": pending_syncs,
                    }
                }
            )
        )

        orch._sync_pending_mcp_writes(pending_syncs)

        assert len(appended_tasks) == 2
        assert appended_tasks[0].title == "Add caching"
        assert appended_tasks[1].title == "Add retries"

        # Staging file should be archived
        assert not staging_path.exists()
        assert Path(str(staging_path) + ".synced").exists()


# ===========================================================================
# Corrupt / missing staging file tests
# ===========================================================================


class TestSyncCorruptOrMissingStagingFile:
    """Tests that _sync_pending_mcp_writes fails loudly on missing or corrupt staging files.

    The safety contract:
    - Missing staging file → error (not silent skip)
    - Unparseable staging file (zero opportunities parsed) → error (not silent archive)
    """

    def test_missing_staging_file_raises_error(self, temp_repo: Path) -> None:
        """If the staging file referenced by a pending sync entry is missing,
        _sync_pending_mcp_writes should fail with a clear error rather than
        silently skipping the entry and clearing it from state."""
        orch = _make_orchestrator(temp_repo, approve_opportunities=True)
        opp_provider = orch._outer_loop_manager.opportunity_provider
        assert isinstance(opp_provider, MCPOpportunityProvider)
        opp_provider.set_agent_callback(lambda p, **k: "ok")

        # Reference a staging file that does NOT exist
        nonexistent_path = str(temp_repo / ".millstone" / "opportunities-gone.md")

        # Set up state.json with pending sync pointing at missing file
        state_file = temp_repo / ".millstone" / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "outer_loop": {
                        "stage": "analyze_complete",
                        "pending_mcp_syncs": [
                            {
                                "type": "opportunities",
                                "staging_file": nonexistent_path,
                                "last_synced_index": 0,
                            }
                        ],
                    }
                }
            )
        )

        pending_syncs = [
            {
                "type": "opportunities",
                "staging_file": nonexistent_path,
                "last_synced_index": 0,
            }
        ]

        # The method should raise an error, not silently skip
        with pytest.raises((FileNotFoundError, RuntimeError, OSError)):
            orch._sync_pending_mcp_writes(pending_syncs)

    def test_corrupt_staging_file_zero_opportunities_raises_error(self, temp_repo: Path) -> None:
        """If the staging file contains unparseable content (malformed markdown
        yielding zero opportunities), _sync_pending_mcp_writes should fail
        loudly rather than silently archiving an empty result."""
        orch = _make_orchestrator(temp_repo, approve_opportunities=True)
        opp_provider = orch._outer_loop_manager.opportunity_provider
        assert isinstance(opp_provider, MCPOpportunityProvider)
        opp_provider.set_agent_callback(lambda p, **k: "ok")
        opp_provider.write_opportunity = lambda opp: None  # should never be called

        # Write a staging file with garbage content that won't parse as opportunities
        staging_path = temp_repo / ".millstone" / "opportunities.md"
        staging_path.write_text("This is not valid opportunity markdown at all.\nJust junk.\n")

        state_file = temp_repo / ".millstone" / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "outer_loop": {
                        "stage": "analyze_complete",
                        "pending_mcp_syncs": [
                            {
                                "type": "opportunities",
                                "staging_file": str(staging_path),
                                "last_synced_index": 0,
                            }
                        ],
                    }
                }
            )
        )

        pending_syncs = [
            {
                "type": "opportunities",
                "staging_file": str(staging_path),
                "last_synced_index": 0,
            }
        ]

        # The method should raise an error when zero items are parsed
        with pytest.raises((ValueError, RuntimeError)):
            orch._sync_pending_mcp_writes(pending_syncs)
