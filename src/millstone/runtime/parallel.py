from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from millstone.artifact_providers.mcp import MCPTasklistProvider
from millstone.artifacts.eval_manager import EvalManager
from millstone.artifacts.models import TaskStatus
from millstone.runtime.locks import AdvisoryLock
from millstone.runtime.merge_pipeline import MergePipeline
from millstone.runtime.parallel_state import ParallelState
from millstone.runtime.scheduler import TaskScheduler
from millstone.runtime.worktree import WorktreeManager

if TYPE_CHECKING:
    from millstone.runtime.orchestrator import Orchestrator


def _resolve_under_repo(repo_dir: Path, p: str | Path) -> Path:
    path = Path(p)
    if path.is_absolute():
        return path
    return repo_dir / path


@runtime_checkable
class WorkerHandle(Protocol):
    pid: int | None

    def poll(self) -> int | None: ...

    def kill(self) -> None: ...


class SubprocessWorkerHandle:
    _MAX_CAPTURE_CHARS = 200_000

    def __init__(self, process: subprocess.Popen):
        self._process = process
        self.pid: int | None = process.pid
        self._stdout_chunks: deque[str] = deque()
        self._stderr_chunks: deque[str] = deque()
        self._stdout_chars = 0
        self._stderr_chars = 0
        self._stdout_thread = self._start_drain_thread(
            getattr(process, "stdout", None),
            is_stdout=True,
        )
        self._stderr_thread = self._start_drain_thread(
            getattr(process, "stderr", None),
            is_stdout=False,
        )

    @classmethod
    def spawn(cls, cmd: list[str], cwd: Path) -> SubprocessWorkerHandle:
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        return cls(process)

    def _start_drain_thread(
        self,
        stream: Any,
        *,
        is_stdout: bool,
    ) -> threading.Thread | None:
        if stream is None:
            return None
        thread = threading.Thread(
            target=self._drain_stream,
            args=(stream, is_stdout),
            daemon=True,
        )
        thread.start()
        return thread

    def _drain_stream(self, stream: Any, is_stdout: bool) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                self._append_output(chunk, is_stdout=is_stdout)
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    def _append_output(self, chunk: str, *, is_stdout: bool) -> None:
        chunks = self._stdout_chunks if is_stdout else self._stderr_chunks
        chars = self._stdout_chars if is_stdout else self._stderr_chars
        chunks.append(chunk)
        chars += len(chunk)
        while chars > self._MAX_CAPTURE_CHARS and chunks:
            chars -= len(chunks.popleft())
        if is_stdout:
            self._stdout_chars = chars
        else:
            self._stderr_chars = chars

    def communicate(self) -> tuple[str, str]:
        self._process.wait()
        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=1.0)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1.0)
        return "".join(self._stdout_chunks), "".join(self._stderr_chunks)

    def poll(self) -> int | None:
        return self._process.poll()

    def kill(self) -> None:
        if self.pid is None:
            return
        try:
            pgid = os.getpgid(self.pid)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return


class _ThreadWorkerHandle:
    """Adapter that runs a synchronous worker runner in a background thread."""

    def __init__(
        self,
        task_id: str,
        task_text: str,
        worktree_path: Path,
        worker_runner: Callable[[str, str, Path], dict],
        parallel_state: ParallelState,
    ):
        self.pid: int | None = None
        self._killed = threading.Event()
        self._returncode: int | None = None
        self._thread = threading.Thread(
            target=self._run,
            args=(task_id, task_text, worktree_path, worker_runner, parallel_state),
            daemon=True,
        )
        self._thread.start()

    def _run(
        self,
        task_id: str,
        task_text: str,
        worktree_path: Path,
        worker_runner: Callable[[str, str, Path], dict],
        parallel_state: ParallelState,
    ) -> None:
        if self._killed.is_set():
            self._returncode = -9
            return
        try:
            result = worker_runner(task_id, task_text, worktree_path) or {}
            parallel_state.write_task_result(task_id, result)
            self._returncode = 0
        except Exception as exc:
            parallel_state.write_task_result(
                task_id,
                {
                    "status": "failed",
                    "error": f"worker_runner_exception: {exc}",
                },
            )
            self._returncode = 1

    def poll(self) -> int | None:
        if self._killed.is_set():
            return -9
        if self._thread.is_alive():
            return None
        if self._returncode is None:
            return 1
        return self._returncode

    def kill(self) -> None:
        self._killed.set()


