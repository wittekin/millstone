import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from millstone.runtime.merge_pipeline import IntegrationResult
from millstone.runtime.orchestrator import Orchestrator
from millstone.runtime.parallel import ParallelOrchestrator, SubprocessWorkerHandle, WorkerHandle


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


class _DummyEvalManager:
    def __init__(self, passed: bool = True):
        self.passed = passed

    def run_eval(self, mode=None, **_kwargs) -> dict:
        return {"_passed": self.passed, "mode": mode}


def _eval_factory(passed: bool = True):
    def _factory(**_kwargs):
        return _DummyEvalManager(passed)

    return _factory


class _StubWorkerHandle:
    def __init__(self):
        self.pid = None
        self.cancelled = False

    def poll(self) -> int | None:
        return None

    def kill(self) -> None:
        self.cancelled = True


class _ThreadBackedHandle:
    def __init__(self, target):
        self.pid = None
        self._target = target
        self._killed = False
        self._returncode: int | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._target()
            if self._returncode is None:
                self._returncode = 0
        except Exception:
            self._returncode = 1

    def poll(self) -> int | None:
        if self._killed:
            return -9
        if self._thread.is_alive():
            return None
        if self._returncode is None:
            return 1
        return self._returncode

    def kill(self) -> None:
        self._killed = True
        self._returncode = -9


class _ImmediateHandle:
    def __init__(self, returncode: int):
        self.pid = None
        self._returncode = returncode
        self.killed = False

    def poll(self) -> int | None:
        return self._returncode

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9


class _NeverFinishingHandle:
    def __init__(self):
        self.pid = None
        self.killed = False

    def poll(self) -> int | None:
        return -9 if self.killed else None

    def kill(self) -> None:
        self.killed = True


class TestWorkerHandleProtocol:
    def test_worker_handle_protocol_subprocess(self, monkeypatch, tmp_path):
        captured: dict[str, object] = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                captured["kwargs"] = kwargs
                self.pid = 321

            def poll(self):
                return 7

            def communicate(self):
                return ("", "")

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)

        handle = SubprocessWorkerHandle.spawn(["python", "-V"], cwd=tmp_path)
        assert isinstance(handle, WorkerHandle)
        assert handle.pid == 321
        assert handle.poll() == 7
        assert captured["cmd"] == ["python", "-V"]
        assert captured["kwargs"] == {
            "cwd": tmp_path,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "start_new_session": True,
        }

    def test_worker_handle_protocol_stub(self):
        stub = _StubWorkerHandle()
        assert isinstance(stub, WorkerHandle)
        assert stub.poll() is None
        stub.kill()
        assert stub.cancelled is True

    def test_worker_handle_drains_pipe_output(self, tmp_path):
        script = (
            "import sys\n"
            "chunk = 'x' * 65536\n"
            "for _ in range(48):\n"
            "    sys.stdout.write(chunk)\n"
            "sys.stdout.flush()\n"
        )
        handle = SubprocessWorkerHandle.spawn([sys.executable, "-c", script], cwd=tmp_path)

        deadline = time.time() + 5.0
        returncode = None
        while time.time() < deadline:
            returncode = handle.poll()
            if returncode is not None:
                break
            time.sleep(0.02)

        if returncode is None:
            handle.kill()
        stdout, _stderr = handle.communicate()
        assert returncode == 0
        assert stdout

    def test_worker_handle_kill_process_group(self, monkeypatch):
        class _FakePopen:
            def __init__(self):
                self.pid = 444

            def poll(self):
                return None

        handle = SubprocessWorkerHandle(_FakePopen())
        calls: dict[str, int] = {}

        def _fake_getpgid(pid: int) -> int:
            calls["pid"] = pid
            return 777

        def _fake_killpg(pgid: int, _sig: int) -> None:
            calls["pgid"] = pgid

        monkeypatch.setattr("millstone.runtime.parallel.os.getpgid", _fake_getpgid)
        monkeypatch.setattr("millstone.runtime.parallel.os.killpg", _fake_killpg)

        handle.kill()
        assert calls == {"pid": 444, "pgid": 777}


