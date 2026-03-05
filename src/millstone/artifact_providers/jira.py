"""Jira MCP-backed tasklist provider.

Implements a Jira provider following the same pattern as the Linear MCP provider.
All operations are delegated to a CLI agent via MCP tools exposed by the Jira MCP server.

Configuration example in .millstone/config.toml::

    [millstone]
    tasklist_provider = "mcp"
    mcp_server = "jira"

    [tasklist_filter]
    project = "ENG"
    label = "millstone"
    status = "To Do"
    assignee = "me"

Auth is handled by the MCP server via environment variables:
JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from millstone.artifact_providers.mcp import MCPTasklistProvider, _strip_json_fences
from millstone.artifact_providers.registry import register_tasklist_provider_class
from millstone.artifacts.models import TasklistItem, TaskStatus
from millstone.policy.effects import EffectRecord

logger = logging.getLogger(__name__)

# Map from Jira status strings (lowercased) to TaskStatus enum values.
_JIRA_STATUS_TO_TASK: dict[str, TaskStatus] = {
    "to do": TaskStatus.todo,
    "todo": TaskStatus.todo,
    "open": TaskStatus.todo,
    "backlog": TaskStatus.todo,
    "in progress": TaskStatus.in_progress,
    "in_progress": TaskStatus.in_progress,
    "done": TaskStatus.done,
    "closed": TaskStatus.done,
    "resolved": TaskStatus.done,
    "blocked": TaskStatus.blocked,
}

# Map from TaskStatus to canonical Jira transition names.
_TASK_TO_JIRA_STATUS: dict[TaskStatus, str] = {
    TaskStatus.todo: "To Do",
    TaskStatus.in_progress: "In Progress",
    TaskStatus.done: "Done",
    TaskStatus.blocked: "Blocked",
}


class JiraTasklistProvider(MCPTasklistProvider):
    """Tasklist provider for Jira issues via MCP, using Jira-specific issue terminology.

    Subclasses ``MCPTasklistProvider`` and overrides list/update operations to use
    Jira MCP tool syntax (issue keys, project/status/assignee filters, status transitions).
    """

    def __init__(
        self,
        *,
        project: str | None = None,
        label: str | None = None,
        status_filter: str | None = None,
        assignee: str | None = None,
        effect_applier: Callable[[Any], EffectRecord] | None = None,
    ) -> None:
        super().__init__("jira", effect_applier=effect_applier)
        self._project = project
        self._label = label
        self._status_filter = status_filter
        self._assignee = assignee

    # ------------------------------------------------------------------
    # Filter helpers
    # ------------------------------------------------------------------

    def _filter_clauses(self) -> str:
        """Build a human-readable filter description for prompt injection."""
        parts = []
        if self._project:
            parts.append(f"project '{self._project}'")
        if self._label:
            parts.append(f"label '{self._label}'")
        if self._status_filter:
            parts.append(f"status '{self._status_filter}'")
        if self._assignee:
            parts.append(f"assignee '{self._assignee}'")
        if not parts:
            return ""
        return " with " + " and ".join(parts)

    def _jira_status(self, status: TaskStatus) -> str:
        return _TASK_TO_JIRA_STATUS.get(status, status.value)

    # ------------------------------------------------------------------
    # Read operations — Jira-specific prompts
    # ------------------------------------------------------------------

    def list_tasks(self) -> list[TasklistItem]:
        """Fetch Jira issues via agent callback using Jira-specific prompt format."""
        if self._task_cache is not None:
            return self._task_cache
        cb = self._require_callback()
        filter_clauses = self._filter_clauses()
        prompt = (
            f"Use the jira MCP to list ALL issues in all states"
            f"{filter_clauses}. "
            f"Output ONLY a JSON array, no other text. "
            f'Each item: {{"id": "<issue_key e.g. ENG-123>", "title": "<summary>", '
            f'"status": "todo|in_progress|done|blocked", '
            f'"description": "full issue body/description text"}}'
        )
        response = cb(prompt)
        try:
            items = json.loads(_strip_json_fences(response))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"JiraTasklistProvider.list_tasks: agent returned invalid JSON: {exc}\n"
                f"Response: {response!r}"
            ) from exc
        results = []
        for item in items:
            raw_status = str(item.get("status") or "todo").lower().strip()
            status = _JIRA_STATUS_TO_TASK.get(raw_status, TaskStatus.todo)
            results.append(
                TasklistItem(
                    task_id=item["id"],
                    title=item["title"],
                    status=status,
                    raw=item.get("description") or "",
                )
            )
        self._task_cache = results
        return results

    # ------------------------------------------------------------------
    # Write operations — Jira status transition terminology
    # ------------------------------------------------------------------

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        """Transition a Jira issue to the given status via agent callback."""
        cb = self._require_callback()
        jira_status = self._jira_status(status)
        self._apply_write_effect(
            operation="update_status",
            artifact_id=task_id,
            description=(
                f"Transition Jira issue '{task_id}' to status '{jira_status}' via jira MCP tools."
            ),
        )
        prompt = (
            f"Use the jira MCP tools to transition issue '{task_id}' to status '{jira_status}'."
        )
        cb(prompt)
        self.invalidate_cache()

    def append_tasks(self, tasks: list[TasklistItem]) -> None:
        """Create new Jira issues via agent callback."""
        cb = self._require_callback()
        filter_clauses = self._filter_clauses()
        for task in tasks:
            self._apply_write_effect(
                operation="create",
                artifact_id=task.task_id,
                description=(f"Create Jira issue '{task.title}' via jira MCP tools."),
            )
            prompt = (
                f"Use the jira MCP tools to create a new issue"
                f"{filter_clauses}"
                f" with the following details:\n\n"
                f"- Key/ID: {task.task_id}\n"
                f"- Summary: {task.title}\n"
                f"- Status: {self._jira_status(task.status)}\n"
            )
            if task.context:
                prompt += f"- Context: {task.context}\n"
            if task.criteria:
                prompt += f"- Acceptance criteria: {task.criteria}\n"
            cb(prompt)
        self.invalidate_cache()

    def get_prompt_placeholders(self) -> dict[str, str]:
        """Return Jira-idiomatic instructions for prompt template substitution."""
        filter_clauses = self._filter_clauses()
        return {
            "TASKLIST_READ_INSTRUCTIONS": (
                f"Use the jira MCP to list all issues{filter_clauses}. "
                f"Pick the first pending item (status 'To Do' or 'In Progress')."
            ),
            "TASKLIST_COMPLETE_INSTRUCTIONS": (
                "Use the jira MCP to transition exactly this one issue to 'Done'. "
                "Do not update any other issues."
            ),
            "TASKLIST_REWRITE_INSTRUCTIONS": (
                "Use the jira MCP to update each issue's summary and status "
                "to match the compacted content provided above."
            ),
            "TASKLIST_APPEND_INSTRUCTIONS": (
                f"Use the jira MCP to create new issues{filter_clauses}."
            ),
            "TASKLIST_UPDATE_INSTRUCTIONS": (
                "Use the jira MCP to find and edit the existing issues "
                "in place to address the feedback. Do not create duplicate issues."
            ),
        }

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, options: dict[str, Any]) -> JiraTasklistProvider:
        """Construct from config dict, accepting Jira-specific filter options."""
        filter_dict = options.get("filter", {})

        project = options.get("project") or filter_dict.get("project")

        label = options.get("label") or filter_dict.get("label")
        if not label:
            labels = options.get("labels") or filter_dict.get("labels") or []
            label = labels[0] if labels else None

        # Plural/orchestrator-normalized forms ("statuses") take precedence over singular shortcut.
        statuses = options.get("statuses") or filter_dict.get("statuses") or []
        if isinstance(statuses, str):
            statuses = [statuses]
        status_filter = (
            statuses[0] if statuses else (options.get("status") or filter_dict.get("status"))
        )

        # Plural/orchestrator-normalized forms ("assignees") take precedence over singular shortcut.
        assignees = options.get("assignees") or filter_dict.get("assignees") or []
        if isinstance(assignees, str):
            assignees = [assignees]
        assignee = (
            assignees[0] if assignees else (options.get("assignee") or filter_dict.get("assignee"))
        )

        return cls(
            project=project,
            label=label,
            status_filter=status_filter,
            assignee=assignee,
        )


# Self-register at module import time
register_tasklist_provider_class("jira", JiraTasklistProvider)
