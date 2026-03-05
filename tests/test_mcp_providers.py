"""Tests for MCP-backed artifact providers."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest

import millstone.artifact_providers.file  # noqa: F401 — registers "file" backend
import millstone.artifact_providers.mcp  # noqa: F401 — triggers registration side-effect
from millstone.artifact_providers.mcp import (
    MCPDesignProvider,
    MCPOpportunityProvider,
    MCPTasklistProvider,
    _strip_json_fences,
)
from millstone.artifact_providers.registry import (
    list_design_backends,
    list_opportunity_backends,
    list_tasklist_backends,
)
from millstone.artifacts.models import (
    Design,
    DesignStatus,
    Opportunity,
    OpportunityStatus,
    TasklistItem,
    TaskStatus,
)
from millstone.artifacts.tasklist import TasklistManager
from millstone.policy.effects import EffectIntent, EffectRecord, EffectStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _applied_record(intent=None):
    return EffectRecord(
        intent=intent or MagicMock(),
        status=EffectStatus.applied,
        timestamp="2026-01-01T00:00:00Z",
    )


def _denied_record(intent=None):
    return EffectRecord(
        intent=intent or MagicMock(),
        status=EffectStatus.denied,
        timestamp="2026-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


def test_mcp_registered_in_tasklist_backends():
    assert "mcp" in list_tasklist_backends()


def test_mcp_registered_in_design_backends():
    assert "mcp" in list_design_backends()


def test_mcp_registered_in_opportunity_backends():
    assert "mcp" in list_opportunity_backends()


# ---------------------------------------------------------------------------
# MCPTasklistProvider — write-without-callback guard
# ---------------------------------------------------------------------------


def test_append_tasks_without_callback_raises_runtime_error():
    provider = MCPTasklistProvider("linear")
    task = TasklistItem(task_id="t-1", title="Task One", status=TaskStatus.todo)
    with pytest.raises(RuntimeError, match="set_agent_callback"):
        provider.append_tasks([task])


def test_update_task_status_without_callback_raises_runtime_error():
    provider = MCPTasklistProvider("linear")
    with pytest.raises(RuntimeError, match="set_agent_callback"):
        provider.update_task_status("t-1", TaskStatus.done)


def test_list_tasks_without_callback_raises_runtime_error():
    provider = MCPTasklistProvider("linear")
    with pytest.raises(RuntimeError, match="set_agent_callback"):
        provider.list_tasks()


# ---------------------------------------------------------------------------
# MCPTasklistProvider — list_tasks via agent callback
# ---------------------------------------------------------------------------


def test_list_tasks_uses_agent_callback_with_json_response():
    provider = MCPTasklistProvider("linear")
    response = json.dumps(
        [
            {"id": "t-1", "title": "Task One", "status": "todo", "description": ""},
            {"id": "t-2", "title": "Task Two", "status": "done", "description": "body"},
            {"id": "t-3", "title": "Task Three", "status": "in_progress", "description": ""},
            {"id": "t-4", "title": "Task Four", "status": "blocked", "description": ""},
        ]
    )
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    result = provider.list_tasks()

    assert len(result) == 4
    assert result[0].task_id == "t-1"
    assert result[0].status == TaskStatus.todo
    assert result[1].task_id == "t-2"
    assert result[1].status == TaskStatus.done
    assert result[2].status == TaskStatus.in_progress
    assert result[3].status == TaskStatus.blocked
    mock_cb.assert_called_once()
    # Prompt should mention the MCP server and all states
    prompt = mock_cb.call_args[0][0]
    assert "linear" in prompt
    assert "ALL tasks" in prompt


def test_list_tasks_with_malformed_json_raises_value_error():
    provider = MCPTasklistProvider("linear")
    mock_cb = MagicMock(return_value="not valid json {{{")
    provider.set_agent_callback(mock_cb)

    with pytest.raises(ValueError, match="invalid JSON"):
        provider.list_tasks()


def test_list_tasks_uses_cache_on_second_call():
    provider = MCPTasklistProvider("linear")
    response = json.dumps([{"id": "t-1", "title": "Task", "status": "todo", "description": ""}])
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    provider.list_tasks()
    provider.list_tasks()

    assert mock_cb.call_count == 1


def test_list_tasks_maps_raw_description_to_raw_field():
    provider = MCPTasklistProvider("linear")
    description = "- Risk: low\n  - Tests: pytest\n  - Criteria: done"
    response = json.dumps(
        [{"id": "t-1", "title": "My Task", "status": "todo", "description": description}]
    )
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    result = provider.list_tasks()

    assert result[0].raw == description


# ---------------------------------------------------------------------------
# MCPTasklistProvider — get_snapshot() full-block markdown
# ---------------------------------------------------------------------------


def test_get_snapshot_returns_full_block_markdown_with_metadata():
    """get_snapshot() reconstructs full-block markdown including metadata lines."""
    provider = MCPTasklistProvider("linear")
    description = (
        "Implement auth flow\n"
        "  - Tests: pytest tests/test_auth.py\n"
        "  - Risk: medium\n"
        "  - Criteria: all tests pass\n"
        "  - Context: see RFC-42"
    )
    response = json.dumps(
        [
            {
                "id": "t-1",
                "title": "Add authentication",
                "status": "todo",
                "description": description,
            }
        ]
    )
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    snapshot = provider.get_snapshot()

    assert "- [ ]" in snapshot
    assert "Add authentication" in snapshot
    assert "Tests:" in snapshot or "pytest tests/test_auth.py" in snapshot


def test_get_snapshot_uses_done_checkbox_for_done_status():
    provider = MCPTasklistProvider("linear")
    response = json.dumps(
        [{"id": "t-1", "title": "Done Task", "status": "done", "description": "body"}]
    )
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    snapshot = provider.get_snapshot()

    assert "- [x]" in snapshot


def test_get_snapshot_uses_unchecked_for_non_done_statuses():
    provider = MCPTasklistProvider("linear")
    response = json.dumps(
        [
            {"id": "t-1", "title": "Blocked Task", "status": "blocked", "description": "body"},
            {"id": "t-2", "title": "In Progress", "status": "in_progress", "description": "body"},
        ]
    )
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    snapshot = provider.get_snapshot()

    # blocked and in_progress should be unchecked
    assert snapshot.count("- [ ]") == 2


def test_get_snapshot_reconstructs_from_fields_when_raw_empty():
    """When raw is empty, snapshot uses individual fields for metadata."""
    provider = MCPTasklistProvider("linear")
    response = json.dumps(
        [
            {
                "id": "t-1",
                "title": "My Task",
                "status": "todo",
                "description": "",
            }
        ]
    )
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    snapshot = provider.get_snapshot()

    assert "- [ ]" in snapshot
    assert "My Task" in snapshot


def test_get_snapshot_updates_checkbox_when_raw_is_full_block():
    """When raw already has a checkbox line, status is updated from t.status."""
    provider = MCPTasklistProvider("linear")
    # Raw contains done checkbox but actual status is todo
    raw = "- [x] **Old Task**: some description\n  - Risk: low"
    response = json.dumps(
        [{"id": "t-1", "title": "Old Task", "status": "todo", "description": raw}]
    )
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    snapshot = provider.get_snapshot()

    # Should be unchecked because status=todo
    assert "- [ ]" in snapshot
    assert "- [x]" not in snapshot


def test_get_snapshot_stores_snapshot_task_ids():
    provider = MCPTasklistProvider("linear")
    response = json.dumps(
        [
            {"id": "t-1", "title": "T1", "status": "todo", "description": ""},
            {"id": "t-2", "title": "T2", "status": "done", "description": ""},
        ]
    )
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    provider.get_snapshot()

    assert provider._snapshot_task_ids == {"t-1", "t-2"}


def test_get_snapshot_full_metadata_fields_reconstructed():
    """Full reconstruction from individual fields includes all metadata lines."""
    # TasklistItem with fields set explicitly (empty raw)
    provider = MCPTasklistProvider("linear")
    # We'll use a task that has no raw but has metadata via description body
    description = ""  # empty raw
    response = json.dumps(
        [
            {
                "id": "t-1",
                "title": "My Feature",
                "status": "todo",
                "description": description,
            }
        ]
    )
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    snapshot = provider.get_snapshot()
    # At minimum should contain checkbox and title
    assert "My Feature" in snapshot
    assert "- [ ]" in snapshot


def test_get_snapshot_output_passes_validate_generated_tasks_without_metadata_violations(tmp_path):
    """get_snapshot() output for a task with all required fields produces no
    metadata-missing violations when fed to _validate_generated_tasks(old='', new=snapshot).

    This ensures the snapshot markdown format is compatible with the atomizer
    validation logic used in _run_plan_impl.
    """
    provider = MCPTasklistProvider("linear")
    # Full-block raw format: get_snapshot() will update the checkbox and pass
    # it through verbatim, preserving the indented metadata lines that
    # TasklistManager._parse_task_metadata expects.
    description = (
        "- [ ] **Add new feature**\n"
        "  - Tests: pytest tests/test_feature.py\n"
        "  - Risk: low\n"
        "  - Criteria: all tests pass\n"
        "  - Context: see design doc"
    )
    response = json.dumps(
        [
            {
                "id": "t-1",
                "title": "Add new feature",
                "status": "todo",
                "description": description,
            }
        ]
    )
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    snapshot = provider.get_snapshot()

    # Parse snapshot using the real TasklistManager metadata parser so we
    # exercise the same code path as _validate_generated_tasks.
    mgr = TasklistManager(repo_dir=tmp_path)
    # _extract_new_tasks returns task text after "- [ ] ", so strip the prefix
    # the same way the real code does before calling _parse_task_metadata.
    import re as _re

    new_task_texts = _re.findall(r"^- \[ \] (.+(?:\n(?:  .+))*)", snapshot, _re.MULTILINE)
    assert new_task_texts, "get_snapshot() produced no parseable task blocks"

    parsed = mgr._parse_task_metadata(new_task_texts[0])

    assert parsed["tests"] is not None, "tests field missing from snapshot"
    assert parsed["criteria"] is not None, "criteria field missing from snapshot"


def test_reset_snapshot_baseline_allows_new_baseline_on_next_get_snapshot():
    """reset_snapshot_baseline() clears the stored baseline so the next
    get_snapshot() captures a fresh one, preventing stale rollback targets."""
    provider = MCPTasklistProvider("linear")
    first_response = json.dumps(
        [{"id": "t-1", "title": "Original", "status": "todo", "description": ""}]
    )
    second_response = json.dumps(
        [
            {"id": "t-1", "title": "Original", "status": "todo", "description": ""},
            {"id": "t-2", "title": "Added", "status": "todo", "description": ""},
        ]
    )
    mock_cb = MagicMock(side_effect=[first_response, second_response])
    provider.set_agent_callback(mock_cb)

    # First planning session: baseline = {"t-1"}
    provider.get_snapshot()
    assert provider._snapshot_task_ids == {"t-1"}

    # Simulate subsequent get_snapshot() calls during validation — baseline must not change.
    provider.invalidate_cache()
    provider.get_snapshot()
    assert provider._snapshot_task_ids == {"t-1"}, (
        "repeated get_snapshot() must not overwrite the baseline"
    )

    # Reset for a new planning session.
    provider.reset_snapshot_baseline()
    assert provider._snapshot_task_ids is None


# ---------------------------------------------------------------------------
# MCPTasklistProvider — restore_snapshot() scoped rollback
# ---------------------------------------------------------------------------


def test_restore_snapshot_deletes_extra_tasks_via_callback():
    """restore_snapshot invokes callback to delete tasks added after snapshot."""
    provider = MCPTasklistProvider("linear")

    # First call: snapshot state (t-1 exists)
    snapshot_response = json.dumps(
        [{"id": "t-1", "title": "Original", "status": "todo", "description": ""}]
    )
    # Second call (after restore): current state has t-1 and t-2 (new)
    current_response = json.dumps(
        [
            {"id": "t-1", "title": "Original", "status": "todo", "description": ""},
            {"id": "t-2", "title": "New Task", "status": "todo", "description": ""},
        ]
    )
    # Third call: after first invalidation (fresh fetch for rollback)
    mock_cb = MagicMock(side_effect=[snapshot_response, current_response, "ok"])
    provider.set_agent_callback(mock_cb)

    provider.get_snapshot()  # sets _snapshot_task_ids = {"t-1"}
    provider.invalidate_cache()  # simulate time passing

    provider.restore_snapshot("")

    # The third call should be the delete prompt
    delete_call = mock_cb.call_args_list[2]
    delete_prompt = delete_call[0][0]
    assert "delete" in delete_prompt.lower() or "archive" in delete_prompt.lower()
    assert "t-2" in delete_prompt
    assert "New Task" in delete_prompt


def test_restore_snapshot_does_not_modify_preexisting_tasks():
    """restore_snapshot only deletes new tasks, does NOT restore pre-existing task edits."""
    provider = MCPTasklistProvider("linear")

    # Snapshot: t-1 and t-2 exist
    snapshot_response = json.dumps(
        [
            {"id": "t-1", "title": "T1", "status": "todo", "description": ""},
            {"id": "t-2", "title": "T2", "status": "todo", "description": ""},
        ]
    )
    # After planning: t-1 and t-2 still present (no new tasks), but t-2 status changed
    current_response = json.dumps(
        [
            {"id": "t-1", "title": "T1", "status": "todo", "description": ""},
            {"id": "t-2", "title": "T2", "status": "done", "description": ""},
        ]
    )
    mock_cb = MagicMock(side_effect=[snapshot_response, current_response])
    provider.set_agent_callback(mock_cb)

    provider.get_snapshot()  # sets snapshot_task_ids = {"t-1", "t-2"}
    provider.invalidate_cache()

    provider.restore_snapshot("")

    # No delete/archive call was made (both IDs in snapshot)
    # Only 2 calls: snapshot list + restore list
    assert mock_cb.call_count == 2


def test_restore_snapshot_without_prior_get_snapshot_logs_warning_and_returns(caplog):
    """restore_snapshot without get_snapshot logs warning and exits cleanly."""
    provider = MCPTasklistProvider("linear")
    mock_cb = MagicMock(return_value="[]")
    provider.set_agent_callback(mock_cb)

    with caplog.at_level(logging.WARNING, logger="millstone.artifact_providers.mcp"):
        provider.restore_snapshot("")

    assert "no rollback performed" in caplog.text.lower() or "restore_snapshot" in caplog.text
    mock_cb.assert_not_called()


def test_restore_snapshot_no_extra_tasks_no_callback():
    """restore_snapshot with no new tasks does not call the agent."""
    provider = MCPTasklistProvider("linear")

    response = json.dumps([{"id": "t-1", "title": "T1", "status": "todo", "description": ""}])
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    provider.get_snapshot()  # _snapshot_task_ids = {"t-1"}
    provider.invalidate_cache()

    provider.restore_snapshot("")

    # Only 2 calls: get_snapshot list + restore list; no delete call
    assert mock_cb.call_count == 2


# ---------------------------------------------------------------------------
# MCPTasklistProvider — cache invalidation
# ---------------------------------------------------------------------------


def test_task_cache_populated_on_first_list_tasks():
    provider = MCPTasklistProvider("linear")
    response = json.dumps([{"id": "t-1", "title": "T", "status": "todo", "description": ""}])
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    assert provider._task_cache is None
    provider.list_tasks()
    assert provider._task_cache is not None


def test_task_cache_invalidated_after_append_tasks():
    provider = MCPTasklistProvider("linear")
    list_response = json.dumps([{"id": "t-1", "title": "T", "status": "todo", "description": ""}])
    mock_cb = MagicMock(return_value=list_response)
    provider.set_agent_callback(mock_cb)

    provider.list_tasks()
    assert provider._task_cache is not None

    task = TasklistItem(task_id="t-2", title="New", status=TaskStatus.todo)
    provider.append_tasks([task])

    assert provider._task_cache is None


def test_invalidate_cache_clears_cache():
    provider = MCPTasklistProvider("linear")
    response = json.dumps([{"id": "t-1", "title": "T", "status": "todo", "description": ""}])
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    provider.list_tasks()
    assert provider._task_cache is not None

    provider.invalidate_cache()

    assert provider._task_cache is None


def test_task_cache_invalidated_after_update_task_status():
    provider = MCPTasklistProvider("linear")
    list_response = json.dumps([{"id": "t-1", "title": "T", "status": "todo", "description": ""}])
    mock_cb = MagicMock(return_value=list_response)
    provider.set_agent_callback(mock_cb)

    provider.list_tasks()
    assert provider._task_cache is not None

    provider.update_task_status("t-1", TaskStatus.done)

    assert provider._task_cache is None


# ---------------------------------------------------------------------------
# MCPTasklistProvider — writes call agent callback
# ---------------------------------------------------------------------------


def test_append_tasks_calls_callback_with_task_title_and_server():
    provider = MCPTasklistProvider("linear")
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    task = TasklistItem(task_id="t-1", title="Implement login", status=TaskStatus.todo)
    provider.append_tasks([task])

    mock_cb.assert_called_once()
    prompt = mock_cb.call_args[0][0]
    assert "linear" in prompt
    assert "Implement login" in prompt


def test_append_tasks_calls_callback_once_per_task():
    provider = MCPTasklistProvider("linear")
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    tasks = [
        TasklistItem(task_id="t-1", title="Task A", status=TaskStatus.todo),
        TasklistItem(task_id="t-2", title="Task B", status=TaskStatus.todo),
    ]
    provider.append_tasks(tasks)
    assert mock_cb.call_count == 2


def test_update_task_status_calls_callback():
    provider = MCPTasklistProvider("linear")
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    provider.update_task_status("t-1", TaskStatus.done)

    mock_cb.assert_called_once()
    prompt = mock_cb.call_args[0][0]
    assert "t-1" in prompt
    assert "done" in prompt
    assert "linear" in prompt


# ---------------------------------------------------------------------------
# MCPTasklistProvider — effect gate integration
# ---------------------------------------------------------------------------


def test_update_task_status_routes_through_effect_gate():
    mock_gate = MagicMock(return_value=_applied_record())
    provider = MCPTasklistProvider("linear", effect_applier=mock_gate)
    provider.set_agent_callback(MagicMock(return_value="ok"))

    provider.update_task_status("t-1", TaskStatus.done)

    mock_gate.assert_called_once()
    intent: EffectIntent = mock_gate.call_args[0][0]
    assert intent.idempotency_key == "t-1"
    assert intent.metadata["mcp_server"] == "linear"


def test_effect_gate_denial_raises_before_callback():
    mock_gate = MagicMock(return_value=_denied_record())
    mock_cb = MagicMock(return_value="ok")
    provider = MCPTasklistProvider("linear", effect_applier=mock_gate)
    provider.set_agent_callback(mock_cb)

    with pytest.raises(RuntimeError, match="MCP write blocked"):
        provider.update_task_status("t-1", TaskStatus.done)

    mock_cb.assert_not_called()


# ---------------------------------------------------------------------------
# MCPTasklistProvider — from_config
# ---------------------------------------------------------------------------


def test_tasklist_from_config_requires_mcp_server():
    with pytest.raises(ValueError, match="mcp_server"):
        MCPTasklistProvider.from_config({})


def test_tasklist_from_config_constructs_without_read_backend():
    provider = MCPTasklistProvider.from_config({"mcp_server": "linear"})
    assert isinstance(provider, MCPTasklistProvider)
    assert provider._mcp_server == "linear"


def test_tasklist_from_config_emits_deprecation_warning_for_read_backend():
    with pytest.warns(DeprecationWarning, match="read_backend"):
        MCPTasklistProvider.from_config({"mcp_server": "linear", "read_backend": "file"})


def test_tasklist_from_config_reads_label_option():
    provider = MCPTasklistProvider.from_config({"mcp_server": "linear", "label": "eng"})
    assert provider._labels == ["eng"]


def test_tasklist_from_config_reads_labels_list_option():
    provider = MCPTasklistProvider.from_config(
        {"mcp_server": "linear", "labels": ["eng", "backend"]}
    )
    assert provider._labels == ["eng", "backend"]


def test_tasklist_from_config_reads_projects_option():
    provider = MCPTasklistProvider.from_config({"mcp_server": "linear", "projects": ["my-project"]})
    assert provider._projects == ["my-project"]


def test_tasklist_from_config_backward_compatible_no_label_project():
    provider = MCPTasklistProvider.from_config({"mcp_server": "linear"})
    assert provider._labels == []
    assert provider._projects == []


def test_tasklist_from_config_reads_filter_dict_labels_and_projects():
    provider = MCPTasklistProvider.from_config(
        {"mcp_server": "linear", "filter": {"labels": ["sprint-5"], "projects": ["proj-1"]}}
    )
    assert provider._labels == ["sprint-5"]
    assert provider._projects == ["proj-1"]


def test_tasklist_from_config_top_level_labels_takes_precedence_over_filter():
    provider = MCPTasklistProvider.from_config(
        {
            "mcp_server": "linear",
            "labels": ["override"],
            "filter": {"labels": ["sprint-5"]},
        }
    )
    assert provider._labels == ["override"]


def test_tasklist_from_config_explicit_empty_top_level_labels_not_fallen_through():
    # An explicit empty list at top level must win over filter dict entries.
    provider = MCPTasklistProvider.from_config(
        {
            "mcp_server": "linear",
            "labels": [],
            "filter": {"labels": ["sprint-5"]},
        }
    )
    assert provider._labels == []


def test_tasklist_from_config_neither_key_produces_empty_lists():
    provider = MCPTasklistProvider.from_config({"mcp_server": "linear"})
    assert provider._labels == []
    assert provider._projects == []


def test_tasklist_from_config_filter_labels_appear_in_prompt_placeholders():
    provider = MCPTasklistProvider.from_config(
        {"mcp_server": "linear", "filter": {"labels": ["sprint-5"], "projects": ["proj-1"]}}
    )
    placeholders = provider.get_prompt_placeholders()
    assert "sprint-5" in placeholders["TASKLIST_READ_INSTRUCTIONS"]
    assert "proj-1" in placeholders["TASKLIST_READ_INSTRUCTIONS"]


def test_tasklist_from_config_explicit_empty_labels_suppresses_filter_label_in_prompt():
    # Explicit empty list at top-level wins over filter dict; filter label must not
    # appear in the rendered prompt placeholder.
    provider = MCPTasklistProvider.from_config(
        {
            "mcp_server": "github",
            "labels": [],
            "filter": {"labels": ["sprint-9"], "assignees": [], "statuses": []},
        }
    )
    read_instructions = provider.get_prompt_placeholders()["TASKLIST_READ_INSTRUCTIONS"]
    assert "sprint-9" not in read_instructions, (
        f"Expected 'sprint-9' absent from TASKLIST_READ_INSTRUCTIONS "
        f"(explicit empty labels suppresses filter label clause).\n"
        f"Got: {read_instructions!r}"
    )


# ---------------------------------------------------------------------------
# MCPTasklistProvider — get_prompt_placeholders
# ---------------------------------------------------------------------------


def test_tasklist_get_prompt_placeholders_returns_all_five_keys():
    provider = MCPTasklistProvider("linear")
    placeholders = provider.get_prompt_placeholders()
    assert set(placeholders) >= {
        "TASKLIST_READ_INSTRUCTIONS",
        "TASKLIST_COMPLETE_INSTRUCTIONS",
        "TASKLIST_REWRITE_INSTRUCTIONS",
        "TASKLIST_APPEND_INSTRUCTIONS",
        "TASKLIST_UPDATE_INSTRUCTIONS",
    }


def test_tasklist_get_prompt_placeholders_without_label_no_label_text():
    provider = MCPTasklistProvider("linear")
    placeholders = provider.get_prompt_placeholders()
    assert "label" not in placeholders["TASKLIST_READ_INSTRUCTIONS"]
    assert "label" not in placeholders["TASKLIST_APPEND_INSTRUCTIONS"]


def test_tasklist_get_prompt_placeholders_with_label():
    provider = MCPTasklistProvider("linear", labels=["my-label"])
    placeholders = provider.get_prompt_placeholders()
    assert "my-label" in placeholders["TASKLIST_READ_INSTRUCTIONS"]
    assert "my-label" in placeholders["TASKLIST_APPEND_INSTRUCTIONS"]
    # Complete/Rewrite/Update don't include label clause
    assert "my-label" not in placeholders["TASKLIST_COMPLETE_INSTRUCTIONS"]


def test_tasklist_get_prompt_placeholders_with_project():
    provider = MCPTasklistProvider("linear", projects=["my-project"])
    placeholders = provider.get_prompt_placeholders()
    assert "my-project" in placeholders["TASKLIST_READ_INSTRUCTIONS"]
    assert "my-project" in placeholders["TASKLIST_APPEND_INSTRUCTIONS"]


def test_tasklist_get_prompt_placeholders_values_mention_mcp_server():
    provider = MCPTasklistProvider("jira")
    placeholders = provider.get_prompt_placeholders()
    for value in placeholders.values():
        assert "jira" in value


# ---------------------------------------------------------------------------
# MCPTasklistProvider — append_tasks inline prompt includes label/project
# ---------------------------------------------------------------------------


def test_append_tasks_prompt_includes_label_when_configured():
    provider = MCPTasklistProvider("linear", labels=["backend"])
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    task = TasklistItem(task_id="t-1", title="Add feature", status=TaskStatus.todo)
    provider.append_tasks([task])

    prompt = mock_cb.call_args[0][0]
    assert "backend" in prompt


def test_append_tasks_prompt_includes_project_when_configured():
    provider = MCPTasklistProvider("linear", projects=["api-project"])
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    task = TasklistItem(task_id="t-1", title="Add feature", status=TaskStatus.todo)
    provider.append_tasks([task])

    prompt = mock_cb.call_args[0][0]
    assert "api-project" in prompt


def test_append_tasks_prompt_no_label_when_not_configured():
    provider = MCPTasklistProvider("linear")
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    task = TasklistItem(task_id="t-1", title="Add feature", status=TaskStatus.todo)
    provider.append_tasks([task])

    prompt = mock_cb.call_args[0][0]
    assert "label" not in prompt


# ---------------------------------------------------------------------------
# MCPTasklistProvider — update_task_status inline prompt includes label/project
# ---------------------------------------------------------------------------


def test_update_task_status_prompt_includes_label_when_configured():
    provider = MCPTasklistProvider("linear", labels=["backend"])
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    provider.update_task_status("t-1", TaskStatus.done)

    prompt = mock_cb.call_args[0][0]
    assert "backend" in prompt


def test_update_task_status_prompt_includes_project_when_configured():
    provider = MCPTasklistProvider("linear", projects=["api-project"])
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    provider.update_task_status("t-1", TaskStatus.done)

    prompt = mock_cb.call_args[0][0]
    assert "api-project" in prompt


def test_update_task_status_prompt_no_scope_when_not_configured():
    provider = MCPTasklistProvider("linear")
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    provider.update_task_status("t-1", TaskStatus.done)

    prompt = mock_cb.call_args[0][0]
    assert "label" not in prompt
    assert "project" not in prompt


# ---------------------------------------------------------------------------
# MCPDesignProvider — write-without-callback guard
# ---------------------------------------------------------------------------


def test_write_design_without_callback_raises_runtime_error():
    provider = MCPDesignProvider("notion")
    design = Design(
        design_id="my-design",
        title="My Design",
        status=DesignStatus.draft,
        body="content",
        opportunity_ref="opp-1",
    )
    with pytest.raises(RuntimeError, match="set_agent_callback"):
        provider.write_design(design)


def test_update_design_status_without_callback_raises_runtime_error():
    provider = MCPDesignProvider("notion")
    with pytest.raises(RuntimeError, match="set_agent_callback"):
        provider.update_design_status("my-design", DesignStatus.approved)


# ---------------------------------------------------------------------------
# MCPDesignProvider — reads via agent callback
# ---------------------------------------------------------------------------


def test_list_designs_uses_agent_callback():
    provider = MCPDesignProvider("notion")
    response = json.dumps(
        [
            {
                "id": "d-1",
                "title": "Auth Design",
                "status": "draft",
                "opportunity_ref": "opp-1",
                "body": "body text",
            }
        ]
    )
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    result = provider.list_designs()

    assert len(result) == 1
    assert result[0].design_id == "d-1"
    assert result[0].title == "Auth Design"
    mock_cb.assert_called_once()
    assert "notion" in mock_cb.call_args[0][0]


def test_get_design_uses_agent_callback():
    provider = MCPDesignProvider("notion")
    response = json.dumps(
        {
            "id": "d-1",
            "title": "Auth Design",
            "status": "approved",
            "opportunity_ref": "opp-1",
            "body": "body text",
        }
    )
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    result = provider.get_design("d-1")

    assert result is not None
    assert result.design_id == "d-1"
    assert result.status == DesignStatus.approved
    mock_cb.assert_called_once()
    assert "d-1" in mock_cb.call_args[0][0]


def test_get_design_returns_none_on_invalid_json():
    provider = MCPDesignProvider("notion")
    mock_cb = MagicMock(return_value="not json")
    provider.set_agent_callback(mock_cb)

    result = provider.get_design("d-1")

    assert result is None


# ---------------------------------------------------------------------------
# MCPDesignProvider — writes call agent callback
# ---------------------------------------------------------------------------


def test_write_design_calls_callback_with_design_id_and_server():
    provider = MCPDesignProvider("notion")
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    design = Design(
        design_id="auth-design",
        title="Auth Design",
        status=DesignStatus.draft,
        body="Design body",
        opportunity_ref="opp-1",
    )
    provider.write_design(design)

    mock_cb.assert_called_once()
    prompt = mock_cb.call_args[0][0]
    assert "notion" in prompt
    assert "auth-design" in prompt


def test_update_design_status_calls_callback():
    provider = MCPDesignProvider("notion")
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    provider.update_design_status("auth-design", DesignStatus.approved)

    mock_cb.assert_called_once()
    prompt = mock_cb.call_args[0][0]
    assert "auth-design" in prompt
    assert "approved" in prompt


# ---------------------------------------------------------------------------
# MCPDesignProvider — effect gate integration
# ---------------------------------------------------------------------------


def test_write_design_routes_through_effect_gate():
    mock_gate = MagicMock(return_value=_applied_record())
    provider = MCPDesignProvider("notion", effect_applier=mock_gate)
    provider.set_agent_callback(MagicMock(return_value="ok"))

    design = Design(
        design_id="d-1", title="D1", status=DesignStatus.draft, body="body", opportunity_ref="o-1"
    )
    provider.write_design(design)

    mock_gate.assert_called_once()
    intent: EffectIntent = mock_gate.call_args[0][0]
    assert intent.idempotency_key == "d-1"
    assert intent.metadata["mcp_server"] == "notion"


def test_design_effect_gate_denial_raises_before_callback():
    mock_gate = MagicMock(return_value=_denied_record())
    mock_cb = MagicMock(return_value="ok")
    provider = MCPDesignProvider("notion", effect_applier=mock_gate)
    provider.set_agent_callback(mock_cb)

    design = Design(
        design_id="d-1", title="D1", status=DesignStatus.draft, body="body", opportunity_ref="o-1"
    )
    with pytest.raises(RuntimeError, match="MCP write blocked"):
        provider.write_design(design)

    mock_cb.assert_not_called()


# ---------------------------------------------------------------------------
# MCPDesignProvider — get_prompt_placeholders
# ---------------------------------------------------------------------------


def test_design_get_prompt_placeholders_returns_design_write_instructions():
    provider = MCPDesignProvider("notion")
    placeholders = provider.get_prompt_placeholders()
    assert "DESIGN_WRITE_INSTRUCTIONS" in placeholders
    assert "notion" in placeholders["DESIGN_WRITE_INSTRUCTIONS"]


def test_design_get_prompt_placeholders_without_project_no_project_text():
    provider = MCPDesignProvider("notion")
    placeholders = provider.get_prompt_placeholders()
    assert "project" not in placeholders["DESIGN_WRITE_INSTRUCTIONS"]


def test_design_get_prompt_placeholders_with_project():
    provider = MCPDesignProvider("notion", projects=["design-proj"])
    placeholders = provider.get_prompt_placeholders()
    assert "design-proj" in placeholders["DESIGN_WRITE_INSTRUCTIONS"]


# ---------------------------------------------------------------------------
# MCPDesignProvider — write_design inline prompt includes project
# ---------------------------------------------------------------------------


def test_write_design_prompt_includes_project_when_configured():
    provider = MCPDesignProvider("notion", projects=["my-proj"])
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    design = Design(
        design_id="d-1", title="D1", status=DesignStatus.draft, body="body", opportunity_ref="o-1"
    )
    provider.write_design(design)

    prompt = mock_cb.call_args[0][0]
    assert "my-proj" in prompt


def test_write_design_prompt_no_project_when_not_configured():
    provider = MCPDesignProvider("notion")
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    design = Design(
        design_id="d-1", title="D1", status=DesignStatus.draft, body="body", opportunity_ref="o-1"
    )
    provider.write_design(design)

    prompt = mock_cb.call_args[0][0]
    assert "project" not in prompt


# ---------------------------------------------------------------------------
# MCPDesignProvider — from_config
# ---------------------------------------------------------------------------


def test_design_from_config_requires_mcp_server():
    with pytest.raises(ValueError, match="mcp_server"):
        MCPDesignProvider.from_config({})


def test_design_from_config_constructs_without_read_backend():
    provider = MCPDesignProvider.from_config({"mcp_server": "notion"})
    assert isinstance(provider, MCPDesignProvider)
    assert provider._mcp_server == "notion"


def test_design_from_config_emits_deprecation_warning_for_read_backend(tmp_path):
    with pytest.warns(DeprecationWarning, match="read_backend"):
        MCPDesignProvider.from_config({"mcp_server": "notion", "read_backend": "file"})


def test_design_from_config_reads_project_option():
    provider = MCPDesignProvider.from_config({"mcp_server": "notion", "project": "design-space"})
    assert provider._projects == ["design-space"]


def test_design_from_config_backward_compatible_no_project():
    provider = MCPDesignProvider.from_config({"mcp_server": "notion"})
    assert provider._projects == []


# ---------------------------------------------------------------------------
# MCPOpportunityProvider — CRUD via agent callback
# ---------------------------------------------------------------------------


def test_opportunity_list_uses_agent_callback():
    provider = MCPOpportunityProvider("jira")
    response = json.dumps(
        [
            {
                "id": "opp-1",
                "title": "Speed up CI",
                "status": "identified",
                "description": "CI is slow",
            }
        ]
    )
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    result = provider.list_opportunities()

    assert len(result) == 1
    assert result[0].opportunity_id == "opp-1"
    assert result[0].status == OpportunityStatus.identified
    mock_cb.assert_called_once()
    assert "jira" in mock_cb.call_args[0][0]


def test_opportunity_get_uses_agent_callback():
    provider = MCPOpportunityProvider("jira")
    response = json.dumps(
        {
            "id": "opp-1",
            "title": "Speed up CI",
            "status": "adopted",
            "description": "CI is slow",
        }
    )
    mock_cb = MagicMock(return_value=response)
    provider.set_agent_callback(mock_cb)

    result = provider.get_opportunity("opp-1")

    assert result is not None
    assert result.opportunity_id == "opp-1"
    assert result.status == OpportunityStatus.adopted


def test_opportunity_write_calls_callback_with_details():
    provider = MCPOpportunityProvider("jira")
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    opp = Opportunity(
        opportunity_id="opp-1",
        title="Speed up CI",
        status=OpportunityStatus.identified,
        description="CI is too slow",
    )
    provider.write_opportunity(opp)

    mock_cb.assert_called_once()
    prompt = mock_cb.call_args[0][0]
    assert "jira" in prompt
    assert "opp-1" in prompt
    assert "Speed up CI" in prompt


def test_opportunity_update_status_calls_callback():
    provider = MCPOpportunityProvider("jira")
    mock_cb = MagicMock(return_value="ok")
    provider.set_agent_callback(mock_cb)

    provider.update_opportunity_status("opp-1", OpportunityStatus.adopted)

    mock_cb.assert_called_once()
    prompt = mock_cb.call_args[0][0]
    assert "opp-1" in prompt
    assert "adopted" in prompt


def test_opportunity_write_calls_apply_write_effect():
    """write_opportunity() must route through _apply_write_effect (EffectIntent parity)."""
    mock_gate = MagicMock(return_value=_applied_record())
    provider = MCPOpportunityProvider("jira", effect_applier=mock_gate)
    provider.set_agent_callback(MagicMock(return_value="ok"))

    opp = Opportunity(
        opportunity_id="opp-1",
        title="Speed up CI",
        status=OpportunityStatus.identified,
        description="CI is slow",
    )
    provider.write_opportunity(opp)

    mock_gate.assert_called_once()
    intent: EffectIntent = mock_gate.call_args[0][0]
    assert intent.idempotency_key == "opp-1"
    assert intent.metadata["mcp_server"] == "jira"
    assert intent.metadata["artifact_type"] == "opportunity"


def test_opportunity_write_without_callback_raises():
    provider = MCPOpportunityProvider("jira")
    opp = Opportunity(
        opportunity_id="opp-1",
        title="T",
        status=OpportunityStatus.identified,
        description="D",
    )
    with pytest.raises(RuntimeError, match="set_agent_callback"):
        provider.write_opportunity(opp)


def test_opportunity_effect_gate_denial_raises():
    mock_gate = MagicMock(return_value=_denied_record())
    mock_cb = MagicMock(return_value="ok")
    provider = MCPOpportunityProvider("jira", effect_applier=mock_gate)
    provider.set_agent_callback(mock_cb)

    opp = Opportunity(
        opportunity_id="opp-1",
        title="T",
        status=OpportunityStatus.identified,
        description="D",
    )
    with pytest.raises(RuntimeError, match="MCP write blocked"):
        provider.write_opportunity(opp)
    mock_cb.assert_not_called()


def test_opportunity_get_prompt_placeholders():
    provider = MCPOpportunityProvider("jira")
    placeholders = provider.get_prompt_placeholders()
    assert "OPPORTUNITY_WRITE_INSTRUCTIONS" in placeholders
    assert "OPPORTUNITY_READ_INSTRUCTIONS" in placeholders
    assert "jira" in placeholders["OPPORTUNITY_WRITE_INSTRUCTIONS"]


def test_opportunity_from_config_requires_mcp_server():
    with pytest.raises(ValueError, match="mcp_server"):
        MCPOpportunityProvider.from_config({})


def test_opportunity_from_config_constructs():
    provider = MCPOpportunityProvider.from_config({"mcp_server": "jira"})
    assert isinstance(provider, MCPOpportunityProvider)
    assert provider._mcp_server == "jira"


# ---------------------------------------------------------------------------
# OuterLoopManager._inject_agent_callbacks integration
# ---------------------------------------------------------------------------


def test_inject_agent_callbacks_sets_callback_on_mcp_providers(tmp_path):
    """_inject_agent_callbacks calls set_agent_callback on providers that support it."""
    from millstone.loops.outer import OuterLoopManager

    work_dir = tmp_path / ".millstone"
    work_dir.mkdir()

    mcp_tasklist = MCPTasklistProvider("linear")
    mcp_design = MCPDesignProvider("notion")

    manager = OuterLoopManager(
        work_dir=work_dir,
        repo_dir=tmp_path,
        tasklist="docs/tasklist.md",
        task_constraints={
            "max_loc": 200,
            "require_tests": False,
            "require_criteria": False,
            "require_risk": False,
            "require_context": False,
            "max_split_attempts": 0,
        },
        tasklist_provider=mcp_tasklist,
        design_provider=mcp_design,
    )

    # Before injection, writes should fail (no callback)
    with pytest.raises(RuntimeError, match="set_agent_callback"):
        mcp_tasklist.append_tasks(
            [TasklistItem(task_id="t-1", title="Test Task", status=TaskStatus.todo)]
        )

    list_response = json.dumps([])
    mock_cb = MagicMock(return_value=list_response)
    manager._inject_agent_callbacks(mock_cb)

    # After injection, writes should use mock_cb
    mcp_tasklist.append_tasks(
        [TasklistItem(task_id="t-1", title="Test Task", status=TaskStatus.todo)]
    )
    mock_cb.assert_called_once()


def test_inject_agent_callbacks_ignores_providers_without_set_agent_callback(tmp_path):
    """File providers don't have set_agent_callback — inject should be a no-op for them."""
    from millstone.loops.outer import OuterLoopManager

    work_dir = tmp_path / ".millstone"
    work_dir.mkdir()
    tasklist_path = tmp_path / "docs" / "tasklist.md"
    tasklist_path.parent.mkdir()
    tasklist_path.write_text("# Tasklist\n")

    manager = OuterLoopManager(
        work_dir=work_dir,
        repo_dir=tmp_path,
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

    # Should not raise even though file providers have no set_agent_callback
    manager._inject_agent_callbacks(MagicMock(return_value="response"))


def test_restore_snapshot_removes_only_interleaved_task():
    """
    Two providers share a simulated backing store.

    Steps:
      1. Provider 1 calls get_snapshot() — captures t-1 and t-2 as the baseline.
      2. Provider 2 (sharing the same store) concurrently appends t-3.
      3. Provider 1 calls restore_snapshot() — should delete only t-3.

    Assertions:
      (a) The delete callback is invoked exactly once.
      (b) The delete prompt references t-3 (the interleaved task).
      (c) The delete prompt does NOT reference t-1 or t-2 (original tasks).
    """
    # Shared in-memory backing store: list of task dicts.
    store: list[dict] = [
        {"id": "t-1", "title": "Original Task A", "status": "todo", "description": ""},
        {"id": "t-2", "title": "Original Task B", "status": "todo", "description": ""},
    ]
    delete_calls: list[str] = []

    def shared_callback(prompt: str) -> str:
        # Simulate delete: remove matching tasks from the shared store.
        if "delete" in prompt.lower() or "archive" in prompt.lower():
            delete_calls.append(prompt)
            # Apply side-effect: evict any task whose ID appears in the prompt.
            store[:] = [t for t in store if t["id"] not in prompt]
            return "deleted"
        # Default: return current store state as JSON.
        return json.dumps(store)

    # Provider 1: the "worker" that owns the snapshot baseline.
    provider1 = MCPTasklistProvider(mcp_server="github")
    provider1.set_agent_callback(shared_callback)

    # Provider 2: simulates a concurrent worker that appends a task.
    provider2 = MCPTasklistProvider(mcp_server="github")
    provider2.set_agent_callback(shared_callback)

    # Step 1: Provider 1 captures snapshot — baseline = {t-1, t-2}.
    provider1.get_snapshot()
    assert provider1._snapshot_task_ids == {"t-1", "t-2"}, (
        f"Expected snapshot baseline {{t-1, t-2}}, got {provider1._snapshot_task_ids!r}"
    )

    # Step 2: Provider 2 appends t-3 concurrently (mutates shared store).
    store.append({"id": "t-3", "title": "Interleaved Task C", "status": "todo", "description": ""})
    # Invalidate provider 2's cache to reflect the new state.
    provider2.invalidate_cache()
    # Confirm provider 2 now sees all three tasks.
    all_tasks = provider2.list_tasks()
    assert len(all_tasks) == 3, f"Expected 3 tasks after concurrent append, got {len(all_tasks)}"

    # Step 3: Provider 1 restores — should delete only t-3.
    provider1.restore_snapshot("")

    # (a) Exactly one delete call was made.
    assert len(delete_calls) == 1, (
        f"Expected exactly 1 delete call, got {len(delete_calls)}: {delete_calls!r}"
    )
    delete_prompt = delete_calls[0]

    # (b) The delete prompt references the interleaved task.
    assert "t-3" in delete_prompt, f"Expected 't-3' in delete prompt.\nPrompt: {delete_prompt!r}"
    assert "Interleaved Task C" in delete_prompt, (
        f"Expected 'Interleaved Task C' in delete prompt.\nPrompt: {delete_prompt!r}"
    )

    # (c) The delete prompt does NOT reference the original tasks.
    assert "t-1" not in delete_prompt, (
        f"'t-1' should not appear in delete prompt (original task).\nPrompt: {delete_prompt!r}"
    )
    assert "t-2" not in delete_prompt, (
        f"'t-2' should not appear in delete prompt (original task).\nPrompt: {delete_prompt!r}"
    )

    # (d) Post-restore state: interleaved task removed, originals intact.
    provider1.invalidate_cache()
    final_tasks = provider1.list_tasks()
    final_ids = {t.task_id for t in final_tasks}
    assert final_ids == {"t-1", "t-2"}, (
        f"Expected final store {{t-1, t-2}} after restore, got {final_ids!r}"
    )
    assert not any(t.task_id == "t-3" for t in final_tasks), (
        "t-3 (interleaved task) must be absent from the store after restore_snapshot()"
    )


# ---------------------------------------------------------------------------
# _strip_json_fences — unit tests
# ---------------------------------------------------------------------------


class TestStripJsonFences:
    def test_plain_json_unchanged(self):
        text = '[{"id": "t1"}]'
        assert _strip_json_fences(text) == text

    def test_lowercase_json_fence_stripped(self):
        text = '```json\n[{"id": "t1"}]\n```'
        assert _strip_json_fences(text) == '[{"id": "t1"}]'

    def test_bare_fence_stripped(self):
        text = '```\n[{"id": "t1"}]\n```'
        assert _strip_json_fences(text) == '[{"id": "t1"}]'

    def test_surrounding_whitespace_stripped_before_match(self):
        text = '  \n```json\n[{"id": "t1"}]\n```\n  '
        assert _strip_json_fences(text) == '[{"id": "t1"}]'

    def test_uppercase_json_tag_not_stripped(self):
        # Regex uses (?:json)? (literal lowercase) — uppercase tag is not matched.
        text = '```JSON\n[{"id": "t1"}]\n```'
        # Falls through: _strip_json_fences returns the original stripped text.
        result = _strip_json_fences(text)
        assert result == text.strip()

    def test_multiline_content_preserved(self):
        inner = '[\n  {"id": "t1"},\n  {"id": "t2"}\n]'
        text = f"```json\n{inner}\n```"
        assert _strip_json_fences(text) == inner


# ---------------------------------------------------------------------------
# Fenced JSON responses — integration tests for MCP read paths
# ---------------------------------------------------------------------------


class TestMCPFencedJsonResponses:
    """Verify that fenced ```json ... ``` agent responses are correctly parsed
    by all six MCP read methods: list_tasks, get_task, list_designs, get_design,
    list_opportunities, get_opportunity."""

    # --- MCPTasklistProvider ---

    def test_list_tasks_fenced_response(self):
        provider = MCPTasklistProvider("linear")
        payload = [{"id": "t1", "title": "A", "status": "todo", "description": ""}]
        fenced = f"```json\n{json.dumps(payload)}\n```"
        provider.set_agent_callback(lambda _prompt: fenced)
        tasks = provider.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].task_id == "t1"
        assert tasks[0].status == TaskStatus.todo

    def test_list_tasks_invalid_json_raises_value_error(self):
        provider = MCPTasklistProvider("linear")
        provider.set_agent_callback(lambda _prompt: "```json\nnot-json\n```")
        with pytest.raises(ValueError, match="invalid JSON"):
            provider.list_tasks()

    def test_get_task_fenced_response(self):
        provider = MCPTasklistProvider("linear")
        payload = {
            "id": "t1",
            "title": "A",
            "status": "in_progress",
            "context": "ctx",
            "criteria": "crit",
        }
        fenced = f"```json\n{json.dumps(payload)}\n```"
        provider.set_agent_callback(lambda _prompt: fenced)
        task = provider.get_task("t1")
        assert task is not None
        assert task.task_id == "t1"
        assert task.status == TaskStatus.in_progress

    def test_get_task_invalid_json_returns_none(self):
        provider = MCPTasklistProvider("linear")
        provider.set_agent_callback(lambda _prompt: "```json\nnot-json\n```")
        assert provider.get_task("t1") is None

    # --- MCPDesignProvider ---

    def test_list_designs_fenced_response(self):
        provider = MCPDesignProvider("notion")
        payload = [
            {
                "id": "d1",
                "title": "Design A",
                "status": "draft",
                "opportunity_ref": None,
                "body": "body text",
            }
        ]
        fenced = f"```json\n{json.dumps(payload)}\n```"
        provider.set_agent_callback(lambda _prompt: fenced)
        designs = provider.list_designs()
        assert len(designs) == 1
        assert designs[0].design_id == "d1"
        assert designs[0].status == DesignStatus.draft

    def test_list_designs_invalid_json_returns_empty(self):
        provider = MCPDesignProvider("notion")
        provider.set_agent_callback(lambda _prompt: "```json\nnot-json\n```")
        assert provider.list_designs() == []

    def test_get_design_fenced_response(self):
        provider = MCPDesignProvider("notion")
        payload = {
            "id": "d1",
            "title": "Design A",
            "status": "reviewed",
            "body": "body",
            "opportunity_ref": "o1",
        }
        fenced = f"```json\n{json.dumps(payload)}\n```"
        provider.set_agent_callback(lambda _prompt: fenced)
        design = provider.get_design("d1")
        assert design is not None
        assert design.design_id == "d1"
        assert design.status == DesignStatus.reviewed

    def test_get_design_invalid_json_returns_none(self):
        provider = MCPDesignProvider("notion")
        provider.set_agent_callback(lambda _prompt: "```json\nnot-json\n```")
        assert provider.get_design("d1") is None

    # --- MCPOpportunityProvider ---

    def test_list_opportunities_fenced_response(self):
        provider = MCPOpportunityProvider("github")
        payload = [
            {"id": "o1", "title": "Opportunity A", "status": "identified", "description": "desc"}
        ]
        fenced = f"```json\n{json.dumps(payload)}\n```"
        provider.set_agent_callback(lambda _prompt: fenced)
        opps = provider.list_opportunities()
        assert len(opps) == 1
        assert opps[0].opportunity_id == "o1"
        assert opps[0].status == OpportunityStatus.identified

    def test_list_opportunities_invalid_json_returns_empty(self):
        provider = MCPOpportunityProvider("github")
        provider.set_agent_callback(lambda _prompt: "```json\nnot-json\n```")
        assert provider.list_opportunities() == []

    def test_get_opportunity_fenced_response(self):
        provider = MCPOpportunityProvider("github")
        payload = {"id": "o1", "title": "Opportunity A", "status": "adopted", "description": "desc"}
        fenced = f"```json\n{json.dumps(payload)}\n```"
        provider.set_agent_callback(lambda _prompt: fenced)
        opp = provider.get_opportunity("o1")
        assert opp is not None
        assert opp.opportunity_id == "o1"
        assert opp.status == OpportunityStatus.adopted

    def test_get_opportunity_invalid_json_returns_none(self):
        provider = MCPOpportunityProvider("github")
        provider.set_agent_callback(lambda _prompt: "```json\nnot-json\n```")
        assert provider.get_opportunity("o1") is None
