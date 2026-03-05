"""Tests for LoopRegistryAdapter over the canonical loops registry."""

import pytest

from millstone.loops.registry.loops import DEV_REVIEW_LOOP
from millstone.loops.registry_adapter import ARTIFACT_TYPE_TO_MODEL, LoopRegistryAdapter
from millstone.loops.types import ArtifactType


def test_get_loop_returns_dev_review_loop() -> None:
    adapter = LoopRegistryAdapter()
    assert adapter.get_loop("dev.review") is DEV_REVIEW_LOOP


def test_get_loop_raises_for_unknown_loop_id() -> None:
    adapter = LoopRegistryAdapter()
    with pytest.raises(KeyError):
        adapter.get_loop("nonexistent")


def test_get_capability_tier_returns_dev_review_tier() -> None:
    adapter = LoopRegistryAdapter()
    assert adapter.get_capability_tier("dev.review") == "C1_local_write"


def test_validate_role_id_for_known_and_unknown_roles() -> None:
    adapter = LoopRegistryAdapter()
    assert adapter.validate_role_id("dev.review", "author") is True
    assert adapter.validate_role_id("dev.review", "reviewer") is True
    assert adapter.validate_role_id("dev.review", "unknown_role") is False


def test_get_role_returns_none_for_unknown_role() -> None:
    adapter = LoopRegistryAdapter()
    assert adapter.get_role("dev.review", "unknown_role") is None


def test_get_checks_includes_loc_threshold_and_sensitive_files() -> None:
    adapter = LoopRegistryAdapter()
    check_ids = {check.id for check in adapter.get_checks("dev.review")}
    assert "loc_threshold" in check_ids
    assert "sensitive_files" in check_ids


def test_artifact_type_to_model_mapping_exact_entries() -> None:
    assert ARTIFACT_TYPE_TO_MODEL == {
        ArtifactType.TASKLIST: "TasklistItem",
        ArtifactType.DESIGN: "Design",
    }
