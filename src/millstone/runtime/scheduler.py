from __future__ import annotations

from collections import deque
from typing import Any


class TaskScheduler:
    """Task scheduler that dispatches non-overlapping work under DAG constraints."""

    def __init__(self, concurrency: int, high_risk_concurrency: int):
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if high_risk_concurrency < 1:
            raise ValueError("high_risk_concurrency must be >= 1")

        self.concurrency = concurrency
        self.high_risk_concurrency = high_risk_concurrency

        self._tasks_by_id: dict[str, dict[str, Any]] = {}
        self._task_order: list[str] = []
        self._dependencies: dict[str, set[str]] = {}
        self._dependents: dict[str, set[str]] = {}
        self._status: dict[str, str] = {}
        self._failure_reasons: dict[str, str] = {}
        self._groups: dict[str, str | None] = {}
        self._file_refs: dict[str, set[str]] = {}

    def build_graph(self, tasks: list[dict], dependencies: list[dict]) -> None:
        self._tasks_by_id = {}
        self._task_order = []
        self._dependencies = {}
        self._dependents = {}
        self._status = {}
        self._failure_reasons = {}
        self._groups = {}
        self._file_refs = {}

        for task in tasks:
            task_id = task.get("task_id")
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("Each task must include a non-empty task_id")
            if task_id in self._tasks_by_id:
                raise ValueError(f"Duplicate task_id: {task_id}")

            group = task.get("group")
            group_value = group.strip() if isinstance(group, str) else None
            if not group_value:
                group_value = None

            file_refs = self._normalize_file_refs(task.get("file_refs"))
            risk = task.get("risk")
            risk_value = risk.strip().lower() if isinstance(risk, str) and risk.strip() else None

            normalized = dict(task)
            normalized["group"] = group_value
            normalized["file_refs"] = list(file_refs)
            normalized["risk"] = risk_value

            self._tasks_by_id[task_id] = normalized
            self._task_order.append(task_id)
            self._dependencies[task_id] = set()
            self._dependents[task_id] = set()
            self._status[task_id] = "pending"
            self._groups[task_id] = group_value
            self._file_refs[task_id] = set(file_refs)

        for dep in dependencies:
            from_id = dep.get("from_id")
            to_id = dep.get("to_id")

            if from_id not in self._tasks_by_id or to_id not in self._tasks_by_id:
                raise ValueError(
                    f"Dependency references unknown task IDs: from_id={from_id!r}, to_id={to_id!r}"
                )

            self._dependencies[to_id].add(from_id)
            self._dependents[from_id].add(to_id)

        self._validate_acyclic()

    def has_remaining(self) -> bool:
        return any(status == "pending" for status in self._status.values())

    def next_available(self, in_flight: set[str], completed: set[str]) -> list[str]:
        available_slots = self.concurrency - len(in_flight)
        if available_slots <= 0:
            return []

        high_risk_in_flight = sum(1 for task_id in in_flight if self._is_high_risk(task_id))
        high_risk_slots = self.high_risk_concurrency - high_risk_in_flight
        if high_risk_slots < 0:
            high_risk_slots = 0

        selected: list[str] = []
        selected_set: set[str] = set()
        selected_high_risk = 0

        for task_id in self._task_order:
            if len(selected) >= available_slots:
                break
            if self._status.get(task_id) != "pending":
                continue
            if task_id in in_flight:
                continue
            if not self._dependencies[task_id].issubset(completed):
                continue
            if self._overlaps_any(task_id, in_flight):
                continue
            if self._overlaps_any(task_id, selected_set):
                continue
            if self._is_high_risk(task_id) and selected_high_risk >= high_risk_slots:
                continue

            selected.append(task_id)
            selected_set.add(task_id)
            if self._is_high_risk(task_id):
                selected_high_risk += 1

        return selected

    def get_task(self, task_id: str) -> dict:
        return self._tasks_by_id[task_id]

    def get_remaining_task_ids(self) -> list[str]:
        return [task_id for task_id in self._task_order if self._status.get(task_id) == "pending"]

    def mark_completed(self, task_id: str) -> None:
        if task_id not in self._status:
            raise KeyError(task_id)
        self._status[task_id] = "completed"
        self._failure_reasons.pop(task_id, None)

    def mark_failed(self, task_id: str, reason: str) -> None:
        if task_id not in self._status:
            raise KeyError(task_id)
        self._status[task_id] = "failed"
        self._failure_reasons[task_id] = reason

    @staticmethod
    def _normalize_file_refs(file_refs: Any) -> list[str]:
        if file_refs is None:
            return []
        if isinstance(file_refs, str):
            refs = [file_refs]
        elif isinstance(file_refs, (list, tuple, set)):
            refs = list(file_refs)
        else:
            return []

        normalized: list[str] = []
        seen: set[str] = set()
        for ref in refs:
            ref_text = str(ref).strip()
            if not ref_text or ref_text in seen:
                continue
            normalized.append(ref_text)
            seen.add(ref_text)
        return normalized

    def _is_high_risk(self, task_id: str) -> bool:
        task = self._tasks_by_id.get(task_id)
        if not task:
            return False
        return task.get("risk") == "high"

    def _overlaps_any(self, task_id: str, other_task_ids: set[str]) -> bool:
        for other_id in other_task_ids:
            if other_id not in self._tasks_by_id:
                continue
            if self._tasks_overlap(task_id, other_id):
                return True
        return False

    def _tasks_overlap(self, left_id: str, right_id: str) -> bool:
        if left_id == right_id:
            return True

        left_refs = self._file_refs.get(left_id, set())
        right_refs = self._file_refs.get(right_id, set())
        return bool(left_refs & right_refs)

    def _validate_acyclic(self) -> None:
        indegree = {task_id: len(deps) for task_id, deps in self._dependencies.items()}
        queue = deque(task_id for task_id in self._task_order if indegree[task_id] == 0)
        visited = 0

        while queue:
            current = queue.popleft()
            visited += 1
            for dependent in self._dependents[current]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    queue.append(dependent)

        if visited != len(self._task_order):
            cycle_nodes = [task_id for task_id in self._task_order if indegree[task_id] > 0]
            preview = ", ".join(cycle_nodes[:5])
            raise ValueError(f"Dependency cycle detected involving task IDs: {preview}")
