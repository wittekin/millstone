"""Unit tests for EvidenceStore, EvidenceRecord, and evidence factory functions.

Covers:
  - EvidenceStore.emit() writes correct JSON
  - EvidenceStore.list() filtering by kind and work_item_id
  - EvidenceStore.list() on missing directory
  - make_review_evidence, make_eval_evidence, make_design_review_evidence factories
  - evidence_from_effect_record utility
  - extract_current_task_metadata() resolves canonical task_id from raw block
  - Orchestrator._evidence_store construction
  - Evidence emission from save_task_metrics() (approved-only gate)
  - Evidence emission from run_eval()
  - Evidence emission from review_design()
"""

import json
from pathlib import Path
from unittest.mock import patch

from millstone.artifacts.evidence_store import (
    EvidenceStore,
    evidence_from_effect_record,
    make_design_review_evidence,
    make_eval_evidence,
    make_review_evidence,
)
from millstone.artifacts.models import EvidenceKind, EvidenceRecord
from millstone.artifacts.tasklist import TasklistManager

# ---------------------------------------------------------------------------
# EvidenceStore
# ---------------------------------------------------------------------------


class TestEvidenceStore:
    def test_emit_writes_json_file(self, tmp_path):
        store = EvidenceStore(tmp_path / ".millstone")
        record = EvidenceRecord(
            evidence_id="20260301_143022_000000-review-fix-auth",
            kind=EvidenceKind.review,
            timestamp="2026-03-01T14:30:22+00:00",
            outcome="approved",
            work_item_id="fix-auth",
            work_item_kind="task",
            capability_tier="C1_local_write",
            detail={"cycles": 1},
        )
        path = store.emit(record)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["evidence_id"] == record.evidence_id
        assert data["kind"] == "review"
        assert data["outcome"] == "approved"
        assert data["work_item_id"] == "fix-auth"
        assert data["capability_tier"] == "C1_local_write"
        assert data["detail"] == {"cycles": 1}

    def test_emit_creates_directory(self, tmp_path):
        store = EvidenceStore(tmp_path / "nonexistent" / ".millstone")
        record = EvidenceRecord(
            evidence_id="20260301_143022_000000-eval-x",
            kind=EvidenceKind.eval,
            timestamp="2026-03-01T14:30:22+00:00",
            outcome="passed",
        )
        path = store.emit(record)
        assert path.exists()

    def test_list_returns_empty_when_directory_missing(self, tmp_path):
        store = EvidenceStore(tmp_path / "no-such-dir" / ".millstone")
        assert store.list() == []

    def test_list_returns_all_records(self, tmp_path):
        store = EvidenceStore(tmp_path / ".millstone")
        for i, kind in enumerate(
            [EvidenceKind.review, EvidenceKind.eval, EvidenceKind.design_review]
        ):
            store.emit(
                EvidenceRecord(
                    evidence_id=f"20260301_14302{i}_000000-{kind.value}-item",
                    kind=kind,
                    timestamp="2026-03-01T14:30:22+00:00",
                    outcome="ok",
                )
            )
        assert len(store.list()) == 3

    def test_list_filters_by_kind(self, tmp_path):
        store = EvidenceStore(tmp_path / ".millstone")
        store.emit(
            EvidenceRecord(
                evidence_id="20260301_143022_000000-review-a",
                kind=EvidenceKind.review,
                timestamp="2026-03-01T14:30:22+00:00",
                outcome="approved",
            )
        )
        store.emit(
            EvidenceRecord(
                evidence_id="20260301_143023_000000-eval-a",
                kind=EvidenceKind.eval,
                timestamp="2026-03-01T14:30:23+00:00",
                outcome="passed",
            )
        )
        results = store.list(kind=EvidenceKind.review)
        assert len(results) == 1
        assert results[0].kind == EvidenceKind.review

    def test_list_filters_by_work_item_id(self, tmp_path):
        store = EvidenceStore(tmp_path / ".millstone")
        store.emit(
            EvidenceRecord(
                evidence_id="20260301_143022_000000-review-task-a",
                kind=EvidenceKind.review,
                timestamp="2026-03-01T14:30:22+00:00",
                outcome="approved",
                work_item_id="task-a",
            )
        )
        store.emit(
            EvidenceRecord(
                evidence_id="20260301_143023_000000-review-task-b",
                kind=EvidenceKind.review,
                timestamp="2026-03-01T14:30:23+00:00",
                outcome="approved",
                work_item_id="task-b",
            )
        )
        results = store.list(work_item_id="task-a")
        assert len(results) == 1
        assert results[0].work_item_id == "task-a"

    def test_list_ignores_malformed_json(self, tmp_path):
        store = EvidenceStore(tmp_path / ".millstone")
        evidence_dir = tmp_path / ".millstone" / "evidence"
        evidence_dir.mkdir(parents=True)
        (evidence_dir / "bad.json").write_text("not json")
        assert store.list() == []

    def test_microsecond_ids_produce_distinct_filenames(self, tmp_path):
        """Consecutive records for the same task should have distinct IDs and both persist."""
        store = EvidenceStore(tmp_path / ".millstone")
        r1 = make_review_evidence("fix auth", "approved", 1, 0, {}, 500)
        r2 = make_review_evidence("fix auth", "approved", 1, 0, {}, 500)
        # Microsecond timestamps make same-ID collisions astronomically unlikely.
        assert r1.evidence_id != r2.evidence_id
        store.emit(r1)
        store.emit(r2)
        # Both records must be retrievable — no silent overwrite.
        assert len(store.list()) == 2


