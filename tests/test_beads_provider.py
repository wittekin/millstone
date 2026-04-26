"""Tests for the beads-backed artifact providers (subprocess-based)."""

from __future__ import annotations

import json
from typing import Any

import pytest

import millstone.artifact_providers.beads  # noqa: F401 — triggers registration
from millstone.artifact_providers import (
    DependencyLinker,
    OpportunityProvider,
    ReadyAwareTasklistProvider,
    TasklistProvider,
)
from millstone.artifact_providers.beads import (
    BeadsCLIError,
    BeadsOpportunityProvider,
    BeadsTasklistProvider,
    _BeadsCLI,
    _coerce_items,
)
from millstone.artifact_providers.registry import (
    list_opportunity_backends,
    list_tasklist_backends,
)
from millstone.artifacts.models import (
    DependencyKind,
    Opportunity,
    OpportunityStatus,
    TasklistItem,
    TaskStatus,
)


class FakeBeadsCLI:
    """Captures invocations and returns scripted JSON responses by command head."""

    def __init__(self, responses: dict[tuple[str, ...], Any] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    def _resolve(self, args: list[str]) -> Any:
        # Match longest prefix in responses table.
        for length in range(len(args), 0, -1):
            key = tuple(args[:length])
            if key in self.responses:
                value = self.responses[key]
                return value() if callable(value) else value
        return ""

    def run(self, args: list[str], *, json_output: bool = False) -> str:
        self.calls.append(list(args))
        result = self._resolve(args)
        if isinstance(result, BeadsCLIError):
            raise result
        if json_output:
            return json.dumps(result) if not isinstance(result, str) else result
        return result if isinstance(result, str) else json.dumps(result)

    def run_json(self, args: list[str]) -> Any:
        self.calls.append(list(args))
        result = self._resolve(args)
        if isinstance(result, BeadsCLIError):
            raise result
        return result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_beads_registered():
    assert "beads" in list_tasklist_backends()
    assert "beads" in list_opportunity_backends()


# ---------------------------------------------------------------------------
# _BeadsCLI discovery
# ---------------------------------------------------------------------------


def test_cli_discovery_errors_when_bd_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(BeadsCLIError, match="not found"):
        _BeadsCLI()


def test_cli_uses_explicit_path(tmp_path):
    fake_bd = tmp_path / "bd"
    fake_bd.write_text("#!/bin/sh\necho ok\n")
    fake_bd.chmod(0o755)
    cli = _BeadsCLI(bd_path=str(fake_bd))
    assert cli.bd_path == str(fake_bd)


# ---------------------------------------------------------------------------
# _coerce_items
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,expected_count",
    [
        (None, 0),
        ([], 0),
        ([{"id": "bd-1"}, {"id": "bd-2"}], 2),
        ({"issues": [{"id": "bd-1"}]}, 1),
        ({"items": [{"id": "bd-1"}, {"id": "bd-2"}]}, 2),
        ({"id": "bd-9", "title": "x"}, 1),
        ({"unrelated": "data"}, 0),
    ],
)
def test_coerce_items_normalizes_shapes(payload, expected_count):
    assert len(_coerce_items(payload)) == expected_count


# ---------------------------------------------------------------------------
# Tasklist provider — read paths
# ---------------------------------------------------------------------------


def test_list_tasks_calls_bd_list_with_label_and_status_filter():
    fake = FakeBeadsCLI(
        responses={
            ("list",): [
                {"id": "bd-a1", "title": "First", "status": "open"},
                {"id": "bd-a2", "title": "Second", "status": "in_progress"},
            ]
        }
    )
    provider = BeadsTasklistProvider(label="millstone-task", cli=fake)
    items = provider.list_tasks()

    assert [t.task_id for t in items] == ["bd-a1", "bd-a2"]
    assert items[0].status == TaskStatus.todo
    assert items[1].status == TaskStatus.in_progress

    args = fake.calls[0]
    assert args[0] == "list"
    assert "--label" in args and "millstone-task" in args
    assert "--status" in args


def test_list_ready_tasks_uses_bd_ready():
    fake = FakeBeadsCLI(
        responses={("ready",): [{"id": "bd-r1", "title": "Ready", "status": "open"}]}
    )
    provider = BeadsTasklistProvider(cli=fake)
    items = provider.list_ready_tasks()

    assert len(items) == 1
    assert items[0].task_id == "bd-r1"
    assert fake.calls[0][0] == "ready"


def test_get_task_returns_none_when_bd_show_fails():
    fake = FakeBeadsCLI(responses={("show", "bd-missing"): BeadsCLIError("not found")})
    provider = BeadsTasklistProvider(cli=fake)
    assert provider.get_task("bd-missing") is None


def test_get_task_unwraps_single_issue():
    fake = FakeBeadsCLI(
        responses={("show", "bd-1"): {"id": "bd-1", "title": "T", "status": "open"}}
    )
    provider = BeadsTasklistProvider(cli=fake)
    item = provider.get_task("bd-1")
    assert item is not None and item.task_id == "bd-1"


# ---------------------------------------------------------------------------
# Tasklist provider — write paths
# ---------------------------------------------------------------------------


def test_append_tasks_creates_and_assigns_bd_id():
    fake = FakeBeadsCLI(responses={("create",): {"id": "bd-new1", "title": "T"}})
    provider = BeadsTasklistProvider(cli=fake)
    task = TasklistItem(task_id="placeholder", title="Implement X", status=TaskStatus.todo)

    provider.append_tasks([task])

    assert task.task_id == "bd-new1"  # mutated in place
    create_call = next(c for c in fake.calls if c[0] == "create")
    assert "Implement X" in create_call
    assert "-t" in create_call and "task" in create_call


