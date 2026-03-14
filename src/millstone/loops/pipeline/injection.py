"""Injection helpers — create StageItems from unstructured input.

These allow onboarding raw text, file paths, or external references into any
point in the pipeline.

Injected opportunities carry the raw text as their artifact (a plain string),
NOT an ``Opportunity`` model.  This preserves the current ``run_design()``
semantics where ``--design "text"`` and roadmap goals pass text directly
without a canonical opportunity_id.
"""

from __future__ import annotations

import re

from millstone.loops.pipeline.stage import HandoffKind, StageItem


def inject_opportunity(text: str) -> StageItem:
    """Create an opportunity StageItem from raw text.

    The artifact is the raw text string — not an ``Opportunity`` model.
    ``DesignStage`` detects strings vs. real ``Opportunity`` objects and
    omits ``opportunity_id`` for raw text, matching existing semantics.

    Args:
        text: Opportunity description / objective text.
    """
    slug = _slugify(text)
    return StageItem(
        kind=HandoffKind.OPPORTUNITY,
        artifact=text,  # raw text, not Opportunity model
        artifact_id=slug,
        source_stage="injection",
        metadata={"original_text": text},
    )


def inject_design(path_or_id: str) -> StageItem:
    """Create a design StageItem from a file path or design ID."""
    return StageItem(
        kind=HandoffKind.DESIGN,
        artifact=path_or_id,
        artifact_id=path_or_id,
        source_stage="injection",
    )


def inject_worklist() -> StageItem:
    """Create a worklist StageItem meaning 'execute pending tasks'."""
    return StageItem(
        kind=HandoffKind.WORKLIST,
        artifact=None,
        artifact_id="tasklist",
        source_stage="injection",
    )


def _slugify(text: str) -> str:
    """Convert text to a kebab-case slug suitable as an artifact ID."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
    return slug.strip("-")[:40]
