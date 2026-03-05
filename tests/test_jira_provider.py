"""Tests for the Jira MCP-backed tasklist provider."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import millstone.artifact_providers.jira  # noqa: F401 — triggers registration
from millstone.artifact_providers.jira import _JIRA_STATUS_TO_TASK, JiraTasklistProvider
from millstone.artifact_providers.registry import list_tasklist_backends
from millstone.artifacts.models import TasklistItem, TaskStatus
from millstone.policy.effects import EffectRecord, EffectStatus

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


def _provider(**kwargs) -> JiraTasklistProvider:
    return JiraTasklistProvider(**kwargs)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_jira_registered_in_tasklist_backends():
    assert "jira" in list_tasklist_backends()


# ---------------------------------------------------------------------------
# from_config
# ---------------------------------------------------------------------------


def test_from_config_minimal():
    p = JiraTasklistProvider.from_config({"mcp_server": "jira"})
    assert p._project is None
    assert p._label is None
    assert p._status_filter is None
    assert p._assignee is None


def test_from_config_empty_statuses_list_means_no_filter():
    """Empty statuses list should not implicitly narrow to 'To Do'."""
    p = JiraTasklistProvider.from_config({"filter": {"statuses": []}})
    assert p._status_filter is None


def test_from_config_full_flat():
    p = JiraTasklistProvider.from_config(
        {
            "project": "ENG",
            "label": "millstone",
            "status": "In Progress",
            "assignee": "me",
        }
    )
    assert p._project == "ENG"
    assert p._label == "millstone"
    assert p._status_filter == "In Progress"
    assert p._assignee == "me"


def test_from_config_filter_dict():
    p = JiraTasklistProvider.from_config(
        {
            "filter": {
                "project": "OPS",
                "label": "backend",
                "status": "To Do",
                "assignee": "alice",
            }
        }
    )
    assert p._project == "OPS"
    assert p._label == "backend"
    assert p._status_filter == "To Do"
    assert p._assignee == "alice"


def test_from_config_labels_list():
    p = JiraTasklistProvider.from_config({"labels": ["first", "second"]})
    assert p._label == "first"


def test_from_config_filter_labels_list():
    p = JiraTasklistProvider.from_config({"filter": {"labels": ["alpha", "beta"]}})
    assert p._label == "alpha"


def test_from_config_normalized_statuses_list():
    """Orchestrator-normalized 'statuses' list should set status_filter."""
    p = JiraTasklistProvider.from_config({"filter": {"statuses": ["In Progress", "To Do"]}})
    assert p._status_filter == "In Progress"


def test_from_config_normalized_statuses_top_level():
    p = JiraTasklistProvider.from_config({"statuses": ["Done"]})
    assert p._status_filter == "Done"


def test_from_config_normalized_assignees_list():
    """Orchestrator-normalized 'assignees' list should set assignee."""
    p = JiraTasklistProvider.from_config({"filter": {"assignees": ["alice", "bob"]}})
    assert p._assignee == "alice"


def test_from_config_normalized_assignees_top_level():
    p = JiraTasklistProvider.from_config({"assignees": ["charlie"]})
    assert p._assignee == "charlie"


def test_from_config_plural_takes_precedence_over_singular():
    """Orchestrator-normalized plural forms win over singular shortcuts."""
    p = JiraTasklistProvider.from_config(
        {"status": "Blocked", "statuses": ["To Do"], "assignee": "me", "assignees": ["them"]}
    )
    assert p._status_filter == "To Do"
    assert p._assignee == "them"


def test_from_config_singular_used_as_fallback_when_no_plural():
    """Singular shortcut is still used when no plural form is present."""
    p = JiraTasklistProvider.from_config({"status": "Blocked", "assignee": "alice"})
    assert p._status_filter == "Blocked"
    assert p._assignee == "alice"


def test_jira_registered_via_outer_loop_bootstrap():
    """'jira' backend is discoverable after outer loop module is imported (no manual jira import)."""
    import importlib
    import sys

    import millstone.artifact_providers.registry as reg
    import millstone.loops.outer

    # Save state to restore after the test.
    removed = reg.TASKLIST_PROVIDERS.pop("jira", None)
    jira_module = sys.modules.pop("millstone.artifact_providers.jira", None)
    try:
        assert "jira" not in reg.list_tasklist_backends(), "precondition: jira must be unregistered"
        # Reloading outer re-executes its top-level import of the jira module,
        # triggering the registration side-effect.
        importlib.reload(millstone.loops.outer)
        assert "jira" in reg.list_tasklist_backends()
    finally:
        # Restore previous module/registry state so other tests are unaffected.
        if jira_module is not None:
            sys.modules["millstone.artifact_providers.jira"] = jira_module
        if removed is not None:
            reg.TASKLIST_PROVIDERS["jira"] = removed


# ---------------------------------------------------------------------------
# Guard: no callback
# ---------------------------------------------------------------------------


def test_list_tasks_without_callback_raises():
    p = _provider()
    with pytest.raises(RuntimeError, match="set_agent_callback"):
        p.list_tasks()


def test_update_task_status_without_callback_raises():
    p = _provider()
    with pytest.raises(RuntimeError, match="set_agent_callback"):
        p.update_task_status("ENG-1", TaskStatus.done)


def test_append_tasks_without_callback_raises():
    p = _provider()
    task = TasklistItem(task_id="ENG-1", title="Fix bug", status=TaskStatus.todo)
    with pytest.raises(RuntimeError, match="set_agent_callback"):
        p.append_tasks([task])


# ---------------------------------------------------------------------------
# list_tasks — Jira-specific prompt and status mapping
# ---------------------------------------------------------------------------


def test_list_tasks_uses_jira_mcp_in_prompt():
    p = _provider(project="ENG", label="millstone")
    response = json.dumps([{"id": "ENG-1", "title": "Fix it", "status": "todo", "description": ""}])
    cb = MagicMock(return_value=response)
    p.set_agent_callback(cb)

    p.list_tasks()

    prompt = cb.call_args[0][0]
    assert "jira" in prompt.lower()
    assert "ENG" in prompt
    assert "millstone" in prompt


def test_list_tasks_maps_jira_statuses():
    p = _provider()
    response = json.dumps(
        [
            {"id": "ENG-1", "title": "Todo item", "status": "to do", "description": ""},
            {"id": "ENG-2", "title": "WIP item", "status": "in progress", "description": ""},
            {"id": "ENG-3", "title": "Done item", "status": "done", "description": ""},
            {"id": "ENG-4", "title": "Closed item", "status": "closed", "description": ""},
            {"id": "ENG-5", "title": "Resolved item", "status": "resolved", "description": ""},
            {"id": "ENG-6", "title": "Open item", "status": "open", "description": ""},
            {"id": "ENG-7", "title": "Blocked item", "status": "blocked", "description": ""},
        ]
    )
    cb = MagicMock(return_value=response)
    p.set_agent_callback(cb)

    tasks = p.list_tasks()

    assert tasks[0].status == TaskStatus.todo
    assert tasks[1].status == TaskStatus.in_progress
    assert tasks[2].status == TaskStatus.done
    assert tasks[3].status == TaskStatus.done
    assert tasks[4].status == TaskStatus.done
    assert tasks[5].status == TaskStatus.todo
    assert tasks[6].status == TaskStatus.blocked


def test_list_tasks_maps_issue_key_to_task_id():
    p = _provider()
    response = json.dumps(
        [{"id": "ENG-42", "title": "Implement feature", "status": "todo", "description": "body"}]
    )
    cb = MagicMock(return_value=response)
    p.set_agent_callback(cb)

    tasks = p.list_tasks()

    assert tasks[0].task_id == "ENG-42"
    assert tasks[0].raw == "body"


def test_list_tasks_raises_on_invalid_json():
    p = _provider()
    cb = MagicMock(return_value="not json {{")
    p.set_agent_callback(cb)

    with pytest.raises(ValueError, match="invalid JSON"):
        p.list_tasks()


def test_list_tasks_null_status_defaults_to_todo():
    """Null or missing status from MCP response must not raise AttributeError."""
    p = _provider()
    items = [
        {"id": "ENG-1", "title": "Null status", "status": None, "description": ""},
        {"id": "ENG-2", "title": "Missing status", "description": ""},
    ]
    cb = MagicMock(return_value=json.dumps(items))
    p.set_agent_callback(cb)

    results = p.list_tasks()
    assert len(results) == 2
    assert all(r.status == TaskStatus.todo for r in results)


def test_list_tasks_uses_cache():
    p = _provider()
    response = json.dumps([{"id": "ENG-1", "title": "Task", "status": "todo", "description": ""}])
    cb = MagicMock(return_value=response)
    p.set_agent_callback(cb)

    p.list_tasks()
    p.list_tasks()

    assert cb.call_count == 1


def test_invalidate_cache_forces_refetch():
    p = _provider()
    response = json.dumps([{"id": "ENG-1", "title": "Task", "status": "todo", "description": ""}])
    cb = MagicMock(return_value=response)
    p.set_agent_callback(cb)

    p.list_tasks()
    p.invalidate_cache()
    p.list_tasks()

    assert cb.call_count == 2


# ---------------------------------------------------------------------------
# update_task_status — Jira transition terminology
# ---------------------------------------------------------------------------


def test_update_task_status_uses_jira_transition_names():
    p = _provider()
    cb = MagicMock(return_value="ok")
    p.set_agent_callback(cb)

    p.update_task_status("ENG-1", TaskStatus.done)

    prompt = cb.call_args[0][0]
    assert "jira" in prompt.lower()
    assert "ENG-1" in prompt
    assert "Done" in prompt


def test_update_task_status_in_progress_uses_jira_name():
    p = _provider()
    cb = MagicMock(return_value="ok")
    p.set_agent_callback(cb)

    p.update_task_status("ENG-5", TaskStatus.in_progress)

    prompt = cb.call_args[0][0]
    assert "In Progress" in prompt


def test_update_task_status_invalidates_cache():
    p = _provider()
    list_response = json.dumps([{"id": "ENG-1", "title": "T", "status": "todo", "description": ""}])
    cb = MagicMock(side_effect=[list_response, "ok", list_response])
    p.set_agent_callback(cb)

    p.list_tasks()
    p.update_task_status("ENG-1", TaskStatus.done)
    p.list_tasks()

    # list_tasks called twice (second after cache invalidation), plus one update call
    assert cb.call_count == 3


# ---------------------------------------------------------------------------
# update_task_status — effect gate
# ---------------------------------------------------------------------------


def test_update_task_status_blocked_by_effect_gate_raises():
    p = _provider(effect_applier=lambda _: _denied_record())
    cb = MagicMock(return_value="ok")
    p.set_agent_callback(cb)

    with pytest.raises(RuntimeError, match="MCP write blocked"):
        p.update_task_status("ENG-1", TaskStatus.done)


def test_update_task_status_allowed_by_effect_gate_calls_agent():
    p = _provider(effect_applier=lambda intent: _applied_record(intent))
    cb = MagicMock(return_value="ok")
    p.set_agent_callback(cb)

    p.update_task_status("ENG-1", TaskStatus.done)

    cb.assert_called_once()


# ---------------------------------------------------------------------------
# append_tasks
# ---------------------------------------------------------------------------


def test_append_tasks_includes_jira_mcp_in_prompt():
    p = _provider(project="ENG", label="millstone")
    cb = MagicMock(return_value="ok")
    p.set_agent_callback(cb)
    task = TasklistItem(task_id="ENG-10", title="New feature", status=TaskStatus.todo)

    p.append_tasks([task])

    prompt = cb.call_args[0][0]
    assert "jira" in prompt.lower()
    assert "New feature" in prompt


def test_append_tasks_includes_filter_clauses():
    p = _provider(project="OPS", label="infra", assignee="me")
    cb = MagicMock(return_value="ok")
    p.set_agent_callback(cb)
    task = TasklistItem(task_id="OPS-1", title="Scale DB", status=TaskStatus.todo)

    p.append_tasks([task])

    prompt = cb.call_args[0][0]
    assert "OPS" in prompt
    assert "infra" in prompt
    assert "me" in prompt


def test_append_tasks_uses_jira_status_name():
    p = _provider()
    cb = MagicMock(return_value="ok")
    p.set_agent_callback(cb)
    task = TasklistItem(task_id="ENG-11", title="Task", status=TaskStatus.in_progress)

    p.append_tasks([task])

    prompt = cb.call_args[0][0]
    assert "In Progress" in prompt


# ---------------------------------------------------------------------------
# get_prompt_placeholders
# ---------------------------------------------------------------------------


def test_get_prompt_placeholders_contains_jira_keywords():
    p = _provider(project="ENG", label="millstone")
    placeholders = p.get_prompt_placeholders()

    assert "TASKLIST_READ_INSTRUCTIONS" in placeholders
    assert "TASKLIST_COMPLETE_INSTRUCTIONS" in placeholders
    assert "TASKLIST_APPEND_INSTRUCTIONS" in placeholders
    assert "jira" in placeholders["TASKLIST_READ_INSTRUCTIONS"].lower()
    assert "Done" in placeholders["TASKLIST_COMPLETE_INSTRUCTIONS"]


def test_get_prompt_placeholders_includes_filter_in_read():
    p = _provider(project="ENG", label="millstone", assignee="alice")
    placeholders = p.get_prompt_placeholders()

    read_instr = placeholders["TASKLIST_READ_INSTRUCTIONS"]
    assert "ENG" in read_instr
    assert "millstone" in read_instr
    assert "alice" in read_instr


def test_get_prompt_placeholders_read_instructions_accept_in_progress():
    """Read instructions must allow 'In Progress' selection, consistent with has_remaining_tasks."""
    p = _provider()
    placeholders = p.get_prompt_placeholders()
    read_instr = placeholders["TASKLIST_READ_INSTRUCTIONS"]
    # Both statuses must be mentioned so an in_progress-only backlog isn't stranded.
    assert "In Progress" in read_instr
    assert "To Do" in read_instr


def test_get_prompt_placeholders_no_filter_read_instructions_include_both_pending_statuses():
    """No-filter config: read instructions must include todo and in_progress selection semantics."""
    p = _provider(status_filter=None)
    read_instr = p.get_prompt_placeholders()["TASKLIST_READ_INSTRUCTIONS"]
    assert "To Do" in read_instr
    assert "In Progress" in read_instr


# ---------------------------------------------------------------------------
# _filter_clauses
# ---------------------------------------------------------------------------


def test_filter_clauses_empty_when_no_filters():
    p = _provider(status_filter=None)
    assert p._filter_clauses() == ""


def test_filter_clauses_includes_all_set_filters():
    p = _provider(project="ENG", label="ml", status_filter="To Do", assignee="bot")
    clause = p._filter_clauses()
    assert "ENG" in clause
    assert "ml" in clause
    assert "To Do" in clause
    assert "bot" in clause


# ---------------------------------------------------------------------------
# Status mapping completeness
# ---------------------------------------------------------------------------


def test_jira_status_map_covers_common_values():
    expected = {"to do", "in progress", "done", "closed", "resolved", "blocked", "open", "backlog"}
    assert expected.issubset(_JIRA_STATUS_TO_TASK.keys())
