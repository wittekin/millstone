"""Beads-backed artifact providers (subprocess-based).

Implements ``BeadsTasklistProvider`` and ``BeadsOpportunityProvider`` against
the ``bd`` CLI from https://github.com/gastownhall/beads. Beads is a
distributed graph issue tracker — millstone uses it as a deterministic remote
backend, calling ``bd`` directly rather than going through an LLM/MCP loop.

Configuration (``.millstone/config.toml``)::

    # Flat top-level keys — millstone's load_config reads the top level only,
    # so do NOT nest these under a ``[millstone]`` table.
    tasklist_provider = "beads"
    opportunity_provider = "beads"

    [tasklist_provider_options]
    label = "millstone-task"   # optional filter; recommended to namespace artifacts
    bd_path = "/usr/local/bin/bd"  # optional override; default uses $PATH lookup

    [opportunity_provider_options]
    label = "millstone-opportunity"

The ``bd`` binary must be on ``PATH`` (or ``bd_path`` set), and the working
directory must be a beads-initialized repo (``bd init``).

Both providers implement the optional ``ReadyAwareTasklistProvider`` /
``DependencyLinker`` capability protocols. Tasks appended with an
``opportunity_ref`` set are auto-linked to the referenced opportunity via
``bd dep add ... --type discovered-from``.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from millstone.artifact_providers.base import (
    OpportunityProviderBase,
    TasklistProviderBase,
)
from millstone.artifact_providers.registry import (
    register_opportunity_provider_class,
    register_tasklist_provider_class,
)
from millstone.artifacts.models import (
    DependencyKind,
    Opportunity,
    OpportunityStatus,
    TasklistItem,
    TaskStatus,
)

logger = logging.getLogger(__name__)


# Status mapping: TaskStatus / OpportunityStatus <-> beads status strings.
_TASK_STATUS_TO_BD: dict[TaskStatus, str] = {
    TaskStatus.todo: "open",
    TaskStatus.in_progress: "in_progress",
    TaskStatus.done: "closed",
    TaskStatus.blocked: "blocked",
}

_BD_TO_TASK_STATUS: dict[str, TaskStatus] = {
    "open": TaskStatus.todo,
    "ready": TaskStatus.todo,
    "in_progress": TaskStatus.in_progress,
    "closed": TaskStatus.done,
    "done": TaskStatus.done,
    "blocked": TaskStatus.blocked,
}

_OPP_STATUS_TO_BD: dict[OpportunityStatus, str] = {
    OpportunityStatus.identified: "open",
    OpportunityStatus.adopted: "in_progress",
    OpportunityStatus.rejected: "closed",
}

_BD_TO_OPP_STATUS: dict[str, OpportunityStatus] = {
    "open": OpportunityStatus.identified,
    "ready": OpportunityStatus.identified,
    "in_progress": OpportunityStatus.adopted,
    "closed": OpportunityStatus.rejected,
    "done": OpportunityStatus.rejected,
}


class BeadsCLIError(RuntimeError):
    """Raised when a `bd` invocation fails."""


class _BeadsCLI:
    """Thin wrapper around `bd` invocations. Injectable for tests."""

    def __init__(self, bd_path: str | None = None, cwd: Path | None = None) -> None:
        resolved = bd_path or shutil.which("bd")
        if not resolved:
            raise BeadsCLIError(
                "beads CLI ('bd') not found on PATH. "
                "Install from https://github.com/gastownhall/beads or set "
                "bd_path in [tasklist_provider_options]."
            )
        self.bd_path = resolved
        self.cwd = cwd

    def run(self, args: list[str], *, json_output: bool = False) -> str:
        """Invoke `bd` with the given args; return stdout. Raises BeadsCLIError on failure."""
        cmd = [self.bd_path]
        if json_output:
            cmd.append("--json")
        cmd.extend(args)
        try:
            result = subprocess.run(  # noqa: S603 — bd_path resolved via which()
                cmd,
                cwd=str(self.cwd) if self.cwd else None,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise BeadsCLIError(f"failed to invoke bd: {exc}") from exc
        if result.returncode != 0:
            raise BeadsCLIError(
                f"bd {' '.join(args)} failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout

    def run_json(self, args: list[str]) -> Any:
        """Invoke `bd --json ...` and parse the JSON result."""
        out = self.run(args, json_output=True).strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise BeadsCLIError(
                f"bd {' '.join(args)} returned non-JSON output: {out[:200]!r}"
            ) from exc


def _coerce_items(payload: Any) -> list[dict]:
    """Normalize bd JSON list output to a list of issue dicts.

    Different bd subcommands wrap the array under different keys
    (`issues`, `items`, `results`); fall back to top-level list.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("issues", "items", "results", "data"):
            if key in payload and isinstance(payload[key], list):
                return [x for x in payload[key] if isinstance(x, dict)]
        # Single issue object
        if "id" in payload:
            return [payload]
    return []


