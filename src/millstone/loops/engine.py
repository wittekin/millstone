"""
Generic loop engine for artifact-based write/review cycles.

This module provides an abstraction for any process that follows the pattern:
1. Producer generates/modifies an artifact.
2. Reviewer evaluates the artifact.
3. Decision: APPROVED or REQUEST_CHANGES.
4. If REJECTED, Producer fixes based on feedback and loops.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from millstone.utils import progress

T = TypeVar("T")  # The artifact type (e.g., str for code/plan)
V = TypeVar("V")  # The verdict type

@dataclass
class LoopResult(Generic[T, V]):
    """Result of a loop execution."""
    success: bool
    artifact: T | None = None
    verdict: V | None = None
    cycles: int = 0
    duration_ms: int = 0
    error: str | None = None

@dataclass
class ArtifactReviewLoop(Generic[T, V]):
    """Generic engine for running artifact-based review loops.

    Attributes:
        name: Name of the loop (for logging).
        max_cycles: Maximum number of iterations.
        producer: Callable that generates the artifact.
        reviewer: Callable that reviews the artifact.
        is_approved: Callable that determines if verdict is an approval.
        on_cycle_start: Optional callback before each cycle.
        on_success: Optional callback on approval.
    """
    name: str
    producer: Callable[..., T]
    reviewer: Callable[[T], V]
    is_approved: Callable[[V], bool]
    validator: Callable[[T], tuple[bool, str | None]] | None = None
    max_cycles: int = 3
    on_cycle_start: Callable[[int], None] | None = None
    on_success: Callable[[T, V], bool] | None = None

    def run(self, *producer_args, **producer_kwargs) -> LoopResult[T, V]:
        """Execute the loop."""
        start_time = time.time()
        current_cycle = 0
        last_artifact: T | None = None
        last_verdict: V | None = None
        feedback: str | None = None

        while current_cycle < self.max_cycles:
            current_cycle += 1
            if self.on_cycle_start:
                self.on_cycle_start(current_cycle)

            progress(f"[{self.name}] Cycle {current_cycle}/{self.max_cycles}: Running producer...")

            # Step 1: Produce/Fix
            try:
                if current_cycle == 1:
                    last_artifact = self.producer(*producer_args, **producer_kwargs)
                else:
                    last_artifact = self.producer(
                        *producer_args,
                        feedback=feedback,
                        **producer_kwargs,
                    )
            except Exception as e:
                return LoopResult(False, error=f"Producer failed: {str(e)}", cycles=current_cycle)

            # Step 2: Validate (Mechanical/Sanity)
            if self.validator:
                progress(f"[{self.name}] Cycle {current_cycle}/{self.max_cycles}: Validating...")
                valid, reason = self.validator(last_artifact)
                if not valid:
                    return LoopResult(False, last_artifact, error=reason or "Validation failed", cycles=current_cycle)

            # Step 3: Review
            progress(f"[{self.name}] Cycle {current_cycle}/{self.max_cycles}: Running reviewer...")
            try:
                last_verdict = self.reviewer(last_artifact)
            except Exception as e:
                return LoopResult(False, last_artifact, error=f"Reviewer failed: {str(e)}", cycles=current_cycle)

            # Step 3: Decision
            if self.is_approved(last_verdict):
                progress(f"[{self.name}] Approved after {current_cycle} cycle(s).")

                # Optional completion step (e.g. commit)
                if self.on_success:
                    success = self.on_success(last_artifact, last_verdict)
                    if not success:
                        return LoopResult(False, last_artifact, last_verdict, cycles=current_cycle, error="Completion step failed")

                duration = int((time.time() - start_time) * 1000)
                return LoopResult(True, last_artifact, last_verdict, cycles=current_cycle, duration_ms=duration)

            # Step 4: Extract feedback for next cycle
            # Extract feedback from verdict (attribute, dict key, or string)
            if hasattr(last_verdict, 'feedback'):
                feedback_raw = last_verdict.feedback
            elif isinstance(last_verdict, dict):
                feedback_raw = last_verdict.get('feedback', str(last_verdict))
            else:
                feedback_raw = str(last_verdict)

            # Format as newline-separated string if it's a list
            if isinstance(feedback_raw, list):
                feedback = "\n".join(f"- {item}" for item in feedback_raw)
            else:
                feedback = str(feedback_raw)

            progress(f"[{self.name}] Cycle {current_cycle}/{self.max_cycles}: Requested changes.")

        duration = int((time.time() - start_time) * 1000)
        return LoopResult(
            False,
            last_artifact,
            last_verdict,
            cycles=current_cycle,
            duration_ms=duration,
            error=f"Maximum cycles ({self.max_cycles}) reached without approval"
        )