class TestParallelOrchestratorAnalyzeTasks:
    def test_analyze_tasks_enriches_metadata(self, temp_repo):
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n"
            "## Group: Core Engine\n\n"
            "- [ ] **Task One**: update millstone/parallel.py\n"
            "  - ID: t1\n"
            "  - Risk: high\n"
        )

        orch = Orchestrator(parallel_enabled=True, tasklist="docs/tasklist.md")
        po = ParallelOrchestrator(orch)

        task_ids = orch._tasklist_manager.extract_all_task_ids()
        enriched_tasks, dependencies = po._analyze_tasks(task_ids)

        assert dependencies == []
        assert len(enriched_tasks) == 1

        task = enriched_tasks[0]
        assert task["task_id"] == "t1"
        assert task["title"] == "Task One"
        assert task["group"] == "Core Engine"
        assert task["risk"] == "high"
        assert "millstone/parallel.py" in task["file_refs"]

    def test_analyze_tasks_converts_deps_to_ids(self, temp_repo):
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n"
            "- [ ] **Task One**: touch millstone/parallel.py\n"
            "  - ID: t1\n"
            "- [ ] **Task Two**: touch millstone/parallel.py\n"
            "  - ID: t2\n"
        )

        orch = Orchestrator(parallel_enabled=True, tasklist="docs/tasklist.md")
        po = ParallelOrchestrator(orch)

        task_ids = orch._tasklist_manager.extract_all_task_ids()
        enriched_tasks, dependencies = po._analyze_tasks(task_ids)

        assert len(enriched_tasks) == 2
        assert dependencies
        assert dependencies[0]["from_id"] == "t1"
        assert dependencies[0]["to_id"] == "t2"
        assert dependencies[0]["type"] == "file_overlap"

    def test_analyze_tasks_default_metadata(self, temp_repo):
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n- [ ] simple task without metadata\n"
        )

        orch = Orchestrator(parallel_enabled=True, tasklist="docs/tasklist.md")
        po = ParallelOrchestrator(orch)

        task_ids = orch._tasklist_manager.extract_all_task_ids()
        enriched_tasks, _dependencies = po._analyze_tasks(task_ids)

        assert len(enriched_tasks) == 1
        assert enriched_tasks[0]["group"] is None
        assert enriched_tasks[0]["risk"] is None
        assert enriched_tasks[0]["file_refs"] == []

    def test_analyze_tasks_preserves_dep_direction(self, temp_repo, monkeypatch):
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n"
            "- [ ] **Task Alpha**: first\n"
            "  - ID: a\n"
            "- [ ] **Task Beta**: second\n"
            "  - ID: b\n"
        )

        orch = Orchestrator(parallel_enabled=True, tasklist="docs/tasklist.md")
        po = ParallelOrchestrator(orch)

        def fake_analyze_tasklist(*_args, **_kwargs):
            return {
                "tasks": [],
                "dependencies": [
                    {"from_idx": 1, "to_idx": 0, "reason": "synthetic", "type": "explicit"}
                ],
            }

        monkeypatch.setattr(orch._tasklist_manager, "analyze_tasklist", fake_analyze_tasklist)

        task_ids = orch._tasklist_manager.extract_all_task_ids()
        _enriched_tasks, dependencies = po._analyze_tasks(task_ids)

        assert dependencies == [
            {"from_id": "b", "to_id": "a", "reason": "synthetic", "type": "explicit"}
        ]