# ---------------------------------------------------------------------------
# BeadsTasklistProvider
# ---------------------------------------------------------------------------


class BeadsTasklistProvider(TasklistProviderBase):
    """Tasklist provider backed by the beads CLI.

    Implements ``ReadyAwareTasklistProvider`` and ``DependencyLinker`` capabilities.

    Notes on identity: beads assigns hash-based IDs (``bd-a1b2``) at creation
    time. ``append_tasks`` mutates each ``TasklistItem.task_id`` in place to the
    beads-assigned ID after creation, so subsequent reads / updates use the
    canonical ID.
    """

    def __init__(
        self,
        *,
        label: str | None = None,
        bd_path: str | None = None,
        cwd: Path | None = None,
        cli: _BeadsCLI | None = None,
    ) -> None:
        self._label = label
        self._cli = cli or _BeadsCLI(bd_path=bd_path, cwd=cwd)

    @classmethod
    def from_config(cls, options: dict[str, Any]) -> BeadsTasklistProvider:
        return cls(
            label=options.get("label"),
            bd_path=options.get("bd_path"),
            cwd=Path(options["cwd"]) if options.get("cwd") else None,
        )

    # -- read --------------------------------------------------------------

    def _list_args(self) -> list[str]:
        args = ["list", "--status", "open,in_progress,blocked"]
        if self._label:
            args.extend(["--label", self._label])
        return args

    def list_tasks(self) -> list[TasklistItem]:
        payload = self._cli.run_json(self._list_args())
        return [self._to_item(d) for d in _coerce_items(payload)]

    def list_ready_tasks(self) -> list[TasklistItem]:
        """Return only unblocked tasks (uses ``bd ready``)."""
        args = ["ready"]
        if self._label:
            args.extend(["--label", self._label])
        payload = self._cli.run_json(args)
        return [self._to_item(d) for d in _coerce_items(payload)]

    def get_task(self, task_id: str) -> TasklistItem | None:
        try:
            payload = self._cli.run_json(["show", task_id])
        except BeadsCLIError:
            return None
        items = _coerce_items(payload)
        return self._to_item(items[0]) if items else None

    @staticmethod
    def _to_item(d: dict) -> TasklistItem:
        raw_status = str(d.get("status") or "open").lower().strip()
        status = _BD_TO_TASK_STATUS.get(raw_status, TaskStatus.todo)
        return TasklistItem(
            task_id=str(d["id"]),
            title=str(d.get("title") or d.get("summary") or ""),
            status=status,
            raw=str(d.get("description") or d.get("body") or "") or None,
        )

    # -- write -------------------------------------------------------------

    def append_tasks(self, tasks: list[TasklistItem]) -> None:
        for task in tasks:
            task.validate()
            new_id = self._create(task)
            # Mutate to beads-assigned ID so callers / subsequent reads match.
            task.task_id = new_id
            if task.opportunity_ref:
                # Best-effort: link new task to its source opportunity.
                try:
                    self.link(new_id, task.opportunity_ref, DependencyKind.discovered_from)
                except BeadsCLIError as exc:
                    logger.warning(
                        "failed to link beads task %s -> opportunity %s: %s",
                        new_id,
                        task.opportunity_ref,
                        exc,
                    )

    def _create(self, task: TasklistItem) -> str:
        args = ["create", task.title, "-t", "task"]
        if self._label:
            args.extend(["-l", self._label])
        description = _format_task_body(task)
        if description:
            args.extend(["-d", description])
        payload = self._cli.run_json(args)
        items = _coerce_items(payload)
        if not items or "id" not in items[0]:
            raise BeadsCLIError(f"bd create did not return an id for task {task.title!r}")
        return str(items[0]["id"])

    def update_task(self, task: TasklistItem) -> None:
        task.validate()
        body = _format_task_body(task)
        args = ["update", task.task_id, "--title", task.title]
        if body:
            args.extend(["--description", body])
        bd_status = _TASK_STATUS_TO_BD.get(task.status)
        if bd_status:
            args.extend(["--status", bd_status])
        self._cli.run(args)

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        bd_status = _TASK_STATUS_TO_BD.get(status)
        if bd_status is None:
            raise ValueError(f"unsupported task status for beads: {status}")
        if status == TaskStatus.done:
            self._cli.run(["close", task_id])
        else:
            self._cli.run(["update", task_id, "--status", bd_status])

    # -- snapshot / restore -----------------------------------------------

    def get_snapshot(self) -> str:
        """Snapshot = JSON list of current task ids. Used for scoped rollback."""
        ids = sorted(t.task_id for t in self.list_tasks())
        return json.dumps({"task_ids": ids})

    def restore_snapshot(self, content: str) -> None:
        """Delete tasks added since the snapshot was taken.

        Mirrors the MCP provider's scoped-rollback semantics: only newly added
        tasks are removed; status/content edits to pre-existing tasks are not
        reverted (beads' own history is the source of truth for those).
        """
        try:
            baseline = set(json.loads(content).get("task_ids") or [])
        except (json.JSONDecodeError, AttributeError) as exc:
            raise ValueError(f"invalid beads snapshot: {exc}") from exc
        current = {t.task_id for t in self.list_tasks()}
        for task_id in current - baseline:
            try:
                self._cli.run(["delete", task_id])
            except BeadsCLIError as exc:
                logger.warning("failed to delete beads task %s on rollback: %s", task_id, exc)

    # -- DependencyLinker capability --------------------------------------

    def link(self, from_id: str, to_id: str, kind: DependencyKind) -> None:
        """Add a typed link between two beads issues via ``bd dep add``.

        Semantics follow the kind: for ``blocks``, ``from_id`` blocks ``to_id``;
        for ``discovered_from``, ``from_id`` was discovered while working on
        ``to_id``; etc.
        """
        self._cli.run(["dep", "add", from_id, to_id, "--type", kind.value])

    # -- prompt placeholders ----------------------------------------------

    def get_prompt_placeholders(self) -> dict[str, str]:
        label_clause = f" with label '{self._label}'" if self._label else ""
        return {
            "TASKLIST_READ_INSTRUCTIONS": (
                f"Use the beads CLI: `bd ready --json`{(' --label ' + self._label) if self._label else ''}"
                f" to list unblocked tasks{label_clause}. "
                "Pick the first item; use `bd show <id>` for full details."
            ),
            "TASKLIST_COMPLETE_INSTRUCTIONS": (
                "Mark the task done with `bd close <id>`. Do not modify other issues."
            ),
            "TASKLIST_APPEND_INSTRUCTIONS": (
                f'Create new beads tasks with `bd create "<title>" -t task'
                f'{(" -l " + self._label) if self._label else ""} -d "<body>"`.'
            ),
            "TASKLIST_UPDATE_INSTRUCTIONS": (
                "Edit existing tasks in place via `bd update <id> --title/--description/--status`. "
                "Do not create duplicates."
            ),
            "TASKLIST_REWRITE_INSTRUCTIONS": (
                "For bulk updates, iterate the compacted list and `bd update <id>` each entry."
            ),
        }


