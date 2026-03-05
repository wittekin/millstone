import subprocess
from pathlib import Path
from unittest.mock import patch

from millstone.runtime.locks import AdvisoryLock
from millstone.runtime.merge_pipeline import MergePipeline, run_safety_gates
from millstone.runtime.worktree import WorktreeManager


def _git(repo_dir: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=check,
    ).stdout


def _rev_parse(repo_dir: Path, ref: str) -> str:
    return _git(repo_dir, "rev-parse", ref).strip()


def _current_branch(repo_dir: Path) -> str:
    return _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").strip()


def _commit_file(repo_dir: Path, path: str, content: str, msg: str) -> str:
    p = repo_dir / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    subprocess.run(["git", "add", path], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo_dir, capture_output=True, check=True)
    return _rev_parse(repo_dir, "HEAD")


class _DummyEvalManager:
    def __init__(self, passed: bool):
        self.passed = passed
        self.modes: list[str | None] = []

    def run_eval(self, coverage: bool = False, mode: str | None = None, **_kwargs) -> dict:
        self.modes.append(mode)
        return {"_passed": self.passed}


def _eval_factory(passed: bool):
    def _factory(**_kwargs):
        return _DummyEvalManager(passed)

    return _factory


class TestSafetyGates:
    def test_safety_loc_threshold_exceeded(self, temp_repo):
        base = _rev_parse(temp_repo, "HEAD")
        _commit_file(temp_repo, "a.txt", "1\n2\n3\n4\n5\n", "add lines")
        policy = {"limits": {"max_loc_per_task": 1}}
        ok, reason = run_safety_gates(temp_repo, base_ref=base, head_ref="HEAD", policy=policy, loc_threshold=1)
        assert ok is False
        assert "loc_threshold_exceeded" in reason

    def test_safety_sensitive_path_detected(self, temp_repo):
        base = _rev_parse(temp_repo, "HEAD")
        _commit_file(temp_repo, ".env", "SECRET=1\n", "touch env")
        policy = {
            "limits": {"max_loc_per_task": 1000},
            "sensitive": {"enabled": True, "paths": [".env"], "require_approval": True},
        }
        ok, reason = run_safety_gates(temp_repo, base_ref=base, head_ref="HEAD", policy=policy, loc_threshold=1000)
        assert ok is False
        assert reason.startswith("sensitive_path:")

    def test_safety_dangerous_pattern_detected(self, temp_repo):
        base = _rev_parse(temp_repo, "HEAD")
        _commit_file(temp_repo, "danger.txt", "rm -rf /tmp\n", "danger")
        policy = {
            "limits": {"max_loc_per_task": 1000},
            "dangerous": {"patterns": ["rm -rf"], "block": True},
        }
        ok, reason = run_safety_gates(temp_repo, base_ref=base, head_ref="HEAD", policy=policy, loc_threshold=1000)
        assert ok is False
        assert reason.startswith("dangerous_pattern:")

    def test_safety_all_clear(self, temp_repo):
        base = _rev_parse(temp_repo, "HEAD")
        _commit_file(temp_repo, "ok.txt", "hi\n", "ok")
        policy = {"limits": {"max_loc_per_task": 1000}}
        ok, reason = run_safety_gates(temp_repo, base_ref=base, head_ref="HEAD", policy=policy, loc_threshold=1000)
        assert ok is True
        assert reason == ""


