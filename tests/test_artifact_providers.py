"""Tests for artifact provider Protocol interfaces.

TDD: tests written before implementation to confirm red → green.
"""

from millstone.artifacts.models import (
    Design,
    DesignStatus,
    Opportunity,
    OpportunityStatus,
    TasklistItem,
    TaskStatus,
)

# ---------------------------------------------------------------------------
# Stub implementations for Protocol conformance checks
# ---------------------------------------------------------------------------

class StubOpportunityProvider:
    def list_opportunities(self) -> list[Opportunity]:
        return []

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        return None

    def write_opportunity(self, opportunity: Opportunity) -> None:
        pass

    def update_opportunity_status(self, opportunity_id: str, status: OpportunityStatus) -> None:
        pass

    def get_prompt_placeholders(self) -> dict[str, str]:
        return {}


class StubDesignProvider:
    def list_designs(self) -> list[Design]:
        return []

    def get_design(self, design_id: str) -> Design | None:
        return None

    def write_design(self, design: Design) -> None:
        pass

    def update_design_status(self, design_id: str, status: DesignStatus) -> None:
        pass

    def get_prompt_placeholders(self) -> dict[str, str]:
        return {}


class StubTasklistProvider:
    def list_tasks(self) -> list[TasklistItem]:
        return []

    def get_task(self, task_id: str) -> TasklistItem | None:
        return None

    def append_tasks(self, tasks: list[TasklistItem]) -> None:
        pass

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        pass

    def get_snapshot(self) -> str:
        return ""

    def restore_snapshot(self, content: str) -> None:
        pass

    def get_prompt_placeholders(self) -> dict[str, str]:
        return {}


# ---------------------------------------------------------------------------
# Protocol import tests
# ---------------------------------------------------------------------------

def test_protocol_imports():
    """OpportunityProvider, DesignProvider, TasklistProvider are importable."""
    from millstone.artifact_providers.protocols import (
        DesignProvider,
        OpportunityProvider,
        TasklistProvider,
    )
    assert OpportunityProvider is not None
    assert DesignProvider is not None
    assert TasklistProvider is not None


# ---------------------------------------------------------------------------
# runtime_checkable isinstance tests
# ---------------------------------------------------------------------------

def test_opportunity_provider_protocol_isinstance_check():
    """isinstance(stub, OpportunityProvider) returns True for a compliant object."""
    from millstone.artifact_providers.protocols import OpportunityProvider
    stub = StubOpportunityProvider()
    assert isinstance(stub, OpportunityProvider)


def test_design_provider_protocol_isinstance_check():
    """isinstance(stub, DesignProvider) returns True for a compliant object."""
    from millstone.artifact_providers.protocols import DesignProvider
    stub = StubDesignProvider()
    assert isinstance(stub, DesignProvider)


def test_tasklist_provider_protocol_isinstance_check():
    """isinstance(stub, TasklistProvider) returns True for a compliant object."""
    from millstone.artifact_providers.protocols import TasklistProvider
    stub = StubTasklistProvider()
    assert isinstance(stub, TasklistProvider)


# ---------------------------------------------------------------------------
# Non-conforming objects fail isinstance
# ---------------------------------------------------------------------------

def test_non_conforming_object_fails_opportunity_provider():
    """An object missing required methods does not satisfy OpportunityProvider."""
    from millstone.artifact_providers.protocols import OpportunityProvider

    class Incomplete:
        def list_opportunities(self) -> list[Opportunity]:
            return []
        # missing get_opportunity, write_opportunity, update_opportunity_status

    assert not isinstance(Incomplete(), OpportunityProvider)


def test_non_conforming_object_fails_design_provider():
    """An object missing required methods does not satisfy DesignProvider."""
    from millstone.artifact_providers.protocols import DesignProvider

    class Incomplete:
        def list_designs(self) -> list[Design]:
            return []
        # missing get_design, write_design, update_design_status

    assert not isinstance(Incomplete(), DesignProvider)


def test_non_conforming_object_fails_tasklist_provider():
    """An object missing required methods does not satisfy TasklistProvider."""
    from millstone.artifact_providers.protocols import TasklistProvider

    class Incomplete:
        def list_tasks(self) -> list[TasklistItem]:
            return []
        # missing get_task, append_tasks, update_task_status

    assert not isinstance(Incomplete(), TasklistProvider)


