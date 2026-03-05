"""Tests for artifact provider backend registries."""

from unittest.mock import Mock

import pytest

from millstone.artifact_providers.base import (
    DesignProviderBase,
    OpportunityProviderBase,
    TasklistProviderBase,
)
from millstone.artifact_providers.registry import (
    DESIGN_PROVIDERS,
    OPPORTUNITY_PROVIDERS,
    TASKLIST_PROVIDERS,
    get_design_provider,
    get_opportunity_provider,
    get_tasklist_provider,
    list_design_backends,
    list_opportunity_backends,
    list_tasklist_backends,
    register_design_provider,
    register_design_provider_class,
    register_opportunity_provider,
    register_opportunity_provider_class,
    register_tasklist_provider,
    register_tasklist_provider_class,
)
from millstone.artifacts.models import (
    Design,
    DesignStatus,
    Opportunity,
    OpportunityStatus,
    TasklistItem,
    TaskStatus,
)


@pytest.fixture(autouse=True)
def reset_provider_registries():
    """Isolate global registry mutations across tests."""
    original_opportunity = OPPORTUNITY_PROVIDERS.copy()
    original_design = DESIGN_PROVIDERS.copy()
    original_tasklist = TASKLIST_PROVIDERS.copy()

    OPPORTUNITY_PROVIDERS.clear()
    DESIGN_PROVIDERS.clear()
    TASKLIST_PROVIDERS.clear()

    yield

    OPPORTUNITY_PROVIDERS.clear()
    DESIGN_PROVIDERS.clear()
    TASKLIST_PROVIDERS.clear()
    OPPORTUNITY_PROVIDERS.update(original_opportunity)
    DESIGN_PROVIDERS.update(original_design)
    TASKLIST_PROVIDERS.update(original_tasklist)


class MockOpportunityProvider:
    @classmethod
    def from_config(cls, options):  # pragma: no cover - class-registry tests use base subclasses below
        return cls()

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


class MockDesignProvider:
    @classmethod
    def from_config(cls, options):  # pragma: no cover - class-registry tests use base subclasses below
        return cls()

    def list_designs(self) -> list[Design]:
        return []

    def get_design(self, design_id: str) -> Design | None:
        return None

    def write_design(self, design: Design) -> None:
        return None

    def update_design_status(self, design_id: str, status: DesignStatus) -> None:
        return None

    def get_prompt_placeholders(self) -> dict[str, str]:
        return {}


class MockTasklistProvider:
    @classmethod
    def from_config(cls, options):  # pragma: no cover - class-registry tests use base subclasses below
        return cls()

    def list_tasks(self) -> list[TasklistItem]:
        return []

    def get_task(self, task_id: str) -> TasklistItem | None:
        return None

    def append_tasks(self, tasks: list[TasklistItem]) -> None:
        return None

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        return None

    def get_snapshot(self) -> str:
        return ""

    def restore_snapshot(self, content: str) -> None:
        return None

    def get_prompt_placeholders(self) -> dict[str, str]:
        return {}


def test_get_opportunity_provider_unknown_raises_with_available_backends():
    register_opportunity_provider("mock", lambda options: MockOpportunityProvider())

    with pytest.raises(ValueError) as exc_info:
        get_opportunity_provider("unknown", {})

    message = str(exc_info.value)
    assert "unknown" in message
    assert "mock" in message


def test_register_and_get_opportunity_provider_calls_factory():
    provider = MockOpportunityProvider()
    factory = Mock(return_value=provider)
    register_opportunity_provider("mock", factory)

    actual = get_opportunity_provider("mock", {})

    assert actual is provider
    factory.assert_called_once_with({})


def test_list_opportunity_backends_reflects_registered_names():
    register_opportunity_provider("zeta", lambda options: MockOpportunityProvider())
    register_opportunity_provider("alpha", lambda options: MockOpportunityProvider())

    assert list_opportunity_backends() == ["alpha", "zeta"]


def test_get_design_provider_unknown_raises_with_available_backends():
    register_design_provider("mock", lambda options: MockDesignProvider())

    with pytest.raises(ValueError) as exc_info:
        get_design_provider("unknown", {})

    message = str(exc_info.value)
    assert "unknown" in message
    assert "mock" in message


def test_register_and_get_design_provider_calls_factory():
    provider = MockDesignProvider()
    factory = Mock(return_value=provider)
    register_design_provider("mock", factory)

    actual = get_design_provider("mock", {})

    assert actual is provider
    factory.assert_called_once_with({})


