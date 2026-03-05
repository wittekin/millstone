"""Tests for artifact model dataclasses and status enums (artifact-models task)."""
import pytest

from millstone.artifacts.models import (
    ArtifactValidationError,
    Design,
    DesignStatus,
    Opportunity,
    OpportunityStatus,
    TasklistItem,
    TaskStatus,
)


class TestOpportunityStatus:
    def test_values(self):
        assert OpportunityStatus.identified == "identified"
        assert OpportunityStatus.adopted == "adopted"
        assert OpportunityStatus.rejected == "rejected"

    def test_is_str_enum(self):
        assert isinstance(OpportunityStatus.identified, str)

    def test_all_values(self):
        values = {s.value for s in OpportunityStatus}
        assert values == {"identified", "adopted", "rejected"}


class TestDesignStatus:
    def test_values(self):
        assert DesignStatus.draft == "draft"
        assert DesignStatus.reviewed == "reviewed"
        assert DesignStatus.approved == "approved"
        assert DesignStatus.superseded == "superseded"

    def test_is_str_enum(self):
        assert isinstance(DesignStatus.draft, str)

    def test_all_values(self):
        values = {s.value for s in DesignStatus}
        assert values == {"draft", "reviewed", "approved", "superseded"}


class TestTaskStatus:
    def test_values(self):
        assert TaskStatus.todo == "todo"
        assert TaskStatus.in_progress == "in_progress"
        assert TaskStatus.done == "done"
        assert TaskStatus.blocked == "blocked"

    def test_is_str_enum(self):
        assert isinstance(TaskStatus.todo, str)

    def test_all_values(self):
        values = {s.value for s in TaskStatus}
        assert values == {"todo", "in_progress", "done", "blocked"}


class TestOpportunity:
    def test_required_fields(self):
        opp = Opportunity(
            opportunity_id="test-opp",
            title="Test Opportunity",
            status=OpportunityStatus.identified,
            description="A test opportunity",
        )
        assert opp.opportunity_id == "test-opp"
        assert opp.title == "Test Opportunity"
        assert opp.status == OpportunityStatus.identified
        assert opp.description == "A test opportunity"

    def test_optional_fields_default_none(self):
        opp = Opportunity(
            opportunity_id="test-opp",
            title="Test",
            status=OpportunityStatus.identified,
            description="desc",
        )
        assert opp.requires_design is None
        assert opp.design_ref is None
        assert opp.source_ref is None
        assert opp.priority is None
        assert opp.roi_score is None
        assert opp.raw is None

    def test_optional_fields_can_be_set(self):
        opp = Opportunity(
            opportunity_id="test-opp",
            title="Test",
            status=OpportunityStatus.identified,
            description="desc",
            requires_design=True,
            design_ref="some-design",
            source_ref="some-source",
            priority="high",
            roi_score=2.5,
            raw="raw markdown block",
        )
        assert opp.requires_design is True
        assert opp.design_ref == "some-design"
        assert opp.source_ref == "some-source"
        assert opp.priority == "high"
        assert opp.roi_score == 2.5
        assert opp.raw == "raw markdown block"

    def test_is_dataclass(self):
        import dataclasses
        assert dataclasses.is_dataclass(Opportunity)


class TestDesign:
    def test_required_fields(self):
        design = Design(
            design_id="test-design",
            title="Test Design",
            status=DesignStatus.draft,
            body="# Body content",
        )
        assert design.design_id == "test-design"
        assert design.title == "Test Design"
        assert design.status == DesignStatus.draft
        assert design.body == "# Body content"

    def test_opportunity_ref_is_optional(self):
        # TEMPORARY NON-CONFORMANCE: ontology requires opportunity_ref (ontology.md:82,103)
        # but it is Optional here to accommodate legacy files parsed from disk without it.
        design = Design(
            design_id="test-design",
            title="Test Design",
            status=DesignStatus.draft,
            body="body",
        )
        assert design.opportunity_ref is None

    def test_opportunity_ref_can_be_set(self):
        design = Design(
            design_id="test-design",
            title="Test Design",
            status=DesignStatus.draft,
            body="body",
            opportunity_ref="some-opportunity",
        )
        assert design.opportunity_ref == "some-opportunity"

    def test_optional_fields_default_none(self):
        design = Design(
            design_id="test-design",
            title="Test Design",
            status=DesignStatus.draft,
            body="body",
        )
        assert design.opportunity_ref is None
        assert design.tasklist_ref is None
        assert design.review_summary is None

    def test_is_dataclass(self):
        import dataclasses
        assert dataclasses.is_dataclass(Design)


class TestTasklistItem:
    def test_required_fields(self):
        item = TasklistItem(
            task_id="task-001",
            title="Do something",
            status=TaskStatus.todo,
        )
        assert item.task_id == "task-001"
        assert item.title == "Do something"
        assert item.status == TaskStatus.todo

    def test_optional_fields_default_none(self):
        item = TasklistItem(
            task_id="task-001",
            title="Do something",
            status=TaskStatus.todo,
        )
        assert item.design_ref is None
        assert item.opportunity_ref is None
        assert item.risk is None
        assert item.tests is None
        assert item.criteria is None
        assert item.context is None
        assert item.raw is None

    def test_optional_fields_can_be_set(self):
        item = TasklistItem(
            task_id="task-001",
            title="Do something",
            status=TaskStatus.in_progress,
            design_ref="my-design",
            opportunity_ref="my-opp",
            risk="medium",
            tests="pytest tests/test_foo.py",
            criteria="must pass",
            context="some context",
            raw="- [ ] Do something\n  - ID: task-001",
        )
        assert item.design_ref == "my-design"
        assert item.opportunity_ref == "my-opp"
        assert item.risk == "medium"
        assert item.tests == "pytest tests/test_foo.py"
        assert item.criteria == "must pass"
        assert item.context == "some context"
        assert item.raw == "- [ ] Do something\n  - ID: task-001"

    def test_is_dataclass(self):
        import dataclasses
        assert dataclasses.is_dataclass(TasklistItem)