class TestParallelOrchestratorPhase1:
    def test_sequential_two_tasks_land(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n"
            "- [ ] **Task One**: add file\n"
            "  - ID: t1\n"
            "  - Risk: low\n"
            "- [ ] **Task Two**: add file\n"
            "  - ID: t2\n"
            "  - Risk: low\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "add task ids"], cwd=temp_repo, capture_output=True, check=True
        )

        # Detach so base branch is not checked out (required for git push . landing).
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            tasklist="docs/tasklist.md",
            merge_strategy="cherry-pick",
            worktree_cleanup="always",
            loc_threshold=1000,
        )

        worktrees_seen: list[Path] = []

        def worker_runner(task_id: str, _task_text: str, worktree_path: Path) -> dict:
            worktrees_seen.append(worktree_path)
            (worktree_path / f"{task_id}.txt").write_text(f"{task_id}\n")
            subprocess.run(["git", "add", "."], cwd=worktree_path, capture_output=True, check=True)
            subprocess.run(
                ["git", "commit", "-m", f"task {task_id}"],
                cwd=worktree_path,
                capture_output=True,
                check=True,
            )
            return {
                "status": "success",
                "commit_sha": _rev_parse(worktree_path, "HEAD"),
                "risk": "low",
            }

        po = ParallelOrchestrator(
            orch,
            worker_runner=worker_runner,
            eval_manager_factory=_eval_factory(True),
        )
        rc = po.run()
        assert rc == 0
        assert len(worktrees_seen) == 2
        assert worktrees_seen[0] != worktrees_seen[1]

        # Base branch contains both files.
        base_head = _rev_parse(temp_repo, base_branch)
        assert "t1" in _git(temp_repo, "show", f"{base_head}:t1.txt")
        assert "t2" in _git(temp_repo, "show", f"{base_head}:t2.txt")
        cl = _git(temp_repo, "show", f"{base_head}:docs/tasklist.md")
        assert "- [x] **Task One**" in cl
        assert "- [x] **Task Two**" in cl

    def test_dry_run_no_side_effects(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        orch = Orchestrator(parallel_enabled=True, dry_run=True, base_branch=base_branch)
        po = ParallelOrchestrator(orch)
        rc = po.run()
        assert rc == 0
        assert not (temp_repo / ".millstone" / "worktrees").exists()

    def test_merge_conflict_marks_blocked(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n"
            "- [ ] **Task One**: add shared\n"
            "  - ID: t1\n"
            "- [ ] **Task Two**: add shared\n"
            "  - ID: t2\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "add tasks"], cwd=temp_repo, capture_output=True, check=True
        )

        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            tasklist="docs/tasklist.md",
            merge_strategy="cherry-pick",
            worktree_cleanup="on_success",
            loc_threshold=1000,
        )

        def worker_runner(task_id: str, _task_text: str, worktree_path: Path) -> dict:
            # Both tasks add the same file, causing add/add conflict on task 2.
            (worktree_path / "shared.txt").write_text(f"{task_id}\n")
            subprocess.run(["git", "add", "."], cwd=worktree_path, capture_output=True, check=True)
            subprocess.run(
                ["git", "commit", "-m", f"task {task_id}"],
                cwd=worktree_path,
                capture_output=True,
                check=True,
            )
            return {
                "status": "success",
                "commit_sha": _rev_parse(worktree_path, "HEAD"),
                "risk": "low",
            }

        po = ParallelOrchestrator(
            orch,
            worker_runner=worker_runner,
            eval_manager_factory=_eval_factory(True),
        )
        rc = po.run()
        assert rc == 1

        state = po.parallel_state.load_control_state()
        assert state is not None
        assert state["task_records"]["t2"]["status"] == "blocked"

    def test_injectable_worker_runner(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )
        orch = Orchestrator(base_branch=base_branch, parallel_enabled=True, dry_run=True)
        called = {"n": 0}

        def worker_runner(task_id: str, task_text: str, worktree_path: Path) -> dict:
            called["n"] += 1
            return {"status": "success"}

        po = ParallelOrchestrator(orch, worker_runner=worker_runner)
        po.run()
        assert called["n"] == 0  # dry-run never calls worker runner

    def test_validate_layout_fails_when_checked_out(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        orch = Orchestrator(base_branch=base_branch, parallel_enabled=True)
        po = ParallelOrchestrator(orch, eval_manager_factory=_eval_factory(True))
        rc = po.run()
        assert rc == 1

    def test_detached_head_without_base_branch_fails(self, temp_repo):
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )
        orch = Orchestrator(parallel_enabled=True)
        po = ParallelOrchestrator(orch, eval_manager_factory=_eval_factory(True))
        rc = po.run()
        assert rc == 1

    def test_recover_state_removes_stale_worktrees(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        base_ref_sha = _rev_parse(temp_repo, "HEAD")

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            parallel_heartbeat_ttl=0.1,
        )
        po = ParallelOrchestrator(orch, eval_manager_factory=_eval_factory(True))

        stale_wt = po.worktree_mgr.create_task_worktree("stale", base_ref_sha)
        po.parallel_state.save_control_state(
            base_ref_sha=base_ref_sha,
            base_branch=base_branch,
            integration_branch=orch.parallel_integration_branch,
            integration_worktree=po.worktree_mgr._integration_worktree_path(),
            task_records={"stale": {"status": "running"}},
            merge_queue=[],
        )
        hb_path = po.parallel_state.shared_state_dir / "tasks" / "stale" / "heartbeat"
        hb_path.parent.mkdir(parents=True, exist_ok=True)
        hb_path.write_text("1.0\n")  # definitely stale

        po._recover_state()

        assert not stale_wt.exists()
        branch_exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/millstone/task/stale"],
            cwd=temp_repo,
        ).returncode
        assert branch_exists != 0

        state = po.parallel_state.load_control_state()
        assert state is not None
        assert state["task_records"]["stale"]["status"] == "failed"
        assert "prior_run" in state["task_records"]["stale"]["error"]

    def test_recover_state_fails_on_fresh_heartbeat(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        base_ref_sha = _rev_parse(temp_repo, "HEAD")

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            parallel_heartbeat_ttl=300,
        )
        po = ParallelOrchestrator(orch, eval_manager_factory=_eval_factory(True))

        stale_wt = po.worktree_mgr.create_task_worktree("alive", base_ref_sha)
        po.parallel_state.save_control_state(
            base_ref_sha=base_ref_sha,
            base_branch=base_branch,
            integration_branch=orch.parallel_integration_branch,
            integration_worktree=po.worktree_mgr._integration_worktree_path(),
            task_records={"alive": {"status": "running"}},
            merge_queue=[],
        )
        po.parallel_state.write_heartbeat("alive")

        try:
            with pytest.raises(RuntimeError):
                po._recover_state()
        finally:
            # Ensure cleanup can proceed in later tests if needed.
            po.worktree_mgr.remove_worktree(stale_wt, delete_branch=True)

    def test_e2e_crash_recovery_then_run_succeeds(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n- [ ] **Task One**: add file\n  - ID: t1\n  - Risk: low\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "single task"], cwd=temp_repo, capture_output=True, check=True
        )
        base_ref_sha = _rev_parse(temp_repo, "HEAD")

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            tasklist="docs/tasklist.md",
            merge_strategy="cherry-pick",
            worktree_cleanup="always",
            parallel_heartbeat_ttl=300,
            loc_threshold=1000,
        )
        po = ParallelOrchestrator(orch, eval_manager_factory=_eval_factory(True))

        stale_wt = po.worktree_mgr.create_task_worktree("stale", base_ref_sha)
        po.parallel_state.save_control_state(
            base_ref_sha=base_ref_sha,
            base_branch=base_branch,
            integration_branch=orch.parallel_integration_branch,
            integration_worktree=po.worktree_mgr._integration_worktree_path(),
            task_records={"stale": {"status": "running"}},
            merge_queue=[],
        )
        hb_path = po.parallel_state.shared_state_dir / "tasks" / "stale" / "heartbeat"
        hb_path.parent.mkdir(parents=True, exist_ok=True)
        hb_path.write_text("1.0\n")

        # Detach so landing via git push . is allowed.
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        def worker_runner(task_id: str, _task_text: str, worktree_path: Path) -> dict:
            (worktree_path / "recovered.txt").write_text(f"{task_id}\n")
            subprocess.run(["git", "add", "."], cwd=worktree_path, capture_output=True, check=True)
            subprocess.run(
                ["git", "commit", "-m", f"task {task_id}"],
                cwd=worktree_path,
                capture_output=True,
                check=True,
            )
            return {
                "status": "success",
                "commit_sha": _rev_parse(worktree_path, "HEAD"),
                "risk": "low",
            }

        po.worker_runner = worker_runner
        rc = po.run()
        assert rc == 0
        assert not stale_wt.exists()

        base_head = _rev_parse(temp_repo, base_branch)
        assert "t1" in _git(temp_repo, "show", f"{base_head}:recovered.txt")

    def test_e2e_worker_tasklist_edit_blocked(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n- [ ] **Task One**: try forbidden edit\n  - ID: t1\n  - Risk: low\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "single task"], cwd=temp_repo, capture_output=True, check=True
        )

        # Detach so landing path is available (run should still fail due worker failure).
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            merge_strategy="cherry-pick",
            worktree_cleanup="always",
            loc_threshold=1000,
        )
        po = ParallelOrchestrator(orch, eval_manager_factory=_eval_factory(True))
        blocked = {"seen": False}

        def worker_runner(_task_id: str, task_text: str, worktree_path: Path) -> dict:
            worker = Orchestrator(
                task=task_text,
                repo_dir=worktree_path,
                no_tasklist_edits=True,
                max_cycles=1,
                skip_eval=True,
            )
            worker.loc_baseline_ref = worker.git("rev-parse", "HEAD").strip()

            def fake_run_agent(*_a, role=None, **_k):
                if role == "author":
                    tasklist = worktree_path / "docs" / "tasklist.md"
                    tasklist.write_text(tasklist.read_text() + "\n- [ ] sneaky\n")
                    subprocess.run(
                        ["git", "add", "docs/tasklist.md"],
                        cwd=worktree_path,
                        capture_output=True,
                        check=True,
                    )
                    subprocess.run(
                        ["git", "commit", "-m", "sneaky tasklist edit"],
                        cwd=worktree_path,
                        capture_output=True,
                        check=True,
                    )
                return "ok"

            worker.run_agent = fake_run_agent
            worker.sanity_check_impl = lambda *_a, **_k: True
            worker.sanity_check_review = lambda *_a, **_k: True
            try:
                blocked["seen"] = worker.run_single_task() is False
            finally:
                worker.cleanup()

            # Return an invalid commit to ensure the control plane marks this task failed.
            return {
                "status": "failed",
                "commit_sha": "deadbeef",
                "risk": "low",
                "error": "worker_failed",
            }

        po.worker_runner = worker_runner
        rc = po.run()
        assert rc == 1
        assert blocked["seen"] is True

    def test_e2e_cleanup_preserves_parallel_dirs(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n- [ ] **Task One**: add file\n  - ID: t1\n  - Risk: low\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "single task"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            tasklist="docs/tasklist.md",
            merge_strategy="cherry-pick",
            worktree_cleanup="always",
            loc_threshold=1000,
        )
        po = ParallelOrchestrator(orch, eval_manager_factory=_eval_factory(True))

        def worker_runner(task_id: str, _task_text: str, worktree_path: Path) -> dict:
            (worktree_path / "ok.txt").write_text(f"{task_id}\n")
            subprocess.run(["git", "add", "."], cwd=worktree_path, capture_output=True, check=True)
            subprocess.run(
                ["git", "commit", "-m", "ok"], cwd=worktree_path, capture_output=True, check=True
            )
            return {
                "status": "success",
                "commit_sha": _rev_parse(worktree_path, "HEAD"),
                "risk": "low",
            }

        po.worker_runner = worker_runner
        assert po.run() == 0

        parallel_dir = temp_repo / ".millstone" / "parallel"
        assert parallel_dir.exists()
        orch.cleanup()
        assert parallel_dir.exists()


