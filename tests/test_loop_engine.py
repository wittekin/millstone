"""Tests for the generic loop engine."""

from unittest.mock import MagicMock, patch

from millstone.loops.engine import ArtifactReviewLoop


class MockVerdict:
    def __init__(self, approved: bool, feedback: str = ""):
        self.approved = approved
        self.feedback = feedback

def test_loop_success_first_try():
    """Loop succeeds if producer is approved on first try."""
    producer = MagicMock(return_value="artifact v1")
    reviewer = MagicMock(return_value=MockVerdict(True))
    def is_approved(v):
        return v.approved

    loop = ArtifactReviewLoop(
        name="TestLoop",
        producer=producer,
        reviewer=reviewer,
        is_approved=is_approved,
        max_cycles=3
    )

    result = loop.run(input="start")

    assert result.success is True
    assert result.cycles == 1
    assert result.artifact == "artifact v1"
    producer.assert_called_once_with(input="start")
    reviewer.assert_called_once_with("artifact v1")

def test_loop_iteration_and_fix():
    """Loop iterates and passes feedback to producer if rejected."""

    def producer_side_effect(*args, **kwargs):
        if "feedback" in kwargs:
            return "artifact v2"
        return "artifact v1"

    producer = MagicMock(side_effect=producer_side_effect)

    # First review fails, second succeeds
    reviewer = MagicMock(side_effect=[
        MockVerdict(False, "needs more detail"),
        MockVerdict(True)
    ])
    def is_approved(v):
        return v.approved

    loop = ArtifactReviewLoop(
        name="TestLoop",
        producer=producer,
        reviewer=reviewer,
        is_approved=is_approved,
        max_cycles=3
    )

    result = loop.run(arg1="val1")

    assert result.success is True
    assert result.cycles == 2
    assert result.artifact == "artifact v2"

    # Check calls
    assert producer.call_count == 2
    producer.assert_any_call(arg1="val1")
    producer.assert_any_call(feedback="needs more detail", arg1="val1")

    assert reviewer.call_count == 2

def test_loop_reaches_max_cycles():
    """Loop fails if max cycles reached without approval."""
    producer = MagicMock(return_value="artifact")
    reviewer = MagicMock(return_value=MockVerdict(False, "still bad"))
    def is_approved(v):
        return v.approved

    loop = ArtifactReviewLoop(
        name="TestLoop",
        producer=producer,
        reviewer=reviewer,
        is_approved=is_approved,
        max_cycles=2
    )

    result = loop.run()

    assert result.success is False
    assert result.cycles == 2
    assert "Maximum cycles" in result.error

def test_on_success_callback():
    """Loop calls on_success and respects its result."""
    producer = MagicMock(return_value="artifact")
    reviewer = MagicMock(return_value=MockVerdict(True))
    on_success = MagicMock(return_value=True)

    loop = ArtifactReviewLoop(
        name="TestLoop",
        producer=producer,
        reviewer=reviewer,
        is_approved=lambda v: v.approved,
        on_success=on_success
    )

    result = loop.run()
    assert result.success is True
    on_success.assert_called_once()

def test_on_success_failure_halts():
    """If on_success returns False, the loop reports failure."""
    producer = MagicMock(return_value="artifact")
    reviewer = MagicMock(return_value=MockVerdict(True))
    on_success = MagicMock(return_value=False)

    loop = ArtifactReviewLoop(
        name="TestLoop",
        producer=producer,
        reviewer=reviewer,
        is_approved=lambda v: v.approved,
        on_success=on_success
    )

    result = loop.run()
    assert result.success is False
    assert "Completion step failed" in result.error

def test_validator_failure_halts():
    """If validator returns False, the loop halts before review."""
    producer = MagicMock(return_value="artifact")
    reviewer = MagicMock()
    validator = MagicMock(return_value=(False, "security breach"))

    loop = ArtifactReviewLoop(
        name="TestLoop",
        producer=producer,
        reviewer=reviewer,
        is_approved=lambda v: True,
        validator=validator
    )

    result = loop.run()
    assert result.success is False
    assert result.error == "security breach"
    reviewer.assert_not_called()


def test_loop_survives_broken_pipe_after_request_changes():
    """Regression: a BrokenPipe during progress output must not crash the loop."""

    def producer_side_effect(*args, **kwargs):
        if "feedback" in kwargs:
            return "artifact v2"
        return "artifact v1"

    producer = MagicMock(side_effect=producer_side_effect)
    reviewer = MagicMock(side_effect=[
        MockVerdict(False, "needs more detail"),
        MockVerdict(True),
    ])

    loop = ArtifactReviewLoop(
        name="TestLoop",
        producer=producer,
        reviewer=reviewer,
        is_approved=lambda v: v.approved,
        max_cycles=3,
    )

    def flaky_print(*args, **kwargs):
        message = str(args[0]) if args else ""
        if "Requested changes." in message:
            raise BrokenPipeError()
        return None

    with patch("builtins.print", side_effect=flaky_print):
        result = loop.run()

    assert result.success is True
    assert result.cycles == 2