# ---------------------------------------------------------------------------
# make_review_evidence
# ---------------------------------------------------------------------------


class TestMakeReviewEvidence:
    def test_kind_is_review(self):
        r = make_review_evidence("fix auth", "approved", 2, 3, {"critical": 0, "high": 1}, 1000)
        assert r.kind == EvidenceKind.review

    def test_outcome_preserved(self):
        r = make_review_evidence("fix auth", "approved", 1, 0, {}, 500)
        assert r.outcome == "approved"

    def test_work_item_kind_is_task(self):
        r = make_review_evidence("fix auth", "approved", 1, 0, {}, 500)
        assert r.work_item_kind == "task"

    def test_capability_tier_stored(self):
        r = make_review_evidence(
            "fix auth", "approved", 1, 0, {}, 500, capability_tier="C1_local_write"
        )
        assert r.capability_tier == "C1_local_write"

    def test_detail_contains_cycles_and_findings(self):
        r = make_review_evidence("fix auth", "approved", 3, 5, {"critical": 1, "high": 4}, 2000)
        assert r.detail["cycles"] == 3
        assert r.detail["findings_count"] == 5
        assert r.detail["duration_ms"] == 2000

    def test_work_item_id_falls_back_to_slug_when_not_provided(self):
        r = make_review_evidence("Fix Auth Token", "approved", 1, 0, {}, 0)
        assert r.work_item_id == "fix-auth-token"

    def test_work_item_id_uses_explicit_id_over_slug(self):
        r = make_review_evidence(
            "Refactor auth module to use new token library",
            "approved",
            1,
            0,
            {},
            0,
            work_item_id="refactor-auth",
        )
        assert r.work_item_id == "refactor-auth"
        assert r.evidence_id.endswith("-review-refactor-auth")


# ---------------------------------------------------------------------------
# make_eval_evidence
# ---------------------------------------------------------------------------


class TestMakeEvalEvidence:
    def test_outcome_passed(self):
        e = make_eval_evidence({"_passed": True, "tests": {}})
        assert e.outcome == "passed"

    def test_outcome_failed_on_false(self):
        e = make_eval_evidence({"_passed": False, "tests": {}})
        assert e.outcome == "failed"

    def test_outcome_failed_when_key_absent(self):
        e = make_eval_evidence({"tests": {}})
        assert e.outcome == "failed"

    def test_kind_is_eval(self):
        e = make_eval_evidence({"_passed": True, "tests": {}})
        assert e.kind == EvidenceKind.eval

    def test_work_item_id_linked_when_provided(self):
        e = make_eval_evidence({"_passed": True, "tests": {}}, work_item_id="task-42")
        assert e.work_item_id == "task-42"
        assert e.work_item_kind == "task"

    def test_eval_file_in_detail(self):
        e = make_eval_evidence({"_passed": True, "tests": {}, "_eval_file": "20260301.json"})
        assert e.detail["eval_file"] == "20260301.json"

    def test_composite_score_in_detail(self):
        e = make_eval_evidence({"_passed": True, "composite_score": 0.95, "tests": {}})
        assert e.detail["composite_score"] == 0.95


