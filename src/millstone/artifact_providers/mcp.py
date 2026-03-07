"""MCP-backed artifact provider implementations.

These providers delegate ALL operations (reads and writes) to a CLI agent via
a prompt that instructs it to use its configured MCP tools. Both read and write
operations go through the agent callback — there is no inner/HTTP provider.

Write operations are gated through ``_apply_write_effect`` (C2 control) so
that ``EffectPolicyGate`` can enforce capability-tier constraints when
configured.

Usage pattern in .millstone/config.toml::

    tasklist_provider = "mcp"
    [tasklist_provider_options]
    mcp_server = "linear"

    design_provider = "mcp"
    [design_provider_options]
    mcp_server = "notion"

MCP write prompt pattern::

    EffectIntent(
        effect_class=EffectClass.transactional,
        description="<verb> <artifact_type> via <mcp_server> MCP tools",
        idempotency_key=<artifact_id>,
        rollback_plan="Close/delete the created item via the <mcp_server> MCP tools.",
        metadata={"backend": "mcp", "mcp_server": ..., "artifact_type": ..., "operation": ...},
    )
"""

from __future__ import annotations

import json
import logging
import re
import warnings
from collections.abc import Callable
from typing import Any

from millstone.artifact_providers.base import (
    DesignProviderBase,
    OpportunityProviderBase,
    TasklistProviderBase,
)
from millstone.artifact_providers.registry import (
    register_design_provider_class,
    register_opportunity_provider_class,
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
from millstone.policy.effects import EffectClass, EffectIntent, EffectRecord, EffectStatus

logger = logging.getLogger(__name__)

_CODE_FENCE_JSON_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```\s*\n(.*?)\n```", re.DOTALL)


def _strip_json_fences(text: str) -> str:
    """Strip markdown code fences from agent responses before JSON parsing.

    Prefers ```json fences over plain ``` fences so that prefixed prose or
    non-JSON fenced blocks (e.g. ```python) don't shadow the JSON payload.
    """
    stripped = text.strip()
    m = _CODE_FENCE_JSON_RE.search(stripped) or _CODE_FENCE_RE.search(stripped)
    return m.group(1) if m else text


_TASK_STATUS_MAP: dict[str, TaskStatus] = {
    "todo": TaskStatus.todo,
    "in_progress": TaskStatus.in_progress,
    "done": TaskStatus.done,
    "blocked": TaskStatus.blocked,
}


class MCPTasklistProvider(TasklistProviderBase):
    """Tasklist provider that delegates ALL operations to a CLI agent via MCP tools.

    Both read and write operations invoke the agent callback, instructing it to
    use its configured MCP tools for ``mcp_server``. There is no inner provider.

    ``set_agent_callback`` must be called before any operation.
    ``OuterLoopManager._inject_agent_callbacks`` does this automatically.

    A session-level task cache is maintained to avoid redundant agent calls.
    Call ``invalidate_cache()`` (or writes do this automatically) to refresh.

    Note on ``restore_snapshot()``: MCP rollback only removes newly added tasks
    — it does NOT restore status changes or content edits made to pre-existing
    tasks. This is a known, accepted limitation:
    (a) The planner prompt explicitly instructs the agent to only append new tasks;
    (b) MCP backends retain native audit history for manual review;
    (c) Full per-task diff + restore would require O(n) update calls and
        bidirectional status mapping.
    """

    def __init__(
        self,
        mcp_server: str,
        *,
        labels: list[str] | None = None,
        projects: list[str] | None = None,
        effect_applier: Callable[[EffectIntent], EffectRecord] | None = None,
    ) -> None:
        self._mcp_server = mcp_server
        self._labels: list[str] = labels or []
        self._projects: list[str] = projects or []
        self._agent_callback: Callable[[str], str] | None = None
        self._effect_applier: Callable[[EffectIntent], EffectRecord] | None = effect_applier
        self._task_cache: list[TasklistItem] | None = None
        # Set by get_snapshot(); used by restore_snapshot() for scoped rollback.
        self._snapshot_task_ids: set[str] | None = None

    def _label_clause(self) -> str:
        return f" with label '{self._labels[0]}'" if self._labels else ""

    def _project_clause(self) -> str:
        return f" in project '{self._projects[0]}'" if self._projects else ""

    def set_agent_callback(self, cb: Callable[[str], str]) -> None:
        """Inject the agent callback used for MCP operations."""
        self._agent_callback = cb

    def set_effect_applier(self, apply_effect: Callable[[EffectIntent], EffectRecord]) -> None:
        """Attach an outer-loop effect applier for remote write operations."""
        self._effect_applier = apply_effect

    def invalidate_cache(self) -> None:
        """Clear the task cache so the next list_tasks() fetches fresh data."""
        self._task_cache = None

    def reset_snapshot_baseline(self) -> None:
        """Reset the rollback baseline so the next get_snapshot() captures a fresh one.

        Call this at the start of each planning session to ensure restore_snapshot()
        rolls back to the correct pre-plan state rather than a stale one from a
        prior session.
        """
        self._snapshot_task_ids = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_callback(self) -> Callable[[str], str]:
        if self._agent_callback is None:
            raise RuntimeError(
                "MCP provider requires agent callback — call set_agent_callback() before use"
            )
        return self._agent_callback

    def _apply_write_effect(self, *, operation: str, artifact_id: str, description: str) -> None:
        if self._effect_applier is None:
            return
        intent = EffectIntent(
            effect_class=EffectClass.transactional,
            description=description,
            idempotency_key=artifact_id,
            rollback_plan=(
                f"Close or delete the created item via the {self._mcp_server} MCP tools."
            ),
            metadata={
                "backend": "mcp",
                "mcp_server": self._mcp_server,
                "artifact_type": "task",
                "operation": operation,
            },
        )
        record = self._effect_applier(intent)
        if record.status in {EffectStatus.denied, EffectStatus.failed}:
            raise RuntimeError(
                f"MCP write blocked for task:{artifact_id} (status={record.status.value})"
            )

    # ------------------------------------------------------------------
    # Read operations — via agent callback (cached)
    # ------------------------------------------------------------------

    def list_tasks(self) -> list[TasklistItem]:
        """Fetch all tasks via agent callback, returning cached results when available.

        Fetches all task states (todo, in_progress, done, blocked) so that
        run_plan()'s snapshot diff correctly detects newly added tasks.
        """
        if self._task_cache is not None:
            return self._task_cache
        cb = self._require_callback()
        label_clause = self._label_clause()
        project_clause = self._project_clause()
        prompt = (
            f"Use the {self._mcp_server} MCP to list ALL tasks in all states "
            f"(todo, in_progress, done, blocked){label_clause}{project_clause}. "
            f"Output ONLY a compact JSON array directly in your response — do not write to a file. "
            f'Each item: {{"id": "...", "title": "...", "status": "todo|in_progress|done|blocked"}}'
        )
        response = cb(prompt)
        try:
            items = json.loads(_strip_json_fences(response))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"MCPTasklistProvider.list_tasks: agent returned invalid JSON: {exc}\n"
                f"Response: {response!r}"
            ) from exc
        results = []
        for item in items:
            status = _TASK_STATUS_MAP.get(item.get("status", "todo"), TaskStatus.todo)
            results.append(
                TasklistItem(
                    task_id=item["id"],
                    title=item["title"],
                    status=status,
                )
            )
        self._task_cache = results
        return results

    def get_task(self, task_id: str) -> TasklistItem | None:
        """Fetch a specific task by ID via agent callback."""
        cb = self._require_callback()
        prompt = (
            f"Use the {self._mcp_server} MCP to get the task with ID '{task_id}'. "
            f"Output ONLY a JSON object with fields: id, title, status, context, criteria, tests, risk."
        )
        response = cb(prompt)
        try:
            item = json.loads(_strip_json_fences(response))
        except json.JSONDecodeError:
            return None
        if not item:
            return None
        status = _TASK_STATUS_MAP.get(item.get("status", "todo"), TaskStatus.todo)
        return TasklistItem(
            task_id=item.get("id", task_id),
            title=item.get("title", ""),
            status=status,
            context=item.get("context"),
            criteria=item.get("criteria"),
            tests=item.get("tests"),
            risk=item.get("risk"),
        )

    def get_snapshot(self) -> str:
        """Reconstruct full-block markdown tasklist from agent-provided tasks.

        Stores the current task ID set for scoped rollback via restore_snapshot().

        Each task block uses ``t.raw`` directly when non-empty (it stores the full
        block text including metadata lines). Falls back to reconstructing from
        individual field values when raw is empty.

        The status checkbox is always derived from ``t.status`` (not from raw),
        so the snapshot reflects the true current state.

        For MCP providers, _validate_generated_tasks() fetches full task details
        via get_task() instead of parsing snapshot text, so compact snapshots
        (title-only) are fine for validation purposes.
        """
        tasks = self.list_tasks()
        # Only capture the baseline on the first call; subsequent calls (e.g.
        # during validation loops in _run_plan_impl) must not overwrite it, or
        # restore_snapshot() would compute an empty diff and delete nothing.
        if self._snapshot_task_ids is None:
            self._snapshot_task_ids = {t.task_id for t in tasks}

        blocks = []
        for t in tasks:
            checkbox = "[x]" if t.status == TaskStatus.done else "[ ]"
            if t.raw:
                stripped = t.raw.lstrip()
                if stripped.startswith("- ["):
                    # Full block already in raw — update checkbox to match current status
                    updated = re.sub(r"^- \[[ x]\]", f"- {checkbox}", t.raw, count=1)
                    blocks.append(updated)
                else:
                    # Body-only raw — prepend title line
                    blocks.append(f"- {checkbox} **{t.title}**: {t.raw}")
            else:
                # Reconstruct from individual fields
                lines = [f"- {checkbox} **{t.title}**"]
                if t.risk:
                    lines.append(f"  - Risk: {t.risk}")
                if t.tests:
                    lines.append(f"  - Tests: {t.tests}")
                if t.criteria:
                    lines.append(f"  - Criteria: {t.criteria}")
                if t.context:
                    lines.append(f"  - Context: {t.context}")
                blocks.append("\n".join(lines))
        return "\n".join(blocks)

    def restore_snapshot(self, content: str) -> None:  # noqa: ARG002
        """Roll back by deleting tasks added after the last snapshot.

        Uses ``self._snapshot_task_ids`` set by the last ``get_snapshot()`` call.
        If no snapshot was taken, logs a warning and returns without error.

        **Known limitation**: Only removes newly added tasks. Does NOT restore
        status changes or content edits made to pre-existing tasks. This is
        acceptable because: (a) the planner prompt explicitly instructs the agent
        to only append new tasks; (b) MCP backends retain native audit history;
        (c) full per-task diff + restore would require O(n) update calls and
        bidirectional status mapping.
        """
        if self._snapshot_task_ids is None:
            logger.warning(
                "MCPTasklistProvider.restore_snapshot called before get_snapshot; "
                "no rollback performed"
            )
            return
        self.invalidate_cache()
        current_tasks = self.list_tasks()
        extra_tasks = [t for t in current_tasks if t.task_id not in self._snapshot_task_ids]
        if extra_tasks:
            cb = self._require_callback()
            titles = ", ".join(f"'{t.title}'" for t in extra_tasks)
            ids = ", ".join(t.task_id for t in extra_tasks)
            prompt = (
                f"Use the {self._mcp_server} MCP to delete or archive these tasks that "
                f"were created in error: {titles} (IDs: {ids})"
            )
            cb(prompt)
        self.invalidate_cache()

    # ------------------------------------------------------------------
    # Write operations — routed through effect gate, then agent callback
    # ------------------------------------------------------------------

    def get_prompt_placeholders(self) -> dict[str, str]:
        """Return MCP-idiomatic instructions for prompt template substitution."""
        label_clause = self._label_clause()
        project_clause = self._project_clause()
        return {
            "TASKLIST_READ_INSTRUCTIONS": (
                f"Use the {self._mcp_server} MCP to list all tasks"
                f"{label_clause}{project_clause}. Pick the first pending/todo item."
            ),
            "TASKLIST_COMPLETE_INSTRUCTIONS": (
                f"Use the {self._mcp_server} MCP to mark exactly this one task as "
                f"done/complete. Do not update any other tasks."
            ),
            "TASKLIST_REWRITE_INSTRUCTIONS": (
                f"Use the {self._mcp_server} MCP to update each task's title and "
                f"status to match the compacted content provided above."
            ),
            "TASKLIST_APPEND_INSTRUCTIONS": (
                f"Use the {self._mcp_server} MCP to create new tasks{label_clause}{project_clause}."
            ),
            "TASKLIST_UPDATE_INSTRUCTIONS": (
                f"Use the {self._mcp_server} MCP to find and edit the existing tasks "
                f"in place to address the feedback. Do not create duplicate tasks."
            ),
        }

    def append_tasks(self, tasks: list[TasklistItem]) -> None:
        """Create new tasks via the configured MCP server."""
        cb = self._require_callback()
        label_clause = self._label_clause()
        project_clause = self._project_clause()
        for task in tasks:
            self._apply_write_effect(
                operation="create",
                artifact_id=task.task_id,
                description=(f"Create task '{task.title}' via {self._mcp_server} MCP tools."),
            )
            prompt = (
                f"Use the {self._mcp_server} MCP tools to create a new task"
                f"{label_clause}{project_clause}"
                f" with the following details:\n\n"
                f"- ID: {task.task_id}\n"
                f"- Title: {task.title}\n"
                f"- Status: {task.status.value}\n"
            )
            if task.design_ref:
                prompt += f"- Design reference: {task.design_ref}\n"
            if task.opportunity_ref:
                prompt += f"- Opportunity reference: {task.opportunity_ref}\n"
            if task.context:
                prompt += f"- Context: {task.context}\n"
            if task.criteria:
                prompt += f"- Acceptance criteria: {task.criteria}\n"
            cb(prompt)
        self.invalidate_cache()

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        """Update task status via the configured MCP server."""
        cb = self._require_callback()
        self._apply_write_effect(
            operation="update_status",
            artifact_id=task_id,
            description=(
                f"Update task '{task_id}' to status '{status.value}' "
                f"via {self._mcp_server} MCP tools."
            ),
        )
        label_clause = self._label_clause()
        project_clause = self._project_clause()
        prompt = (
            f"Use the {self._mcp_server} MCP tools to find task '{task_id}'"
            f"{label_clause}{project_clause}"
            f" and update its status to '{status.value}'."
        )
        cb(prompt)
        self.invalidate_cache()

    @classmethod
    def from_config(cls, options: dict[str, Any]) -> MCPTasklistProvider:
        mcp_server = options.get("mcp_server")
        if not mcp_server:
            raise ValueError("MCPTasklistProvider.from_config requires 'mcp_server' in options")
        if "read_backend" in options:
            warnings.warn(
                "MCPTasklistProvider: 'read_backend' is deprecated and ignored. "
                "Reads now go through the agent callback.",
                DeprecationWarning,
                stacklevel=2,
            )
        # Accept normalized filter dict (from tasklist_filter config) or top-level keys.
        # Precedence (highest first): top-level labels/projects → filter.labels/projects → label/project shortcuts.
        # Key-presence checks (not truthiness) so that an explicit empty list is respected.
        filter_dict = options.get("filter", {})
        if "labels" in options:
            labels = list(options["labels"])
        elif "labels" in filter_dict:
            labels = list(filter_dict["labels"])
        elif "label" in options:
            labels = [options["label"]] if options["label"] else []
        elif "label" in filter_dict:
            labels = [filter_dict["label"]] if filter_dict["label"] else []
        else:
            labels = []
        if "projects" in options:
            projects = list(options["projects"])
        elif "projects" in filter_dict:
            projects = list(filter_dict["projects"])
        elif "project" in options:
            projects = [options["project"]] if options["project"] else []
        elif "project" in filter_dict:
            projects = [filter_dict["project"]] if filter_dict["project"] else []
        else:
            projects = []
        return cls(mcp_server=mcp_server, labels=labels, projects=projects)


