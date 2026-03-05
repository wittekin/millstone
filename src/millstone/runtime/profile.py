"""Profiles and profile registry for role aliasing and capability declarations."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from millstone.policy.capability import CapabilityTier
from millstone.policy.effects import EffectClass


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    role_aliases: Mapping[str, str]
    capability_tier: CapabilityTier = CapabilityTier.C1_LOCAL_WRITE
    artifact_contracts: tuple[str, ...] = ()
    permitted_effect_classes: frozenset[EffectClass] = field(default_factory=frozenset)
    default_providers: Mapping[str, str] = field(default_factory=dict)
    loop_id: str | None = None

    def __post_init__(self) -> None:
        # Freeze mapping fields deeply enough for safe shared use by copying
        # caller-provided mappings and exposing them as read-only views.
        object.__setattr__(self, "role_aliases", MappingProxyType(dict(self.role_aliases)))
        object.__setattr__(
            self,
            "default_providers",
            MappingProxyType(dict(self.default_providers)),
        )

    def resolve_role(self, role: str) -> str:
        return self.role_aliases.get(role, role)


DEV_IMPLEMENTATION = Profile(
    id="dev_implementation",
    name="Development Implementation",
    role_aliases={"builder": "author"},
    capability_tier=CapabilityTier.C1_LOCAL_WRITE,
    artifact_contracts=("opportunity", "design", "tasklist_item"),
    permitted_effect_classes=frozenset(),
    default_providers={
        "opportunities": "file",
        "designs": "file",
        "tasklist": "file",
    },
    loop_id="dev.review",
)


_BUILT_IN_PROFILES: dict[str, Profile] = {DEV_IMPLEMENTATION.id: DEV_IMPLEMENTATION}


class ProfileRegistry:
    def __init__(self, extra: dict[str, Profile] | None = None) -> None:
        self._profiles: dict[str, Profile] = dict(_BUILT_IN_PROFILES)
        if extra:
            self._profiles.update(extra)

    def get(self, profile_id: str) -> Profile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            available = ", ".join(self.profile_ids)
            raise KeyError(
                f"Unknown profile_id {profile_id!r}. Available profiles: {available}"
            ) from exc

    def register(self, profile: Profile) -> None:
        self._profiles[profile.id] = profile

    @property
    def profile_ids(self) -> list[str]:
        return sorted(self._profiles)
