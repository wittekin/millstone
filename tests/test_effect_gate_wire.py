"""Tests for effect-gate plumbing in outer loop and orchestrator wiring."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from millstone.loops.outer import OuterLoopManager
from millstone.policy.effects import (
    EffectClass,
    EffectIntent,
    EffectNotAllowedError,
    EffectRecord,
    EffectStatus,
)
from millstone.runtime.orchestrator import Orchestrator


def _make_outer_manager(repo_dir: Path, **kwargs) -> OuterLoopManager:
    work_dir = repo_dir / ".millstone"
    work_dir.mkdir(exist_ok=True)
    return OuterLoopManager(
        work_dir=work_dir,
        repo_dir=repo_dir,
        tasklist="docs/tasklist.md",
        task_constraints={
            "max_loc": 200,
            "require_tests": False,
            "require_criteria": False,
            "require_risk": False,
            "require_context": False,
            "max_split_attempts": 0,
        },
        **kwargs,
    )


def test_apply_provider_effect_returns_skipped_record_without_gate(temp_repo):
    manager = _make_outer_manager(temp_repo)
    intent = EffectIntent(
        effect_class=EffectClass.transactional,
        description="update design via remote backend",
    )

    record = manager._apply_provider_effect(intent)

    assert isinstance(record, EffectRecord)
    assert record.status == EffectStatus.skipped
    assert record.intent is intent


def test_apply_provider_effect_delegates_to_gate_apply(temp_repo):
    intent = EffectIntent(
        effect_class=EffectClass.transactional,
        description="update design via remote backend",
    )
    expected = EffectRecord(intent=intent, status=EffectStatus.applied, timestamp="ts")
    mock_gate = MagicMock()
    mock_gate.apply.return_value = expected
    manager = _make_outer_manager(temp_repo, effect_gate=mock_gate)

    result = manager._apply_provider_effect(intent)

    mock_gate.apply.assert_called_once_with(intent)
    assert result is expected


def test_apply_provider_effect_propagates_effect_not_allowed_error(temp_repo):
    intent = EffectIntent(
        effect_class=EffectClass.transactional,
        description="update design via remote backend",
    )
    mock_gate = MagicMock()
    mock_gate.apply.side_effect = EffectNotAllowedError("blocked")
    manager = _make_outer_manager(temp_repo, effect_gate=mock_gate)

    with pytest.raises(EffectNotAllowedError, match="blocked"):
        manager._apply_provider_effect(intent)


def test_orchestrator_forwards_effect_gate_to_outer_loop_manager(temp_repo):
    with patch("millstone.runtime.orchestrator.OuterLoopManager") as mock_outer_loop_manager:
        mock_outer_loop_manager.return_value = MagicMock()

        orch = Orchestrator(task="wire effect gate", quiet=True)
        try:
            _, kwargs = mock_outer_loop_manager.call_args
            assert kwargs["effect_gate"] is orch._effect_gate
        finally:
            orch.cleanup()