class MCPDesignProvider(DesignProviderBase):
    """Design provider that delegates ALL operations to a CLI agent via MCP tools.

    Both read and write operations invoke the agent callback, instructing it to
    use its configured MCP tools for ``mcp_server``. There is no inner provider.

    ``set_agent_callback`` must be called before any write operation.
    ``OuterLoopManager._inject_agent_callbacks`` does this automatically.
    """

    def __init__(
        self,
        mcp_server: str,
        *,
        projects: list[str] | None = None,
        effect_applier: Callable[[EffectIntent], EffectRecord] | None = None,
    ) -> None:
        self._mcp_server = mcp_server
        self._projects: list[str] = projects or []
        self._agent_callback: Callable[[str], str] | None = None
        self._effect_applier: Callable[[EffectIntent], EffectRecord] | None = effect_applier

    def _project_clause(self) -> str:
        return f" in project '{self._projects[0]}'" if self._projects else ""

    def set_agent_callback(self, cb: Callable[[str], str]) -> None:
        """Inject the agent callback used for MCP operations."""
        self._agent_callback = cb

    def set_effect_applier(self, apply_effect: Callable[[EffectIntent], EffectRecord]) -> None:
        """Attach an outer-loop effect applier for remote write operations."""
        self._effect_applier = apply_effect

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_callback(self) -> Callable[[str], str]:
        if self._agent_callback is None:
            raise RuntimeError(
                "MCP provider requires agent callback — call set_agent_callback() before use"
            )
        return self._agent_callback

    def _apply_write_effect(self, *, operation: str, artifact_id: str, description: str) -> None:
        if self._effect_applier is None:
            return
        intent = EffectIntent(
            effect_class=EffectClass.transactional,
            description=description,
            idempotency_key=artifact_id,
            rollback_plan=(f"Delete or archive the design via the {self._mcp_server} MCP tools."),
            metadata={
                "backend": "mcp",
                "mcp_server": self._mcp_server,
                "artifact_type": "design",
                "operation": operation,
            },
        )
        record = self._effect_applier(intent)
        if record.status in {EffectStatus.denied, EffectStatus.failed}:
            raise RuntimeError(
                f"MCP write blocked for design:{artifact_id} (status={record.status.value})"
            )

    # ------------------------------------------------------------------
    # Read operations — via agent callback
    # ------------------------------------------------------------------

    def list_designs(self) -> list[Design]:
        """Fetch all designs via agent callback."""
        cb = self._require_callback()
        project_clause = self._project_clause()
        prompt = (
            f"Use the {self._mcp_server} MCP to list ALL design documents"
            f"{project_clause}. "
            f"Output ONLY a compact JSON array directly in your response — do not write to a file. "
            f'Each item: {{"id": "...", "title": "...", "status": "draft|reviewed|approved|superseded", '
            f'"opportunity_ref": "..."}}'
        )
        response = cb(prompt)
        try:
            items = json.loads(_strip_json_fences(response))
        except json.JSONDecodeError:
            return []
        results = []
        for item in items:
            try:
                status = DesignStatus(item.get("status", "draft"))
            except ValueError:
                status = DesignStatus.draft
            results.append(
                Design(
                    design_id=item["id"],
                    title=item.get("title", ""),
                    status=status,
                    body="",  # Not fetched in list; use get_design() for full body
                    opportunity_ref=item.get("opportunity_ref"),
                )
            )
        return results

    def get_design(self, design_id: str) -> Design | None:
        """Fetch a specific design by ID via agent callback."""
        cb = self._require_callback()
        prompt = (
            f"Use the {self._mcp_server} MCP to get the design document with ID '{design_id}'. "
            f"Output ONLY a JSON object with fields: id, title, status, opportunity_ref, body."
        )
        response = cb(prompt)
        try:
            item = json.loads(_strip_json_fences(response))
        except json.JSONDecodeError:
            return None
        if not item:
            return None
        try:
            status = DesignStatus(item.get("status", "draft"))
        except ValueError:
            status = DesignStatus.draft
        return Design(
            design_id=item.get("id", design_id),
            title=item.get("title", ""),
            status=status,
            body=item.get("body", ""),
            opportunity_ref=item.get("opportunity_ref"),
        )

    # ------------------------------------------------------------------
    # Write operations — routed through effect gate, then agent callback
    # ------------------------------------------------------------------

    def get_prompt_placeholders(self) -> dict[str, str]:
        """Return MCP-idiomatic instructions for prompt template substitution."""
        project_clause = self._project_clause()
        return {
            "DESIGN_WRITE_INSTRUCTIONS": (
                f"Use the {self._mcp_server} MCP to create or update a design document"
                f"{project_clause}."
            ),
        }

    def write_design(self, design: Design) -> None:
        """Create or update a design via the configured MCP server."""
        cb = self._require_callback()
        project_clause = self._project_clause()
        self._apply_write_effect(
            operation="write",
            artifact_id=design.design_id,
            description=(f"Write design '{design.design_id}' via {self._mcp_server} MCP tools."),
        )
        prompt = (
            f"Use the {self._mcp_server} MCP tools to create or update a design "
            f"document{project_clause} with the following details:\n\n"
            f"- ID: {design.design_id}\n"
            f"- Title: {design.title}\n"
            f"- Status: {design.status.value}\n"
        )
        if design.opportunity_ref:
            prompt += f"- Opportunity reference: {design.opportunity_ref}\n"
        if design.body:
            prompt += f"\n## Body\n\n{design.body}\n"
        cb(prompt)

    def update_design_status(self, design_id: str, status: DesignStatus) -> None:
        """Update design status via the configured MCP server."""
        cb = self._require_callback()
        self._apply_write_effect(
            operation="update_status",
            artifact_id=design_id,
            description=(
                f"Update design '{design_id}' to status '{status.value}' "
                f"via {self._mcp_server} MCP tools."
            ),
        )
        prompt = (
            f"Use the {self._mcp_server} MCP tools to update design '{design_id}' "
            f"to status '{status.value}'."
        )
        cb(prompt)

    @classmethod
    def from_config(cls, options: dict[str, Any]) -> MCPDesignProvider:
        mcp_server = options.get("mcp_server")
        if not mcp_server:
            raise ValueError("MCPDesignProvider.from_config requires 'mcp_server' in options")
        if "read_backend" in options:
            warnings.warn(
                "MCPDesignProvider: 'read_backend' is deprecated and ignored. "
                "Reads now go through the agent callback.",
                DeprecationWarning,
                stacklevel=2,
            )
        projects: list[str] = options.get("projects") or (
            [options["project"]] if options.get("project") else []
        )
        return cls(mcp_server=mcp_server, projects=projects)


