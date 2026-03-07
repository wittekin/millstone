"""Effect provider abstractions and policy gate controls."""

import datetime
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from millstone.policy.capability import CapabilityPolicyGate, CapabilityTier


class EffectClass(str, Enum):
    transactional = "transactional"
    operational = "operational"


EFFECT_CLASS_TIER: dict[EffectClass, CapabilityTier] = {
    EffectClass.transactional: CapabilityTier.C2_REMOTE_BOUNDED,
    EffectClass.operational: CapabilityTier.C3_REMOTE_CRITICAL,
}


class EffectStatus(str, Enum):
    applied = "applied"
    skipped = "skipped"
    failed = "failed"
    denied = "denied"


@dataclass
class EffectIntent:
    effect_class: EffectClass
    description: str
    idempotency_key: str | None = None
    rollback_plan: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EffectRecord:
    intent: EffectIntent
    status: EffectStatus
    timestamp: str
    result: Any | None = None
    error: str | None = None


@runtime_checkable
class EffectProvider(Protocol):
    def apply(self, intent: EffectIntent) -> EffectRecord: ...
    def observe(self, intent: EffectIntent) -> EffectRecord: ...
    def health_check(self) -> bool: ...


class NoOpEffectProvider:
    def apply(self, intent: EffectIntent) -> EffectRecord:
        return EffectRecord(
            intent=intent,
            status=EffectStatus.skipped,
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

    def observe(self, intent: EffectIntent) -> EffectRecord:
        return EffectRecord(
            intent=intent,
            status=EffectStatus.skipped,
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

    def health_check(self) -> bool:
        return True


class EffectNotAllowedError(RuntimeError):
    """Raised when an effect class is not allowlisted for the profile."""


class EffectContractError(ValueError):
    """Raised when required effect contract controls are missing."""


def _default_approval_hook(intent: EffectIntent) -> bool:
    print(f"Effect class: {intent.effect_class.value}")
    print(f"Description: {intent.description}")
    print(f"Idempotency key: {intent.idempotency_key}")
    print(f"Rollback plan: {intent.rollback_plan}")
    decision = input("Approve this effect? [y/N]: ")
    return decision.strip().lower() == "y"


class EffectPolicyGate:
    _C2_INDEX = 2
    _C3_INDEX = 3
    _TIER_ORDER = [
        CapabilityTier.C0_READ_ONLY,
        CapabilityTier.C1_LOCAL_WRITE,
        CapabilityTier.C2_REMOTE_BOUNDED,
        CapabilityTier.C3_REMOTE_CRITICAL,
    ]

    def __init__(
        self,
        capability_gate: CapabilityPolicyGate,
        permitted_effect_classes: frozenset[EffectClass],
        provider: EffectProvider,
        approval_hook: Callable[[EffectIntent], bool] | None = None,
    ) -> None:
        self._gate = capability_gate
        self._permitted_effect_classes = permitted_effect_classes
        self._provider = provider
        self._approval_hook = approval_hook or _default_approval_hook

    def apply(self, intent: EffectIntent) -> EffectRecord:
        required_tier = EFFECT_CLASS_TIER[intent.effect_class]
        self._gate.assert_permitted(required_tier)
        self._enforce_allowlist(intent, required_tier)
        self._enforce_contract(intent, required_tier)

        if self._tier_index(required_tier) >= self._C3_INDEX and not self._approval_hook(intent):
            return EffectRecord(
                intent=intent,
                status=EffectStatus.denied,
                timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                error="Effect denied by approval hook",
            )

        return self._provider.apply(intent)

    def observe(self, intent: EffectIntent) -> EffectRecord:
        required_tier = EFFECT_CLASS_TIER[intent.effect_class]
        self._gate.assert_permitted(required_tier)
        self._enforce_allowlist(intent, required_tier)
        return self._provider.observe(intent)

    def health_check(self) -> bool:
        return self._provider.health_check()

    def _tier_index(self, tier: CapabilityTier) -> int:
        return self._TIER_ORDER.index(tier)

    def _enforce_allowlist(
        self,
        intent: EffectIntent,
        required_tier: CapabilityTier,
    ) -> None:
        if (
            self._tier_index(required_tier) >= self._C2_INDEX
            and intent.effect_class not in self._permitted_effect_classes
        ):
            raise EffectNotAllowedError(
                f"Effect class {intent.effect_class.value} is not in permitted_effect_classes"
            )

    def _enforce_contract(
        self,
        intent: EffectIntent,
        required_tier: CapabilityTier,
    ) -> None:
        if (
            self._tier_index(required_tier) >= self._C2_INDEX
            and not intent.idempotency_key
            and not intent.rollback_plan
        ):
            raise EffectContractError("Effect intent must include idempotency_key or rollback_plan")
