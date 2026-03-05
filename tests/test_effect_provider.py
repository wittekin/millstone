"""Tests for effect provider abstraction and policy gate enforcement."""

import pytest

from millstone.policy.capability import (
    CapabilityPolicyGate,
    CapabilityTier,
    CapabilityViolation,
)
from millstone.policy.effects import (
    EffectClass,
    EffectContractError,
    EffectIntent,
    EffectNotAllowedError,
    EffectPolicyGate,
    EffectProvider,
    EffectStatus,
    NoOpEffectProvider,
)


@pytest.mark.parametrize("effect_class", [EffectClass.transactional, EffectClass.operational])
def test_noop_provider_is_runtime_checkable_and_returns_skipped(effect_class: EffectClass) -> None:
    provider = NoOpEffectProvider()
    intent = EffectIntent(effect_class=effect_class, description="test")

    assert isinstance(provider, EffectProvider)
    assert provider.apply(intent).status == EffectStatus.skipped


def test_noop_provider_health_check_returns_true() -> None:
    assert NoOpEffectProvider().health_check() is True


@pytest.mark.parametrize("effect_class", [EffectClass.transactional, EffectClass.operational])
def test_effect_policy_gate_with_c1_profile_rejects_c2_and_c3_effects(
    effect_class: EffectClass,
) -> None:
    gate = EffectPolicyGate(
        capability_gate=CapabilityPolicyGate(CapabilityTier.C1_LOCAL_WRITE),
        permitted_effect_classes=frozenset({effect_class}),
        provider=NoOpEffectProvider(),
    )

    with pytest.raises(CapabilityViolation):
        gate.apply(EffectIntent(effect_class=effect_class, description="blocked"))


def test_effect_policy_gate_c2_rejects_non_allowlisted_effect() -> None:
    gate = EffectPolicyGate(
        capability_gate=CapabilityPolicyGate(CapabilityTier.C2_REMOTE_BOUNDED),
        permitted_effect_classes=frozenset(),
        provider=NoOpEffectProvider(),
    )

    with pytest.raises(EffectNotAllowedError):
        gate.apply(
            EffectIntent(effect_class=EffectClass.transactional, description="txn")
        )


def test_effect_policy_gate_c2_rejects_missing_idempotency_and_rollback() -> None:
    gate = EffectPolicyGate(
        capability_gate=CapabilityPolicyGate(CapabilityTier.C2_REMOTE_BOUNDED),
        permitted_effect_classes=frozenset({EffectClass.transactional}),
        provider=NoOpEffectProvider(),
    )

    with pytest.raises(EffectContractError):
        gate.apply(
            EffectIntent(effect_class=EffectClass.transactional, description="txn")
        )


def test_effect_policy_gate_c2_accepts_idempotency_key() -> None:
    gate = EffectPolicyGate(
        capability_gate=CapabilityPolicyGate(CapabilityTier.C2_REMOTE_BOUNDED),
        permitted_effect_classes=frozenset({EffectClass.transactional}),
        provider=NoOpEffectProvider(),
    )

    record = gate.apply(
        EffectIntent(
            effect_class=EffectClass.transactional,
            description="txn",
            idempotency_key="k",
        )
    )

    assert record.status == EffectStatus.skipped


def test_effect_policy_gate_c2_accepts_rollback_plan_without_idempotency_key() -> None:
    gate = EffectPolicyGate(
        capability_gate=CapabilityPolicyGate(CapabilityTier.C2_REMOTE_BOUNDED),
        permitted_effect_classes=frozenset({EffectClass.transactional}),
        provider=NoOpEffectProvider(),
    )

    record = gate.apply(
        EffectIntent(
            effect_class=EffectClass.transactional,
            description="txn",
            rollback_plan="revert",
        )
    )

    assert record.status == EffectStatus.skipped


def test_effect_policy_gate_c3_denies_when_approval_hook_rejects() -> None:
    gate = EffectPolicyGate(
        capability_gate=CapabilityPolicyGate(CapabilityTier.C3_REMOTE_CRITICAL),
        permitted_effect_classes=frozenset({EffectClass.operational}),
        provider=NoOpEffectProvider(),
        approval_hook=lambda _: False,
    )

    record = gate.apply(
        EffectIntent(
            effect_class=EffectClass.operational,
            description="ops",
            idempotency_key="k",
        )
    )

    assert record.status == EffectStatus.denied


def test_effect_policy_gate_c3_approval_hook_true_delegates_to_provider() -> None:
    gate = EffectPolicyGate(
        capability_gate=CapabilityPolicyGate(CapabilityTier.C3_REMOTE_CRITICAL),
        permitted_effect_classes=frozenset({EffectClass.operational}),
        provider=NoOpEffectProvider(),
        approval_hook=lambda _: True,
    )

    record = gate.apply(
        EffectIntent(
            effect_class=EffectClass.operational,
            description="ops",
            idempotency_key="k",
        )
    )

    assert record.status == EffectStatus.skipped


def test_observe_enforces_tier_and_allowlist_but_skips_approval_hook() -> None:
    def raising_hook(_: EffectIntent) -> bool:
        raise AssertionError("approval hook must not be called by observe")

    gate = EffectPolicyGate(
        capability_gate=CapabilityPolicyGate(CapabilityTier.C2_REMOTE_BOUNDED),
        permitted_effect_classes=frozenset({EffectClass.transactional}),
        provider=NoOpEffectProvider(),
        approval_hook=raising_hook,
    )

    record = gate.observe(
        EffectIntent(effect_class=EffectClass.transactional, description="observe")
    )
    assert record.status == EffectStatus.skipped

    deny_gate = EffectPolicyGate(
        capability_gate=CapabilityPolicyGate(CapabilityTier.C2_REMOTE_BOUNDED),
        permitted_effect_classes=frozenset(),
        provider=NoOpEffectProvider(),
        approval_hook=raising_hook,
    )

    with pytest.raises(EffectNotAllowedError):
        deny_gate.observe(
            EffectIntent(effect_class=EffectClass.transactional, description="observe")
        )
