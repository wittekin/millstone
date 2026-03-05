"""Provider conformance harness for canonical artifact contracts."""

import pytest

from millstone.artifact_providers.file import (
    FileDesignProvider,
    FileOpportunityProvider,
    FileTasklistProvider,
)
from millstone.artifacts.models import (
    ArtifactValidationError,
    Design,
    DesignStatus,
    Opportunity,
    OpportunityStatus,
    TasklistItem,
    TaskStatus,
)


@pytest.fixture(
    params=[
        pytest.param(
            ("file", lambda tmp_path: FileOpportunityProvider(tmp_path / "opps.md")),
            id="file",
        )
    ]
)
def opportunity_provider_factory(request):
    return request.param[1]


@pytest.fixture(
    params=[
        pytest.param(
            ("file", lambda tmp_path: FileDesignProvider(tmp_path / "designs")),
            id="file",
        )
    ]
)
def design_provider_factory(request):
    return request.param[1]


@pytest.fixture(
    params=[
        pytest.param(
            ("file", lambda tmp_path: FileTasklistProvider(tmp_path / "tasklist.md")),
            id="file",
        ),
    ]
)
def tasklist_provider_factory(request):
    return request.param[1]


class TestOpportunityProviderConformance:
    def test_list_empty(self, tmp_path, opportunity_provider_factory):
        provider = opportunity_provider_factory(tmp_path)
        assert provider.list_opportunities() == []

    def test_write_get_and_update(self, tmp_path, opportunity_provider_factory):
        provider = opportunity_provider_factory(tmp_path)
        provider.write_opportunity(
            Opportunity(
                opportunity_id="opp-one",
                title="Opp One",
                status=OpportunityStatus.identified,
                description="desc one",
            )
        )

        result = provider.get_opportunity("opp-one")
        assert result is not None
        assert result.opportunity_id == "opp-one"
        assert result.title == "Opp One"
        assert result.status == OpportunityStatus.identified
        assert result.description == "desc one"

        provider.update_opportunity_status("opp-one", OpportunityStatus.adopted)
        updated = provider.get_opportunity("opp-one")
        assert updated is not None
        assert updated.status == OpportunityStatus.adopted

    def test_get_nonexistent_returns_none(self, tmp_path, opportunity_provider_factory):
        provider = opportunity_provider_factory(tmp_path)
        assert provider.get_opportunity("missing") is None

    def test_validation_rejection(self, tmp_path, opportunity_provider_factory):
        provider = opportunity_provider_factory(tmp_path)
        with pytest.raises(ArtifactValidationError):
            provider.write_opportunity(
                Opportunity(
                    opportunity_id="",
                    title="Invalid",
                    status=OpportunityStatus.identified,
                    description="desc",
                )
            )

    def test_list_after_two_writes(self, tmp_path, opportunity_provider_factory):
        provider = opportunity_provider_factory(tmp_path)
        provider.write_opportunity(
            Opportunity(
                opportunity_id="opp-a",
                title="Opp A",
                status=OpportunityStatus.identified,
                description="desc",
            )
        )
        provider.write_opportunity(
            Opportunity(
                opportunity_id="opp-b",
                title="Opp B",
                status=OpportunityStatus.adopted,
                description="desc",
            )
        )
        ids = {opp.opportunity_id for opp in provider.list_opportunities()}
        assert ids == {"opp-a", "opp-b"}


class TestDesignProviderConformance:
    def test_list_empty(self, tmp_path, design_provider_factory):
        provider = design_provider_factory(tmp_path)
        assert provider.list_designs() == []

    def test_write_get_and_update(self, tmp_path, design_provider_factory):
        provider = design_provider_factory(tmp_path)
        provider.write_design(
            Design(
                design_id="design-one",
                title="Design One",
                status=DesignStatus.draft,
                body="## Body\n\ncontent",
                opportunity_ref="opp-one",
            )
        )

        result = provider.get_design("design-one")
        assert result is not None
        assert result.design_id == "design-one"
        assert result.title == "Design One"
        assert result.status == DesignStatus.draft
        assert result.opportunity_ref == "opp-one"

        provider.update_design_status("design-one", DesignStatus.approved)
        updated = provider.get_design("design-one")
        assert updated is not None
        assert updated.status == DesignStatus.approved

    def test_get_nonexistent_returns_none(self, tmp_path, design_provider_factory):
        provider = design_provider_factory(tmp_path)
        assert provider.get_design("missing") is None

    def test_validation_rejection(self, tmp_path, design_provider_factory):
        provider = design_provider_factory(tmp_path)
        with pytest.raises(ArtifactValidationError):
            provider.write_design(
                Design(
                    design_id="invalid-design",
                    title="Invalid Design",
                    status=DesignStatus.draft,
                    body="## Body",
                    opportunity_ref=None,
                )
            )

    def test_list_after_two_writes(self, tmp_path, design_provider_factory):
        provider = design_provider_factory(tmp_path)
        provider.write_design(
            Design(
                design_id="design-a",
                title="Design A",
                status=DesignStatus.draft,
                body="A",
                opportunity_ref="opp-a",
            )
        )
        provider.write_design(
            Design(
                design_id="design-b",
                title="Design B",
                status=DesignStatus.reviewed,
                body="B",
                opportunity_ref="opp-b",
            )
        )
        ids = {design.design_id for design in provider.list_designs()}
        assert ids == {"design-a", "design-b"}


class TestTasklistProviderConformance:
    def test_list_empty(self, tmp_path, tasklist_provider_factory):
        provider = tasklist_provider_factory(tmp_path)
        assert provider.list_tasks() == []

    def test_append_get_and_update(self, tmp_path, tasklist_provider_factory):
        provider = tasklist_provider_factory(tmp_path)
        provider.append_tasks(
            [
                TasklistItem(
                    task_id="task-one",
                    title="Task One",
                    status=TaskStatus.todo,
                )
            ]
        )

        listed = provider.list_tasks()
        assert len(listed) == 1
        created_id = listed[0].task_id

        result = provider.get_task(created_id)
        assert result is not None
        assert result.title == "Task One"
        assert result.status == TaskStatus.todo

        provider.update_task_status(created_id, TaskStatus.done)
        updated = provider.get_task(created_id)
        assert updated is not None
        assert updated.status == TaskStatus.done

    def test_get_nonexistent_returns_none(self, tmp_path, tasklist_provider_factory):
        provider = tasklist_provider_factory(tmp_path)
        assert provider.get_task("999999") is None

    def test_validation_rejection(self, tmp_path, tasklist_provider_factory):
        provider = tasklist_provider_factory(tmp_path)
        with pytest.raises(ArtifactValidationError):
            provider.append_tasks(
                [TasklistItem(task_id="", title="Invalid Task", status=TaskStatus.todo)]
            )

    def test_list_after_two_appends(self, tmp_path, tasklist_provider_factory):
        provider = tasklist_provider_factory(tmp_path)
        provider.append_tasks(
            [TasklistItem(task_id="task-a", title="Task A", status=TaskStatus.todo)]
        )
        provider.append_tasks(
            [TasklistItem(task_id="task-b", title="Task B", status=TaskStatus.todo)]
        )
        tasks = provider.list_tasks()
        titles = {task.title for task in tasks}
        assert titles == {"Task A", "Task B"}
