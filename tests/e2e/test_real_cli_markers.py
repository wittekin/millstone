"""Tests for @pytest.mark.real_cli and @pytest.mark.real_mcp skip logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.e2e.conftest import _real_cli_skip_reason, _real_mcp_skip_reason


def _make_provider(available: bool, msg: str = ""):
    """Return a mock provider whose check_available returns (available, msg)."""
    provider = MagicMock()
    provider.check_available.return_value = (available, msg)
    return provider


# ---------------------------------------------------------------------------
# real_cli: flag gate
# ---------------------------------------------------------------------------


def test_real_cli_skips_when_flag_absent_even_with_cli_installed():
    with patch("tests.e2e.conftest.get_provider", return_value=_make_provider(True)):
        reason = _real_cli_skip_reason("claude", flag_passed=False)
    assert reason is not None
    assert "--run-real-cli" in reason


def test_real_cli_skips_when_flag_absent_codex():
    with patch("tests.e2e.conftest.get_provider", return_value=_make_provider(True)):
        reason = _real_cli_skip_reason("codex", flag_passed=False)
    assert reason is not None


def test_real_cli_skips_when_flag_absent_mixed():
    with patch("tests.e2e.conftest.get_provider", return_value=_make_provider(True)):
        reason = _real_cli_skip_reason("mixed", flag_passed=False)
    assert reason is not None


# ---------------------------------------------------------------------------
# real_cli(provider="claude"): CLI availability check
# ---------------------------------------------------------------------------


def test_real_cli_claude_skips_when_binary_unavailable():
    with patch(
        "tests.e2e.conftest.get_provider",
        return_value=_make_provider(False, "claude not found. Install claude CLI."),
    ):
        reason = _real_cli_skip_reason("claude", flag_passed=True)
    assert reason is not None
    assert "claude" in reason.lower()


def test_real_cli_claude_passes_when_binary_available():
    with patch(
        "tests.e2e.conftest.get_provider",
        return_value=_make_provider(True, "claude available: 1.0.0"),
    ):
        reason = _real_cli_skip_reason("claude", flag_passed=True)
    assert reason is None


# ---------------------------------------------------------------------------
# real_cli(provider="codex"): CLI availability check
# ---------------------------------------------------------------------------


def test_real_cli_codex_skips_when_binary_unavailable():
    with patch(
        "tests.e2e.conftest.get_provider",
        return_value=_make_provider(False, "codex not found. Install codex CLI."),
    ):
        reason = _real_cli_skip_reason("codex", flag_passed=True)
    assert reason is not None
    assert "codex" in reason.lower()


def test_real_cli_codex_passes_when_binary_available():
    with patch(
        "tests.e2e.conftest.get_provider",
        return_value=_make_provider(True, "codex available: 1.0.0"),
    ):
        reason = _real_cli_skip_reason("codex", flag_passed=True)
    assert reason is None


# ---------------------------------------------------------------------------
# real_cli(provider="mixed"): both CLIs must be available
# ---------------------------------------------------------------------------


def test_real_cli_mixed_skips_if_first_cli_unavailable():
    def provider_factory(cli_name):
        if cli_name == "claude":
            return _make_provider(False, "claude not found.")
        return _make_provider(True)

    with patch("tests.e2e.conftest.get_provider", side_effect=provider_factory):
        reason = _real_cli_skip_reason("mixed", flag_passed=True)
    assert reason is not None


def test_real_cli_mixed_passes_when_both_available():
    with patch(
        "tests.e2e.conftest.get_provider",
        return_value=_make_provider(True, "available"),
    ):
        reason = _real_cli_skip_reason("mixed", flag_passed=True)
    assert reason is None


# ---------------------------------------------------------------------------
# real_mcp: flag gate only
# ---------------------------------------------------------------------------


def test_real_mcp_skips_when_flag_absent():
    reason = _real_mcp_skip_reason(flag_passed=False)
    assert reason is not None
    assert "--run-real-mcp" in reason


def test_real_mcp_passes_with_flag():
    reason = _real_mcp_skip_reason(flag_passed=True)
    assert reason is None