class TestNoInternalImports:
    """Verify artifact_models has no imports from other millstone modules."""

    def test_module_source_has_no_millstone_imports(self):
        import inspect

        import millstone.artifacts.models as mod
        source = inspect.getsource(mod)
        lines = source.splitlines()
        import_lines = [
            line
            for line in lines
            if line.startswith("from millstone") or line.startswith("import millstone")
        ]
        assert import_lines == [], f"artifact_models must not import from millstone: {import_lines}"


class TestArtifactValidationError:
    def test_exposes_artifact_type_and_violations(self):
        error = ArtifactValidationError(
            "Opportunity",
            ["opportunity_id is required and must not be empty"],
        )
        assert error.artifact_type == "Opportunity"
        assert error.violations == ["opportunity_id is required and must not be empty"]


class TestModelValidation:
    def test_opportunity_validate_accepts_valid_record(self):
        opportunity = Opportunity(
            opportunity_id="valid-opportunity",
            title="Valid opportunity",
            status=OpportunityStatus.identified,
            description="Valid description",
        )
        opportunity.validate()

    def test_opportunity_validate_reports_all_required_field_violations(self):
        opportunity = Opportunity(
            opportunity_id="",
            title="",
            status="identified",
            description="",
        )
        with pytest.raises(ArtifactValidationError) as exc_info:
            opportunity.validate()
        error = exc_info.value
        assert error.artifact_type == "Opportunity"
        assert any("opportunity_id" in violation for violation in error.violations)
        assert any("title" in violation for violation in error.violations)
        assert any("status" in violation for violation in error.violations)
        assert any("description" in violation for violation in error.violations)

    @pytest.mark.parametrize("value", ["Upper", "has_underscore", "bad--", "-start", "end-"])
    def test_opportunity_validate_rejects_non_slug_opportunity_id(self, value):
        opportunity = Opportunity(
            opportunity_id=value,
            title="Valid title",
            status=OpportunityStatus.identified,
            description="Valid description",
        )
        with pytest.raises(ArtifactValidationError) as exc_info:
            opportunity.validate()
        assert any("opportunity_id" in violation for violation in exc_info.value.violations)

    def test_design_validate_accepts_valid_record(self):
        design = Design(
            design_id="valid-design",
            title="Valid design",
            status=DesignStatus.draft,
            body="Design body",
            opportunity_ref="valid-opportunity",
        )
        design.validate()

    def test_design_validate_reports_all_required_field_violations(self):
        design = Design(
            design_id="",
            title="",
            status="draft",
            body="",
            opportunity_ref=None,
        )
        with pytest.raises(ArtifactValidationError) as exc_info:
            design.validate()
        error = exc_info.value
        assert error.artifact_type == "Design"
        assert any("design_id" in violation for violation in error.violations)
        assert any("title" in violation for violation in error.violations)
        assert any("status" in violation for violation in error.violations)
        assert any("opportunity_ref" in violation for violation in error.violations)
        assert any("body" in violation for violation in error.violations)

    @pytest.mark.parametrize("value", ["Upper", "has_underscore", "bad--", "-start", "end-"])
    def test_design_validate_rejects_non_slug_design_id(self, value):
        design = Design(
            design_id=value,
            title="Valid title",
            status=DesignStatus.draft,
            body="Body",
            opportunity_ref="valid-opportunity",
        )
        with pytest.raises(ArtifactValidationError) as exc_info:
            design.validate()
        assert any("design_id" in violation for violation in exc_info.value.violations)

    def test_tasklist_item_validate_accepts_valid_record(self):
        item = TasklistItem(
            task_id="task_01-ok",
            title="Valid task",
            status=TaskStatus.todo,
        )
        item.validate()

    @pytest.mark.parametrize(
        "value",
        [
            "UPPER",
            "bad.id",
            "",
            "x" * 41,
        ],
    )
    def test_tasklist_item_validate_rejects_invalid_task_id_pattern(self, value):
        item = TasklistItem(
            task_id=value,
            title="Valid task",
            status=TaskStatus.todo,
        )
        with pytest.raises(ArtifactValidationError) as exc_info:
            item.validate()
        assert any("task_id" in violation for violation in exc_info.value.violations)

    def test_tasklist_item_validate_reports_all_required_field_violations(self):
        item = TasklistItem(
            task_id="",
            title="",
            status="todo",
        )
        with pytest.raises(ArtifactValidationError) as exc_info:
            item.validate()
        error = exc_info.value
        assert error.artifact_type == "TasklistItem"
        assert any("task_id" in violation for violation in error.violations)
        assert any("title" in violation for violation in error.violations)
        assert any("status" in violation for violation in error.violations)

    def test_validate_is_not_called_on_init(self):
        design = Design(
            design_id="",
            title="",
            status="draft",
            body="",
            opportunity_ref=None,
        )
        assert design.design_id == ""
