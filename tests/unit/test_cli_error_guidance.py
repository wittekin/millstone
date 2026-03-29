"""Tests for T8 — cli_error_guidance() actionable CLI error messages."""

from __future__ import annotations

import pytest

from millstone.utils import cli_error_guidance


@pytest.mark.parametrize(
    "returncode, stderr, expected_substring",
    [
        pytest.param(
            137,
            "",
            "OOM",
            id="exit-137-oom",
        ),
        pytest.param(
            137,
            "some error text",
            "OOM",
            id="exit-137-oom-with-stderr",
        ),
        pytest.param(
            1,
            "rate limit exceeded",
            "Rate limited",
            id="rate-limit",
        ),
        pytest.param(
            1,
            "You've hit your limit · resets 3pm",
            "Rate limited",
            id="limit-reset",
        ),
        pytest.param(
            1,
            "authentication error occurred",
            "Authentication failed",
            id="auth-error",
        ),
        pytest.param(
            1,
            "HTTP 401 Unauthorized",
            "Authentication failed",
            id="401-error",
        ),
        pytest.param(
            1,
            "HTTP 403 Forbidden",
            "Authentication failed",
            id="403-error",
        ),
        pytest.param(
            1,
            "request timeout after 30s",
            "timed out",
            id="timeout",
        ),
        pytest.param(
            1,
            "connection timed out",
            "timed out",
            id="timed-out",
        ),
        pytest.param(
            1,
            "",
            "CLI failed with no details",
            id="exit-1-empty-stderr",
        ),
        pytest.param(
            1,
            "   ",
            "CLI failed with no details",
            id="exit-1-whitespace-stderr",
        ),
        pytest.param(
            2,
            "some unknown error",
            None,
            id="unknown-error",
        ),
        pytest.param(
            0,
            "",
            None,
            id="success-no-guidance",
        ),
    ],
)
def test_cli_error_guidance(returncode: int, stderr: str, expected_substring: str | None):
    result = cli_error_guidance(returncode, stderr)
    if expected_substring is None:
        assert result is None
    else:
        assert result is not None
        assert expected_substring in result


def test_exit_137_takes_priority_over_stderr_patterns():
    """Exit 137 should return OOM guidance even if stderr mentions auth."""
    result = cli_error_guidance(137, "authentication error")
    assert result is not None
    assert "OOM" in result


def test_rate_limit_pattern_case_insensitive():
    """Rate limit detection should be case-insensitive."""
    result = cli_error_guidance(1, "RATE LIMIT exceeded")
    assert result is not None
    assert "Rate limited" in result
