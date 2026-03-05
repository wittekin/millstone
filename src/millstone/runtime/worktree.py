from __future__ import annotations

import contextlib
import shutil
import subprocess
from pathlib import Path

from millstone.config import CONFIG_FILE_NAME, POLICY_FILE_NAME, PROJECT_FILE_NAME, WORK_DIR_NAME
from millstone.runtime.locks import AdvisoryLock


class WorktreeManager:
    def __init__(self, repo_dir: Path, worktree_root: Path, git_lock: AdvisoryLock):
        self.repo_dir = Path(repo_dir)
        self.worktree_root = Path(worktree_root)
        self.git_lock = git_lock

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
            check=check,
        )

    def validate_task_id(self, task_id: str) -> str:
        import re

        if not re.fullmatch(r"[a-z0-9_-]{1,40}", task_id):
            raise ValueError(
                f"Invalid task_id '{task_id}'. Must match [a-z0-9_-]{{1,40}}."
            )
        return task_id

    def _task_branch(self, task_id: str) -> str:
        self.validate_task_id(task_id)
        return f"millstone/task/{task_id}"

    def _task_worktree_path(self, task_id: str) -> Path:
        self.validate_task_id(task_id)
        return self.worktree_root / f"task-{task_id}"

    def _integration_worktree_path(self) -> Path:
        return self.worktree_root / "integration"

    def _copy_millstone_config(self, dest_repo_dir: Path) -> None:
        """Copy essential .millstone/*.toml files into the destination repo."""
        dest = dest_repo_dir / WORK_DIR_NAME
        dest.mkdir(parents=True, exist_ok=True)

        src = self.repo_dir / WORK_DIR_NAME
        if not src.exists():
            return

        for name in (CONFIG_FILE_NAME, POLICY_FILE_NAME, PROJECT_FILE_NAME):
            src_file = src / name
            if src_file.exists():
                shutil.copy2(src_file, dest / name)

    def create_task_worktree(self, task_id: str, base_ref_sha: str) -> Path:
        """Create a new task worktree and branch from base_ref_sha."""
        branch = self._task_branch(task_id)
        path = self._task_worktree_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        with self.git_lock:
            self._git("worktree", "add", "-b", branch, str(path), base_ref_sha)

        self._copy_millstone_config(path)
        return path

    def create_integration_worktree(self, integration_branch: str, base_ref_sha: str) -> Path:
        """Create or recreate the integration worktree.

        Crash-recovery safe: if the integration path or branch already exists,
        remove/reset it first under git_lock, then create a fresh worktree.
        """
        path = self._integration_worktree_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        with self.git_lock:
            target_ref = f"refs/heads/{integration_branch}"

            # Remove any existing worktree that has this integration branch checked out.
            for wt in self.list_worktrees():
                if wt.get("branch") == target_ref:
                    wt_path = Path(wt.get("worktree", ""))
                    if wt_path:
                        removed = self._git(
                            "worktree", "remove", "--force", str(wt_path), check=False
                        )
                        if removed.returncode != 0:
                            msg = (removed.stderr or removed.stdout or "").strip()
                            raise RuntimeError(
                                f"Cannot remove stale integration worktree '{wt_path}': {msg}"
                            )
            self._git("worktree", "prune", check=False)

            # If the integration path still exists but is no longer a registered
            # worktree (partial crash state), remove the directory so add can succeed.
            if path.exists():
                shutil.rmtree(path)

            # If the branch still exists locally, remove it before re-creating with -b.
            branch_ref = f"refs/heads/{integration_branch}"
            exists = self._git("show-ref", "--verify", "--quiet", branch_ref, check=False)
            if exists.returncode == 0:
                deleted = self._git("branch", "-D", integration_branch, check=False)
                if deleted.returncode != 0:
                    msg = (deleted.stderr or deleted.stdout or "").strip()
                    raise RuntimeError(
                        f"Cannot reset integration branch '{integration_branch}': {msg}"
                    )

            self._git("worktree", "add", "-b", integration_branch, str(path), base_ref_sha)

        self._copy_millstone_config(path)
        return path

    def list_worktrees(self) -> list[dict]:
        """Return parsed `git worktree list --porcelain` entries."""
        result = self._git("worktree", "list", "--porcelain")
        entries: list[dict] = []
        current: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                if current:
                    entries.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value.strip()
        if current:
            entries.append(current)
        return entries

    def is_branch_checked_out(self, branch: str) -> bool:
        """Return True if branch is checked out in any worktree."""
        target = branch if branch.startswith("refs/") else f"refs/heads/{branch}"
        return any(wt.get("branch") == target for wt in self.list_worktrees())

    def remove_worktree(self, path: Path, delete_branch: bool = False) -> None:
        """Remove a worktree directory; optionally delete its branch.

        Does not force-remove dirty worktrees.
        """
        path = Path(path)
        branch_to_delete: str | None = None
        if delete_branch:
            for wt in self.list_worktrees():
                if Path(wt.get("worktree", "")) == path and wt.get("branch"):
                    # "refs/heads/foo" -> "foo"
                    ref = wt["branch"]
                    if ref.startswith("refs/heads/"):
                        branch_to_delete = ref[len("refs/heads/") :]
                    else:
                        branch_to_delete = ref
                    break

        with self.git_lock:
            try:
                self._git("worktree", "remove", str(path), check=True)
            except subprocess.CalledProcessError as e:
                # Preserve dirty worktrees; caller may choose a different cleanup policy.
                msg = (e.stderr or e.stdout or "").strip()
                print(f"WARN: Failed to remove worktree {path}: {msg}")
                return
            finally:
                # Prune orphaned metadata entries if any.
                with contextlib.suppress(Exception):
                    self._git("worktree", "prune", check=False)

            if delete_branch and branch_to_delete:
                # These branches are created solely for millstone task worktrees.
                self._git("branch", "-D", branch_to_delete, check=False)

    def cleanup(self, policy: str, task_statuses: dict) -> None:
        """Remove task worktrees/branches according to cleanup policy."""
        # Always attempt to remove the integration worktree at the end of a run.
        integration_path = self._integration_worktree_path()
        if integration_path.exists():
            self.remove_worktree(integration_path, delete_branch=False)

        if policy not in ("always", "on_success", "never"):
            raise ValueError(f"Invalid cleanup policy: {policy}")

        if policy == "never":
            return

        for task_id, status in task_statuses.items():
            path = self._task_worktree_path(task_id)
            if not path.exists():
                continue

            should_remove = policy == "always"
            if policy == "on_success":
                # Accept both a plain status string and a record dict.
                status_value = status
                if isinstance(status, dict):
                    status_value = status.get("status")
                should_remove = status_value in ("completed", "success", "merged", "landed")

            if should_remove:
                self.remove_worktree(path, delete_branch=True)

    def detect_existing(self) -> list[dict]:
        """Return existing millstone/* worktrees for crash recovery."""
        existing = []
        for wt in self.list_worktrees():
            branch = wt.get("branch", "")
            if branch.startswith("refs/heads/millstone/"):
                existing.append(wt)
        return existing
