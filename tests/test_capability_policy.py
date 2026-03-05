"""Tests for capability-tier policy gate and tier requirement mappings."""

from dataclasses import FrozenInstanceError

import pytest

from millstone.policy.capability import (
    TIER_REQUIREMENTS,
    CapabilityPolicyGate,
    CapabilityTier,
    CapabilityViolation,
    TierRequirements,
)


@pytest.mark.parametrize("gate_tier", list(CapabilityTier))
@pytest.mark.parametrize("requested_tier", list(CapabilityTier))
def test_assert_permitted_all_tier_combinations(
    gate_tier: CapabilityTier, requested_tier: CapabilityTier
) -> None:
    gate = CapabilityPolicyGate(gate_tier)

    if list(CapabilityTier).index(requested_tier) <= list(CapabilityTier).index(gate_tier):
        gate.assert_permitted(requested_tier)
    else:
        with pytest.raises(CapabilityViolation):
            gate.assert_permitted(requested_tier)


def test_assert_permitted_violation_message_includes_both_tier_values() -> None:
    gate = CapabilityPolicyGate(CapabilityTier.C1_LOCAL_WRITE)

    with pytest.raises(CapabilityViolation) as exc_info:
        gate.assert_permitted(CapabilityTier.C3_REMOTE_CRITICAL)

    message = str(exc_info.value)
    assert CapabilityTier.C1_LOCAL_WRITE.value in message
    assert CapabilityTier.C3_REMOTE_CRITICAL.value in message


@pytest.mark.parametrize("tier", list(CapabilityTier))
def test_profile_tier_property_returns_construction_tier(tier: CapabilityTier) -> None:
    gate = CapabilityPolicyGate(tier)
    assert gate.profile_tier is tier


@pytest.mark.parametrize("tier", list(CapabilityTier))
def test_tier_requirements_property_returns_requirements_for_profile_tier(
    tier: CapabilityTier,
) -> None:
    gate = CapabilityPolicyGate(tier)
    assert gate.tier_requirements == TIER_REQUIREMENTS[tier]


def test_tier_requirements_has_entry_for_every_tier() -> None:
    assert set(TIER_REQUIREMENTS) == set(CapabilityTier)


def test_tier_requirements_dataclass_is_frozen() -> None:
    requirements = TierRequirements(
        requires_audit_log=True,
        requires_mechanical_checks=False,
        requires_reviewer_approval=False,
        requires_allowlist=False,
        requires_idempotency_or_rollback=False,
        requires_human_approval_gate=False,
        requires_health_checks=False,
    )

    with pytest.raises(FrozenInstanceError):
        requirements.requires_audit_log = False