# ---------------------------------------------------------------------------
# make_design_review_evidence
# ---------------------------------------------------------------------------


class TestMakeDesignReviewEvidence:
    def test_kind_is_design_review(self):
        d = make_design_review_evidence("designs/add-foo.md", "APPROVED", 3, 0)
        assert d.kind == EvidenceKind.design_review

    def test_work_item_id_is_design_stem(self):
        d = make_design_review_evidence("designs/add-foo.md", "APPROVED", 3, 0)
        assert d.work_item_id == "add-foo"

    def test_work_item_kind_is_design(self):
        d = make_design_review_evidence("designs/add-foo.md", "APPROVED", 3, 0)
        assert d.work_item_kind == "design"

    def test_outcome_lowercased(self):
        d = make_design_review_evidence("designs/add-foo.md", "APPROVED", 3, 0)
        assert d.outcome == "approved"

    def test_needs_revision_outcome(self):
        d = make_design_review_evidence("designs/add-foo.md", "NEEDS_REVISION", 1, 2)
        assert d.outcome == "needs_revision"

    def test_counts_in_detail(self):
        d = make_design_review_evidence("designs/add-foo.md", "APPROVED", 4, 1)
        assert d.detail["strengths_count"] == 4
        assert d.detail["issues_count"] == 1


# ---------------------------------------------------------------------------
# evidence_from_effect_record
# ---------------------------------------------------------------------------


