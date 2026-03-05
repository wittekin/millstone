"""Tests for profile registry and built-in development profile."""

from dataclasses import FrozenInstanceError

import pytest

from millstone.policy.capability import CapabilityTier
from millstone.policy.effects import EffectClass
from millstone.runtime.profile import DEV_IMPLEMENTATION, Profile, ProfileRegistry


def test_profile_is_frozen():
    profile = Profile(id="p1", name="Profile One", role_aliases={})
    with pytest.raises(FrozenInstanceError):
        profile.id = "p2"


def test_profile_mappings_are_immutable():
    profile = Profile(
        id="p1",
        name="Profile One",
        role_aliases={"builder": "author"},
        default_providers={"tasklist": "file"},
    )
    with pytest.raises(TypeError):
        profile.role_aliases["builder"] = "editor"
    with pytest.raises(TypeError):
        profile.default_providers["tasklist"] = "db"


def test_profile_copies_input_mappings_to_prevent_external_mutation():
    role_aliases = {"builder": "author"}
    default_providers = {"tasklist": "file"}
    profile = Profile(
        id="p1",
        name="Profile One",
        role_aliases=role_aliases,
        default_providers=default_providers,
    )

    role_aliases["builder"] = "editor"
    default_providers["tasklist"] = "db"

    assert profile.resolve_role("builder") == "author"
    assert profile.default_providers["tasklist"] == "file"


def test_dev_implementation_builder_alias_resolves_to_author():
    assert DEV_IMPLEMENTATION.resolve_role("builder") == "author"


def test_dev_implementation_loop_id_is_dev_review():
    assert DEV_IMPLEMENTATION.loop_id == "dev.review"


def test_profile_loop_id_defaults_to_none():
    profile = Profile(id="p1", name="Profile One", role_aliases={})
    assert profile.loop_id is None


def test_profile_remains_frozen_and_defines_hash():
    profile = Profile(id="p1", name="Profile One", role_aliases={})
    with pytest.raises(FrozenInstanceError):
        profile.loop_id = "dev.review"
    assert callable(Profile.__hash__)


def test_resolve_role_passthrough_for_unknown_role():
    assert DEV_IMPLEMENTATION.resolve_role("unknown") == "unknown"


def test_registry_get_returns_dev_implementation():
    registry = ProfileRegistry()
    assert registry.get("dev_implementation") is DEV_IMPLEMENTATION


def test_registry_get_unknown_raises_keyerror_with_available_ids():
    registry = ProfileRegistry()
    with pytest.raises(KeyError) as exc_info:
        registry.get("unknown")
    message = str(exc_info.value)
    assert "unknown" in message
    assert "dev_implementation" in message


def test_registry_register_custom_profile():
    registry = ProfileRegistry()
    custom = Profile(id="custom", name="Custom", role_aliases={})
    registry.register(custom)
    assert registry.get("custom") is custom


def test_registry_profile_ids_sorted():
    registry = ProfileRegistry()
    assert registry.profile_ids == ["dev_implementation"]


def test_dev_implementation_has_expected_role_aliases():
    assert DEV_IMPLEMENTATION.role_aliases == {"builder": "author"}


def test_profile_default_capability_tier_is_c1_local_write():
    profile = Profile(id="p1", name="Profile One", role_aliases={})
    assert profile.capability_tier == CapabilityTier.C1_LOCAL_WRITE


def test_dev_implementation_capability_tier_is_c1_local_write():
    assert DEV_IMPLEMENTATION.capability_tier == CapabilityTier.C1_LOCAL_WRITE


def test_profile_without_capability_tier_kwarg_defaults_to_c1():
    profile = Profile(id="p1", name="Profile One", role_aliases={})
    assert profile.capability_tier == CapabilityTier.C1_LOCAL_WRITE


def test_profile_with_capability_tier_kwarg_stores_value():
    profile = Profile(
        id="p1",
        name="Profile One",
        role_aliases={},
        capability_tier=CapabilityTier.C0_READ_ONLY,
    )
    assert profile.capability_tier == CapabilityTier.C0_READ_ONLY


def test_profile_default_permitted_effect_classes_is_empty_frozenset():
    profile = Profile(id="p1", name="Profile One", role_aliases={})
    assert profile.permitted_effect_classes == frozenset()


def test_dev_implementation_permitted_effect_classes_is_empty_frozenset():
    assert DEV_IMPLEMENTATION.permitted_effect_classes == frozenset()


def test_profile_without_permitted_effect_classes_kwarg_defaults_to_empty_frozenset():
    profile = Profile(id="p1", name="Profile One", role_aliases={})
    assert profile.permitted_effect_classes == frozenset()


def test_profile_with_permitted_effect_classes_kwarg_stores_value():
    profile = Profile(
        id="p1",
        name="Profile One",
        role_aliases={},
        permitted_effect_classes=frozenset({EffectClass.transactional}),
    )
    assert profile.permitted_effect_classes == frozenset({EffectClass.transactional})


def test_dev_implementation_permitted_effect_classes_is_frozenset():
    assert isinstance(DEV_IMPLEMENTATION.permitted_effect_classes, frozenset)
