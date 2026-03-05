"""Tests for role IDs in loop registry definitions."""

from millstone.loops.registry.loops import DEV_REVIEW_LOOP
from millstone.loops.validation import validate_role_references


def test_dev_review_loop_uses_author_role_id() -> None:
    """DEV_REVIEW_LOOP should use 'author' as the canonical role id."""
    role_ids = {role.id for role in DEV_REVIEW_LOOP.roles}
    assert "author" in role_ids
    assert "builder" not in role_ids

    action_role_ids = {
        action.role_id
        for action in DEV_REVIEW_LOOP.state_actions
        if action.state in {"build", "check", "fix", "commit"}
    }
    assert action_role_ids == {"author"}


def test_validate_role_references_has_no_dev_review_errors() -> None:
    """Role-reference validation should pass for the dev.review loop."""
    errors = [error for error in validate_role_references() if error.entity_id == "dev.review"]
    assert errors == []
