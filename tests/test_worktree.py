import subprocess
from pathlib import Path

import pytest

from millstone.runtime.locks import AdvisoryLock
from millstone.runtime.worktree import WorktreeManager


def _head_sha(repo_dir: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class TestWorktreeManager:
    def test_create_task_worktree(self, temp_repo):
        base_ref = _head_sha(temp_repo)
        lock = AdvisoryLock(temp_repo / ".millstone" / "locks" / "git.lock", timeout=2.0)
        mgr = WorktreeManager(
            repo_dir=temp_repo,
            worktree_root=temp_repo / ".millstone" / "worktrees",
            git_lock=lock,
        )

        wt = mgr.create_task_worktree("task-one", base_ref)
        assert wt.exists()
        assert (wt / ".git").exists()

        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=wt,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert branch == "millstone/task/task-one"

        assert mgr.is_branch_checked_out("millstone/task/task-one") is True

    def test_config_copy_to_worktree(self, temp_repo):
        base_ref = _head_sha(temp_repo)
        millstone_dir = temp_repo / ".millstone"
        millstone_dir.mkdir(exist_ok=True)
        (millstone_dir / "config.toml").write_text("parallel_enabled = true\n")
        (millstone_dir / "policy.toml").write_text("[limits]\nmax_loc_per_task = 123\n")
        (millstone_dir / "project.toml").write_text('[project]\nname = "x"\n')

        lock = AdvisoryLock(temp_repo / ".millstone" / "locks" / "git.lock", timeout=2.0)
        mgr = WorktreeManager(
            repo_dir=temp_repo,
            worktree_root=temp_repo / ".millstone" / "worktrees",
            git_lock=lock,
        )

        wt = mgr.create_task_worktree("task-two", base_ref)
        assert (wt / ".millstone" / "config.toml").read_text() == "parallel_enabled = true\n"
        assert (wt / ".millstone" / "policy.toml").read_text().startswith("[limits]")
        assert (wt / ".millstone" / "project.toml").read_text().startswith("[project]")

    def test_remove_worktree(self, temp_repo):
        base_ref = _head_sha(temp_repo)
        lock = AdvisoryLock(temp_repo / ".millstone" / "locks" / "git.lock", timeout=2.0)
        mgr = WorktreeManager(
            repo_dir=temp_repo,
            worktree_root=temp_repo / ".millstone" / "worktrees",
            git_lock=lock,
        )

        wt = mgr.create_task_worktree("task-rm", base_ref)
        assert mgr.is_branch_checked_out("millstone/task/task-rm") is True
        mgr.remove_worktree(wt, delete_branch=True)
        assert not wt.exists()
        assert mgr.is_branch_checked_out("millstone/task/task-rm") is False

        # Branch should be gone.
        refs = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/millstone/task/task-rm"],
            cwd=temp_repo,
        ).returncode
        assert refs != 0

    def test_create_integration_worktree_recreates_existing(self, temp_repo):
        base_ref = _head_sha(temp_repo)
        lock = AdvisoryLock(temp_repo / ".millstone" / "locks" / "git.lock", timeout=2.0)
        mgr = WorktreeManager(
            repo_dir=temp_repo,
            worktree_root=temp_repo / ".millstone" / "worktrees",
            git_lock=lock,
        )

        first = mgr.create_integration_worktree("millstone/integration", base_ref)
        assert first.exists()

        # Simulate stale/partial crash residue in the integration worktree path.
        (first / "stale.txt").write_text("stale\n")

        second = mgr.create_integration_worktree("millstone/integration", base_ref)
        assert second == first
        assert second.exists()
        assert not (second / "stale.txt").exists()

        # Ensure there is only one integration checkout in worktree metadata.
        integration_entries = [
            wt
            for wt in mgr.list_worktrees()
            if wt.get("branch") == "refs/heads/millstone/integration"
        ]
        assert len(integration_entries) == 1

    def test_cleanup_on_success_preserves_failed(self, temp_repo):
        base_ref = _head_sha(temp_repo)
        lock = AdvisoryLock(temp_repo / ".millstone" / "locks" / "git.lock", timeout=2.0)
        mgr = WorktreeManager(
            repo_dir=temp_repo,
            worktree_root=temp_repo / ".millstone" / "worktrees",
            git_lock=lock,
        )

        wt_ok = mgr.create_task_worktree("ok", base_ref)
        wt_fail = mgr.create_task_worktree("fail", base_ref)
        wt_integration = mgr.create_integration_worktree("millstone/integration", base_ref)

        mgr.cleanup("on_success", {"ok": "completed", "fail": "failed"})

        assert not wt_ok.exists()
        assert wt_fail.exists()
        assert not wt_integration.exists()

    @pytest.mark.parametrize(
        "bad",
        [
            "UPPER",
            "has space",
            "has.dot",
            "x" * 41,
        ],
    )
    def test_validate_task_id_rejects_invalid(self, temp_repo, bad):
        lock = AdvisoryLock(temp_repo / ".millstone" / "locks" / "git.lock", timeout=2.0)
        mgr = WorktreeManager(
            repo_dir=temp_repo,
            worktree_root=temp_repo / ".millstone" / "worktrees",
            git_lock=lock,
        )
        with pytest.raises(ValueError):
            mgr.validate_task_id(bad)