def _format_task_body(task: TasklistItem) -> str:
    """Render TasklistItem metadata into a beads description body."""
    lines: list[str] = []
    if task.opportunity_ref:
        lines.append(f"Opportunity: {task.opportunity_ref}")
    if task.design_ref:
        lines.append(f"Design: {task.design_ref}")
    if task.context:
        lines.append(f"Context: {task.context}")
    if task.criteria:
        lines.append(f"Criteria: {task.criteria}")
    if task.tests:
        lines.append(f"Tests: {task.tests}")
    if task.risk:
        lines.append(f"Risk: {task.risk}")
    if task.acceptance_criteria:
        lines.append("Acceptance criteria:")
        lines.extend(f"- {c}" for c in task.acceptance_criteria)
    if task.raw and not lines:
        return task.raw
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# BeadsOpportunityProvider
# ---------------------------------------------------------------------------


class BeadsOpportunityProvider(OpportunityProviderBase):
    """Opportunity provider backed by beads.

    Treats opportunities as beads issues with a configurable label
    (default: ``millstone-opportunity``) so they can coexist with tasks in the
    same backend without overlap.

    Implements ``DependencyLinker`` for cross-artifact linking.
    """

    def __init__(
        self,
        *,
        label: str | None = "millstone-opportunity",
        bd_path: str | None = None,
        cwd: Path | None = None,
        cli: _BeadsCLI | None = None,
    ) -> None:
        self._label = label
        self._cli = cli or _BeadsCLI(bd_path=bd_path, cwd=cwd)

    @classmethod
    def from_config(cls, options: dict[str, Any]) -> BeadsOpportunityProvider:
        return cls(
            label=options.get("label", "millstone-opportunity"),
            bd_path=options.get("bd_path"),
            cwd=Path(options["cwd"]) if options.get("cwd") else None,
        )

    # -- read --------------------------------------------------------------

    def list_opportunities(self) -> list[Opportunity]:
        args = ["list"]
        if self._label:
            args.extend(["--label", self._label])
        payload = self._cli.run_json(args)
        return [self._to_opp(d) for d in _coerce_items(payload)]

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        try:
            payload = self._cli.run_json(["show", opportunity_id])
        except BeadsCLIError:
            return None
        items = _coerce_items(payload)
        return self._to_opp(items[0]) if items else None

    @staticmethod
    def _to_opp(d: dict) -> Opportunity:
        raw_status = str(d.get("status") or "open").lower().strip()
        status = _BD_TO_OPP_STATUS.get(raw_status, OpportunityStatus.identified)
        description = str(d.get("description") or d.get("body") or "").strip() or "(no description)"
        return Opportunity(
            opportunity_id=str(d["id"]),
            title=str(d.get("title") or d.get("summary") or ""),
            status=status,
            description=description,
            raw=description,
        )

    # -- write -------------------------------------------------------------

    def write_opportunity(self, opportunity: Opportunity) -> None:
        opportunity.validate()
        # Beads assigns its own ID; replace the caller's slug with it.
        args = ["create", opportunity.title, "-t", "task"]
        if self._label:
            args.extend(["-l", self._label])
        if opportunity.description:
            args.extend(["-d", opportunity.description])
        if opportunity.priority:
            args.extend(["-p", str(opportunity.priority)])
        payload = self._cli.run_json(args)
        items = _coerce_items(payload)
        if not items or "id" not in items[0]:
            raise BeadsCLIError(
                f"bd create did not return an id for opportunity {opportunity.title!r}"
            )
        opportunity.opportunity_id = str(items[0]["id"])

    def update_opportunity_status(self, opportunity_id: str, status: OpportunityStatus) -> None:
        bd_status = _OPP_STATUS_TO_BD.get(status)
        if bd_status is None:
            raise ValueError(f"unsupported opportunity status for beads: {status}")
        if status == OpportunityStatus.rejected:
            self._cli.run(["close", opportunity_id])
        else:
            self._cli.run(["update", opportunity_id, "--status", bd_status])

    # -- DependencyLinker capability --------------------------------------

    def link(self, from_id: str, to_id: str, kind: DependencyKind) -> None:
        self._cli.run(["dep", "add", from_id, to_id, "--type", kind.value])

    # -- prompt placeholders ----------------------------------------------

    def get_prompt_placeholders(self) -> dict[str, str]:
        label_clause = f" -l {self._label}" if self._label else ""
        return {
            "OPPORTUNITY_READ_INSTRUCTIONS": (
                f"List opportunities with `bd list{label_clause} --json`. "
                "Use `bd show <id>` for full details."
            ),
            "OPPORTUNITY_WRITE_INSTRUCTIONS": (
                f'Create opportunities with `bd create "<title>" -t task{label_clause} '
                '-d "<description>" -p <priority>`.'
            ),
        }


# Self-register at import time.
register_tasklist_provider_class("beads", BeadsTasklistProvider)
register_opportunity_provider_class("beads", BeadsOpportunityProvider)