class ParallelOrchestrator:
    """Control plane for worktree-based task execution."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        worker_runner: Callable[[str, str, Path], dict] | None = None,
        spawn_worker_async: Callable[[str, str, Path], WorkerHandle] | None = None,
        eval_manager_factory: Callable[..., object] | None = None,
    ):
        self.orch = orchestrator

        self.git_lock = AdvisoryLock(
            _resolve_under_repo(self.orch.repo_dir, self.orch.parallel_lock_git)
        )
        self.state_lock = AdvisoryLock(
            _resolve_under_repo(self.orch.repo_dir, self.orch.parallel_lock_state)
        )
        self.tasklist_lock = AdvisoryLock(
            _resolve_under_repo(self.orch.repo_dir, self.orch.parallel_lock_tasklist)
        )

        worktree_root = _resolve_under_repo(self.orch.repo_dir, self.orch.parallel_worktree_root)
        self.worktree_mgr = WorktreeManager(
            repo_dir=self.orch.repo_dir,
            worktree_root=worktree_root,
            git_lock=self.git_lock,
        )

        shared_state_dir = self.orch.shared_state_dir
        if not shared_state_dir:
            shared_state_dir = str(self.orch.repo_dir / ".millstone" / "parallel")
        self.parallel_state = ParallelState(
            shared_state_dir=_resolve_under_repo(self.orch.repo_dir, shared_state_dir),
            state_lock=self.state_lock,
        )

        self.worker_runner = worker_runner
        self._spawn_worker_async_override = spawn_worker_async
        self.eval_manager_factory = eval_manager_factory or self._make_eval_manager

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.orch.repo_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def _rev_parse(self, ref: str) -> str:
        return self._git("rev-parse", ref).strip()

    def _current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def _validate_layout(self, base_branch: str) -> bool:
        if self.worktree_mgr.is_branch_checked_out(base_branch):
            print()
            print(f"ERROR: Base branch '{base_branch}' is checked out in a worktree.")
            print("Worktree mode requires the base branch to NOT be checked out anywhere.")
            print("Fix: detach HEAD or switch to a different branch, then re-run with --worktrees.")
            print()
            return False
        return True

    def _recover_state(self) -> None:
        """Recover from stale millstone/* worktrees left by a prior crashed run."""
        existing = self.worktree_mgr.detect_existing()
        if not existing:
            return

        saved_state = self.parallel_state.load_control_state()
        task_records = dict((saved_state or {}).get("task_records") or {})
        changed = False

        integration_ref = f"refs/heads/{self.orch.parallel_integration_branch}"
        task_ref_prefix = "refs/heads/millstone/task/"
        now = time.time()
        ttl = float(self.orch.parallel_heartbeat_ttl)

        for wt in existing:
            wt_path_raw = wt.get("worktree", "")
            if not wt_path_raw:
                continue
            wt_path = Path(wt_path_raw)
            branch_ref = wt.get("branch", "")

            # Integration worktree is always recreated on startup.
            if branch_ref == integration_ref:
                self.worktree_mgr.remove_worktree(wt_path, delete_branch=False)
                changed = True
                continue

            # Any non-task millstone branch is treated as orphaned stale state.
            if not branch_ref.startswith(task_ref_prefix):
                self.worktree_mgr.remove_worktree(wt_path, delete_branch=True)
                changed = True
                continue

            task_id = branch_ref[len(task_ref_prefix) :]
            if not saved_state:
                self.worktree_mgr.remove_worktree(wt_path, delete_branch=True)
                changed = True
                continue

            record = task_records.get(task_id)
            if not isinstance(record, dict):
                self.worktree_mgr.remove_worktree(wt_path, delete_branch=True)
                changed = True
                continue

            status = record.get("status")
            if status in {"completed", "landed", "success", "merged"}:
                self.worktree_mgr.remove_worktree(wt_path, delete_branch=True)
                changed = True
                continue

            if status in {"running", "in_progress"}:
                hb = self.parallel_state.read_heartbeat(task_id)
                hb_age = None if hb is None else (now - hb)
                if hb is not None and hb_age is not None and hb_age <= ttl:
                    age = f"{hb_age:.1f}s"
                    raise RuntimeError(
                        f"Task '{task_id}' has a fresh heartbeat ({age}); "
                        "another control plane may still be running."
                    )

                # Missing or stale heartbeat: reclaim and mark failed.
                reason = "stale_from_prior_run"
                if hb is None:
                    reason = "missing_heartbeat_from_prior_run"
                record["status"] = "failed"
                record["error"] = reason
                record["completed_at"] = now
                task_records[task_id] = record
                self.worktree_mgr.remove_worktree(wt_path, delete_branch=True)
                changed = True
                continue

            # Failed/blocked/unknown statuses are stale from a prior run.
            self.worktree_mgr.remove_worktree(wt_path, delete_branch=True)
            changed = True

        # Clean up any orphaned metadata entries after removals.
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=self.orch.repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )

        if not changed or not saved_state:
            return

        self.parallel_state.save_control_state(
            base_ref_sha=saved_state.get("base_ref_sha", ""),
            base_branch=saved_state.get("base_branch", self.orch.base_branch or ""),
            integration_branch=saved_state.get(
                "integration_branch", self.orch.parallel_integration_branch
            ),
            integration_worktree=Path(
                saved_state.get(
                    "integration_worktree",
                    str(self.worktree_mgr._integration_worktree_path()),
                )
            ),
            task_records=task_records,
            merge_queue=saved_state.get("merge_queue", []) or [],
        )

    def _is_mcp_provider(self) -> bool:
        """Check whether the configured tasklist provider is MCP-backed."""
        provider = self.orch._outer_loop_manager.tasklist_provider
        return isinstance(provider, MCPTasklistProvider)

    def _fetch_tasks_from_provider(self) -> list[dict]:
        """Fetch tasks from the MCP provider, returning the scheduler-compatible format.

        Returns the same ``{task_id, checked, title, raw_text, index}`` dicts that
        ``TasklistManager.extract_all_task_ids()`` produces so that the rest of the
        parallel pipeline works unchanged.
        """
        provider = self.orch._outer_loop_manager.tasklist_provider
        if not isinstance(provider, MCPTasklistProvider):
            raise TypeError("_fetch_tasks_from_provider requires an MCPTasklistProvider")
        if provider._agent_callback is None:
            provider.set_agent_callback(lambda p, **k: self.orch.run_agent(p, role="author", **k))
        provider.invalidate_cache()
        items = provider.list_tasks()
        results: list[dict] = []
        for index, item in enumerate(items):
            checked = item.status not in (TaskStatus.todo, TaskStatus.in_progress)
            results.append(
                {
                    "task_id": item.task_id,
                    "checked": checked,
                    "title": item.title,
                    "raw_text": "",
                    "index": index,
                }
            )
        return results

    def _fetch_task_body(self, task_id: str) -> str:
        """Fetch full task body from MCP provider via get_task().

        Returns a formatted text block with title, context, criteria, tests,
        and risk — suitable for passing as ``--task`` to a worker subprocess.

        Raises ``RuntimeError`` if the provider returns ``None`` so that callers
        can surface the failure instead of silently falling back to title-only.
        """
        provider = self.orch._outer_loop_manager.tasklist_provider
        if not isinstance(provider, MCPTasklistProvider):
            return ""
        item = provider.get_task(task_id)
        if item is None:
            raise RuntimeError(f"MCP get_task('{task_id}') returned None")
        parts = [item.title]
        if item.context:
            parts.append(f"  - Context: {item.context}")
        if item.criteria:
            parts.append(f"  - Criteria: {item.criteria}")
        if item.tests:
            parts.append(f"  - Tests: {item.tests}")
        if item.risk:
            parts.append(f"  - Risk: {item.risk}")
        return "\n".join(parts)

    def _analyze_tasks_mcp(self, task_dicts: list[dict]) -> tuple[list[dict], list[dict]]:
        """Build enriched-task list for MCP tasks (no dependency graph).

        Fetches full task body from the MCP provider so that worker subprocesses
        receive meaningful task descriptions via ``--task``.

        Raises ``RuntimeError`` if any task body cannot be fetched.
        """
        enriched: list[dict] = []
        for task in task_dicts:
            task_id = task["task_id"]
            body = self._fetch_task_body(task_id)
            risk = None
            if body:
                # Extract risk from fetched body if present.
                for line in body.splitlines():
                    stripped = line.strip()
                    if stripped.lower().startswith("- risk:"):
                        risk = stripped.split(":", 1)[1].strip() or None
                        break
            enriched.append(
                {
                    "task_id": task_id,
                    "title": task.get("title", ""),
                    "group": None,
                    "file_refs": [],
                    "risk": risk,
                    "raw_text": body or task.get("title", ""),
                }
            )
        return enriched, []

    def _mark_mcp_task_done(self, task_id: str) -> None:
        """Mark a task as done via the MCP provider after a successful merge.

        Raises on failure so the caller can treat it as a merge failure,
        preventing the task from being silently left open on the remote.
        """
        provider = self.orch._outer_loop_manager.tasklist_provider
        if not isinstance(provider, MCPTasklistProvider):
            return
        provider.update_task_status(task_id, TaskStatus.done)

    def _make_eval_manager(self, repo_dir: Path, work_dir: Path) -> EvalManager:
        work_dir.mkdir(parents=True, exist_ok=True)
        return EvalManager(
            work_dir=work_dir,
            repo_dir=repo_dir,
            project_config=self.orch.project_config,
            policy=self.orch.policy,
            category_weights=self.orch.category_weights,
            category_thresholds=self.orch.category_thresholds,
            eval_scripts=self.orch.eval_scripts,
        )

    def _run_worker_subprocess(self, task_id: str, task_text: str, worktree_path: Path) -> dict:
        handle = self._spawn_subprocess_worker(task_id, task_text, worktree_path)
        stdout, stderr = handle.communicate()
        returncode = handle.poll()
        if returncode is None:
            returncode = -1
        result = self.parallel_state.read_task_result(task_id)
        if result is None:
            return {
                "status": "failed",
                "error": "missing result.json",
                "returncode": returncode,
                "stdout": stdout[:2000],
                "stderr": stderr[:2000],
            }
        return result

    def _worker_command(self, task_text: str, worktree_path: Path) -> list[str]:
        shared = self.parallel_state.shared_state_dir
        cmd = [
            sys.executable,
            "-m",
            "millstone.runtime.orchestrator",
            "--task",
            task_text,
            "--repo-dir",
            str(worktree_path),
            "--shared-state-dir",
            str(shared),
            "--max-cycles",
            str(self.orch.max_cycles),
            "--loc-threshold",
            str(self.orch.loc_threshold),
            "--cli",
            self.orch._cli_default,
        ]
        if self.orch.no_tasklist_edits:
            cmd.append("--no-tasklist-edits")
        if self.orch._cli_builder:
            cmd.extend(["--cli-builder", self.orch._cli_builder])
        if self.orch._cli_reviewer:
            cmd.extend(["--cli-reviewer", self.orch._cli_reviewer])
        if self.orch._cli_sanity:
            cmd.extend(["--cli-sanity", self.orch._cli_sanity])
        if self.orch._cli_analyzer:
            cmd.extend(["--cli-analyzer", self.orch._cli_analyzer])
        if self.orch._custom_prompts_dir is not None:
            cmd.extend(["--prompts-dir", str(self.orch._custom_prompts_dir)])
        return cmd

    def _spawn_subprocess_worker(
        self,
        _task_id: str,
        task_text: str,
        worktree_path: Path,
    ) -> SubprocessWorkerHandle:
        return SubprocessWorkerHandle.spawn(
            self._worker_command(task_text, worktree_path),
            cwd=worktree_path,
        )

    def _spawn_worker_async(
        self,
        task_id: str,
        task_text: str,
        worktree_path: Path,
    ) -> WorkerHandle:
        if self._spawn_worker_async_override is not None:
            return self._spawn_worker_async_override(task_id, task_text, worktree_path)

        if self.worker_runner is not None:
            return _ThreadWorkerHandle(
                task_id=task_id,
                task_text=task_text,
                worktree_path=worktree_path,
                worker_runner=self.worker_runner,
                parallel_state=self.parallel_state,
            )

        return self._spawn_subprocess_worker(task_id, task_text, worktree_path)

    def _save_control_state(
        self,
        *,
        base_ref_sha: str,
        base_branch: str,
        integration_wt: Path,
        task_records: dict[str, dict[str, Any]],
    ) -> None:
        self.parallel_state.save_control_state(
            base_ref_sha=base_ref_sha,
            base_branch=base_branch,
            integration_branch=self.orch.parallel_integration_branch,
            integration_worktree=integration_wt,
            task_records=task_records,
            merge_queue=[],
        )

    def _analyze_tasks(self, task_ids: list[dict]) -> tuple[list[dict], list[dict]]:
        """Enrich task metadata and convert dependency indices to task IDs."""
        tasklist_mgr = self.orch._tasklist_manager

        groups_by_index = tasklist_mgr.extract_all_task_groups()
        analysis = tasklist_mgr.analyze_tasklist(print_report=False)

        file_refs_by_index: dict[int, list[str]] = {}
        for analyzed_task in analysis.get("tasks", []):
            if not isinstance(analyzed_task, dict):
                continue
            index = analyzed_task.get("index")
            if not isinstance(index, int):
                continue

            refs = analyzed_task.get("file_refs")
            if not isinstance(refs, list):
                file_refs_by_index[index] = []
                continue

            normalized_refs: list[str] = []
            for ref in refs:
                if isinstance(ref, str) and ref.strip():
                    normalized_refs.append(ref)
            file_refs_by_index[index] = normalized_refs

        index_to_task_id: dict[int, str] = {}
        enriched_tasks: list[dict] = []
        for task in task_ids:
            task_id = task.get("task_id")
            if not isinstance(task_id, str):
                continue

            index = task.get("index")
            raw_text = task.get("raw_text")
            if not isinstance(raw_text, str):
                raw_text = ""

            metadata = tasklist_mgr._parse_task_metadata(raw_text)
            if isinstance(index, int):
                index_to_task_id[index] = task_id

            title = task.get("title")
            if not isinstance(title, str):
                title = metadata.get("title")
            if not isinstance(title, str):
                title = ""

            group = groups_by_index.get(index) if isinstance(index, int) else None
            risk = metadata.get("risk")
            if not isinstance(risk, str):
                risk = None

            enriched_tasks.append(
                {
                    "task_id": task_id,
                    "title": title,
                    "group": group,
                    "file_refs": file_refs_by_index.get(index, [])
                    if isinstance(index, int)
                    else [],
                    "risk": risk,
                    "raw_text": raw_text,
                }
            )

        id_based_dependencies: list[dict] = []
        for dependency in analysis.get("dependencies", []):
            if not isinstance(dependency, dict):
                continue

            from_index = dependency.get("from_idx")
            if from_index is None:
                from_index = dependency.get("from_index")
            to_index = dependency.get("to_idx")
            if to_index is None:
                to_index = dependency.get("to_index")

            if not isinstance(from_index, int) or not isinstance(to_index, int):
                continue

            from_id = index_to_task_id.get(from_index)
            to_id = index_to_task_id.get(to_index)
            if not from_id or not to_id:
                continue

            reason = dependency.get("reason")
            if not isinstance(reason, str):
                reason = ""
            dep_type = dependency.get("type")
            if not isinstance(dep_type, str):
                dep_type = "heuristic"

            id_based_dependencies.append(
                {
                    "from_id": from_id,
                    "to_id": to_id,
                    "reason": reason,
                    "type": dep_type,
                }
            )

        return enriched_tasks, id_based_dependencies

    def _dry_run(self) -> int:
        base_branch = self.orch.base_branch or self._current_branch()
        if base_branch == "HEAD":
            print("ERROR: --base-branch is required when running from detached HEAD.")
            return 1
        base_ref = self.orch.base_ref or base_branch
        base_ref_sha = self._rev_parse(base_ref)

        if self._is_mcp_provider():
            tasks = self._fetch_tasks_from_provider()
        else:
            tasks = self.orch._tasklist_manager.extract_all_task_ids()
        max_tasks = max(0, int(self.orch.max_tasks))
        pending = [t for t in tasks if not t["checked"]][:max_tasks]

        print("=== Worktree Dry Run ===")
        print(f"base_branch: {base_branch}")
        print(f"base_ref_sha: {base_ref_sha}")
        print(f"integration_branch: {self.orch.parallel_integration_branch}")
        print(f"merge_strategy: {self.orch.parallel_merge_strategy}")
        print(f"tasks_pending: {len(pending)}")
        for t in pending:
            print(f"- {t['task_id']}: {t['title']}")
        return 0

    def run(self) -> int:
        if self.orch.dry_run:
            return self._dry_run()

        base_branch = self.orch.base_branch or self._current_branch()
        if base_branch == "HEAD":
            print("ERROR: --base-branch is required when running from detached HEAD.")
            return 1

        try:
            self._recover_state()
        except RuntimeError as e:
            print(f"ERROR: {e}")
            return 1

        if not self._validate_layout(base_branch):
            return 1

        base_ref = self.orch.base_ref or base_branch
        base_ref_sha = self._rev_parse(base_ref)

        integration_wt = self.worktree_mgr.create_integration_worktree(
            self.orch.parallel_integration_branch,
            base_ref_sha,
        )
        use_mcp = self._is_mcp_provider()
        merge_pipeline = MergePipeline(
            repo_dir=self.orch.repo_dir,
            integration_worktree=integration_wt,
            base_branch=base_branch,
            integration_branch=self.orch.parallel_integration_branch,
            merge_strategy=self.orch.parallel_merge_strategy,
            git_lock=self.git_lock,
            tasklist_lock=self.tasklist_lock,
            policy=self.orch.policy,
            loc_threshold=self.orch.loc_threshold,
            max_retries=self.orch.merge_max_retries,
            tasklist=self.orch.tasklist,
            skip_tasklist_mark=use_mcp,
        )
        if use_mcp:
            tasks = self._fetch_tasks_from_provider()
        else:
            tasks = self.orch._tasklist_manager.extract_all_task_ids()
        taskmap = {t["task_id"]: {"index": t["index"]} for t in tasks}
        self.parallel_state.save_taskmap(taskmap)

        max_tasks = max(0, int(self.orch.max_tasks))
        pending = [t for t in tasks if not t["checked"]][:max_tasks]
        task_records: dict[str, dict[str, Any]] = {}
        failures = False
        completed: set[str] = set()
        in_flight: dict[str, WorkerHandle] = {}
        in_flight_worktrees: dict[str, Path] = {}
        in_flight_started_at: dict[str, float] = {}
        heartbeat_ttl = float(self.orch.parallel_heartbeat_ttl)
        poll_interval = 0.1

        try:
            if use_mcp:
                enriched_tasks, dependencies = self._analyze_tasks_mcp(pending)
            else:
                enriched_tasks, dependencies = self._analyze_tasks(pending)
            scheduler = TaskScheduler(
                concurrency=max(1, int(self.orch.parallel_concurrency)),
                high_risk_concurrency=max(1, int(self.orch.high_risk_concurrency)),
            )
            scheduler.build_graph(enriched_tasks, dependencies)
            self._save_control_state(
                base_ref_sha=base_ref_sha,
                base_branch=base_branch,
                integration_wt=integration_wt,
                task_records=task_records,
            )

            while scheduler.has_remaining():
                dispatched_any = False
                for task_id in scheduler.next_available(set(in_flight), completed):
                    task = scheduler.get_task(task_id)
                    task_text = task.get("raw_text")
                    if not isinstance(task_text, str):
                        task_text = ""

                    wt = self.worktree_mgr.create_task_worktree(task_id, base_ref_sha)
                    handle = self._spawn_worker_async(task_id, task_text, wt)
                    now = time.time()

                    in_flight[task_id] = handle
                    in_flight_worktrees[task_id] = wt
                    in_flight_started_at[task_id] = now
                    task_records[task_id] = {
                        "status": "running",
                        "started_at": now,
                    }
                    dispatched_any = True

                if dispatched_any:
                    self._save_control_state(
                        base_ref_sha=base_ref_sha,
                        base_branch=base_branch,
                        integration_wt=integration_wt,
                        task_records=task_records,
                    )

                newly_finished: list[tuple[str, WorkerHandle, int]] = []
                for task_id, handle in list(in_flight.items()):
                    returncode = handle.poll()
                    if returncode is None:
                        continue
                    del in_flight[task_id]
                    newly_finished.append((task_id, handle, returncode))

                now = time.time()
                heartbeat_timed_out = False
                for task_id, handle in list(in_flight.items()):
                    heartbeat = self.parallel_state.read_heartbeat(task_id)
                    last_seen = heartbeat
                    if last_seen is None:
                        last_seen = in_flight_started_at.get(task_id, now)
                    if (now - last_seen) <= heartbeat_ttl:
                        continue

                    handle.kill()
                    del in_flight[task_id]
                    in_flight_worktrees.pop(task_id, None)
                    in_flight_started_at.pop(task_id, None)

                    reason = "heartbeat timeout"
                    scheduler.mark_failed(task_id, reason)
                    task_records[task_id] = {
                        "status": "failed",
                        "error": reason,
                        "completed_at": now,
                    }
                    failures = True
                    heartbeat_timed_out = True

                if heartbeat_timed_out:
                    self._save_control_state(
                        base_ref_sha=base_ref_sha,
                        base_branch=base_branch,
                        integration_wt=integration_wt,
                        task_records=task_records,
                    )

                if newly_finished:
                    for task_id, handle, returncode in newly_finished:
                        if isinstance(handle, SubprocessWorkerHandle):
                            handle.communicate()

                        result = self.parallel_state.read_task_result(task_id)
                        if result is None:
                            reason = f"missing result.json (rc={returncode})"
                            scheduler.mark_failed(task_id, reason)
                            task_records[task_id] = {
                                "status": "failed",
                                "error": reason,
                                "completed_at": time.time(),
                            }
                            failures = True
                            in_flight_worktrees.pop(task_id, None)
                            in_flight_started_at.pop(task_id, None)
                            continue

                        result_status_raw = result.get("status")
                        result_status = (
                            result_status_raw.strip().lower()
                            if isinstance(result_status_raw, str)
                            else ""
                        )
                        if result_status != "success":
                            result_error = result.get("error")
                            if isinstance(result_error, str) and result_error.strip():
                                reason = result_error.strip()
                            elif result_status:
                                reason = f"worker status: {result_status}"
                            else:
                                reason = "worker status missing"
                            scheduler.mark_failed(task_id, reason)
                            task_records[task_id] = {
                                "status": "blocked" if result_status == "blocked" else "failed",
                                "error": reason,
                                "completed_at": time.time(),
                            }
                            failures = True
                            in_flight_worktrees.pop(task_id, None)
                            in_flight_started_at.pop(task_id, None)
                            continue

                        merge_res = merge_pipeline.integrate_eval_and_land(
                            task_id=task_id,
                            task_branch=f"millstone/task/{task_id}",
                            base_ref_sha=base_ref_sha,
                            commit_sha=result.get("commit_sha"),
                            task_risk=result.get("risk"),
                            taskmap=taskmap,
                            eval_manager_factory=self.eval_manager_factory,
                        )

                        if merge_res.success:
                            if use_mcp:
                                try:
                                    self._mark_mcp_task_done(task_id)
                                except Exception as exc:
                                    reason = f"mcp_status_update_failed: {exc}"
                                    scheduler.mark_failed(task_id, reason)
                                    task_records[task_id] = {
                                        "status": "failed",
                                        "error": reason,
                                        "completed_at": time.time(),
                                    }
                                    failures = True
                                    in_flight_worktrees.pop(task_id, None)
                                    in_flight_started_at.pop(task_id, None)
                                    continue
                            completed.add(task_id)
                            scheduler.mark_completed(task_id)
                            task_records[task_id] = {
                                "status": "completed",
                                "completed_at": time.time(),
                            }
                        else:
                            reason = merge_res.error or merge_res.status or "merge failed"
                            scheduler.mark_failed(task_id, reason)
                            task_records[task_id] = {
                                "status": "blocked" if merge_res.status == "conflict" else "failed",
                                "error": reason,
                                "completed_at": time.time(),
                            }
                            failures = True

                        in_flight_worktrees.pop(task_id, None)
                        in_flight_started_at.pop(task_id, None)

                    self._save_control_state(
                        base_ref_sha=base_ref_sha,
                        base_branch=base_branch,
                        integration_wt=integration_wt,
                        task_records=task_records,
                    )

                if (
                    not in_flight
                    and scheduler.has_remaining()
                    and not scheduler.next_available(set(), completed)
                ):
                    failures = True
                    remaining = scheduler.get_remaining_task_ids()
                    deadlock_time = time.time()
                    print(
                        "ERROR: Scheduler deadlock: remaining tasks blocked by failed dependencies."
                    )
                    for task_id in remaining:
                        reason = "blocked by failed dependency"
                        scheduler.mark_failed(task_id, reason)
                        task_records[task_id] = {
                            "status": "blocked",
                            "error": reason,
                            "completed_at": deadlock_time,
                        }
                        print(f"- {task_id}: {reason}")
                    self._save_control_state(
                        base_ref_sha=base_ref_sha,
                        base_branch=base_branch,
                        integration_wt=integration_wt,
                        task_records=task_records,
                    )
                    break

                if scheduler.has_remaining() and in_flight:
                    time.sleep(poll_interval)
        except (ValueError, RuntimeError) as e:
            print(f"ERROR: {e}")
            failures = True
        finally:
            # Cleanup worktrees per policy (preserves failed/blocked under on_success).
            statuses = {tid: rec.get("status") for tid, rec in task_records.items()}
            self.worktree_mgr.cleanup(self.orch.parallel_cleanup, statuses)

        return 1 if failures else 0