# ---------------------------------------------------------------------------
# Method signature spot-checks (call the stubs through Protocol typed var)
# ---------------------------------------------------------------------------

def test_opportunity_provider_methods_callable():
    """All OpportunityProvider methods are callable with correct signatures."""
    from millstone.artifact_providers.protocols import OpportunityProvider
    provider: OpportunityProvider = StubOpportunityProvider()

    result = provider.list_opportunities()
    assert result == []

    opp = provider.get_opportunity("some-id")
    assert opp is None

    sample_opp = Opportunity(
        opportunity_id="test-id",
        title="Test",
        status=OpportunityStatus.identified,
        description="A test",
    )
    provider.write_opportunity(sample_opp)  # should not raise

    provider.update_opportunity_status("test-id", OpportunityStatus.adopted)  # should not raise


def test_design_provider_methods_callable():
    """All DesignProvider methods are callable with correct signatures."""
    from millstone.artifact_providers.protocols import DesignProvider
    provider: DesignProvider = StubDesignProvider()

    result = provider.list_designs()
    assert result == []

    design = provider.get_design("some-id")
    assert design is None

    sample_design = Design(
        design_id="test-design",
        title="Test Design",
        status=DesignStatus.draft,
        body="## Body",
    )
    provider.write_design(sample_design)  # should not raise

    provider.update_design_status("test-design", DesignStatus.approved)  # should not raise


def test_tasklist_provider_methods_callable():
    """All TasklistProvider methods are callable with correct signatures."""
    from millstone.artifact_providers.protocols import TasklistProvider
    provider: TasklistProvider = StubTasklistProvider()

    result = provider.list_tasks()
    assert result == []

    task = provider.get_task("some-id")
    assert task is None

    sample_task = TasklistItem(
        task_id="task-1",
        title="Do something",
        status=TaskStatus.todo,
    )
    provider.append_tasks([sample_task])  # should not raise

    provider.update_task_status("task-1", TaskStatus.done)  # should not raise
    assert provider.get_snapshot() == ""
    provider.restore_snapshot("# Tasklist\n")


# ---------------------------------------------------------------------------
# get_prompt_placeholders default implementation tests
# ---------------------------------------------------------------------------

def test_opportunity_provider_base_get_prompt_placeholders_returns_empty():
    """OpportunityProviderBase.get_prompt_placeholders() returns {} by default."""
    from millstone.artifact_providers.base import OpportunityProviderBase

    class ConcreteOpportunityProvider(OpportunityProviderBase):
        @classmethod
        def from_config(cls, options):
            return cls()
        def list_opportunities(self):
            return []
        def get_opportunity(self, opportunity_id):
            return None
        def write_opportunity(self, opportunity):
            pass
        def update_opportunity_status(self, opportunity_id, status):
            pass

    provider = ConcreteOpportunityProvider()
    assert provider.get_prompt_placeholders() == {}


def test_design_provider_base_get_prompt_placeholders_returns_empty():
    """DesignProviderBase.get_prompt_placeholders() returns {} by default."""
    from millstone.artifact_providers.base import DesignProviderBase

    class ConcreteDesignProvider(DesignProviderBase):
        @classmethod
        def from_config(cls, options):
            return cls()
        def list_designs(self):
            return []
        def get_design(self, design_id):
            return None
        def write_design(self, design):
            pass
        def update_design_status(self, design_id, status):
            pass

    provider = ConcreteDesignProvider()
    assert provider.get_prompt_placeholders() == {}


def test_tasklist_provider_base_get_prompt_placeholders_returns_empty():
    """TasklistProviderBase.get_prompt_placeholders() returns {} by default."""
    from millstone.artifact_providers.base import TasklistProviderBase

    class ConcreteTasklistProvider(TasklistProviderBase):
        @classmethod
        def from_config(cls, options):
            return cls()
        def list_tasks(self):
            return []
        def get_task(self, task_id):
            return None
        def append_tasks(self, tasks):
            pass
        def update_task_status(self, task_id, status):
            pass
        def get_snapshot(self):
            return ""
        def restore_snapshot(self, content):
            pass

    provider = ConcreteTasklistProvider()
    assert provider.get_prompt_placeholders() == {}
