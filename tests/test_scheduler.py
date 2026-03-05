from __future__ import annotations

import pytest

from millstone.runtime.scheduler import TaskScheduler


def _task(
    task_id: str,
    *,
    group: str | None = None,
    file_refs: list[str] | None = None,
    risk: str | None = "low",
) -> dict:
    return {
        "task_id": task_id,
        "title": f"Task {task_id}",
        "group": group,
        "file_refs": file_refs or [],
        "risk": risk,
        "raw_text": f"- [ ] Task {task_id}",
    }


def test_dag_dependency_order():
    scheduler = TaskScheduler(concurrency=2, high_risk_concurrency=1)
    scheduler.build_graph(
        tasks=[_task("a"), _task("b")],
        dependencies=[{"from_id": "a", "to_id": "b", "reason": "dep", "type": "heuristic"}],
    )

    assert scheduler.get_task("a")["task_id"] == "a"
    assert scheduler.next_available(set(), set()) == ["a"]

    scheduler.mark_completed("a")
    assert scheduler.next_available(set(), {"a"}) == ["b"]


def test_same_group_does_not_imply_overlap():
    scheduler = TaskScheduler(concurrency=2, high_risk_concurrency=2)
    scheduler.build_graph(
        tasks=[_task("a", group="core"), _task("b", group="core")],
        dependencies=[],
    )

    assert scheduler.next_available(set(), set()) == ["a", "b"]
    assert scheduler.next_available({"a"}, set()) == ["b"]

    scheduler.mark_completed("a")
    assert scheduler.next_available(set(), {"a"}) == ["b"]


def test_overlap_skips_to_later_non_conflicting_task():
    scheduler = TaskScheduler(concurrency=2, high_risk_concurrency=2)
    scheduler.build_graph(
        tasks=[
            _task("a", file_refs=["shared.py"]),
            _task("b", file_refs=["shared.py"]),
            _task("c", file_refs=["independent.py"]),
        ],
        dependencies=[],
    )

    assert scheduler.next_available(set(), set()) == ["a", "c"]
    assert scheduler.next_available({"a"}, set()) == ["c"]


def test_overlap_avoidance_file_refs():
    scheduler = TaskScheduler(concurrency=2, high_risk_concurrency=2)
    scheduler.build_graph(
        tasks=[
            _task("a", file_refs=["millstone/parallel.py"]),
            _task("b", file_refs=["millstone/parallel.py"]),
        ],
        dependencies=[],
    )

    assert scheduler.next_available(set(), set()) == ["a"]
    assert scheduler.next_available({"a"}, set()) == []

    scheduler.mark_completed("a")
    assert scheduler.next_available(set(), {"a"}) == ["b"]


def test_concurrency_cap():
    scheduler = TaskScheduler(concurrency=2, high_risk_concurrency=2)
    scheduler.build_graph(
        tasks=[_task("a"), _task("b"), _task("c")],
        dependencies=[],
    )

    assert scheduler.next_available(set(), set()) == ["a", "b"]
    assert scheduler.next_available({"a"}, set()) == ["b"]


def test_high_risk_concurrency_cap():
    scheduler = TaskScheduler(concurrency=3, high_risk_concurrency=1)
    scheduler.build_graph(
        tasks=[_task("h1", risk="high"), _task("h2", risk="high"), _task("l1", risk="low")],
        dependencies=[],
    )

    assert scheduler.next_available(set(), set()) == ["h1", "l1"]
    assert scheduler.next_available({"h1", "l1"}, set()) == []

    scheduler.mark_completed("h1")
    scheduler.mark_completed("l1")
    assert scheduler.next_available(set(), {"h1", "l1"}) == ["h2"]


def test_has_remaining_false_when_done():
    scheduler = TaskScheduler(concurrency=2, high_risk_concurrency=1)
    scheduler.build_graph(tasks=[_task("a"), _task("b")], dependencies=[])

    assert scheduler.has_remaining() is True

    scheduler.mark_completed("a")
    assert scheduler.has_remaining() is True

    scheduler.mark_failed("b", "boom")
    assert scheduler.has_remaining() is False


def test_failed_blocks_dependents():
    scheduler = TaskScheduler(concurrency=2, high_risk_concurrency=1)
    scheduler.build_graph(
        tasks=[_task("a"), _task("b")],
        dependencies=[{"from_id": "a", "to_id": "b", "reason": "dep", "type": "heuristic"}],
    )

    scheduler.mark_failed("a", "boom")
    assert scheduler.next_available(set(), set()) == []
    assert scheduler.has_remaining() is True
    assert scheduler.get_remaining_task_ids() == ["b"]


def test_cycle_detection_raises():
    scheduler = TaskScheduler(concurrency=2, high_risk_concurrency=1)

    with pytest.raises(ValueError, match="cycle"):
        scheduler.build_graph(
            tasks=[_task("a"), _task("b")],
            dependencies=[
                {"from_id": "a", "to_id": "b", "reason": "dep", "type": "heuristic"},
                {"from_id": "b", "to_id": "a", "reason": "dep", "type": "heuristic"},
            ],
        )


def test_get_remaining_excludes_terminal():
    scheduler = TaskScheduler(concurrency=2, high_risk_concurrency=1)
    scheduler.build_graph(tasks=[_task("a"), _task("b"), _task("c")], dependencies=[])

    scheduler.mark_completed("a")
    scheduler.mark_failed("b", "boom")

    assert scheduler.get_remaining_task_ids() == ["c"]