class MCPOpportunityProvider(OpportunityProviderBase):
    """Opportunity provider that delegates ALL operations to a CLI agent via MCP tools.

    Both read and write operations invoke the agent callback, instructing it to
    use its configured MCP tools for ``mcp_server``.

    ``set_agent_callback`` must be called before any operation.
    ``OuterLoopManager._inject_agent_callbacks`` does this automatically.
    """

    def __init__(
        self,
        mcp_server: str,
        *,
        projects: list[str] | None = None,
        effect_applier: Callable[[EffectIntent], EffectRecord] | None = None,
    ) -> None:
        self._mcp_server = mcp_server
        self._projects: list[str] = projects or []
        self._agent_callback: Callable[[str], str] | None = None
        self._effect_applier: Callable[[EffectIntent], EffectRecord] | None = effect_applier

    def _project_clause(self) -> str:
        return f" in project '{self._projects[0]}'" if self._projects else ""

    def set_agent_callback(self, cb: Callable[[str], str]) -> None:
        """Inject the agent callback used for MCP operations."""
        self._agent_callback = cb

    def set_effect_applier(self, apply_effect: Callable[[EffectIntent], EffectRecord]) -> None:
        """Attach an outer-loop effect applier for remote write operations."""
        self._effect_applier = apply_effect

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_callback(self) -> Callable[[str], str]:
        if self._agent_callback is None:
            raise RuntimeError(
                "MCP provider requires agent callback — call set_agent_callback() before use"
            )
        return self._agent_callback

    def _apply_write_effect(self, *, operation: str, artifact_id: str, description: str) -> None:
        if self._effect_applier is None:
            return
        intent = EffectIntent(
            effect_class=EffectClass.transactional,
            description=description,
            idempotency_key=artifact_id,
            rollback_plan=(
                f"Delete or archive the opportunity via the {self._mcp_server} MCP tools."
            ),
            metadata={
                "backend": "mcp",
                "mcp_server": self._mcp_server,
                "artifact_type": "opportunity",
                "operation": operation,
            },
        )
        record = self._effect_applier(intent)
        if record.status in {EffectStatus.denied, EffectStatus.failed}:
            raise RuntimeError(
                f"MCP write blocked for opportunity:{artifact_id} (status={record.status.value})"
            )

    # ------------------------------------------------------------------
    # Read operations — via agent callback
    # ------------------------------------------------------------------

    def list_opportunities(self) -> list[Opportunity]:
        """Fetch all opportunities via agent callback."""
        cb = self._require_callback()
        project_clause = self._project_clause()
        prompt = (
            f"Use the {self._mcp_server} MCP to list ALL opportunities"
            f"{project_clause}. "
            f"Output ONLY a JSON array, no other text. "
            f'Each item: {{"id": "...", "title": "...", "status": "identified|adopted|rejected", '
            f'"description": "..."}}'
        )
        response = cb(prompt)
        try:
            items = json.loads(_strip_json_fences(response))
        except json.JSONDecodeError:
            return []
        results = []
        for item in items:
            try:
                status = OpportunityStatus(item.get("status", "identified"))
            except ValueError:
                status = OpportunityStatus.identified
            results.append(
                Opportunity(
                    opportunity_id=item["id"],
                    title=item.get("title", ""),
                    status=status,
                    description=item.get("description", ""),
                )
            )
        return results

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        """Fetch a specific opportunity by ID via agent callback."""
        cb = self._require_callback()
        prompt = (
            f"Use the {self._mcp_server} MCP to get the opportunity with ID '{opportunity_id}'. "
            f"Output ONLY a JSON object with fields: id, title, status, description."
        )
        response = cb(prompt)
        try:
            item = json.loads(_strip_json_fences(response))
        except json.JSONDecodeError:
            return None
        if not item:
            return None
        try:
            status = OpportunityStatus(item.get("status", "identified"))
        except ValueError:
            status = OpportunityStatus.identified
        return Opportunity(
            opportunity_id=item.get("id", opportunity_id),
            title=item.get("title", ""),
            status=status,
            description=item.get("description", ""),
        )

    # ------------------------------------------------------------------
    # Write operations — routed through effect gate, then agent callback
    # ------------------------------------------------------------------

    def get_prompt_placeholders(self) -> dict[str, str]:
        """Return MCP-idiomatic instructions for prompt template substitution."""
        project_clause = self._project_clause()
        return {
            "OPPORTUNITY_WRITE_INSTRUCTIONS": (
                f"Use the {self._mcp_server} MCP to create or update an opportunity record"
                f"{project_clause}."
            ),
            "OPPORTUNITY_READ_INSTRUCTIONS": (
                f"Use the {self._mcp_server} MCP to list opportunities{project_clause}."
            ),
        }

    def write_opportunity(self, opportunity: Opportunity) -> None:
        """Create or update an opportunity via the configured MCP server."""
        cb = self._require_callback()
        project_clause = self._project_clause()
        self._apply_write_effect(
            operation="write",
            artifact_id=opportunity.opportunity_id,
            description=(
                f"Write opportunity '{opportunity.opportunity_id}' "
                f"via {self._mcp_server} MCP tools."
            ),
        )
        prompt = (
            f"Use the {self._mcp_server} MCP tools to create or update an opportunity"
            f"{project_clause} with the following details:\n\n"
            f"- ID: {opportunity.opportunity_id}\n"
            f"- Title: {opportunity.title}\n"
            f"- Status: {opportunity.status.value}\n"
            f"- Description: {opportunity.description}\n"
        )
        if opportunity.priority:
            prompt += f"- Priority: {opportunity.priority}\n"
        if opportunity.roi_score is not None:
            prompt += f"- ROI Score: {opportunity.roi_score}\n"
        cb(prompt)

    def update_opportunity_status(self, opportunity_id: str, status: OpportunityStatus) -> None:
        """Update opportunity status via the configured MCP server."""
        cb = self._require_callback()
        self._apply_write_effect(
            operation="update_status",
            artifact_id=opportunity_id,
            description=(
                f"Update opportunity '{opportunity_id}' to status '{status.value}' "
                f"via {self._mcp_server} MCP tools."
            ),
        )
        prompt = (
            f"Use the {self._mcp_server} MCP tools to update opportunity '{opportunity_id}' "
            f"to status '{status.value}'."
        )
        cb(prompt)

    @classmethod
    def from_config(cls, options: dict[str, Any]) -> MCPOpportunityProvider:
        mcp_server = options.get("mcp_server")
        if not mcp_server:
            raise ValueError("MCPOpportunityProvider.from_config requires 'mcp_server' in options")
        projects: list[str] = options.get("projects") or (
            [options["project"]] if options.get("project") else []
        )
        return cls(mcp_server=mcp_server, projects=projects)


# Self-register at module import time
register_tasklist_provider_class("mcp", MCPTasklistProvider)
register_design_provider_class("mcp", MCPDesignProvider)
register_opportunity_provider_class("mcp", MCPOpportunityProvider)
