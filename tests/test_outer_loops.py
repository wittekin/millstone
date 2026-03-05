"""Tests for OuterLoopManager provider construction and injection."""

from pathlib import Path

import pytest

from millstone.artifact_providers.protocols import (
    DesignProvider,
    OpportunityProvider,
    TasklistProvider,
)
from millstone.artifacts.models import (
    Design,
    DesignStatus,
    Opportunity,
    OpportunityStatus,
    TasklistItem,
    TaskStatus,
)
from millstone.loops.outer import OuterLoopManager


def _make_outer_manager(repo_dir: Path, **kwargs) -> OuterLoopManager:
    work_dir = repo_dir / ".millstone"
    work_dir.mkdir(exist_ok=True)
    return OuterLoopManager(
        work_dir=work_dir,
        repo_dir=repo_dir,
        tasklist=".millstone/tasklist.md",
        task_constraints={
            "max_loc": 200,
            "require_tests": False,
            "require_criteria": False,
            "require_risk": False,
            "require_context": False,
            "max_split_attempts": 0,
        },
        **kwargs,
    )


class MockOpportunityProvider:
    def list_opportunities(self) -> list[Opportunity]:
        return []

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        return None

    def write_opportunity(self, opportunity: Opportunity) -> None:
        return None

    def update_opportunity_status(self, opportunity_id: str, status: OpportunityStatus) -> None:
        return None

    def get_prompt_placeholders(self) -> dict[str, str]:
        return {}


class InMemoryTasklistProvider:
    def __init__(self, content: str) -> None:
        self._content = content
        self.restore_calls = 0

    def list_tasks(self) -> list[TasklistItem]:
        if "new-task" in self._content:
            return [
                TasklistItem(
                    task_id="new-task",
                    title="New Task",
                    status=TaskStatus.todo,
                )
            ]
        return []

    def get_task(self, task_id: str) -> TasklistItem | None:
        for task in self.list_tasks():
            if task.task_id == task_id:
                return task
        return None

    def append_tasks(self, tasks: list[TasklistItem]) -> None:
        return None

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        return None

    def get_snapshot(self) -> str:
        return self._content

    def restore_snapshot(self, content: str) -> None:
        self.restore_calls += 1
        self._content = content

    def get_prompt_placeholders(self) -> dict[str, str]:
        return {}


class InMemoryDesignProvider:
    def __init__(self) -> None:
        self._designs: dict[str, Design] = {}

    def list_designs(self) -> list[Design]:
        return list(self._designs.values())

    def get_design(self, design_id: str) -> Design | None:
        return self._designs.get(design_id)

    def write_design(self, design: Design) -> None:
        self._designs[design.design_id] = design

    def update_design_status(self, design_id: str, status: DesignStatus) -> None:
        design = self._designs.get(design_id)
        if design is not None:
            design.status = status

    def get_prompt_placeholders(self) -> dict[str, str]:
        return {}


def test_default_constructor_uses_file_backends(temp_repo):
    manager = _make_outer_manager(temp_repo)

    assert isinstance(manager.opportunity_provider, OpportunityProvider)
    assert isinstance(manager.design_provider, DesignProvider)
    assert isinstance(manager.tasklist_provider, TasklistProvider)
    assert manager.opportunity_provider.path == temp_repo / ".millstone" / "opportunities.md"
    assert manager.design_provider.path == temp_repo / ".millstone" / "designs"
    assert manager.tasklist_provider.path == temp_repo / ".millstone" / "tasklist.md"


def test_constructor_uses_injected_opportunity_provider(temp_repo):
    injected = MockOpportunityProvider()

    manager = _make_outer_manager(temp_repo, opportunity_provider=injected)

    assert manager.opportunity_provider is injected


def test_constructor_uses_provider_config_backends(temp_repo):
    opportunities_path = temp_repo / "custom" / "opps.md"
    designs_path = temp_repo / "custom" / "designs"
    tasklist_path = temp_repo / "custom" / "tasklist.md"
    provider_config = {
        "opportunity_provider": "file",
        "design_provider": "file",
        "tasklist_provider": "file",
        "opportunity_provider_options": {"path": str(opportunities_path)},
        "design_provider_options": {"path": str(designs_path)},
        "tasklist_provider_options": {"path": str(tasklist_path)},
    }

    manager = _make_outer_manager(temp_repo, provider_config=provider_config)

    assert isinstance(manager.opportunity_provider, OpportunityProvider)
    assert isinstance(manager.design_provider, DesignProvider)
    assert isinstance(manager.tasklist_provider, TasklistProvider)
    assert manager.opportunity_provider.path == opportunities_path
    assert manager.design_provider.path == designs_path
    assert manager.tasklist_provider.path == tasklist_path


def test_constructor_raises_for_unknown_provider_backend(temp_repo):
    provider_config = {"opportunity_provider": "unknown-backend"}

    with pytest.raises(ValueError, match="Unknown opportunity provider backend"):
        _make_outer_manager(temp_repo, provider_config=provider_config)


def test_commit_opportunities_uses_legacy_path(temp_repo):
    """commit_opportunities=True falls back to repo root opportunities.md."""
    manager = _make_outer_manager(temp_repo, commit_opportunities=True)
    assert manager.opportunity_provider.path == temp_repo / "opportunities.md"


def test_commit_designs_uses_legacy_path(temp_repo):
    """commit_designs=True falls back to repo root designs/."""
    manager = _make_outer_manager(temp_repo, commit_designs=True)
    assert manager.design_provider.path == temp_repo / "designs"


def test_explicit_provider_options_override_commit_flag(temp_repo):
    """Explicit provider_options path takes precedence over commit_* flag."""
    custom_path = temp_repo / "custom" / "opps.md"
    provider_config = {
        "opportunity_provider": "file",
        "opportunity_provider_options": {"path": str(custom_path)},
    }
    manager = _make_outer_manager(
        temp_repo, provider_config=provider_config, commit_opportunities=True
    )
    assert manager.opportunity_provider.path == custom_path


def test_explicit_tasklist_provider_options_override_commit_tasklist(temp_repo):
    """Explicit tasklist_provider_options.path takes precedence over commit_tasklist path.

    When commit_tasklist=True, main() sets tasklist to "docs/tasklist.md" and that value is
    forwarded to OuterLoopManager as its tasklist parameter. If the user has also configured
    tasklist_provider_options.path explicitly, that explicit path must win.
    """
    custom_path = temp_repo / "custom" / "my-tasks.md"
    # Use a sub-OuterLoopManager constructed directly to simulate commit_tasklist redirect.
    work_dir = temp_repo / ".millstone"
    work_dir.mkdir(exist_ok=True)
    provider_config = {
        "tasklist_provider": "file",
        "tasklist_provider_options": {"path": str(custom_path)},
    }
    manager = OuterLoopManager(
        work_dir=work_dir,
        repo_dir=temp_repo,
        tasklist="docs/tasklist.md",  # simulates commit_tasklist=True redirect
        task_constraints={
            "max_loc": 200,
            "require_tests": False,
            "require_criteria": False,
            "require_risk": False,
            "require_context": False,
            "max_split_attempts": 0,
        },
        provider_config=provider_config,
    )
    assert manager.tasklist_provider.path == custom_path


def test_provider_options_non_string_values_survive_pipeline(temp_repo):
    """Non-string option values (int, bool, dict, list) must not be coerced to strings."""
    from unittest.mock import patch

    input_options = {
        "path": str(temp_repo / ".millstone" / "opportunities.md"),
        "page_size": 50,
        "enabled": True,
        "tags": ["infra", "perf"],
        "meta": {"key": "value"},
    }
    provider_config = {"opportunity_provider_options": dict(input_options)}

    captured_options = {}

    real_get = __import__(
        "millstone.artifact_providers.registry", fromlist=["get_opportunity_provider"]
    ).get_opportunity_provider

    def capturing_get_opportunity_provider(backend, options):
        captured_options.update(options)
        return real_get(backend=backend, options=options)

    with patch(
        "millstone.loops.outer.get_opportunity_provider",
        side_effect=capturing_get_opportunity_provider,
    ):
        _make_outer_manager(temp_repo, provider_config=provider_config)

    # Verify non-string values are passed through uncoerced.
    assert captured_options["page_size"] == 50
    assert type(captured_options["page_size"]) is int
    assert captured_options["enabled"] is True
    assert type(captured_options["enabled"]) is bool
    assert captured_options["tags"] == ["infra", "perf"]
    assert type(captured_options["tags"]) is list
    assert captured_options["meta"] == {"key": "value"}
    assert type(captured_options["meta"]) is dict


