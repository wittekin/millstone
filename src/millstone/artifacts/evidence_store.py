"""Evidence store: persists EvidenceRecord artifacts to .millstone/evidence/.

Provides factory functions for the three primary emission sites:
  - make_review_evidence     (task review outcome)
  - make_eval_evidence       (test/eval suite outcome)
  - make_design_review_evidence (design review outcome)

Also provides evidence_from_effect_record() to satisfy the deferred linkage
from the add-effect-provider-abstraction design.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from millstone.artifacts.models import EvidenceKind, EvidenceRecord

if TYPE_CHECKING:
    from millstone.policy.effects import EffectRecord


def _slug(text: str, max_len: int = 30) -> str:
    """Convert arbitrary text to a safe filename segment."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len] or "unknown"


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _timestamp_file() -> str:
    # Microsecond resolution eliminates same-second ID collisions.
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


class EvidenceStore:
    """Writes and reads EvidenceRecord artifacts under <work_dir>/evidence/.

    Each record is stored as a single JSON file named:
        <YYYYMMDD_HHMMSS_ffffff>-<kind>[-<work_item_slug>].json

    The store creates the evidence directory on first write.
    """

    def __init__(self, work_dir: Path) -> None:
        self._evidence_dir = Path(work_dir) / "evidence"

    def emit(self, record: EvidenceRecord) -> Path:
        """Persist a record and return its file path.

        The output file is named ``<basename(evidence_id)>.json``; any directory
        separators in ``evidence_id`` are stripped so the file always lands
        inside the evidence directory.
        """
        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        safe_id = Path(record.evidence_id).name
        path = self._evidence_dir / f"{safe_id}.json"
        path.write_text(
            json.dumps(
                {
                    "evidence_id": record.evidence_id,
                    "kind": record.kind.value,
                    "timestamp": record.timestamp,
                    "outcome": record.outcome,
                    "work_item_id": record.work_item_id,
                    "work_item_kind": record.work_item_kind,
                    "capability_tier": record.capability_tier,
                    "detail": record.detail,
                },
                indent=2,
            )
        )
        return path

    def list(
        self,
        kind: EvidenceKind | None = None,
        work_item_id: str | None = None,
    ) -> list[EvidenceRecord]:
        """Return all stored records, optionally filtered by kind and/or work_item_id."""
        if not self._evidence_dir.exists():
            return []
        records: list[EvidenceRecord] = []
        for path in sorted(self._evidence_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                record = EvidenceRecord(
                    evidence_id=data["evidence_id"],
                    kind=EvidenceKind(data["kind"]),
                    timestamp=data["timestamp"],
                    outcome=data["outcome"],
                    work_item_id=data.get("work_item_id"),
                    work_item_kind=data.get("work_item_kind"),
                    capability_tier=data.get("capability_tier"),
                    detail=data.get("detail", {}),
                )
                if kind is not None and record.kind != kind:
                    continue
                if work_item_id is not None and record.work_item_id != work_item_id:
                    continue
                records.append(record)
            except (KeyError, ValueError, OSError):
                continue
        return records


def make_review_evidence(
    task_text: str,
    outcome: str,
    cycles: int,
    findings_count: int,
    findings_by_severity: dict[str, int],
    duration_ms: int,
    review_summary: str | None = None,
    capability_tier: str | None = None,
    work_item_id: str | None = None,
) -> EvidenceRecord:
    """Construct a review EvidenceRecord from save_task_metrics inputs.

    Args:
        work_item_id: Canonical task identity (e.g. from _current_task_id).
                      Falls back to a slug derived from task_text when absent,
                      ensuring review and eval evidence share the same ID.
    """
    ts = _timestamp_file()
    slug = _slug(task_text)
    canonical_id = work_item_id or slug
    return EvidenceRecord(
        evidence_id=f"{ts}-review-{_slug(canonical_id)}",
        kind=EvidenceKind.review,
        timestamp=_now_utc(),
        outcome=outcome,
        work_item_id=canonical_id,
        work_item_kind="task",
        capability_tier=capability_tier,
        detail={
            "review_summary": (review_summary or "")[:500],
            "findings_count": findings_count,
            "findings_by_severity": findings_by_severity,
            "cycles": cycles,
            "duration_ms": duration_ms,
        },
    )


def make_eval_evidence(
    eval_result: dict,
    work_item_id: str | None = None,
    capability_tier: str | None = None,
) -> EvidenceRecord:
    """Construct an eval EvidenceRecord from run_eval output."""
    ts = _timestamp_file()
    passed = eval_result.get("_passed", False)
    outcome = "passed" if passed else "failed"
    ref = work_item_id or (eval_result.get("git_head") or "unknown")[:12]
    slug = _slug(ref)
    tests = eval_result.get("tests", {})
    return EvidenceRecord(
        evidence_id=f"{ts}-eval-{slug}",
        kind=EvidenceKind.eval,
        timestamp=_now_utc(),
        outcome=outcome,
        work_item_id=work_item_id,
        work_item_kind="task" if work_item_id else None,
        capability_tier=capability_tier,
        detail={
            "composite_score": eval_result.get("composite_score"),
            "tests_passed": tests.get("passed", 0),
            "tests_failed": tests.get("failed", 0),
            "duration_seconds": eval_result.get("duration_seconds"),
            "eval_file": eval_result.get("_eval_file"),
        },
    )


def make_design_review_evidence(
    design_path: str,
    outcome: str,
    strengths_count: int,
    issues_count: int,
    capability_tier: str | None = None,
) -> EvidenceRecord:
    """Construct a design_review EvidenceRecord from review_design output."""
    ts = _timestamp_file()
    design_id = Path(design_path).stem
    slug = _slug(design_id)
    return EvidenceRecord(
        evidence_id=f"{ts}-design-review-{slug}",
        kind=EvidenceKind.design_review,
        timestamp=_now_utc(),
        outcome=outcome.lower().replace(" ", "_"),
        work_item_id=design_id,
        work_item_kind="design",
        capability_tier=capability_tier,
        detail={
            "design_path": design_path,
            "strengths_count": strengths_count,
            "issues_count": issues_count,
            "verdict": outcome,
        },
    )


def evidence_from_effect_record(
    effect_record: EffectRecord,
    capability_tier: str | None = None,
) -> EvidenceRecord:
    """Convert an EffectRecord from effect_provider.py into an EvidenceRecord.

    Satisfies the add-effect-provider-abstraction deferral:
    'Wiring EffectRecord into the structured evidence artifact model'.

    No emission sites exist yet; this utility is called by future effect
    provider call sites when concrete providers are implemented.
    """
    ts = _timestamp_file()
    intent = effect_record.intent
    slug = _slug(intent.description)
    return EvidenceRecord(
        evidence_id=f"{ts}-effect-{slug}",
        kind=EvidenceKind.effect,
        timestamp=effect_record.timestamp,
        outcome=effect_record.status.value,
        work_item_id=intent.idempotency_key,
        work_item_kind=None,
        capability_tier=capability_tier,
        detail={
            "effect_class": intent.effect_class.value,
            "description": intent.description,
            "idempotency_key": intent.idempotency_key,
            "rollback_plan": intent.rollback_plan,
            "error": effect_record.error,
        },
    )
