"""Tests for configurable dangerous pattern policy.

Users can:
1. Add/remove patterns from the blocklist via policy.toml
2. Set per-pattern action: "block" (halt) or "flag" (raise to reviewer)
3. Set a default action for all patterns, override individually

Config examples:

    [dangerous]
    action = "block"  # default for all patterns
    patterns = [
        "rm -rf",                                    # uses default action
        {pattern = "DROP TABLE", action = "flag"},   # per-pattern override
    ]

Backward compat: `block = true` maps to action="block", `block = false` to action="flag".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from millstone.config import normalize_dangerous_patterns
from millstone.loops.inner import InnerLoopManager


@pytest.fixture
def inner_loop(tmp_path: Path) -> InnerLoopManager:
    work_dir = tmp_path / ".millstone"
    work_dir.mkdir()
    # Initialize a git repo so git commands work
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    return InnerLoopManager(work_dir=work_dir, repo_dir=tmp_path)


def _make_diff(inner_loop: InnerLoopManager, content: str, filename: str = "script.sql") -> None:
    """Create a file with given content and stage it."""
    (inner_loop.repo_dir / filename).write_text(content)
    subprocess.run(["git", "add", "."], cwd=inner_loop.repo_dir, capture_output=True, check=True)


# =========================================================================
# normalize_dangerous_patterns
# =========================================================================


class TestNormalizeDangerousPatterns:
    """normalize_dangerous_patterns converts mixed config formats to uniform list."""

    def test_plain_strings_use_default_action(self):
        result = normalize_dangerous_patterns(
            patterns=["rm -rf", "DROP TABLE"],
            default_action="block",
        )
        assert result == [
            {"pattern": "rm -rf", "action": "block"},
            {"pattern": "DROP TABLE", "action": "block"},
        ]

    def test_plain_strings_use_flag_default(self):
        result = normalize_dangerous_patterns(
            patterns=["rm -rf"],
            default_action="flag",
        )
        assert result == [{"pattern": "rm -rf", "action": "flag"}]

    def test_dict_entries_preserve_per_pattern_action(self):
        result = normalize_dangerous_patterns(
            patterns=[
                {"pattern": "DROP TABLE", "action": "flag"},
                {"pattern": "rm -rf", "action": "block"},
            ],
            default_action="block",
        )
        assert result == [
            {"pattern": "DROP TABLE", "action": "flag"},
            {"pattern": "rm -rf", "action": "block"},
        ]

    def test_mixed_strings_and_dicts(self):
        result = normalize_dangerous_patterns(
            patterns=[
                "rm -rf",
                {"pattern": "DROP TABLE", "action": "flag"},
                "force push",
            ],
            default_action="block",
        )
        assert result == [
            {"pattern": "rm -rf", "action": "block"},
            {"pattern": "DROP TABLE", "action": "flag"},
            {"pattern": "force push", "action": "block"},
        ]

    def test_dict_without_action_uses_default(self):
        result = normalize_dangerous_patterns(
            patterns=[{"pattern": "DROP TABLE"}],
            default_action="flag",
        )
        assert result == [{"pattern": "DROP TABLE", "action": "flag"}]

    def test_empty_list(self):
        result = normalize_dangerous_patterns(patterns=[], default_action="block")
        assert result == []

    def test_backward_compat_block_true(self):
        """block=true in legacy config maps to action='block'."""
        result = normalize_dangerous_patterns(
            patterns=["rm -rf"],
            default_action="block",  # caller resolves block=true → "block"
        )
        assert result[0]["action"] == "block"

    def test_backward_compat_block_false(self):
        """block=false in legacy config maps to action='flag'."""
        result = normalize_dangerous_patterns(
            patterns=["rm -rf"],
            default_action="flag",  # caller resolves block=false → "flag"
        )
        assert result[0]["action"] == "flag"


# =========================================================================
# Mechanical checks integration
# =========================================================================


class TestDangerousPatternBlock:
    """Patterns with action='block' halt the run."""

    def test_block_action_halts(self, inner_loop: InnerLoopManager):
        inner_loop.policy = {
            "limits": {"max_loc_per_task": 10000},
            "dangerous": {
                "action": "block",
                "patterns": ["DROP TABLE"],
            },
        }
        _make_diff(inner_loop, "DROP TABLE users;")

        passed, _ = inner_loop.mechanical_checks(
            loc_baseline_ref=None,
            skip_mechanical_checks=False,
        )
        assert passed is False

    def test_per_pattern_block_overrides_flag_default(self, inner_loop: InnerLoopManager):
        inner_loop.policy = {
            "limits": {"max_loc_per_task": 10000},
            "dangerous": {
                "action": "flag",  # default is flag
                "patterns": [
                    {"pattern": "DROP TABLE", "action": "block"},  # but this one blocks
                ],
            },
        }
        _make_diff(inner_loop, "DROP TABLE users;")

        passed, _ = inner_loop.mechanical_checks(
            loc_baseline_ref=None,
            skip_mechanical_checks=False,
        )
        assert passed is False


class TestDangerousPatternFlag:
    """Patterns with action='flag' pass mechanical checks but return flag text."""

    def test_flag_action_passes_and_returns_flags(self, inner_loop: InnerLoopManager):
        inner_loop.policy = {
            "limits": {"max_loc_per_task": 10000},
            "dangerous": {
                "action": "flag",
                "patterns": ["DROP TABLE"],
            },
        }
        _make_diff(inner_loop, "DROP TABLE users;")

        passed, _ = inner_loop.mechanical_checks(
            loc_baseline_ref=None,
            skip_mechanical_checks=False,
        )
        assert passed is True
        assert inner_loop.dangerous_flags is not None
        assert "DROP TABLE" in inner_loop.dangerous_flags

    def test_per_pattern_flag_overrides_block_default(self, inner_loop: InnerLoopManager):
        inner_loop.policy = {
            "limits": {"max_loc_per_task": 10000},
            "dangerous": {
                "action": "block",  # default is block
                "patterns": [
                    {"pattern": "DROP TABLE", "action": "flag"},  # but this one flags
                ],
            },
        }
        _make_diff(inner_loop, "DROP TABLE users;")

        passed, _ = inner_loop.mechanical_checks(
            loc_baseline_ref=None,
            skip_mechanical_checks=False,
        )
        assert passed is True
        assert inner_loop.dangerous_flags is not None
        assert "DROP TABLE" in inner_loop.dangerous_flags

    def test_no_flags_when_no_match(self, inner_loop: InnerLoopManager):
        inner_loop.policy = {
            "limits": {"max_loc_per_task": 10000},
            "dangerous": {
                "action": "flag",
                "patterns": ["DROP TABLE"],
            },
        }
        _make_diff(inner_loop, "SELECT * FROM users;")

        passed, _ = inner_loop.mechanical_checks(
            loc_baseline_ref=None,
            skip_mechanical_checks=False,
        )
        assert passed is True
        assert not inner_loop.dangerous_flags

    def test_flags_cleared_between_checks(self, inner_loop: InnerLoopManager):
        """dangerous_flags reset on each mechanical_checks call."""
        inner_loop.policy = {
            "limits": {"max_loc_per_task": 10000},
            "dangerous": {
                "action": "flag",
                "patterns": ["DROP TABLE"],
            },
        }
        _make_diff(inner_loop, "DROP TABLE users;")
        inner_loop.mechanical_checks(loc_baseline_ref=None, skip_mechanical_checks=False)
        assert inner_loop.dangerous_flags

        # Second call with clean diff
        subprocess.run(
            ["git", "add", "-A"], cwd=inner_loop.repo_dir, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "commit"],
            cwd=inner_loop.repo_dir,
            capture_output=True,
            check=True,
        )
        passed, _ = inner_loop.mechanical_checks(
            loc_baseline_ref=None,
            skip_mechanical_checks=False,
        )
        assert not inner_loop.dangerous_flags


class TestDangerousPatternMixed:
    """Mixed block+flag patterns in same policy."""

    def test_block_pattern_halts_even_with_flag_patterns(self, inner_loop: InnerLoopManager):
        """If any block-action pattern matches, the run halts."""
        inner_loop.policy = {
            "limits": {"max_loc_per_task": 10000},
            "dangerous": {
                "patterns": [
                    {"pattern": "DROP TABLE", "action": "flag"},
                    {"pattern": "rm -rf", "action": "block"},
                ],
            },
        }
        _make_diff(inner_loop, "rm -rf /\nDROP TABLE users;")

        passed, _ = inner_loop.mechanical_checks(
            loc_baseline_ref=None,
            skip_mechanical_checks=False,
        )
        assert passed is False

    def test_only_flag_patterns_match_passes(self, inner_loop: InnerLoopManager):
        """If only flag-action patterns match, the run passes with flags."""
        inner_loop.policy = {
            "limits": {"max_loc_per_task": 10000},
            "dangerous": {
                "patterns": [
                    {"pattern": "DROP TABLE", "action": "flag"},
                    {"pattern": "rm -rf", "action": "block"},
                ],
            },
        }
        _make_diff(inner_loop, "DROP TABLE users;")

        passed, _ = inner_loop.mechanical_checks(
            loc_baseline_ref=None,
            skip_mechanical_checks=False,
        )
        assert passed is True
        assert "DROP TABLE" in inner_loop.dangerous_flags


class TestBackwardCompat:
    """Legacy block=true/false config still works."""

    def test_legacy_block_true_blocks(self, inner_loop: InnerLoopManager):
        inner_loop.policy = {
            "limits": {"max_loc_per_task": 10000},
            "dangerous": {
                "patterns": ["DROP TABLE"],
                "block": True,
            },
        }
        _make_diff(inner_loop, "DROP TABLE users;")

        passed, _ = inner_loop.mechanical_checks(
            loc_baseline_ref=None,
            skip_mechanical_checks=False,
        )
        assert passed is False

    def test_legacy_block_false_flags(self, inner_loop: InnerLoopManager):
        inner_loop.policy = {
            "limits": {"max_loc_per_task": 10000},
            "dangerous": {
                "patterns": ["DROP TABLE"],
                "block": False,
            },
        }
        _make_diff(inner_loop, "DROP TABLE users;")

        passed, _ = inner_loop.mechanical_checks(
            loc_baseline_ref=None,
            skip_mechanical_checks=False,
        )
        assert passed is True
        assert inner_loop.dangerous_flags is not None
        assert "DROP TABLE" in inner_loop.dangerous_flags


class TestDangerousFlagsReachReviewer:
    """dangerous_flags propagate to reviewer prompt via the orchestrator."""

    # This is tested at the E2E level in test_e2e_sanity_advisory.py;
    # here we verify the InnerLoopManager attribute contract.

    def test_dangerous_flags_attribute_exists_after_init(self, inner_loop: InnerLoopManager):
        assert inner_loop.dangerous_flags is None or inner_loop.dangerous_flags == ""

    def test_dangerous_flags_populated_after_flag_match(self, inner_loop: InnerLoopManager):
        inner_loop.policy = {
            "limits": {"max_loc_per_task": 10000},
            "dangerous": {
                "action": "flag",
                "patterns": ["DROP TABLE", "truncate table"],
            },
        }
        _make_diff(inner_loop, "DROP TABLE users;\nTRUNCATE TABLE orders;")

        inner_loop.mechanical_checks(loc_baseline_ref=None, skip_mechanical_checks=False)

        # Both patterns should be in the flags
        assert "DROP TABLE" in inner_loop.dangerous_flags
        assert "truncate table" in inner_loop.dangerous_flags