class TestEvidenceFromEffectRecord:
    def test_kind_is_effect(self):
        import datetime

        from millstone.policy.effects import (
            EffectClass,
            EffectIntent,
            EffectRecord,
            EffectStatus,
        )

        intent = EffectIntent(
            effect_class=EffectClass.transactional,
            description="close ticket",
            idempotency_key="TICKET-42",
        )
        rec = EffectRecord(
            intent=intent,
            status=EffectStatus.applied,
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        ev = evidence_from_effect_record(rec)
        assert ev.kind == EvidenceKind.effect

    def test_outcome_matches_effect_status(self):
        import datetime

        from millstone.policy.effects import (
            EffectClass,
            EffectIntent,
            EffectRecord,
            EffectStatus,
        )

        intent = EffectIntent(
            effect_class=EffectClass.transactional,
            description="close ticket",
        )
        rec = EffectRecord(
            intent=intent,
            status=EffectStatus.skipped,
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        ev = evidence_from_effect_record(rec)
        assert ev.outcome == "skipped"

    def test_capability_tier_stored(self):
        import datetime

        from millstone.policy.effects import (
            EffectClass,
            EffectIntent,
            EffectRecord,
            EffectStatus,
        )

        intent = EffectIntent(
            effect_class=EffectClass.operational,
            description="deploy",
            idempotency_key="deploy-v1",
        )
        rec = EffectRecord(
            intent=intent,
            status=EffectStatus.applied,
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        ev = evidence_from_effect_record(rec, capability_tier="C3_remote_critical")
        assert ev.capability_tier == "C3_remote_critical"


# ---------------------------------------------------------------------------
# TasklistManager.extract_current_task_metadata
# ---------------------------------------------------------------------------


class TestExtractCurrentTaskMetadata:
    def test_returns_task_id_from_raw_block(self, temp_repo):
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text(
            "# Tasklist\n\n"
            "- [ ] **Fix auth token**: update the auth module\n"
            "  - ID: fix-auth-token\n"
            "  - Risk: low\n"
        )
        mgr = TasklistManager(repo_dir=temp_repo)
        meta = mgr.extract_current_task_metadata()
        assert meta.get("task_id") == "fix-auth-token"

    def test_returns_empty_when_no_unchecked_task(self, temp_repo):
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [x] Done task\n")
        mgr = TasklistManager(repo_dir=temp_repo)
        assert mgr.extract_current_task_metadata() == {}

    def test_returns_empty_when_file_missing(self, tmp_path):
        mgr = TasklistManager(repo_dir=tmp_path)
        assert mgr.extract_current_task_metadata() == {}

    def test_returns_full_metadata_dict(self, temp_repo):
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text(
            "# Tasklist\n\n"
            "- [ ] **Refactor parser**: desc\n"
            "  - ID: refactor-parser\n"
            "  - Risk: medium\n"
        )
        mgr = TasklistManager(repo_dir=temp_repo)
        meta = mgr.extract_current_task_metadata()
        assert meta["task_id"] == "refactor-parser"
        assert meta["risk"] == "medium"


# ---------------------------------------------------------------------------
# Orchestrator integration: _evidence_store construction and emission
# ---------------------------------------------------------------------------


class TestOrchestratorEvidenceIntegration:
    def test_evidence_store_constructed(self, temp_repo):
        from millstone.runtime.orchestrator import Orchestrator

        orch = Orchestrator(
            repo_dir=str(temp_repo), tasklist="docs/tasklist.md", dry_run=False, quiet=True
        )
        assert hasattr(orch, "_evidence_store")
        assert isinstance(orch._evidence_store, EvidenceStore)

    def test_save_task_metrics_approved_emits_evidence(self, temp_repo):
        from millstone.runtime.orchestrator import Orchestrator

        orch = Orchestrator(
            repo_dir=str(temp_repo), tasklist="docs/tasklist.md", dry_run=False, quiet=True
        )
        orch._task_start_time = None
        orch._task_tokens_in = 0
        orch._task_tokens_out = 0
        orch._task_review_cycles = 1
        orch._task_review_duration_ms = 500
        orch._task_findings_count = 2
        orch._task_findings_by_severity = {
            "critical": 0,
            "high": 0,
            "medium": 2,
            "low": 0,
            "nit": 0,
        }
        orch.current_task_group = None

        with patch.object(orch._eval_manager, "save_task_metrics", return_value=Path("/tmp/x")):
            orch.save_task_metrics("fix auth", "approved", 1)

        records = orch._evidence_store.list(kind=EvidenceKind.review)
        assert len(records) == 1
        assert records[0].outcome == "approved"

    def test_save_task_metrics_failure_does_not_emit_evidence(self, temp_repo):
        from millstone.runtime.orchestrator import Orchestrator

        orch = Orchestrator(
            repo_dir=str(temp_repo), tasklist="docs/tasklist.md", dry_run=False, quiet=True
        )
        orch._task_start_time = None
        orch._task_tokens_in = 0
        orch._task_tokens_out = 0
        orch._task_review_cycles = 0
        orch._task_review_duration_ms = 0
        orch._task_findings_count = 0
        orch._task_findings_by_severity = {
            "critical": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
            "nit": 0,
        }
        orch.current_task_group = None

        with patch.object(orch._eval_manager, "save_task_metrics", return_value=Path("/tmp/x")):
            orch.save_task_metrics("fix auth", "eval_gate_failed", 1)

        records = orch._evidence_store.list(kind=EvidenceKind.review)
        assert len(records) == 0

    def test_run_eval_emits_eval_evidence(self, temp_repo):
        from millstone.runtime.orchestrator import Orchestrator

        orch = Orchestrator(
            repo_dir=str(temp_repo), tasklist="docs/tasklist.md", dry_run=False, quiet=True
        )

        fake_result = {
            "_passed": True,
            "tests": {"passed": 10, "failed": 0},
            "composite_score": 1.0,
            "duration_seconds": 2.0,
            "_eval_file": "20260301.json",
        }

        def _side_effect(**kwargs):
            # Invoke the evidence callback so the orchestrator wrapper's emission fires.
            cb = kwargs.get("emit_evidence_callback")
            if cb:
                cb(fake_result)
            return fake_result

        with patch.object(orch._eval_manager, "run_eval", side_effect=_side_effect):
            orch.run_eval()

        records = orch._evidence_store.list(kind=EvidenceKind.eval)
        assert len(records) == 1
        assert records[0].outcome == "passed"

    def test_review_design_emits_design_review_evidence(self, temp_repo):
        from millstone.runtime.orchestrator import Orchestrator

        orch = Orchestrator(
            repo_dir=str(temp_repo), tasklist="docs/tasklist.md", dry_run=False, quiet=True
        )

        fake_result = {
            "approved": True,
            "verdict": "APPROVED",
            "strengths": ["good scope", "clear criteria"],
            "issues": [],
        }
        with patch.object(orch._outer_loop_manager, "review_design", return_value=fake_result):
            orch.review_design("designs/add-foo.md")

        records = orch._evidence_store.list(kind=EvidenceKind.design_review)
        assert len(records) == 1
        assert records[0].outcome == "approved"
        assert records[0].work_item_id == "add-foo"