def test_append_tasks_links_to_opportunity_when_ref_set():
    fake = FakeBeadsCLI(responses={("create",): {"id": "bd-new1"}})
    provider = BeadsTasklistProvider(cli=fake)
    task = TasklistItem(
        task_id="placeholder",
        title="From opp",
        status=TaskStatus.todo,
        opportunity_ref="bd-opp9",
    )

    provider.append_tasks([task])

    dep_calls = [c for c in fake.calls if c[:2] == ["dep", "add"]]
    assert dep_calls == [
        ["dep", "add", "bd-new1", "bd-opp9", "--type", "discovered-from"],
    ]


def test_append_tasks_logs_link_failure_but_succeeds(caplog):
    fake = FakeBeadsCLI(
        responses={
            ("create",): {"id": "bd-new1"},
            ("dep", "add", "bd-new1", "bd-opp9", "--type", "discovered-from"): BeadsCLIError(
                "no such issue"
            ),
        }
    )
    provider = BeadsTasklistProvider(cli=fake)
    task = TasklistItem(
        task_id="ph",
        title="t",
        status=TaskStatus.todo,
        opportunity_ref="bd-opp9",
    )

    with caplog.at_level("WARNING"):
        provider.append_tasks([task])

    assert task.task_id == "bd-new1"
    assert any("failed to link beads task" in r.message for r in caplog.records)


def test_update_task_status_done_uses_close():
    fake = FakeBeadsCLI()
    provider = BeadsTasklistProvider(cli=fake)
    provider.update_task_status("bd-1", TaskStatus.done)
    assert fake.calls == [["close", "bd-1"]]


def test_update_task_status_in_progress_uses_update():
    fake = FakeBeadsCLI()
    provider = BeadsTasklistProvider(cli=fake)
    provider.update_task_status("bd-1", TaskStatus.in_progress)
    assert fake.calls == [["update", "bd-1", "--status", "in_progress"]]


# ---------------------------------------------------------------------------
# Snapshot / restore — scoped rollback
# ---------------------------------------------------------------------------


def test_snapshot_then_restore_deletes_only_added_tasks():
    state = {
        "current": [
            {"id": "bd-pre1", "title": "Pre 1", "status": "open"},
        ]
    }

    def list_response():
        return list(state["current"])

    fake = FakeBeadsCLI(responses={("list",): list_response})
    provider = BeadsTasklistProvider(cli=fake)

    snap = provider.get_snapshot()
    assert json.loads(snap)["task_ids"] == ["bd-pre1"]

    state["current"].extend(
        [
            {"id": "bd-new1", "title": "New 1", "status": "open"},
            {"id": "bd-new2", "title": "New 2", "status": "open"},
        ]
    )

    provider.restore_snapshot(snap)
    delete_calls = sorted(c for c in fake.calls if c[:1] == ["delete"])
    assert delete_calls == [["delete", "bd-new1"], ["delete", "bd-new2"]]


def test_restore_snapshot_with_invalid_payload_raises():
    provider = BeadsTasklistProvider(cli=FakeBeadsCLI())
    with pytest.raises(ValueError, match="invalid beads snapshot"):
        provider.restore_snapshot("not json")


# ---------------------------------------------------------------------------
# DependencyLinker capability
# ---------------------------------------------------------------------------


def test_link_invokes_bd_dep_add_with_kind():
    fake = FakeBeadsCLI()
    provider = BeadsTasklistProvider(cli=fake)
    provider.link("bd-a", "bd-b", DependencyKind.blocks)
    assert fake.calls == [["dep", "add", "bd-a", "bd-b", "--type", "blocks"]]


def test_provider_satisfies_capability_protocols():
    provider = BeadsTasklistProvider(cli=FakeBeadsCLI())
    assert isinstance(provider, TasklistProvider)
    assert isinstance(provider, ReadyAwareTasklistProvider)
    assert isinstance(provider, DependencyLinker)


# ---------------------------------------------------------------------------
# Opportunity provider
# ---------------------------------------------------------------------------


def test_opp_list_filters_by_default_label():
    fake = FakeBeadsCLI(
        responses={
            ("list",): [
                {"id": "bd-o1", "title": "Idea", "status": "open", "description": "do thing"}
            ]
        }
    )
    provider = BeadsOpportunityProvider(cli=fake)
    opps = provider.list_opportunities()

    assert len(opps) == 1 and opps[0].opportunity_id == "bd-o1"
    assert opps[0].status == OpportunityStatus.identified
    assert "millstone-opportunity" in fake.calls[0]


def test_opp_write_assigns_bd_id():
    fake = FakeBeadsCLI(responses={("create",): {"id": "bd-onew"}})
    provider = BeadsOpportunityProvider(cli=fake)
    opp = Opportunity(
        opportunity_id="placeholder-slug",
        title="An idea",
        status=OpportunityStatus.identified,
        description="Do this thing.",
    )
    provider.write_opportunity(opp)
    assert opp.opportunity_id == "bd-onew"


def test_opp_update_status_rejected_uses_close():
    fake = FakeBeadsCLI()
    provider = BeadsOpportunityProvider(cli=fake)
    provider.update_opportunity_status("bd-o1", OpportunityStatus.rejected)
    assert fake.calls == [["close", "bd-o1"]]


def test_opp_satisfies_capability_protocols():
    provider = BeadsOpportunityProvider(cli=FakeBeadsCLI())
    assert isinstance(provider, OpportunityProvider)
    assert isinstance(provider, DependencyLinker)