def test_list_design_backends_reflects_registered_names():
    register_design_provider("zeta", lambda options: MockDesignProvider())
    register_design_provider("alpha", lambda options: MockDesignProvider())

    assert list_design_backends() == ["alpha", "zeta"]


def test_get_tasklist_provider_unknown_raises_with_available_backends():
    register_tasklist_provider("mock", lambda options: MockTasklistProvider())

    with pytest.raises(ValueError) as exc_info:
        get_tasklist_provider("unknown", {})

    message = str(exc_info.value)
    assert "unknown" in message
    assert "mock" in message


def test_register_and_get_tasklist_provider_calls_factory():
    provider = MockTasklistProvider()
    factory = Mock(return_value=provider)
    register_tasklist_provider("mock", factory)

    actual = get_tasklist_provider("mock", {})

    assert actual is provider
    factory.assert_called_once_with({})


def test_list_tasklist_backends_reflects_registered_names():
    register_tasklist_provider("zeta", lambda options: MockTasklistProvider())
    register_tasklist_provider("alpha", lambda options: MockTasklistProvider())

    assert list_tasklist_backends() == ["alpha", "zeta"]


def test_provider_options_accepts_non_string_values():
    """ProviderOptions accepts structured values; factory receives them uncoerced."""
    received: list[dict] = []

    def capturing_factory(options):
        received.append(options)
        return MockOpportunityProvider()

    register_opportunity_provider("typed", capturing_factory)

    structured_options = {
        "path": "/some/path",
        "page_size": 100,
        "enabled": True,
        "tags": ["a", "b"],
        "meta": {"nested": "value"},
    }
    get_opportunity_provider("typed", structured_options)

    assert len(received) == 1
    opts = received[0]
    assert opts["page_size"] == 100
    assert opts["enabled"] is True
    assert opts["tags"] == ["a", "b"]
    assert opts["meta"] == {"nested": "value"}


class ClassOpportunityProvider(OpportunityProviderBase):
    @classmethod
    def from_config(cls, options):
        return cls()

    def list_opportunities(self) -> list[Opportunity]:
        return []

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        return None

    def write_opportunity(self, opportunity: Opportunity) -> None:
        return None

    def update_opportunity_status(self, opportunity_id: str, status: OpportunityStatus) -> None:
        return None


class ClassDesignProvider(DesignProviderBase):
    @classmethod
    def from_config(cls, options):
        return cls()

    def list_designs(self) -> list[Design]:
        return []

    def get_design(self, design_id: str) -> Design | None:
        return None

    def write_design(self, design: Design) -> None:
        return None

    def update_design_status(self, design_id: str, status: DesignStatus) -> None:
        return None


class ClassTasklistProvider(TasklistProviderBase):
    @classmethod
    def from_config(cls, options):
        return cls()

    def list_tasks(self) -> list[TasklistItem]:
        return []

    def get_task(self, task_id: str) -> TasklistItem | None:
        return None

    def append_tasks(self, tasks: list[TasklistItem]) -> None:
        return None

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        return None

    def get_snapshot(self) -> str:
        return ""

    def restore_snapshot(self, content: str) -> None:
        return None


def test_register_provider_class_helpers_register_and_construct():
    register_opportunity_provider_class("opp-class", ClassOpportunityProvider)
    register_design_provider_class("design-class", ClassDesignProvider)
    register_tasklist_provider_class("task-class", ClassTasklistProvider)

    assert isinstance(get_opportunity_provider("opp-class", {}), ClassOpportunityProvider)
    assert isinstance(get_design_provider("design-class", {}), ClassDesignProvider)
    assert isinstance(get_tasklist_provider("task-class", {}), ClassTasklistProvider)


def test_register_provider_class_helpers_reject_non_base_classes():
    with pytest.raises(TypeError):
        register_opportunity_provider_class("bad-opp", MockOpportunityProvider)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        register_design_provider_class("bad-design", MockDesignProvider)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        register_tasklist_provider_class("bad-task", MockTasklistProvider)  # type: ignore[arg-type]


def test_register_factory_rejects_non_callable():
    with pytest.raises(TypeError):
        register_opportunity_provider("bad-opp", "not-callable")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        register_design_provider("bad-design", "not-callable")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        register_tasklist_provider("bad-task", "not-callable")  # type: ignore[arg-type]
