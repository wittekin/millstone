"""Capability-tier policy definitions and enforcement gate."""

from dataclasses import dataclass
from enum import Enum


class CapabilityTier(str, Enum):
    C0_READ_ONLY = "C0_read_only"
    C1_LOCAL_WRITE = "C1_local_write"
    C2_REMOTE_BOUNDED = "C2_remote_bounded"
    C3_REMOTE_CRITICAL = "C3_remote_critical"


@dataclass(frozen=True)
class TierRequirements:
    requires_audit_log: bool
    requires_mechanical_checks: bool
    requires_reviewer_approval: bool
    requires_allowlist: bool
    requires_idempotency_or_rollback: bool
    requires_human_approval_gate: bool
    requires_health_checks: bool


TIER_REQUIREMENTS: dict[CapabilityTier, TierRequirements] = {
    CapabilityTier.C0_READ_ONLY: TierRequirements(
        requires_audit_log=True,
        requires_mechanical_checks=False,
        requires_reviewer_approval=False,
        requires_allowlist=False,
        requires_idempotency_or_rollback=False,
        requires_human_approval_gate=False,
        requires_health_checks=False,
    ),
    CapabilityTier.C1_LOCAL_WRITE: TierRequirements(
        requires_audit_log=True,
        requires_mechanical_checks=True,
        requires_reviewer_approval=True,
        requires_allowlist=False,
        requires_idempotency_or_rollback=False,
        requires_human_approval_gate=False,
        requires_health_checks=False,
    ),
    CapabilityTier.C2_REMOTE_BOUNDED: TierRequirements(
        requires_audit_log=True,
        requires_mechanical_checks=True,
        requires_reviewer_approval=True,
        requires_allowlist=True,
        requires_idempotency_or_rollback=True,
        requires_human_approval_gate=False,
        requires_health_checks=False,
    ),
    CapabilityTier.C3_REMOTE_CRITICAL: TierRequirements(
        requires_audit_log=True,
        requires_mechanical_checks=True,
        requires_reviewer_approval=True,
        requires_allowlist=True,
        requires_idempotency_or_rollback=True,
        requires_human_approval_gate=True,
        requires_health_checks=True,
    ),
}


_TIER_ORDER: list[CapabilityTier] = [
    CapabilityTier.C0_READ_ONLY,
    CapabilityTier.C1_LOCAL_WRITE,
    CapabilityTier.C2_REMOTE_BOUNDED,
    CapabilityTier.C3_REMOTE_CRITICAL,
]


class CapabilityViolation(RuntimeError):
    """Raised when a requested capability tier exceeds the profile tier."""


class CapabilityPolicyGate:
    def __init__(self, profile_tier: CapabilityTier) -> None:
        self._profile_tier = profile_tier

    def assert_permitted(self, requested_tier: CapabilityTier) -> None:
        if _TIER_ORDER.index(requested_tier) > _TIER_ORDER.index(self._profile_tier):
            raise CapabilityViolation(
                "Requested tier "
                f"{requested_tier.value} exceeds profile tier {self._profile_tier.value}"
            )

    @property
    def profile_tier(self) -> CapabilityTier:
        return self._profile_tier

    @property
    def tier_requirements(self) -> TierRequirements:
        return TIER_REQUIREMENTS[self._profile_tier]