def test_provider_options_string_only_config_unchanged(temp_repo):
    """Existing string-only provider configs continue to work identically."""
    custom_path = temp_repo / "opps.md"
    provider_config = {
        "opportunity_provider": "file",
        "opportunity_provider_options": {"path": str(custom_path)},
    }
    manager = _make_outer_manager(temp_repo, provider_config=provider_config)
    assert manager.opportunity_provider.path == custom_path


def test_run_analyze_succeeds_from_provider_without_filesystem_output(temp_repo):
    provider = MockOpportunityProvider()
    provider.list_opportunities = lambda: [  # type: ignore[method-assign]
        Opportunity(
            opportunity_id="opp-1",
            title="Test Opportunity",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    ]
    manager = _make_outer_manager(temp_repo, opportunity_provider=provider)

    result = manager.run_analyze(
        load_prompt_callback=lambda _name: "prompt_name: analyze_prompt.md",
        run_agent_callback=lambda _prompt: "ok",
    )

    assert result["success"] is True
    assert result["opportunity_count"] == 1
    assert result["opportunities_file"] is None


def test_run_plan_impl_supports_tasklist_provider_without_path_attribute(temp_repo):
    manager = _make_outer_manager(temp_repo)
    manager.opportunity_provider.write_opportunity(
        Opportunity(
            opportunity_id="opp-plan-test",
            title="Plan Test Opportunity",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    )
    manager.design_provider.write_design(
        Design(
            design_id="plan-test",
            title="Plan Test",
            status=DesignStatus.draft,
            body="Design body",
            opportunity_ref="opp-plan-test",
        )
    )
    tasklist_provider = InMemoryTasklistProvider("# Tasklist\n")
    manager.tasklist_provider = tasklist_provider
    manager.review_plan = lambda **_kwargs: {"approved": True}  # type: ignore[method-assign]

    def run_agent(prompt: str) -> str:
        if "prompt_name: plan_prompt.md" in prompt:
            tasklist_provider.restore_snapshot(
                "# Tasklist\n\n- [ ] **New Task**\n  - ID: new-task\n  - Context: none\n"
            )
        return "ok"

    result = manager._run_plan_impl(
        design_path=str(temp_repo / "designs" / "plan-test.md"),
        load_prompt_callback=lambda name: f"prompt_name: {name}\n{{{{TASKLIST_CONTENT}}}}",
        run_agent_callback=run_agent,
    )

    assert result["success"] is True
    assert result["tasks_added"] == 1
    assert not hasattr(tasklist_provider, "path")


def test_run_plan_impl_restores_snapshot_on_validation_failure_rollback(temp_repo):
    manager = _make_outer_manager(temp_repo)
    manager.opportunity_provider.write_opportunity(
        Opportunity(
            opportunity_id="opp-plan-rollback",
            title="Plan Rollback Opportunity",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    )
    manager.design_provider.write_design(
        Design(
            design_id="plan-rollback",
            title="Plan Rollback",
            status=DesignStatus.draft,
            body="Design body",
            opportunity_ref="opp-plan-rollback",
        )
    )
    original = "# Tasklist\n"
    tasklist_provider = InMemoryTasklistProvider(original)
    manager.tasklist_provider = tasklist_provider
    manager._validate_generated_tasks = lambda _old, _new: {  # type: ignore[method-assign]
        "valid": False,
        "tasks": [],
        "violations_summary": "invalid",
    }
    manager.review_plan = lambda **_kwargs: {"approved": False}  # type: ignore[method-assign]

    def run_agent(prompt: str) -> str:
        if "prompt_name: plan_prompt.md" in prompt or "prompt_name: plan_fix_prompt.md" in prompt:
            tasklist_provider.restore_snapshot(
                original + "\n- [ ] **Bad Task**\n  - ID: new-task\n  - Context: none\n"
            )
        return "ok"

    result = manager._run_plan_impl(
        design_path=str(temp_repo / "designs" / "plan-rollback.md"),
        load_prompt_callback=lambda name: f"prompt_name: {name}\n{{{{TASKLIST_CONTENT}}}}",
        run_agent_callback=run_agent,
    )

    assert result["success"] is False
    assert tasklist_provider.get_snapshot() == original
    assert tasklist_provider.restore_calls >= 1


def test_run_cycle_supports_non_file_design_reference_through_plan(temp_repo):
    design_provider = InMemoryDesignProvider()
    tasklist_provider = InMemoryTasklistProvider("# Tasklist\n")
    manager = _make_outer_manager(
        temp_repo,
        design_provider=design_provider,
        tasklist_provider=tasklist_provider,
        review_designs=True,
        approve_opportunities=False,
        approve_designs=False,
        approve_plans=False,
    )
    manager.review_plan = lambda **_kwargs: {"approved": True}  # type: ignore[method-assign]

    manager.opportunity_provider.write_opportunity(
        Opportunity(
            opportunity_id="non-file-opp",
            title="Non File Opportunity",
            status=OpportunityStatus.identified,
            description="Desc",
            roi_score=10.0,
        )
    )

    design_refs_reviewed: list[str] = []
    design_refs_planned: list[str] = []

    def run_design(objective: str, opportunity_id: str | None = None) -> dict:
        assert objective == "Non File Opportunity"
        assert opportunity_id == "non-file-opp"
        design_provider.write_design(
            Design(
                design_id="non-file-design",
                title="Non File Design",
                status=DesignStatus.draft,
                body="Design body from provider",
                opportunity_ref="non-file-opp",
            )
        )
        return {
            "success": True,
            "design_file": None,
            "design_id": "non-file-design",
        }

    def review_design(design_ref: str) -> dict:
        design_refs_reviewed.append(design_ref)
        return {"approved": True, "verdict": "APPROVED"}

    def run_plan(design_ref: str) -> dict:
        design_refs_planned.append(design_ref)

        def run_agent(prompt: str) -> str:
            if "prompt_name: plan_prompt.md" in prompt:
                tasklist_provider.restore_snapshot(
                    "# Tasklist\n\n- [ ] **New Task**\n  - ID: new-task\n  - Context: none\n"
                )
            return "ok"

        return manager.run_plan(
            design_path=design_ref,
            load_prompt_callback=lambda name: f"prompt_name: {name}\n{{{{TASKLIST_CONTENT}}}}",
            run_agent_callback=run_agent,
        )

    result = manager.run_cycle(
        has_remaining_tasks_callback=lambda: False,
        run_callback=lambda: 0,
        run_analyze_callback=lambda _issues_file: {"success": True, "opportunity_count": 1},
        run_design_callback=run_design,
        review_design_callback=review_design,
        run_plan_callback=run_plan,
    )

    assert result == 0
    assert design_refs_reviewed == ["non-file-design"]
    assert design_refs_planned == ["non-file-design"]
    assert "new-task" in tasklist_provider.get_snapshot()


# ── plan-loop-edit-in-place tests ────────────────────────────────────────────


def test_no_restore_before_fix_agent_on_reviewer_rejection(temp_repo):
    """On reviewer rejection, restore_snapshot must NOT be called before the fix agent runs."""
    manager = _make_outer_manager(temp_repo)
    manager.opportunity_provider.write_opportunity(
        Opportunity(
            opportunity_id="opp-edit-in-place",
            title="Edit In Place Opportunity",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    )
    manager.design_provider.write_design(
        Design(
            design_id="edit-in-place",
            title="Edit In Place",
            status=DesignStatus.draft,
            body="Design body",
            opportunity_ref="opp-edit-in-place",
        )
    )
    original = "# Tasklist\n"
    tasklist_provider = InMemoryTasklistProvider(original)
    manager.tasklist_provider = tasklist_provider

    review_count = [0]

    def review_plan_once(**_kwargs):
        review_count[0] += 1
        # Approve on second call to let the loop finish
        return {"approved": review_count[0] > 1}

    manager.review_plan = review_plan_once  # type: ignore[method-assign]

    restore_calls_before_fix: list[int] = []

    def run_agent(prompt: str) -> str:
        if "plan_prompt.md" in prompt:
            tasklist_provider._content = (
                original + "- [ ] **Task**\n  - ID: task-edit-1\n  - Context: none\n"
            )
        elif "plan_fix_prompt.md" in prompt:
            # Record restore count at the moment the fix agent is invoked
            restore_calls_before_fix.append(tasklist_provider.restore_calls)
            # Agent edits in place — content already has the task
        return "ok"

    manager._run_plan_impl(
        design_path=str(temp_repo / "designs" / "edit-in-place.md"),
        load_prompt_callback=lambda name: f"prompt_name: {name}\n{{{{TASKLIST_CONTENT}}}}",
        run_agent_callback=run_agent,
    )

    # The fix agent was invoked at least once
    assert len(restore_calls_before_fix) > 0
    # restore_snapshot must NOT have been called before any fix-agent invocation
    assert all(calls == 0 for calls in restore_calls_before_fix), (
        "restore_snapshot was called before the fix agent — wipe-and-retry pattern detected"
    )


def test_task_count_uses_id_based_diff(temp_repo):
    """Task count uses list_tasks() ID diff, stable under in-place title edits."""
    manager = _make_outer_manager(temp_repo)
    manager.opportunity_provider.write_opportunity(
        Opportunity(
            opportunity_id="opp-id-diff",
            title="ID Diff Opportunity",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    )
    manager.design_provider.write_design(
        Design(
            design_id="id-diff",
            title="ID Diff",
            status=DesignStatus.draft,
            body="Design body",
            opportunity_ref="opp-id-diff",
        )
    )

    class TrackingTasklistProvider(InMemoryTasklistProvider):
        """Provider that tracks list_tasks calls and returns tasks by ID."""

        def __init__(self, content: str) -> None:
            super().__init__(content)
            self._tasks: dict[str, TasklistItem] = {}

        def add_task(self, item: TasklistItem) -> None:
            self._tasks[item.task_id] = item

        def list_tasks(self) -> list[TasklistItem]:
            return list(self._tasks.values())

    tasklist_provider = TrackingTasklistProvider("# Tasklist\n")
    manager.tasklist_provider = tasklist_provider
    manager.review_plan = lambda **_kwargs: {"approved": True}  # type: ignore[method-assign]

    def run_agent(prompt: str) -> str:
        if "plan_prompt.md" in prompt:
            # Add a task via the provider (simulates agent writing)
            tasklist_provider.add_task(
                TasklistItem(
                    task_id="stable-id-1",
                    title="Original Title",
                    status=TaskStatus.todo,
                )
            )
        return "ok"

    result = manager._run_plan_impl(
        design_path=str(temp_repo / "designs" / "id-diff.md"),
        load_prompt_callback=lambda name: f"prompt_name: {name}\n{{{{TASKLIST_CONTENT}}}}",
        run_agent_callback=run_agent,
    )

    assert result["success"] is True
    assert result["tasks_added"] == 1


def test_restore_snapshot_called_on_loop_exhaustion(temp_repo):
    """restore_snapshot IS called when the review loop exhausts without approval."""
    manager = _make_outer_manager(temp_repo)
    manager.opportunity_provider.write_opportunity(
        Opportunity(
            opportunity_id="opp-exhaustion",
            title="Exhaustion Opportunity",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    )
    manager.design_provider.write_design(
        Design(
            design_id="exhaustion",
            title="Exhaustion",
            status=DesignStatus.draft,
            body="Design body",
            opportunity_ref="opp-exhaustion",
        )
    )
    original = "# Tasklist\n"
    tasklist_provider = InMemoryTasklistProvider(original)
    manager.tasklist_provider = tasklist_provider
    manager.review_plan = lambda **_kwargs: {"approved": False}  # type: ignore[method-assign]

    def run_agent(prompt: str) -> str:
        if "plan_prompt.md" in prompt or "plan_fix_prompt.md" in prompt:
            tasklist_provider._content = (
                original + "- [ ] **Task**\n  - ID: new-task\n  - Context: none\n"
            )
        return "ok"

    result = manager._run_plan_impl(
        design_path=str(temp_repo / "designs" / "exhaustion.md"),
        load_prompt_callback=lambda name: f"prompt_name: {name}\n{{{{TASKLIST_CONTENT}}}}",
        run_agent_callback=run_agent,
    )

    assert result["success"] is False
    # Snapshot restored to original on loop exhaustion
    assert tasklist_provider.get_snapshot() == original
    assert tasklist_provider.restore_calls >= 1


def test_restore_snapshot_called_on_reference_integrity_failure(temp_repo):
    """restore_snapshot IS called when reference integrity check fails after approval."""
    from millstone.policy.reference_integrity import (
        ReferenceIntegrityChecker,
        ReferenceIntegrityError,
    )

    manager = _make_outer_manager(temp_repo)
    manager.opportunity_provider.write_opportunity(
        Opportunity(
            opportunity_id="opp-integrity",
            title="Integrity Opportunity",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    )
    manager.design_provider.write_design(
        Design(
            design_id="integrity",
            title="Integrity",
            status=DesignStatus.draft,
            body="Design body",
            opportunity_ref="opp-integrity",
        )
    )
    original = "# Tasklist\n"
    tasklist_provider = InMemoryTasklistProvider(original)
    manager.tasklist_provider = tasklist_provider
    manager.review_plan = lambda **_kwargs: {"approved": True}  # type: ignore[method-assign]

    def run_agent(prompt: str) -> str:
        if "plan_prompt.md" in prompt:
            tasklist_provider._content = (
                original + "- [ ] **Task**\n  - ID: new-task\n  - Context: none\n"
            )
        return "ok"

    # Patch the integrity checker to always raise
    original_check = ReferenceIntegrityChecker.check_tasks

    def failing_check(self, tasks):
        raise ReferenceIntegrityError(violations=["missing opportunity ref"])

    ReferenceIntegrityChecker.check_tasks = failing_check  # type: ignore[method-assign]
    try:
        result = manager._run_plan_impl(
            design_path=str(temp_repo / "designs" / "integrity.md"),
            load_prompt_callback=lambda name: f"prompt_name: {name}\n{{{{TASKLIST_CONTENT}}}}",
            run_agent_callback=run_agent,
        )
    finally:
        ReferenceIntegrityChecker.check_tasks = original_check  # type: ignore[method-assign]

    assert result["success"] is False
    assert tasklist_provider.get_snapshot() == original
    assert tasklist_provider.restore_calls >= 1


# ── run_design in-place revision detection tests ─────────────────────────────


def _setup_design_test(temp_repo, design_provider, design_id="rev-design", opp_id="opp-rev"):
    """Write prerequisite opportunity and design into providers, return manager."""
    manager = _make_outer_manager(temp_repo, design_provider=design_provider)
    manager.opportunity_provider.write_opportunity(
        Opportunity(
            opportunity_id=opp_id,
            title="Rev Opportunity",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    )
    design_provider.write_design(
        Design(
            design_id=design_id,
            title="Rev Design",
            status=DesignStatus.draft,
            body="Original body.",
            opportunity_ref=opp_id,
        )
    )
    return manager


def test_run_design_in_place_with_opportunity_id(temp_repo):
    """When opportunity_id is provided and matches existing design, in-place edit succeeds."""
    design_provider = InMemoryDesignProvider()
    manager = _setup_design_test(temp_repo, design_provider)

    def run_agent(prompt: str) -> str:
        # Agent edits the design body in-place (body changes, ID unchanged)
        design_provider._designs["rev-design"].body = "Updated body."
        return "ok"

    result = manager.run_design(
        opportunity="Rev opportunity",
        opportunity_id="rev-design",
        load_prompt_callback=lambda name: "prompt",
        run_agent_callback=run_agent,
    )

    assert result["success"] is True
    assert result["design_id"] == "rev-design"


def test_run_design_in_place_without_opportunity_id(temp_repo):
    """When opportunity_id=None (--design CLI path), in-place edit detected via body change."""
    design_provider = InMemoryDesignProvider()
    manager = _setup_design_test(temp_repo, design_provider)

    def run_agent(prompt: str) -> str:
        # Agent edits existing design body without knowing opportunity_id
        design_provider._designs["rev-design"].body = "Updated body from no-id path."
        return "ok"

    result = manager.run_design(
        opportunity="Rev opportunity",
        opportunity_id=None,
        load_prompt_callback=lambda name: "prompt",
        run_agent_callback=run_agent,
    )

    assert result["success"] is True
    assert result["design_id"] == "rev-design"


def test_run_design_no_change_returns_failure(temp_repo):
    """When agent produces no new design and no body change, success is False."""
    design_provider = InMemoryDesignProvider()
    manager = _setup_design_test(temp_repo, design_provider)

    def run_agent(prompt: str) -> str:
        # Agent does nothing — no new file, no body change
        return "ok"

    result = manager.run_design(
        opportunity="Rev opportunity",
        opportunity_id=None,
        load_prompt_callback=lambda name: "prompt",
        run_agent_callback=run_agent,
    )

    assert result["success"] is False
    assert result["design_id"] is None


def test_run_design_no_change_with_opportunity_id_returns_failure(temp_repo):
    """When opportunity_id is provided but agent makes no changes, success is False."""
    design_provider = InMemoryDesignProvider()
    manager = _setup_design_test(temp_repo, design_provider)

    def run_agent(prompt: str) -> str:
        # Agent does nothing — existing design body is unchanged
        return "ok"

    result = manager.run_design(
        opportunity="Rev opportunity",
        opportunity_id="rev-design",
        load_prompt_callback=lambda name: "prompt",
        run_agent_callback=run_agent,
    )

    assert result["success"] is False
    assert result["design_id"] is None


def test_run_analyze_accepts_reviewer_callback_none_preserves_single_pass(temp_repo):
    """run_analyze with reviewer_callback=None behaves identically to omitting it."""
    provider = MockOpportunityProvider()
    provider.list_opportunities = lambda: [  # type: ignore[method-assign]
        Opportunity(
            opportunity_id="opp-single",
            title="Single Pass",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    ]
    manager = _make_outer_manager(temp_repo, opportunity_provider=provider)

    agent_calls = []

    def run_agent(prompt: str) -> str:
        agent_calls.append(prompt)
        return "ok"

    result = manager.run_analyze(
        load_prompt_callback=lambda _name: "prompt_name: analyze_prompt.md",
        run_agent_callback=run_agent,
        reviewer_callback=None,
    )

    assert result["success"] is True
    assert len(agent_calls) == 1  # single pass — no reviewer call


def test_run_analyze_with_reviewer_callback_approve_first_cycle(temp_repo):
    """run_analyze with reviewer_callback approves on the first cycle."""
    import json as _json

    provider = MockOpportunityProvider()
    provider.list_opportunities = lambda: [  # type: ignore[method-assign]
        Opportunity(
            opportunity_id="opp-rev",
            title="With Reviewer",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    ]
    manager = _make_outer_manager(temp_repo, opportunity_provider=provider)

    reviewer_calls = []
    approved_verdict = _json.dumps(
        {"verdict": "APPROVED", "score": 9, "strengths": ["Good"], "issues": [], "feedback": ""}
    )

    def reviewer(prompt: str) -> str:
        reviewer_calls.append(prompt)
        return f"```json\n{approved_verdict}\n```"

    result = manager.run_analyze(
        load_prompt_callback=lambda name: (
            f"prompt_name: {name}\n{{{{OPPORTUNITIES_CONTENT}}}}\n{{{{HARD_SIGNALS}}}}\n{{{{PROJECT_GOALS}}}}\n{{{{FEEDBACK}}}}"
        ),
        run_agent_callback=lambda _prompt: "ok",
        reviewer_callback=reviewer,
    )

    assert result["success"] is True
    assert result["opportunity_count"] == 1
    assert len(reviewer_calls) == 1  # reviewer called exactly once


def test_run_analyze_with_reviewer_callback_reject_then_approve(temp_repo):
    """run_analyze reviewer rejects on cycle 1 and approves on cycle 2."""
    import json as _json

    provider = MockOpportunityProvider()
    provider.list_opportunities = lambda: [  # type: ignore[method-assign]
        Opportunity(
            opportunity_id="opp-iter",
            title="Iterated",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    ]
    manager = _make_outer_manager(temp_repo, opportunity_provider=provider)

    call_count = [0]
    agent_calls = []

    def reviewer(prompt: str) -> str:
        call_count[0] += 1
        if call_count[0] == 1:
            verdict = {
                "verdict": "NEEDS_REVISION",
                "score": 4,
                "strengths": [],
                "issues": ["Too vague"],
                "feedback": "Be more specific",
            }
        else:
            verdict = {
                "verdict": "APPROVED",
                "score": 8,
                "strengths": ["Specific"],
                "issues": [],
                "feedback": "",
            }
        return f"```json\n{_json.dumps(verdict)}\n```"

    def run_agent(prompt: str) -> str:
        agent_calls.append(prompt)
        return "ok"

    result = manager.run_analyze(
        load_prompt_callback=lambda name: (
            f"prompt_name: {name}\n{{{{OPPORTUNITIES_CONTENT}}}}\n{{{{HARD_SIGNALS}}}}\n{{{{PROJECT_GOALS}}}}\n{{{{FEEDBACK}}}}"
        ),
        run_agent_callback=run_agent,
        reviewer_callback=reviewer,
    )

    assert result["success"] is True
    assert call_count[0] == 2  # rejected once, approved second
    assert len(agent_calls) == 2  # initial + fix


def test_run_analyze_with_reviewer_callback_max_cycles_exhaustion(temp_repo):
    """run_analyze returns success=False with cycles and error when max_cycles reached."""
    import json as _json

    provider = MockOpportunityProvider()
    provider.list_opportunities = lambda: [  # type: ignore[method-assign]
        Opportunity(
            opportunity_id="opp-fail",
            title="Failing",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    ]
    manager = _make_outer_manager(temp_repo, opportunity_provider=provider)
    manager.max_cycles = 2

    def reviewer(prompt: str) -> str:
        verdict = {
            "verdict": "NEEDS_REVISION",
            "score": 2,
            "strengths": [],
            "issues": ["Bad"],
            "feedback": "Fix everything",
        }
        return f"```json\n{_json.dumps(verdict)}\n```"

    result = manager.run_analyze(
        load_prompt_callback=lambda name: (
            f"prompt_name: {name}\n{{{{OPPORTUNITIES_CONTENT}}}}\n{{{{HARD_SIGNALS}}}}\n{{{{PROJECT_GOALS}}}}\n{{{{FEEDBACK}}}}"
        ),
        run_agent_callback=lambda _prompt: "ok",
        reviewer_callback=reviewer,
    )

    assert result["success"] is False
    assert result["cycles"] == 2
    assert "error" in result
    assert "cycles" in result


def test_run_analyze_with_reviewer_callback_opportunities_not_reverted_on_failure(temp_repo):
    """Opportunities file is left as-is (not reverted) when review loop fails."""
    import json as _json

    provider = MockOpportunityProvider()
    provider.list_opportunities = lambda: [  # type: ignore[method-assign]
        Opportunity(
            opportunity_id="opp-persist",
            title="Persisted",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    ]
    manager = _make_outer_manager(temp_repo, opportunity_provider=provider)
    manager.max_cycles = 1

    def reviewer(prompt: str) -> str:
        verdict = {
            "verdict": "NEEDS_REVISION",
            "score": 1,
            "strengths": [],
            "issues": ["Bad"],
            "feedback": "Fix it",
        }
        return f"```json\n{_json.dumps(verdict)}\n```"

    result = manager.run_analyze(
        load_prompt_callback=lambda name: (
            f"prompt_name: {name}\n{{{{OPPORTUNITIES_CONTENT}}}}\n{{{{HARD_SIGNALS}}}}\n{{{{PROJECT_GOALS}}}}\n{{{{FEEDBACK}}}}"
        ),
        run_agent_callback=lambda _prompt: "ok",
        reviewer_callback=reviewer,
    )

    assert result["success"] is False
    # Opportunities provider still has the opportunity (not reverted)
    assert len(provider.list_opportunities()) == 1


def test_run_design_accepts_reviewer_callback_none_preserves_single_pass(temp_repo):
    """run_design with reviewer_callback=None behaves identically to omitting it."""
    design_provider = InMemoryDesignProvider()
    manager = _setup_design_test(temp_repo, design_provider)

    agent_calls = []

    def run_agent(prompt: str) -> str:
        agent_calls.append(prompt)
        design_provider._designs["rev-design"].body = "Updated body."
        return "ok"

    result = manager.run_design(
        opportunity="Rev opportunity",
        opportunity_id="rev-design",
        load_prompt_callback=lambda name: "prompt",
        run_agent_callback=run_agent,
        reviewer_callback=None,
    )

    assert result["success"] is True
    assert len(agent_calls) == 1  # single pass — no reviewer call


def test_run_design_accepts_reviewer_callback_parameter(temp_repo):
    """run_design with reviewer_callback invokes the loop and approves on first review."""
    import json as _json

    design_provider = InMemoryDesignProvider()
    manager = _setup_design_test(temp_repo, design_provider)

    reviewer_calls = []
    approved_verdict = _json.dumps(
        {
            "verdict": "APPROVED",
            "approved": True,
            "strengths": ["Good"],
            "issues": [],
            "questions": [],
        }
    )

    def reviewer(prompt: str) -> str:
        reviewer_calls.append(prompt)
        return f"```json\n{approved_verdict}\n```"

    def run_agent(prompt: str) -> str:
        design_provider._designs["rev-design"].body = "Updated body."
        return "ok"

    result = manager.run_design(
        opportunity="Rev opportunity",
        opportunity_id="rev-design",
        load_prompt_callback=lambda name: "prompt",
        run_agent_callback=run_agent,
        reviewer_callback=reviewer,
    )

    assert result["success"] is True
    assert len(reviewer_calls) == 1  # reviewer called exactly once


def test_run_design_with_reviewer_callback_approve_first_cycle(temp_repo):
    """run_design with reviewer_callback approves on the first cycle."""
    import json as _json

    design_provider = InMemoryDesignProvider()
    manager = _setup_design_test(temp_repo, design_provider)

    agent_calls = []
    approved_verdict = _json.dumps(
        {
            "verdict": "APPROVED",
            "approved": True,
            "strengths": ["Solid"],
            "issues": [],
            "questions": [],
        }
    )

    def run_agent(prompt: str) -> str:
        agent_calls.append(prompt)
        design_provider._designs["rev-design"].body = "New body."
        return "ok"

    def reviewer(prompt: str) -> str:
        return f"```json\n{approved_verdict}\n```"

    result = manager.run_design(
        opportunity="Rev opportunity",
        opportunity_id="rev-design",
        load_prompt_callback=lambda name: "prompt",
        run_agent_callback=run_agent,
        reviewer_callback=reviewer,
    )

    assert result["success"] is True
    assert result["design_id"] == "rev-design"
    assert len(agent_calls) == 1  # builder called exactly once


def test_run_design_with_reviewer_callback_reject_then_approve(temp_repo):
    """run_design reviewer rejects on cycle 1 and approves on cycle 2."""
    import json as _json

    design_provider = InMemoryDesignProvider()
    manager = _setup_design_test(temp_repo, design_provider)

    review_count = [0]
    agent_calls = []

    def run_agent(prompt: str) -> str:
        agent_calls.append(prompt)
        design_provider._designs["rev-design"].body = f"Body revision {len(agent_calls)}."
        return "ok"

    def reviewer(prompt: str) -> str:
        review_count[0] += 1
        if review_count[0] == 1:
            verdict = {
                "verdict": "NEEDS_REVISION",
                "approved": False,
                "issues": ["Too vague"],
                "feedback": "Add more detail",
            }
        else:
            verdict = {
                "verdict": "APPROVED",
                "approved": True,
                "strengths": ["Detailed"],
                "issues": [],
                "feedback": "",
            }
        return f"```json\n{_json.dumps(verdict)}\n```"

    result = manager.run_design(
        opportunity="Rev opportunity",
        opportunity_id="rev-design",
        load_prompt_callback=lambda name: "prompt",
        run_agent_callback=run_agent,
        reviewer_callback=reviewer,
    )

    assert result["success"] is True
    assert review_count[0] == 2  # rejected once, approved second
    assert len(agent_calls) == 2  # initial produce + fix


def test_run_design_with_reviewer_callback_max_cycles_exhaustion(temp_repo):
    """run_design returns success=False with error when max_cycles reached without approval."""
    import json as _json

    design_provider = InMemoryDesignProvider()
    manager = _setup_design_test(temp_repo, design_provider)
    manager.max_cycles = 2

    def run_agent(prompt: str) -> str:
        design_provider._designs["rev-design"].body = "Updated."
        return "ok"

    def reviewer(prompt: str) -> str:
        verdict = {
            "verdict": "NEEDS_REVISION",
            "approved": False,
            "issues": ["Still not good"],
            "feedback": "Keep improving",
        }
        return f"```json\n{_json.dumps(verdict)}\n```"

    result = manager.run_design(
        opportunity="Rev opportunity",
        opportunity_id="rev-design",
        load_prompt_callback=lambda name: "prompt",
        run_agent_callback=run_agent,
        reviewer_callback=reviewer,
    )

    assert result["success"] is False
    assert "error" in result
    assert result["design_file"] is None


def test_run_design_with_reviewer_callback_opportunity_id_none(temp_repo):
    """run_design with reviewer_callback detects in-place revisions when opportunity_id=None."""
    import json as _json

    design_provider = InMemoryDesignProvider()
    manager = _setup_design_test(temp_repo, design_provider)

    approved_verdict = _json.dumps(
        {
            "verdict": "APPROVED",
            "approved": True,
            "strengths": ["Ok"],
            "issues": [],
            "questions": [],
        }
    )

    def run_agent(prompt: str) -> str:
        # Agent edits existing design without knowing the opportunity_id
        design_provider._designs["rev-design"].body = "Revised body via no-id path."
        return "ok"

    def reviewer(prompt: str) -> str:
        return f"```json\n{approved_verdict}\n```"

    result = manager.run_design(
        opportunity="Rev opportunity",
        opportunity_id=None,
        load_prompt_callback=lambda name: "prompt",
        run_agent_callback=run_agent,
        reviewer_callback=reviewer,
    )

    assert result["success"] is True
    assert result["design_id"] == "rev-design"


def test_design_fix_prompt_exists_and_contains_required_placeholders():
    """design_fix_prompt.md exists and contains the expected placeholder keys."""
    from importlib.resources import files

    content = files("millstone.prompts").joinpath("design_fix_prompt.md").read_text()
    assert "{{OPPORTUNITY}}" in content
    assert "{{DESIGN_CONTENT}}" in content
    assert "{{FEEDBACK}}" in content


# ── max_cycles plumbing tests ─────────────────────────────────────────────────


def test_outer_loop_manager_default_max_cycles(temp_repo):
    """OuterLoopManager defaults max_cycles to 3."""
    manager = _make_outer_manager(temp_repo)
    assert manager.max_cycles == 3


def test_outer_loop_manager_stores_configured_max_cycles(temp_repo):
    """OuterLoopManager stores the configured max_cycles value."""
    manager = _make_outer_manager(temp_repo, max_cycles=7)
    assert manager.max_cycles == 7


def test_run_plan_impl_uses_configured_max_cycles(temp_repo):
    """_run_plan_impl passes self.max_cycles to the ArtifactReviewLoop."""
    from unittest.mock import patch

    manager = _make_outer_manager(temp_repo, max_cycles=5)
    manager.opportunity_provider.write_opportunity(
        Opportunity(
            opportunity_id="opp-mc",
            title="MC Test Opportunity",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    )
    manager.design_provider.write_design(
        Design(
            design_id="mc-test",
            title="MC Test",
            status=DesignStatus.draft,
            body="Design body",
            opportunity_ref="opp-mc",
        )
    )
    tasklist_provider = InMemoryTasklistProvider("# Tasklist\n")
    manager.tasklist_provider = tasklist_provider
    manager.review_plan = lambda **_kwargs: {"approved": True}  # type: ignore[method-assign]

    captured_max_cycles: list[int] = []
    import millstone.loops.outer as _outer_loops_mod

    OriginalLoop = _outer_loops_mod.ArtifactReviewLoop

    class CapturingLoop(OriginalLoop):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            captured_max_cycles.append(kwargs.get("max_cycles", -1))
            super().__init__(*args, **kwargs)

    def run_agent(prompt: str) -> str:
        if "plan_prompt.md" in prompt:
            tasklist_provider._content = (
                "# Tasklist\n\n- [ ] **MC Task**\n  - ID: mc-task\n  - Context: none\n"
            )
        return "ok"

    with patch.object(_outer_loops_mod, "ArtifactReviewLoop", CapturingLoop):
        manager._run_plan_impl(
            design_path=str(temp_repo / "designs" / "mc-test.md"),
            load_prompt_callback=lambda name: f"prompt_name: {name}\n{{{{TASKLIST_CONTENT}}}}",
            run_agent_callback=run_agent,
        )

    assert captured_max_cycles == [5]


def test_run_plan_impl_cycle_cap_honored_end_to_end(temp_repo):
    """Configured max_cycles caps loop iterations at runtime.

    With max_cycles=2 and a reviewer that always rejects, the loop must stop
    after exactly 2 cycles, return failure, and restore the tasklist snapshot.
    """
    initial_content = "# Tasklist\n"

    manager = _make_outer_manager(temp_repo, max_cycles=2)
    manager.opportunity_provider.write_opportunity(
        Opportunity(
            opportunity_id="opp-cap",
            title="Cap Test Opportunity",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    )
    manager.design_provider.write_design(
        Design(
            design_id="cap-test",
            title="Cap Test",
            status=DesignStatus.draft,
            body="Design body",
            opportunity_ref="opp-cap",
        )
    )
    tasklist_provider = InMemoryTasklistProvider(initial_content)
    manager.tasklist_provider = tasklist_provider

    call_counts: dict[str, int] = {"produce": 0, "review": 0}

    original_review_plan = manager.review_plan  # noqa: F841

    def always_reject_review(**_kwargs):
        call_counts["review"] += 1
        return {"approved": False, "feedback": "Not good enough"}

    manager.review_plan = always_reject_review  # type: ignore[method-assign]

    def run_agent(prompt: str) -> str:
        call_counts["produce"] += 1
        # Simulate agent adding a task on each call
        tasklist_provider._content = (
            f"# Tasklist\n\n- [ ] **Cap Task {call_counts['produce']}**\n"
            f"  - ID: cap-task-{call_counts['produce']}\n  - Context: none\n"
        )
        return "ok"

    result = manager._run_plan_impl(
        design_path=str(temp_repo / "designs" / "cap-test.md"),
        load_prompt_callback=lambda name: f"prompt_name: {name}\n{{{{TASKLIST_CONTENT}}}}",
        run_agent_callback=run_agent,
    )

    # Loop must have stopped at the configured cap (2)
    assert call_counts["review"] == 2, (
        f"Expected 2 review calls (one per cycle), got {call_counts['review']}"
    )
    # Result must be failure
    assert result["success"] is False
    # Snapshot must be restored to original content
    assert tasklist_provider._content == initial_content, (
        "Tasklist should be restored to original after loop failure"
    )
    assert tasklist_provider.restore_calls >= 1


# ── analyze prompt template tests ─────────────────────────────────────────────


def test_analyze_review_prompt_exists_and_contains_required_placeholders():
    """analyze_review_prompt.md exists and contains the expected placeholder keys."""
    from importlib.resources import files

    content = files("millstone.prompts").joinpath("analyze_review_prompt.md").read_text()
    assert "{{OPPORTUNITIES_CONTENT}}" in content
    assert "{{HARD_SIGNALS}}" in content
    assert "{{PROJECT_GOALS}}" in content


def test_analyze_review_prompt_specifies_json_output_contract():
    """analyze_review_prompt.md instructs for a JSON block with required fields."""
    from importlib.resources import files

    content = files("millstone.prompts").joinpath("analyze_review_prompt.md").read_text()
    assert "verdict" in content
    assert "score" in content
    assert "strengths" in content
    assert "issues" in content
    assert "feedback" in content
    # Must include the valid verdict values
    assert "APPROVED" in content
    assert "NEEDS_REVISION" in content


def test_analyze_fix_prompt_exists_and_contains_required_placeholders():
    """analyze_fix_prompt.md exists and contains the expected placeholder keys."""
    from importlib.resources import files

    content = files("millstone.prompts").joinpath("analyze_fix_prompt.md").read_text()
    assert "{{OPPORTUNITIES_CONTENT}}" in content
    assert "{{FEEDBACK}}" in content
    assert "{{HARD_SIGNALS}}" in content
    assert "{{PROJECT_GOALS}}" in content


def test_analyze_review_prompt_loadable_via_load_prompt(tmp_path):
    """analyze_review_prompt.md is loadable via Orchestrator.load_prompt() and contains required placeholders."""
    from millstone.runtime.orchestrator import Orchestrator

    orc = Orchestrator(repo_dir=tmp_path)
    content = orc.load_prompt("analyze_review_prompt.md")
    assert "{{OPPORTUNITIES_CONTENT}}" in content
    assert "{{HARD_SIGNALS}}" in content
    assert "{{PROJECT_GOALS}}" in content
    # load_prompt appends a hidden tag usable for stable test assertions
    assert "analyze_review_prompt.md" in content


def test_analyze_fix_prompt_loadable_via_load_prompt(tmp_path):
    """analyze_fix_prompt.md is loadable via Orchestrator.load_prompt() and contains required placeholders."""
    from millstone.runtime.orchestrator import Orchestrator

    orc = Orchestrator(repo_dir=tmp_path)
    content = orc.load_prompt("analyze_fix_prompt.md")
    assert "{{OPPORTUNITIES_CONTENT}}" in content
    assert "{{FEEDBACK}}" in content
    assert "{{HARD_SIGNALS}}" in content
    assert "{{PROJECT_GOALS}}" in content
    assert "analyze_fix_prompt.md" in content


# ---------------------------------------------------------------------------
# tasklist_filter plumbing into OuterLoopManager
# ---------------------------------------------------------------------------


def test_tasklist_filter_config_plumbed_to_provider_options(temp_repo, monkeypatch):
    """tasklist_filter in provider_config is merged into tasklist provider options."""
    captured = {}

    import millstone.loops.outer as _ol

    original = _ol.get_tasklist_provider

    def capturing_factory(backend: str, options):
        captured["backend"] = backend
        captured["options"] = dict(options)
        return original(backend, options)

    monkeypatch.setattr(_ol, "get_tasklist_provider", capturing_factory)

    provider_config = {
        "tasklist_filter": {
            "labels": ["sprint-1"],
            "assignees": ["alice"],
            "statuses": ["Todo"],
        },
    }
    _make_outer_manager(temp_repo, provider_config=provider_config)

    assert "filter" in captured["options"]
    assert captured["options"]["filter"]["labels"] == ["sprint-1"]
    assert captured["options"]["filter"]["assignees"] == ["alice"]
    assert captured["options"]["filter"]["statuses"] == ["Todo"]


def test_explicit_tasklist_provider_options_filter_takes_precedence(temp_repo, monkeypatch):
    """Explicit tasklist_provider_options['filter'] wins over tasklist_filter config."""
    captured = {}

    import millstone.loops.outer as _ol

    original = _ol.get_tasklist_provider

    def capturing_factory(backend: str, options):
        captured["options"] = dict(options)
        return original(backend, options)

    monkeypatch.setattr(_ol, "get_tasklist_provider", capturing_factory)

    explicit_filter = {"labels": ["explicit"], "assignees": [], "statuses": []}
    provider_config = {
        "tasklist_provider_options": {"filter": explicit_filter},
        "tasklist_filter": {
            "labels": ["from-tasklist-filter"],
            "assignees": ["bob"],
            "statuses": [],
        },
    }
    _make_outer_manager(temp_repo, provider_config=provider_config)

    # Explicit provider option wins; tasklist_filter is NOT applied
    assert captured["options"]["filter"] == explicit_filter


def test_tasklist_filter_absent_in_config_does_not_set_filter_key(temp_repo, monkeypatch):
    """When tasklist_filter is absent from config, 'filter' key is not added to options."""
    captured = {}

    import millstone.loops.outer as _ol

    original = _ol.get_tasklist_provider

    def capturing_factory(backend: str, options):
        captured["options"] = dict(options)
        return original(backend, options)

    monkeypatch.setattr(_ol, "get_tasklist_provider", capturing_factory)

    _make_outer_manager(temp_repo, provider_config={})

    # filter key should not be set (or if set by default filter schema, it has empty lists)
    filter_val = captured["options"].get("filter")
    if filter_val is not None:
        # If default empty filter is injected, all lists must be empty
        assert filter_val.get("labels", []) == []
        assert filter_val.get("assignees", []) == []
        assert filter_val.get("statuses", []) == []


# ---------------------------------------------------------------------------
# tasklist_filter UX shortcut expansion
# ---------------------------------------------------------------------------


def _capture_tasklist_options(temp_repo, monkeypatch, provider_config: dict) -> dict:
    """Helper: build OuterLoopManager with provider_config and return captured options."""
    captured = {}
    import millstone.loops.outer as _ol

    original = _ol.get_tasklist_provider

    def capturing_factory(backend: str, options):
        captured["options"] = dict(options)
        return original(backend, options)

    monkeypatch.setattr(_ol, "get_tasklist_provider", capturing_factory)
    _make_outer_manager(temp_repo, provider_config=provider_config)
    return captured.get("options", {})


def test_label_shortcut_expands_to_labels_list(temp_repo, monkeypatch):
    """label shortcut produces labels = [value] in provider filter."""
    options = _capture_tasklist_options(
        temp_repo,
        monkeypatch,
        {"tasklist_filter": {"label": "sprint-1", "labels": [], "assignees": [], "statuses": []}},
    )
    assert options["filter"]["labels"] == ["sprint-1"]
    assert options["filter"]["assignees"] == []
    assert options["filter"]["statuses"] == []


def test_assignee_shortcut_expands_to_assignees_list(temp_repo, monkeypatch):
    """assignee shortcut produces assignees = [value] in provider filter."""
    options = _capture_tasklist_options(
        temp_repo,
        monkeypatch,
        {"tasklist_filter": {"assignee": "alice", "labels": [], "assignees": [], "statuses": []}},
    )
    assert options["filter"]["assignees"] == ["alice"]
    assert options["filter"]["labels"] == []


def test_status_shortcut_expands_to_statuses_list(temp_repo, monkeypatch):
    """status shortcut produces statuses = [value] in provider filter."""
    options = _capture_tasklist_options(
        temp_repo,
        monkeypatch,
        {"tasklist_filter": {"status": "Todo", "labels": [], "assignees": [], "statuses": []}},
    )
    assert options["filter"]["statuses"] == ["Todo"]
    assert options["filter"]["labels"] == []


def test_all_shortcuts_together(temp_repo, monkeypatch):
    """All three shortcuts expand simultaneously."""
    options = _capture_tasklist_options(
        temp_repo,
        monkeypatch,
        {
            "tasklist_filter": {
                "label": "sprint-1",
                "assignee": "bob",
                "status": "In Progress",
                "labels": [],
                "assignees": [],
                "statuses": [],
            }
        },
    )
    assert options["filter"]["labels"] == ["sprint-1"]
    assert options["filter"]["assignees"] == ["bob"]
    assert options["filter"]["statuses"] == ["In Progress"]


def test_list_form_takes_precedence_over_shortcut(temp_repo, monkeypatch):
    """Explicit list form wins when both shortcut and list are set."""
    options = _capture_tasklist_options(
        temp_repo,
        monkeypatch,
        {
            "tasklist_filter": {
                "label": "shortcut-label",
                "labels": ["explicit-label"],
                "assignee": "shortcut-user",
                "assignees": ["explicit-user"],
                "status": "shortcut-status",
                "statuses": ["explicit-status"],
            }
        },
    )
    assert options["filter"]["labels"] == ["explicit-label"]
    assert options["filter"]["assignees"] == ["explicit-user"]
    assert options["filter"]["statuses"] == ["explicit-status"]


def test_empty_shortcut_does_not_add_entry(temp_repo, monkeypatch):
    """Empty-string shortcut leaves the list empty (no filter)."""
    options = _capture_tasklist_options(
        temp_repo,
        monkeypatch,
        {
            "tasklist_filter": {
                "label": "",
                "assignee": "",
                "status": "",
                "labels": [],
                "assignees": [],
                "statuses": [],
            }
        },
    )
    assert options["filter"]["labels"] == []
    assert options["filter"]["assignees"] == []
    assert options["filter"]["statuses"] == []


def test_empty_list_treats_shortcut_as_active(temp_repo, monkeypatch):
    """Empty list [] is treated as absent, so a non-empty shortcut still applies.

    When the list key is present but empty (e.g. labels=[]) alongside a shortcut
    (e.g. label="sprint-1"), the shortcut value is used. This is consistent with
    the semantics that an empty filter list means "no constraint" rather than
    "explicitly empty". To suppress a shortcut, omit the shortcut key or set it
    to an empty string.
    """
    options = _capture_tasklist_options(
        temp_repo,
        monkeypatch,
        {
            "tasklist_filter": {
                "label": "sprint-1",
                "labels": [],  # empty list: shortcut is still applied
                "assignee": "alice",
                "assignees": [],
                "status": "Todo",
                "statuses": [],
            }
        },
    )
    assert options["filter"]["labels"] == ["sprint-1"]
    assert options["filter"]["assignees"] == ["alice"]
    assert options["filter"]["statuses"] == ["Todo"]


# ---------------------------------------------------------------------------
# Provider placeholder substitution tests
# ---------------------------------------------------------------------------


class MockOpportunityProviderWithPlaceholders(MockOpportunityProvider):
    """Opportunity provider that returns known prompt placeholders."""

    def get_prompt_placeholders(self) -> dict[str, str]:
        return {
            "OPPORTUNITY_WRITE_INSTRUCTIONS": "Write to /mock/opportunities.md.",
            "OPPORTUNITY_READ_INSTRUCTIONS": "Read from /mock/opportunities.md.",
        }

    def list_opportunities(self) -> list[Opportunity]:
        return [
            Opportunity(
                opportunity_id="opp-mock",
                title="Mock Opportunity",
                status=OpportunityStatus.identified,
                description="Desc",
            )
        ]


class MockTasklistProviderWithPlaceholders(InMemoryTasklistProvider):
    """Tasklist provider that returns known prompt placeholders."""

    def get_prompt_placeholders(self) -> dict[str, str]:
        return {
            "TASKLIST_APPEND_INSTRUCTIONS": "Append tasks to /mock/tasklist.md.",
            "TASKLIST_UPDATE_INSTRUCTIONS": "Edit tasks in /mock/tasklist.md.",
            "TASKLIST_READ_INSTRUCTIONS": "Read tasks from /mock/tasklist.md.",
            "TASKLIST_COMPLETE_INSTRUCTIONS": "Mark done in /mock/tasklist.md.",
            "TASKLIST_REWRITE_INSTRUCTIONS": "Rewrite /mock/tasklist.md.",
        }


class MockDesignProviderWithPlaceholders(InMemoryDesignProvider):
    """Design provider that returns known prompt placeholders."""

    def get_prompt_placeholders(self) -> dict[str, str]:
        return {
            "DESIGN_WRITE_INSTRUCTIONS": "Write design to /mock/designs/{slug}.md.",
            "DESIGN_READ_INSTRUCTIONS": "Read design from /mock/designs/.",
        }


def test_run_analyze_prompt_contains_opportunity_write_instructions(temp_repo):
    """run_analyze() substitutes OPPORTUNITY_WRITE_INSTRUCTIONS from the provider."""
    provider = MockOpportunityProviderWithPlaceholders()
    manager = _make_outer_manager(temp_repo, opportunity_provider=provider)

    captured_prompts: list[str] = []

    def capture_prompt(prompt: str) -> str:
        captured_prompts.append(prompt)
        return "ok"

    manager.run_analyze(
        load_prompt_callback=lambda _name: "{{OPPORTUNITY_WRITE_INSTRUCTIONS}} {{OTHER_TOKEN}}",
        run_agent_callback=capture_prompt,
    )

    assert captured_prompts, "agent callback was never called"
    dispatched = captured_prompts[0]
    assert "Write to /mock/opportunities.md." in dispatched
    # Non-provider tokens must survive substitution
    assert "{{OTHER_TOKEN}}" in dispatched
    # No unresolved provider placeholder token
    assert "{{OPPORTUNITY_WRITE_INSTRUCTIONS}}" not in dispatched


def test_run_analyze_fix_prompt_contains_opportunity_write_instructions(temp_repo):
    """run_analyze() with reviewer_callback substitutes OPPORTUNITY_WRITE_INSTRUCTIONS in fix prompt."""
    provider = MockOpportunityProviderWithPlaceholders()
    manager = _make_outer_manager(temp_repo, opportunity_provider=provider)

    fix_prompts: list[str] = []

    def agent_cb(prompt: str) -> str:
        return "ok"

    def reviewer_cb(prompt: str) -> str:
        return '{"verdict": "APPROVED", "score": 9, "strengths": [], "issues": [], "feedback": ""}'

    # Track fix_prompt by supplying a load_prompt_callback that differentiates by name.
    def load_prompt(name: str) -> str:
        return f"prompt_name: {name}\n{{{{OPPORTUNITY_WRITE_INSTRUCTIONS}}}}"

    def capturing_agent(prompt: str) -> str:
        for name in ("analyze_fix_prompt.md",):
            if f"prompt_name: {name}" in prompt:
                fix_prompts.append(prompt)
        return "ok"

    manager.run_analyze(
        load_prompt_callback=load_prompt,
        run_agent_callback=capturing_agent,
        reviewer_callback=reviewer_cb,
    )

    # The fix prompt is only dispatched on cycle 2+; reviewer approved on first pass.
    # We just verify that the initial analyze prompt had the substitution applied.
    # To test fix prompt substitution, use a reviewer that rejects once.
    fix_prompts.clear()
    reject_count = [0]

    def rejecting_reviewer(prompt: str) -> str:
        if reject_count[0] == 0:
            reject_count[0] += 1
            return '{"verdict": "NEEDS_REVISION", "score": 2, "strengths": [], "issues": ["Major: fix this"], "feedback": "fix it"}'
        return '{"verdict": "APPROVED", "score": 9, "strengths": [], "issues": [], "feedback": ""}'

    manager2 = _make_outer_manager(
        temp_repo, opportunity_provider=MockOpportunityProviderWithPlaceholders()
    )
    manager2.max_cycles = 3

    manager2.run_analyze(
        load_prompt_callback=load_prompt,
        run_agent_callback=capturing_agent,
        reviewer_callback=rejecting_reviewer,
    )

    # At least one fix prompt should have been dispatched with the substitution.
    assert fix_prompts, "fix prompt was never dispatched"
    assert all("Write to /mock/opportunities.md." in p for p in fix_prompts)
    assert all("{{OPPORTUNITY_WRITE_INSTRUCTIONS}}" not in p for p in fix_prompts)


def test_run_plan_prompt_contains_tasklist_append_instructions(temp_repo):
    """run_plan() substitutes TASKLIST_APPEND_INSTRUCTIONS from the provider in the plan prompt."""
    manager = _make_outer_manager(temp_repo)
    manager.opportunity_provider.write_opportunity(
        Opportunity(
            opportunity_id="opp-placeholder-test",
            title="Placeholder Test",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    )
    manager.design_provider.write_design(
        Design(
            design_id="placeholder-test",
            title="Placeholder Test Design",
            status=DesignStatus.draft,
            body="Design body",
            opportunity_ref="opp-placeholder-test",
        )
    )
    tasklist_provider = MockTasklistProviderWithPlaceholders("# Tasklist\n")
    manager.tasklist_provider = tasklist_provider
    manager.review_plan = lambda **_kwargs: {"approved": True}  # type: ignore[method-assign]

    plan_prompts: list[str] = []

    def run_agent(prompt: str) -> str:
        if "prompt_name: plan_prompt.md" in prompt:
            plan_prompts.append(prompt)
            tasklist_provider.restore_snapshot(
                "# Tasklist\n\n- [ ] **New Task**\n  - ID: new-task\n  - Context: none\n"
            )
        return "ok"

    manager._run_plan_impl(
        design_path=str(temp_repo / "designs" / "placeholder-test.md"),
        load_prompt_callback=lambda name: (
            f"prompt_name: {name}\n{{{{TASKLIST_APPEND_INSTRUCTIONS}}}} {{{{OTHER_TOKEN}}}}"
        ),
        run_agent_callback=run_agent,
    )

    assert plan_prompts, "plan prompt was never dispatched"
    dispatched = plan_prompts[0]
    assert "Append tasks to /mock/tasklist.md." in dispatched
    # Non-provider tokens survive substitution
    assert "{{OTHER_TOKEN}}" in dispatched
    # No unresolved provider placeholder token
    assert "{{TASKLIST_APPEND_INSTRUCTIONS}}" not in dispatched


def test_run_analyze_non_provider_tokens_survive(temp_repo):
    """Non-provider tokens in analyze prompts are not touched by provider substitution."""
    provider = MockOpportunityProviderWithPlaceholders()
    manager = _make_outer_manager(temp_repo, opportunity_provider=provider)

    captured: list[str] = []

    manager.run_analyze(
        load_prompt_callback=lambda _name: (
            "{{HARD_SIGNALS}} {{CUSTOM_TOKEN}} {{OPPORTUNITY_WRITE_INSTRUCTIONS}}"
        ),
        run_agent_callback=lambda p: captured.append(p) or "ok",
    )

    assert captured
    dispatched = captured[0]
    # HARD_SIGNALS and CUSTOM_TOKEN are not provider placeholder keys — must survive.
    assert "{{CUSTOM_TOKEN}}" in dispatched
    # OPPORTUNITY_WRITE_INSTRUCTIONS is a provider key — must be substituted.
    assert "{{OPPORTUNITY_WRITE_INSTRUCTIONS}}" not in dispatched
    assert "Write to /mock/opportunities.md." in dispatched


def test_run_plan_impl_legacy_tasklist_path_resolved_in_plan_prompt(temp_repo):
    """_run_plan_impl resolves legacy {{TASKLIST_PATH}} in custom plan_prompt.md templates."""
    manager = _make_outer_manager(temp_repo)
    manager.opportunity_provider.write_opportunity(
        Opportunity(
            opportunity_id="opp-legacy-path",
            title="Legacy Path Test",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    )
    manager.design_provider.write_design(
        Design(
            design_id="legacy-path-test",
            title="Legacy Path Design",
            status=DesignStatus.draft,
            body="Design body",
            opportunity_ref="opp-legacy-path",
        )
    )
    tasklist_provider = MockTasklistProviderWithPlaceholders("# Tasklist\n")
    manager.tasklist_provider = tasklist_provider
    manager.review_plan = lambda **_kwargs: {"approved": True}  # type: ignore[method-assign]

    captured: list[str] = []

    def run_agent(prompt: str) -> str:
        captured.append(prompt)
        if "plan_prompt.md" in prompt:
            tasklist_provider.restore_snapshot(
                "# Tasklist\n\n- [ ] **New Task**\n  - ID: new-task\n  - Context: none\n"
            )
        return "ok"

    manager._run_plan_impl(
        design_path=str(temp_repo / "designs" / "legacy-path-test.md"),
        load_prompt_callback=lambda name: f"prompt_name: {name}\n{{{{TASKLIST_PATH}}}}",
        run_agent_callback=run_agent,
    )

    assert captured, "plan prompt was never dispatched"
    dispatched = captured[0]
    assert "{{TASKLIST_PATH}}" not in dispatched
    assert ".millstone/tasklist.md" in dispatched


def test_run_plan_impl_legacy_tasklist_path_resolved_in_fix_prompt(temp_repo):
    """_run_plan_impl resolves legacy {{TASKLIST_PATH}} in plan_fix_prompt.md templates."""
    manager = _make_outer_manager(temp_repo)
    manager.opportunity_provider.write_opportunity(
        Opportunity(
            opportunity_id="opp-legacy-fix",
            title="Legacy Fix Test",
            status=OpportunityStatus.identified,
            description="Desc",
        )
    )
    manager.design_provider.write_design(
        Design(
            design_id="legacy-fix-test",
            title="Legacy Fix Design",
            status=DesignStatus.draft,
            body="Design body",
            opportunity_ref="opp-legacy-fix",
        )
    )
    tasklist_provider = MockTasklistProviderWithPlaceholders("# Tasklist\n")
    manager.tasklist_provider = tasklist_provider

    call_count = 0

    def run_agent(prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        if "plan_prompt.md" in prompt:
            tasklist_provider.restore_snapshot(
                "# Tasklist\n\n- [ ] **New Task**\n  - ID: new-task\n  - Context: none\n"
            )
        return "ok"

    def fake_review_plan(**kwargs):
        if call_count == 1:
            return {"approved": False, "feedback": "Please add more detail."}
        return {"approved": True}

    manager.review_plan = fake_review_plan  # type: ignore[method-assign]
    manager.max_cycles = 3

    captured: list[str] = []

    def capturing_run_agent(prompt: str) -> str:
        captured.append(prompt)
        return run_agent(prompt)

    manager._run_plan_impl(
        design_path=str(temp_repo / "designs" / "legacy-fix-test.md"),
        load_prompt_callback=lambda name: f"prompt_name: {name}\n{{{{TASKLIST_PATH}}}}",
        run_agent_callback=capturing_run_agent,
    )

    fix_dispatched = [p for p in captured if "plan_fix_prompt.md" in p]
    assert fix_dispatched, "fix prompt was never dispatched"
    assert "{{TASKLIST_PATH}}" not in fix_dispatched[0]
    assert ".millstone/tasklist.md" in fix_dispatched[0]