class TestParallelOrchestratorPhase2:
    @staticmethod
    def _commit_task_file(worktree_path: Path, task_id: str) -> str:
        (worktree_path / f"{task_id}.txt").write_text(f"{task_id}\n")
        subprocess.run(["git", "add", "."], cwd=worktree_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"task {task_id}"],
            cwd=worktree_path,
            capture_output=True,
            check=True,
        )
        return _rev_parse(worktree_path, "HEAD")

    def test_worker_command_forwards_cli_and_cycle_settings(self, temp_repo):
        orch = Orchestrator(
            repo_dir=temp_repo,
            parallel_enabled=True,
            max_cycles=4,
            loc_threshold=777,
            cli="codex",
            cli_builder="codex",
            cli_reviewer="codex",
            cli_sanity="codex",
            cli_analyzer="codex",
        )
        po = ParallelOrchestrator(orch)
        cmd = po._worker_command("task text", temp_repo / ".millstone" / "worktrees" / "task-t1")

        assert "--cli" in cmd
        assert cmd[cmd.index("--cli") + 1] == "codex"
        assert "--cli-builder" in cmd
        assert cmd[cmd.index("--cli-builder") + 1] == "codex"
        assert "--cli-reviewer" in cmd
        assert cmd[cmd.index("--cli-reviewer") + 1] == "codex"
        assert "--cli-sanity" in cmd
        assert cmd[cmd.index("--cli-sanity") + 1] == "codex"
        assert "--cli-analyzer" in cmd
        assert cmd[cmd.index("--cli-analyzer") + 1] == "codex"
        assert "--max-cycles" in cmd
        assert cmd[cmd.index("--max-cycles") + 1] == "4"
        assert "--loc-threshold" in cmd
        assert cmd[cmd.index("--loc-threshold") + 1] == "777"
        assert "--no-tasklist-edits" not in cmd

    def test_worker_command_can_opt_in_to_no_tasklist_edits(self, temp_repo):
        orch = Orchestrator(
            repo_dir=temp_repo,
            parallel_enabled=True,
            no_tasklist_edits=True,
            cli="codex",
        )
        po = ParallelOrchestrator(orch)
        cmd = po._worker_command("task text", temp_repo / ".millstone" / "worktrees" / "task-t1")
        assert "--no-tasklist-edits" in cmd

    def test_concurrent_two_independent_tasks(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n"
            "- [ ] **Task One**: add t1\n"
            "  - ID: t1\n"
            "  - Risk: low\n"
            "- [ ] **Task Two**: add t2\n"
            "  - ID: t2\n"
            "  - Risk: low\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "add tasks"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            tasklist="docs/tasklist.md",
            parallel_concurrency=2,
            merge_strategy="cherry-pick",
            worktree_cleanup="always",
            loc_threshold=1000,
        )
        po_ref: dict[str, ParallelOrchestrator] = {}
        active = 0
        max_active = 0
        lock = threading.Lock()

        def spawn_worker(task_id: str, _task_text: str, worktree_path: Path):
            def _run() -> None:
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.15)
                commit_sha = self._commit_task_file(worktree_path, task_id)
                po_ref["po"].parallel_state.write_task_result(
                    task_id,
                    {"status": "success", "commit_sha": commit_sha, "risk": "low"},
                )
                with lock:
                    active -= 1

            return _ThreadBackedHandle(_run)

        po = ParallelOrchestrator(
            orch,
            spawn_worker_async=spawn_worker,
            eval_manager_factory=_eval_factory(True),
        )
        po_ref["po"] = po
        rc = po.run()
        assert rc == 0
        assert max_active >= 2

        base_head = _rev_parse(temp_repo, base_branch)
        assert "t1" in _git(temp_repo, "show", f"{base_head}:t1.txt")
        assert "t2" in _git(temp_repo, "show", f"{base_head}:t2.txt")

    def test_concurrent_respects_max_tasks_limit(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n"
            "- [ ] **Task One**: add t1\n"
            "  - ID: t1\n"
            "  - Risk: low\n"
            "- [ ] **Task Two**: add t2\n"
            "  - ID: t2\n"
            "  - Risk: low\n"
            "- [ ] **Task Three**: add t3\n"
            "  - ID: t3\n"
            "  - Risk: low\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "add three tasks"],
            cwd=temp_repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        dispatched: list[str] = []
        po_ref: dict[str, ParallelOrchestrator] = {}

        def spawn_worker(task_id: str, _task_text: str, worktree_path: Path):
            dispatched.append(task_id)
            commit_sha = self._commit_task_file(worktree_path, task_id)
            po_ref["po"].parallel_state.write_task_result(
                task_id,
                {"status": "success", "commit_sha": commit_sha, "risk": "low"},
            )
            return _ImmediateHandle(0)

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            tasklist="docs/tasklist.md",
            parallel_concurrency=2,
            max_tasks=1,
            merge_strategy="cherry-pick",
            worktree_cleanup="always",
            loc_threshold=1000,
        )
        po = ParallelOrchestrator(
            orch,
            spawn_worker_async=spawn_worker,
            eval_manager_factory=_eval_factory(True),
        )
        po_ref["po"] = po

        rc = po.run()
        assert rc == 0
        assert dispatched == ["t1"]

        base_tasklist = _git(temp_repo, "show", f"{base_branch}:docs/tasklist.md")
        assert "- [x] **Task One**: add t1" in base_tasklist
        assert "- [ ] **Task Two**: add t2" in base_tasklist
        assert "- [ ] **Task Three**: add t3" in base_tasklist

        base_head = _rev_parse(temp_repo, base_branch)
        assert "t1" in _git(temp_repo, "show", f"{base_head}:t1.txt")
        show_t2 = subprocess.run(
            ["git", "show", f"{base_head}:t2.txt"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
            check=False,
        )
        show_t3 = subprocess.run(
            ["git", "show", f"{base_head}:t3.txt"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert show_t2.returncode != 0
        assert show_t3.returncode != 0

    def test_concurrent_overlap_serialized(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n"
            "## Group: Shared Group\n\n"
            "- [ ] **Task One**: edit shared.py for t1\n"
            "  - ID: t1\n"
            "  - Risk: low\n"
            "- [ ] **Task Two**: edit shared.py for t2\n"
            "  - ID: t2\n"
            "  - Risk: low\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "add grouped tasks"],
            cwd=temp_repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            tasklist="docs/tasklist.md",
            parallel_concurrency=2,
            merge_strategy="cherry-pick",
            worktree_cleanup="always",
            loc_threshold=1000,
        )
        po_ref: dict[str, ParallelOrchestrator] = {}
        active = 0
        max_active = 0
        lock = threading.Lock()

        def spawn_worker(task_id: str, _task_text: str, worktree_path: Path):
            def _run() -> None:
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.1)
                commit_sha = self._commit_task_file(worktree_path, task_id)
                po_ref["po"].parallel_state.write_task_result(
                    task_id,
                    {"status": "success", "commit_sha": commit_sha, "risk": "low"},
                )
                with lock:
                    active -= 1

            return _ThreadBackedHandle(_run)

        po = ParallelOrchestrator(
            orch,
            spawn_worker_async=spawn_worker,
            eval_manager_factory=_eval_factory(True),
        )
        po_ref["po"] = po
        rc = po.run()
        assert rc == 0
        assert max_active == 1

    def test_concurrent_worker_timeout_kills_tree(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n- [ ] **Task One**: timeout\n  - ID: t1\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "add timeout task"],
            cwd=temp_repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        handle = _NeverFinishingHandle()

        def spawn_worker(_task_id: str, _task_text: str, _worktree_path: Path):
            return handle

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            tasklist="docs/tasklist.md",
            parallel_concurrency=1,
            parallel_heartbeat_ttl=0.01,
            merge_strategy="cherry-pick",
            worktree_cleanup="always",
            loc_threshold=1000,
        )
        po = ParallelOrchestrator(
            orch,
            spawn_worker_async=spawn_worker,
            eval_manager_factory=_eval_factory(True),
        )
        rc = po.run()
        assert rc == 1
        assert handle.killed is True

        state = po.parallel_state.load_control_state()
        assert state is not None
        assert state["task_records"]["t1"]["status"] == "failed"
        assert "heartbeat timeout" in state["task_records"]["t1"]["error"]

    def test_concurrent_missing_result_json(self, temp_repo, monkeypatch):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n- [ ] **Task One**: missing result\n  - ID: t1\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "add missing-result task"],
            cwd=temp_repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        merge_calls = {"n": 0}

        def fake_integrate(*_args, **_kwargs):
            merge_calls["n"] += 1
            return IntegrationResult(success=True, status="merged")

        monkeypatch.setattr(
            "millstone.runtime.parallel.MergePipeline.integrate_eval_and_land",
            fake_integrate,
        )

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            tasklist="docs/tasklist.md",
            parallel_concurrency=1,
            merge_strategy="cherry-pick",
            worktree_cleanup="always",
            loc_threshold=1000,
        )
        po = ParallelOrchestrator(
            orch,
            spawn_worker_async=lambda *_args, **_kwargs: _ImmediateHandle(0),
            eval_manager_factory=_eval_factory(True),
        )
        rc = po.run()
        assert rc == 1
        assert merge_calls["n"] == 0

        state = po.parallel_state.load_control_state()
        assert state is not None
        assert state["task_records"]["t1"]["status"] == "failed"
        assert "missing result.json" in state["task_records"]["t1"]["error"]

    def test_concurrent_merge_serialized(self, temp_repo, monkeypatch):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n"
            "- [ ] **Task One**: merge t1\n"
            "  - ID: t1\n"
            "- [ ] **Task Two**: merge t2\n"
            "  - ID: t2\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "add merge tasks"],
            cwd=temp_repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        merge_active = 0
        merge_max_active = 0
        merge_lock = threading.Lock()
        merged_tasks: list[str] = []

        def fake_integrate(_self, **kwargs):
            nonlocal merge_active, merge_max_active
            task_id = kwargs["task_id"]
            with merge_lock:
                merge_active += 1
                merge_max_active = max(merge_max_active, merge_active)
            time.sleep(0.05)
            with merge_lock:
                merge_active -= 1
                merged_tasks.append(task_id)
            return IntegrationResult(success=True, status="merged")

        monkeypatch.setattr(
            "millstone.runtime.parallel.MergePipeline.integrate_eval_and_land",
            fake_integrate,
        )

        po_ref: dict[str, ParallelOrchestrator] = {}

        def spawn_worker(task_id: str, _task_text: str, _worktree_path: Path):
            def _run() -> None:
                time.sleep(0.05)
                po_ref["po"].parallel_state.write_task_result(
                    task_id,
                    {"status": "success", "commit_sha": f"{task_id}-sha", "risk": "low"},
                )

            return _ThreadBackedHandle(_run)

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            tasklist="docs/tasklist.md",
            parallel_concurrency=2,
            merge_strategy="cherry-pick",
            worktree_cleanup="always",
            loc_threshold=1000,
        )
        po = ParallelOrchestrator(
            orch,
            spawn_worker_async=spawn_worker,
            eval_manager_factory=_eval_factory(True),
        )
        po_ref["po"] = po
        rc = po.run()
        assert rc == 0
        assert merge_max_active == 1
        assert sorted(merged_tasks) == ["t1", "t2"]

    def test_concurrent_deadlock_exits_cleanly(self, temp_repo, monkeypatch, capsys):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n"
            "- [ ] **Task One**: dep source\n"
            "  - ID: t1\n"
            "- [ ] **Task Two**: dep target\n"
            "  - ID: t2\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "add deadlock tasks"],
            cwd=temp_repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            tasklist="docs/tasklist.md",
            parallel_concurrency=2,
            merge_strategy="cherry-pick",
            worktree_cleanup="always",
            loc_threshold=1000,
        )
        po = ParallelOrchestrator(orch, eval_manager_factory=_eval_factory(True))

        def fake_analyze(_task_ids: list[dict]):
            return (
                [
                    {
                        "task_id": "t1",
                        "title": "Task One",
                        "group": None,
                        "file_refs": [],
                        "risk": "low",
                        "raw_text": "- [ ] t1",
                    },
                    {
                        "task_id": "t2",
                        "title": "Task Two",
                        "group": None,
                        "file_refs": [],
                        "risk": "low",
                        "raw_text": "- [ ] t2",
                    },
                ],
                [{"from_id": "t1", "to_id": "t2", "reason": "dep", "type": "explicit"}],
            )

        monkeypatch.setattr(po, "_analyze_tasks", fake_analyze)

        def spawn_worker(task_id: str, _task_text: str, _worktree_path: Path):
            assert task_id == "t1"
            po.parallel_state.write_task_result(
                task_id,
                {"status": "failed", "error": "synthetic worker failure"},
            )
            return _ImmediateHandle(1)

        po._spawn_worker_async_override = spawn_worker
        rc = po.run()
        assert rc == 1

        out = capsys.readouterr().out
        assert "Scheduler deadlock" in out
        assert "- t2: blocked by failed dependency" in out
        assert "Traceback" not in out

        state = po.parallel_state.load_control_state()
        assert state is not None
        assert state["task_records"]["t2"]["status"] == "blocked"

    def test_concurrent_cycle_detection(self, temp_repo, monkeypatch, capsys):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n"
            "- [ ] **Task One**: cycle one\n"
            "  - ID: t1\n"
            "- [ ] **Task Two**: cycle two\n"
            "  - ID: t2\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "add cycle tasks"],
            cwd=temp_repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            tasklist="docs/tasklist.md",
            parallel_concurrency=2,
            merge_strategy="cherry-pick",
            worktree_cleanup="always",
            loc_threshold=1000,
        )
        po = ParallelOrchestrator(orch, eval_manager_factory=_eval_factory(True))

        def fake_analyze(_task_ids: list[dict]):
            return (
                [
                    {
                        "task_id": "t1",
                        "title": "Task One",
                        "group": None,
                        "file_refs": [],
                        "risk": "low",
                        "raw_text": "- [ ] t1",
                    },
                    {
                        "task_id": "t2",
                        "title": "Task Two",
                        "group": None,
                        "file_refs": [],
                        "risk": "low",
                        "raw_text": "- [ ] t2",
                    },
                ],
                [
                    {"from_id": "t1", "to_id": "t2", "reason": "dep", "type": "explicit"},
                    {"from_id": "t2", "to_id": "t1", "reason": "dep", "type": "explicit"},
                ],
            )

        monkeypatch.setattr(po, "_analyze_tasks", fake_analyze)
        rc = po.run()
        assert rc == 1

        out = capsys.readouterr().out
        assert "Dependency cycle detected" in out
        assert "Traceback" not in out

    def test_concurrent_degrades_to_sequential(self, temp_repo):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n"
            "- [ ] **Task One**: sequential t1\n"
            "  - ID: t1\n"
            "  - Risk: low\n"
            "- [ ] **Task Two**: sequential t2\n"
            "  - ID: t2\n"
            "  - Risk: low\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "add sequential tasks"],
            cwd=temp_repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            tasklist="docs/tasklist.md",
            parallel_concurrency=1,
            merge_strategy="cherry-pick",
            worktree_cleanup="always",
            loc_threshold=1000,
        )
        po_ref: dict[str, ParallelOrchestrator] = {}
        active = 0
        max_active = 0
        lock = threading.Lock()

        def spawn_worker(task_id: str, _task_text: str, worktree_path: Path):
            def _run() -> None:
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.1)
                commit_sha = self._commit_task_file(worktree_path, task_id)
                po_ref["po"].parallel_state.write_task_result(
                    task_id,
                    {"status": "success", "commit_sha": commit_sha, "risk": "low"},
                )
                with lock:
                    active -= 1

            return _ThreadBackedHandle(_run)

        po = ParallelOrchestrator(
            orch,
            spawn_worker_async=spawn_worker,
            eval_manager_factory=_eval_factory(True),
        )
        po_ref["po"] = po
        rc = po.run()
        assert rc == 0
        assert max_active == 1

        base_head = _rev_parse(temp_repo, base_branch)
        assert "t1" in _git(temp_repo, "show", f"{base_head}:t1.txt")
        assert "t2" in _git(temp_repo, "show", f"{base_head}:t2.txt")

    def test_concurrent_degrades_to_sequential_skips_merge_on_worker_failure(
        self,
        temp_repo,
        monkeypatch,
    ):
        base_branch = _git(temp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        (temp_repo / "docs" / "tasklist.md").write_text(
            "# Tasklist\n\n"
            "- [ ] **Task One**: sequential failure parity\n"
            "  - ID: t1\n"
            "  - Risk: low\n"
        )
        subprocess.run(
            ["git", "add", "docs/tasklist.md"], cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "add sequential parity task"],
            cwd=temp_repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=temp_repo, capture_output=True, check=True
        )

        merge_calls: list[dict] = []

        def fake_integrate(_self, **kwargs):
            merge_calls.append(kwargs)
            return IntegrationResult(
                success=False, status="land_fail", error="synthetic merge failure"
            )

        monkeypatch.setattr(
            "millstone.runtime.parallel.MergePipeline.integrate_eval_and_land",
            fake_integrate,
        )

        po_ref: dict[str, ParallelOrchestrator] = {}

        def spawn_worker(task_id: str, _task_text: str, _worktree_path: Path):
            po_ref["po"].parallel_state.write_task_result(
                task_id,
                {"status": "failed", "error": "worker failed", "risk": "low"},
            )
            return _ImmediateHandle(1)

        orch = Orchestrator(
            base_branch=base_branch,
            parallel_enabled=True,
            tasklist="docs/tasklist.md",
            parallel_concurrency=1,
            merge_strategy="cherry-pick",
            worktree_cleanup="always",
            loc_threshold=1000,
        )
        po = ParallelOrchestrator(
            orch,
            spawn_worker_async=spawn_worker,
            eval_manager_factory=_eval_factory(True),
        )
        po_ref["po"] = po

        rc = po.run()
        assert rc == 1
        assert len(merge_calls) == 0

        state = po.parallel_state.load_control_state()
        assert state is not None
        assert state["task_records"]["t1"]["status"] == "failed"
        assert "worker failed" in state["task_records"]["t1"]["error"]
