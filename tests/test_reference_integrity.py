"""Tests for cross-artifact reference integrity enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest

from millstone.artifact_providers.file import FileDesignProvider, FileOpportunityProvider
from millstone.artifacts.models import (
    Design,
    DesignStatus,
    Opportunity,
    OpportunityStatus,
    TasklistItem,
    TaskStatus,
)
from millstone.loops.outer import OuterLoopManager
from millstone.policy.reference_integrity import (
    DesignLookup,
    OpportunityLookup,
    ReferenceIntegrityChecker,
    ReferenceIntegrityError,
)


class StubOpportunityProvider:
    def __init__(self, opportunities: dict[str, Opportunity] | None = None) -> None:
        self._opportunities = opportunities or {}

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        return self._opportunities.get(opportunity_id)


class StubDesignProvider:
    def __init__(self, designs: dict[str, Design] | None = None) -> None:
        self._designs = designs or {}

    def get_design(self, design_id: str) -> Design | None:
        return self._designs.get(design_id)


def _make_opportunity(opportunity_id: str = "opp-1") -> Opportunity:
    return Opportunity(
        opportunity_id=opportunity_id,
        title="Opportunity",
        status=OpportunityStatus.identified,
        description="Desc",
    )


def _make_design(
    design_id: str = "design-1",
    opportunity_ref: str | None = "opp-1",
) -> Design:
    return Design(
        design_id=design_id,
        title="Design",
        status=DesignStatus.draft,
        body="Body",
        opportunity_ref=opportunity_ref,
    )


def _make_task(
    task_id: str = "task-1",
    design_ref: str | None = None,
    opportunity_ref: str | None = None,
) -> TasklistItem:
    return TasklistItem(
        task_id=task_id,
        title="Task",
        status=TaskStatus.todo,
        design_ref=design_ref,
        opportunity_ref=opportunity_ref,
    )


def _make_outer_manager(repo_dir: Path) -> OuterLoopManager:
    work_dir = repo_dir / ".millstone"
    work_dir.mkdir(exist_ok=True)
    return OuterLoopManager(
        work_dir=work_dir,
        repo_dir=repo_dir,
        tasklist="docs/tasklist.md",
        task_constraints={
            "max_loc": 200,
            "require_tests": False,
            "require_criteria": False,
            "require_risk": False,
            "require_context": False,
            "max_split_attempts": 0,
        },
    )


class TestReferenceIntegrityChecker:
    def test_reference_integrity_error_exposes_violations(self):
        exc = ReferenceIntegrityError(["v1"])
        assert exc.violations == ["v1"]
        assert "v1" in str(exc)

    def test_checker_constructable_with_optional_providers(self):
        checker = ReferenceIntegrityChecker(opportunity_provider=StubOpportunityProvider())
        assert checker is not None

    def test_check_design_raises_when_opportunity_ref_missing(self):
        checker = ReferenceIntegrityChecker(opportunity_provider=StubOpportunityProvider())
        with pytest.raises(ReferenceIntegrityError):
            checker.check_design(_make_design(opportunity_ref=None))

    def test_check_design_raises_when_opportunity_missing(self):
        checker = ReferenceIntegrityChecker(opportunity_provider=StubOpportunityProvider())
        with pytest.raises(ReferenceIntegrityError) as exc_info:
            checker.check_design(_make_design(opportunity_ref="missing"))
        assert "missing" in str(exc_info.value)

    def test_check_design_passes_when_opportunity_exists(self):
        checker = ReferenceIntegrityChecker(
            opportunity_provider=StubOpportunityProvider({"opp-1": _make_opportunity("opp-1")})
        )
        checker.check_design(_make_design(opportunity_ref="opp-1"))

    def test_check_design_without_opportunity_provider_raises_value_error(self):
        checker = ReferenceIntegrityChecker()
        with pytest.raises(ValueError):
            checker.check_design(_make_design(opportunity_ref="opp-1"))

    def test_check_opportunity_allows_missing_design_ref(self):
        checker = ReferenceIntegrityChecker()
        checker.check_opportunity(_make_opportunity())

    def test_check_opportunity_raises_when_design_missing(self):
        checker = ReferenceIntegrityChecker(design_provider=StubDesignProvider())
        opportunity = _make_opportunity()
        opportunity.design_ref = "missing-design"
        with pytest.raises(ReferenceIntegrityError):
            checker.check_opportunity(opportunity)

    def test_check_opportunity_passes_when_design_exists(self):
        checker = ReferenceIntegrityChecker(
            design_provider=StubDesignProvider({"design-1": _make_design("design-1")})
        )
        opportunity = _make_opportunity()
        opportunity.design_ref = "design-1"
        checker.check_opportunity(opportunity)

    def test_check_task_no_refs_is_noop(self):
        checker = ReferenceIntegrityChecker()
        checker.check_task(_make_task())

    def test_check_task_design_ref_raises_when_missing(self):
        checker = ReferenceIntegrityChecker(design_provider=StubDesignProvider())
        with pytest.raises(ReferenceIntegrityError):
            checker.check_task(_make_task(design_ref="missing-design"))

    def test_check_tasks_accumulates_multiple_violations(self):
        checker = ReferenceIntegrityChecker(
            opportunity_provider=StubOpportunityProvider(),
            design_provider=StubDesignProvider(),
        )
        tasks = [
            _make_task(task_id="task-a", design_ref="missing-design"),
            _make_task(task_id="task-b", opportunity_ref="missing-opportunity"),
        ]
        with pytest.raises(ReferenceIntegrityError) as exc_info:
            checker.check_tasks(tasks)
        assert len(exc_info.value.violations) == 2
        assert "task-a" in str(exc_info.value)
        assert "task-b" in str(exc_info.value)

    def test_file_providers_match_lookup_protocols(self, tmp_path):
        opportunity_provider = FileOpportunityProvider(tmp_path / "opps.md")
        design_provider = FileDesignProvider(tmp_path / "designs")
        assert isinstance(opportunity_provider, OpportunityLookup)
        assert isinstance(design_provider, DesignLookup)


class TestOuterLoopReferenceIntegrity:
    def test_run_design_succeeds_with_opportunity_id_substitution(self, temp_repo):
        manager = _make_outer_manager(temp_repo)
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)
        manager.opportunity_provider.write_opportunity(
            Opportunity(
                opportunity_id="my-id",
                title="Test opportunity",
                status=OpportunityStatus.identified,
                description="Desc",
            )
        )

        def load_prompt(prompt_name: str) -> str:
            return (
                f"prompt_name: {prompt_name}\n"
                "opportunity={{OPPORTUNITY}}\n"
                "opportunity_id={{OPPORTUNITY_ID}}"
            )

        def run_agent(prompt: str) -> str:
            assert "opportunity=Test opportunity" in prompt
            assert "opportunity_id=my-id" in prompt
            assert "{{OPPORTUNITY}}" not in prompt
            assert "{{OPPORTUNITY_ID}}" not in prompt
            (designs_dir / "new-design.md").write_text(
                "\n".join(
                    [
                        "# New Design",
                        "",
                        "- **design_id**: new-design",
                        "- **title**: New Design",
                        "- **status**: draft",
                        "- **opportunity_ref**: my-id",
                        "- **created**: 2026-03-02",
                        "",
                        "---",
                        "",
                        "Body",
                    ]
                )
            )
            return "done"

        result = manager.run_design(
            opportunity="Test opportunity",
            opportunity_id="my-id",
            load_prompt_callback=load_prompt,
            run_agent_callback=run_agent,
        )

        assert result["success"] is True
        assert result["design_file"] == str(designs_dir / "new-design.md")

    def test_run_design_without_opportunity_id_replaces_placeholder_with_empty(self, temp_repo):
        manager = _make_outer_manager(temp_repo)
        captured_prompt: list[str] = []

        def load_prompt(prompt_name: str) -> str:
            return (
                f"prompt_name: {prompt_name}\n"
                "opportunity={{OPPORTUNITY}}\n"
                "opportunity_id={{OPPORTUNITY_ID}}"
            )

        def run_agent(prompt: str) -> str:
            captured_prompt.append(prompt)
            return "done"

        result = manager.run_design(
            opportunity="Test opportunity",
            load_prompt_callback=load_prompt,
            run_agent_callback=run_agent,
        )

        assert result["success"] is False
        assert result["design_file"] is None
        assert captured_prompt
        assert "opportunity=Test opportunity" in captured_prompt[0]
        assert "opportunity_id=" in captured_prompt[0]
        assert "{{OPPORTUNITY}}" not in captured_prompt[0]
        assert "{{OPPORTUNITY_ID}}" not in captured_prompt[0]

    def test_run_design_fails_when_new_design_missing_opportunity_ref(self, temp_repo):
        manager = _make_outer_manager(temp_repo)
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)

        def load_prompt(prompt_name: str) -> str:
            return f"prompt_name: {prompt_name}\n{{{{OPPORTUNITY}}}}"

        def run_agent(_prompt: str) -> str:
            (designs_dir / "new-design.md").write_text(
                "\n".join(
                    [
                        "# New Design",
                        "",
                        "- **design_id**: new-design",
                        "- **title**: New Design",
                        "- **status**: draft",
                        "- **created**: 2026-03-02",
                        "",
                        "---",
                        "",
                        "Body",
                    ]
                )
            )
            return "done"

        result = manager.run_design(
            opportunity="Test opportunity",
            load_prompt_callback=load_prompt,
            run_agent_callback=run_agent,
        )

        assert result["success"] is False
        assert "integrity_error" in result

    def test_run_design_fails_when_new_design_ref_is_unresolvable(self, temp_repo):
        manager = _make_outer_manager(temp_repo)
        designs_dir = temp_repo / ".millstone" / "designs"
        designs_dir.mkdir(exist_ok=True)

        def load_prompt(prompt_name: str) -> str:
            return f"prompt_name: {prompt_name}\n{{{{OPPORTUNITY}}}}"

        def run_agent(_prompt: str) -> str:
            (designs_dir / "new-design.md").write_text(
                "\n".join(
                    [
                        "# New Design",
                        "",
                        "- **design_id**: new-design",
                        "- **title**: New Design",
                        "- **status**: draft",
                        "- **opportunity_ref**: missing-opportunity",
                        "- **created**: 2026-03-02",
                        "",
                        "---",
                        "",
                        "Body",
                    ]
                )
            )
            return "done"

        result = manager.run_design(
            opportunity="Test opportunity",
            load_prompt_callback=load_prompt,
            run_agent_callback=run_agent,
        )

        assert result["success"] is False
        assert "integrity_error" in result
        assert "missing-opportunity" in result["integrity_error"]

    def test_run_plan_reverts_tasklist_on_reference_integrity_failure(self, temp_repo):
        manager = _make_outer_manager(temp_repo)
        tasklist_path = temp_repo / "docs" / "tasklist.md"
        original_content = "# Tasklist\n\n- [ ] Existing task\n"
        tasklist_path.write_text(original_content)

        manager.design_provider.write_design(
            Design(
                design_id="plan-design",
                title="Plan Design",
                status=DesignStatus.draft,
                body="Body",
                opportunity_ref="opp-1",
            )
        )

        def load_prompt(prompt_name: str) -> str:
            return f"prompt_name: {prompt_name}"

        def run_agent(prompt: str) -> str:
            if "prompt_name: plan_prompt.md" in prompt:
                tasklist_path.write_text(
                    original_content
                    + "\n".join(
                        [
                            "",
                            "- [ ] **Broken Task**: Desc",
                            "  - design-ref: missing-design",
                            "  - Context: none",
                            "",
                        ]
                    )
                )
                return "done"
            if "prompt_name: plan_review_prompt.md" in prompt:
                return '{"verdict":"APPROVED","score":10}'
            return "done"

        result = manager._run_plan_impl(
            design_path=str(temp_repo / "designs" / "plan-design.md"),
            load_prompt_callback=load_prompt,
            run_agent_callback=run_agent,
        )

        assert result["success"] is False
        assert "integrity_error" in result
        assert "missing-design" in result["integrity_error"]
        assert tasklist_path.read_text() == original_content

    def test_run_plan_succeeds_when_task_references_resolve(self, temp_repo):
        manager = _make_outer_manager(temp_repo)
        tasklist_path = temp_repo / "docs" / "tasklist.md"
        original_content = "# Tasklist\n\n- [ ] Existing task\n"
        tasklist_path.write_text(original_content)

        manager.opportunity_provider.write_opportunity(
            Opportunity(
                opportunity_id="opp-1",
                title="Opportunity 1",
                status=OpportunityStatus.identified,
                description="Desc",
            )
        )
        manager.design_provider.write_design(
            Design(
                design_id="plan-design",
                title="Plan Design",
                status=DesignStatus.draft,
                body="Body",
                opportunity_ref="opp-1",
            )
        )
        manager.design_provider.write_design(
            Design(
                design_id="linked-design",
                title="Linked Design",
                status=DesignStatus.draft,
                body="Body",
                opportunity_ref="opp-1",
            )
        )

        def load_prompt(prompt_name: str) -> str:
            return f"prompt_name: {prompt_name}"

        def run_agent(prompt: str) -> str:
            if "prompt_name: plan_prompt.md" in prompt:
                tasklist_path.write_text(
                    original_content
                    + "\n".join(
                        [
                            "",
                            "- [ ] **Linked Task**: Desc",
                            "  - design-ref: linked-design",
                            "  - opportunity-ref: opp-1",
                            "  - Context: none",
                            "",
                        ]
                    )
                )
                return "done"
            if "prompt_name: plan_review_prompt.md" in prompt:
                return '{"verdict":"APPROVED","score":10}'
            return "done"

        result = manager._run_plan_impl(
            design_path=str(temp_repo / "designs" / "plan-design.md"),
            load_prompt_callback=load_prompt,
            run_agent_callback=run_agent,
        )

        assert result["success"] is True
        assert result["tasks_added"] == 1
        assert "integrity_error" not in result
        assert "Linked Task" in tasklist_path.read_text()