class TestMergePipeline:
    def _setup(self, temp_repo, merge_strategy: str):
        base_branch = _current_branch(temp_repo)
        base_ref_sha = _rev_parse(temp_repo, "HEAD")

        # Create a task branch from the base snapshot.
        git_lock = AdvisoryLock(temp_repo / ".millstone" / "locks" / "git.lock", timeout=5.0)
        mgr = WorktreeManager(temp_repo, temp_repo / ".millstone" / "worktrees", git_lock=git_lock)
        task_wt = mgr.create_task_worktree("t1", base_ref_sha)
        _commit_file(task_wt, "task.txt", "from task\n", "task change")
        commit_sha = _rev_parse(task_wt, "HEAD")

        # Advance base branch with a tasklist that includes the task ID.
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n- [ ] **Do thing**: test\n  - ID: t1\n"
        )
        subprocess.run(["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "add task id"], cwd=temp_repo, capture_output=True, check=True)

        # Detach HEAD so base_branch is not checked out when pushing.
        subprocess.run(["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True)

        integration_branch = "millstone/integration"
        integration_wt = mgr.create_integration_worktree(integration_branch, base_ref_sha)

        policy = {"limits": {"max_loc_per_task": 1000}}
        tasklist_lock = AdvisoryLock(temp_repo / ".millstone" / "locks" / "tasklist.lock", timeout=5.0)
        pipeline = MergePipeline(
            repo_dir=temp_repo,
            integration_worktree=integration_wt,
            base_branch=base_branch,
            integration_branch=integration_branch,
            merge_strategy=merge_strategy,
            git_lock=git_lock,
            tasklist_lock=tasklist_lock,
            policy=policy,
            loc_threshold=1000,
            max_retries=1,
        )
        return pipeline, base_branch, base_ref_sha, commit_sha, integration_wt, task_wt

    def test_cherry_pick_success(self, temp_repo):
        pipeline, base_branch, base_ref_sha, commit_sha, _integration_wt, _task_wt = self._setup(
            temp_repo, merge_strategy="cherry-pick"
        )
        res = pipeline.integrate_eval_and_land(
            task_id="t1",
            task_branch="millstone/task/t1",
            base_ref_sha=base_ref_sha,
            commit_sha=commit_sha,
            task_risk="low",
            taskmap={},
            eval_manager_factory=_eval_factory(True),
        )
        assert res.success is True
        assert res.status == "merged"
        # Base branch ref updated to include task file.
        base_head = _rev_parse(temp_repo, base_branch)
        assert "from task" in _git(temp_repo, "show", f"{base_head}:task.txt")

    def test_cherry_pick_success_when_task_already_checked(self, temp_repo):
        base_branch = _current_branch(temp_repo)
        base_ref_sha = _rev_parse(temp_repo, "HEAD")

        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n- [ ] **Do thing**: test\n  - ID: t1\n"
        )
        subprocess.run(["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "add task id"], cwd=temp_repo, capture_output=True, check=True)
        base_ref_sha = _rev_parse(temp_repo, "HEAD")

        git_lock = AdvisoryLock(temp_repo / ".millstone" / "locks" / "git.lock", timeout=5.0)
        mgr = WorktreeManager(temp_repo, temp_repo / ".millstone" / "worktrees", git_lock=git_lock)
        task_wt = mgr.create_task_worktree("t1", base_ref_sha)
        _commit_file(task_wt, "task.txt", "from task\n", "task change")
        _commit_file(
            task_wt,
            "docs/tasklist.md",
            "# Tasklist\n\n- [x] **Do thing**: test\n  - ID: t1\n",
            "check task in tasklist",
        )
        commit_sha = _rev_parse(task_wt, "HEAD")

        subprocess.run(["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True)
        integration_branch = "millstone/integration"
        integration_wt = mgr.create_integration_worktree(integration_branch, base_ref_sha)

        pipeline = MergePipeline(
            repo_dir=temp_repo,
            integration_worktree=integration_wt,
            base_branch=base_branch,
            integration_branch=integration_branch,
            merge_strategy="cherry-pick",
            git_lock=git_lock,
            tasklist_lock=AdvisoryLock(temp_repo / ".millstone" / "locks" / "tasklist.lock", timeout=5.0),
            policy={"limits": {"max_loc_per_task": 1000}},
            loc_threshold=1000,
            max_retries=1,
        )
        res = pipeline.integrate_eval_and_land(
            task_id="t1",
            task_branch="millstone/task/t1",
            base_ref_sha=base_ref_sha,
            commit_sha=commit_sha,
            task_risk="low",
            taskmap={},
            eval_manager_factory=_eval_factory(True),
        )
        assert res.success is True
        assert res.status == "merged"
        base_head = _rev_parse(temp_repo, base_branch)
        tasklist = _git(temp_repo, "show", f"{base_head}:docs/tasklist.md")
        assert "- [x] **Do thing**: test" in tasklist

    def test_merge_noff_success(self, temp_repo):
        pipeline, base_branch, base_ref_sha, commit_sha, integration_wt, _task_wt = self._setup(
            temp_repo, merge_strategy="merge"
        )
        res = pipeline.integrate_eval_and_land(
            task_id="t1",
            task_branch="millstone/task/t1",
            base_ref_sha=base_ref_sha,
            commit_sha=commit_sha,
            task_risk="low",
            taskmap={},
            eval_manager_factory=_eval_factory(True),
        )
        assert res.success is True
        assert res.status == "merged"
        # Merge commit exists (HEAD^ is tasklist commit; HEAD^^ is merge commit).
        parents = _git(integration_wt, "rev-list", "--parents", "-n", "1", "HEAD^").strip().split()
        assert len(parents) == 3  # merge commit + 2 parents
        base_head = _rev_parse(temp_repo, base_branch)
        assert "from task" in _git(temp_repo, "show", f"{base_head}:task.txt")

    def test_conflict_aborts_cleanly(self, temp_repo):
        base_branch = _current_branch(temp_repo)
        base_ref_sha = _rev_parse(temp_repo, "HEAD")

        git_lock = AdvisoryLock(temp_repo / ".millstone" / "locks" / "git.lock", timeout=5.0)
        mgr = WorktreeManager(temp_repo, temp_repo / ".millstone" / "worktrees", git_lock=git_lock)

        task_wt = mgr.create_task_worktree("t1", base_ref_sha)
        _commit_file(task_wt, "conflict.txt", "task\n", "task change")

        # Advance base with conflicting change.
        _commit_file(temp_repo, "conflict.txt", "base\n", "base change")
        subprocess.run(["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True)
        integration_branch = "millstone/integration"
        integration_wt = mgr.create_integration_worktree(integration_branch, base_ref_sha)

        pipeline = MergePipeline(
            repo_dir=temp_repo,
            integration_worktree=integration_wt,
            base_branch=base_branch,
            integration_branch=integration_branch,
            merge_strategy="cherry-pick",
            git_lock=git_lock,
            tasklist_lock=AdvisoryLock(temp_repo / ".millstone" / "locks" / "tasklist.lock", timeout=5.0),
            policy={"limits": {"max_loc_per_task": 1000}},
            loc_threshold=1000,
            max_retries=0,
        )

        res = pipeline.integrate_eval_and_land(
            task_id="t1",
            task_branch="millstone/task/t1",
            base_ref_sha=base_ref_sha,
            commit_sha=None,
            task_risk="low",
            taskmap={},
            eval_manager_factory=_eval_factory(True),
        )
        assert res.success is False
        assert res.status == "conflict"
        # Integration worktree should be clean after abort/reset.
        assert _git(integration_wt, "status", "--porcelain").strip() == ""

    def test_safety_gate_failure(self, temp_repo):
        pipeline, _base_branch, base_ref_sha, commit_sha, integration_wt, _task_wt = self._setup(
            temp_repo, merge_strategy="cherry-pick"
        )
        # Force a failure via tiny LoC threshold.
        pipeline.loc_threshold = 0
        res = pipeline.integrate_eval_and_land(
            task_id="t1",
            task_branch="millstone/task/t1",
            base_ref_sha=base_ref_sha,
            commit_sha=commit_sha,
            task_risk="low",
            taskmap={},
            eval_manager_factory=_eval_factory(True),
        )
        assert res.success is False
        assert res.status == "safety_fail"
        assert _git(integration_wt, "status", "--porcelain").strip() == ""

    def test_eval_gate_failure(self, temp_repo):
        pipeline, _base_branch, base_ref_sha, commit_sha, integration_wt, _task_wt = self._setup(
            temp_repo, merge_strategy="cherry-pick"
        )
        res = pipeline.integrate_eval_and_land(
            task_id="t1",
            task_branch="millstone/task/t1",
            base_ref_sha=base_ref_sha,
            commit_sha=commit_sha,
            task_risk="low",
            taskmap={},
            eval_manager_factory=_eval_factory(False),
        )
        assert res.success is False
        assert res.status == "eval_fail"
        assert _git(integration_wt, "status", "--porcelain").strip() == ""

    def test_stale_base_retry(self, temp_repo):
        pipeline, _base_branch, base_ref_sha, commit_sha, _integration_wt, _task_wt = self._setup(
            temp_repo, merge_strategy="cherry-pick"
        )

        real_run = subprocess.run
        calls = {"push": 0}

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[:2] == ["git", "push"]:
                calls["push"] += 1
                if calls["push"] == 1:
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="rejected non-fast-forward")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch("millstone.runtime.merge_pipeline.subprocess.run", side_effect=fake_run):
            res = pipeline.integrate_eval_and_land(
                task_id="t1",
                task_branch="millstone/task/t1",
                base_ref_sha=base_ref_sha,
                commit_sha=commit_sha,
                task_risk="low",
                taskmap={},
                eval_manager_factory=_eval_factory(True),
            )
        assert res.success is True
        assert calls["push"] == 2

    def test_git_lock_held_entire_pipeline(self, temp_repo):
        pipeline, _base_branch, base_ref_sha, commit_sha, _integration_wt, _task_wt = self._setup(
            temp_repo, merge_strategy="cherry-pick"
        )

        class CountingLock:
            def __init__(self):
                self.enters = 0
                self.exits = 0

            def __enter__(self):
                self.enters += 1
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                self.exits += 1

        pipeline.git_lock = CountingLock()  # type: ignore[assignment]
        res = pipeline.integrate_eval_and_land(
            task_id="t1",
            task_branch="millstone/task/t1",
            base_ref_sha=base_ref_sha,
            commit_sha=commit_sha,
            task_risk="low",
            taskmap={},
            eval_manager_factory=_eval_factory(True),
        )
        assert res.status in ("merged", "conflict", "safety_fail", "eval_fail", "land_fail")
        assert pipeline.git_lock.enters == 1  # type: ignore[attr-defined]
        assert pipeline.git_lock.exits == 1  # type: ignore[attr-defined]

    def test_tasklist_bound_to_integration_worktree(self, temp_repo):
        pipeline, _base_branch, _base_ref_sha, _commit_sha, integration_wt, _task_wt = self._setup(
            temp_repo, merge_strategy="cherry-pick"
        )
        assert pipeline.tasklist_manager.repo_dir == integration_wt
