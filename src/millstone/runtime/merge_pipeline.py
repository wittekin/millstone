from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from millstone.artifacts.tasklist import TasklistManager
from millstone.config import WORK_DIR_NAME
from millstone.runtime.locks import AdvisoryLock


def run_safety_gates(
    repo_dir: Path,
    base_ref: str,
    head_ref: str,
    policy: dict,
    loc_threshold: int,
) -> tuple[bool, str]:
    """Run LoC/sensitive/dangerous checks against a git diff between refs."""
    repo_dir = Path(repo_dir)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    diff_range = f"{base_ref}...{head_ref}"

    # LoC threshold
    total_loc = 0
    numstat = git("diff", "--numstat", diff_range)
    for line in numstat.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        if parts[0] != "-" and parts[1] != "-":
            total_loc += int(parts[0]) + int(parts[1])

    # Enforce the stricter of policy and caller limits.
    policy_loc_limit = policy.get("limits", {}).get("max_loc_per_task", loc_threshold)
    effective_loc_threshold = min(int(policy_loc_limit), int(loc_threshold))
    if total_loc > effective_loc_threshold:
        return False, f"loc_threshold_exceeded:{total_loc}>{effective_loc_threshold}"

    changed_files = git("diff", "--name-only", diff_range)

    # Sensitive paths
    sensitive = policy.get("sensitive", {})
    if sensitive.get("enabled", False):
        patterns = sensitive.get("paths", []) or []
        require_approval = sensitive.get("require_approval", True)
        if require_approval:
            for pattern in patterns:
                regex = re.escape(pattern).replace(r"\*", ".*")
                if re.search(regex, changed_files, re.IGNORECASE):
                    return False, f"sensitive_path:{pattern}"

    # Dangerous patterns
    dangerous = policy.get("dangerous", {})
    dangerous_patterns = dangerous.get("patterns", []) or []
    should_block = dangerous.get("block", True)
    if dangerous_patterns and should_block:
        diff_content = git("diff", diff_range)
        for pattern in dangerous_patterns:
            if re.search(pattern, diff_content, re.IGNORECASE):
                return False, f"dangerous_pattern:{pattern}"

    return True, ""


@dataclass
class IntegrationResult:
    success: bool
    status: str  # "merged", "conflict", "safety_fail", "eval_fail", "land_fail"
    error: str | None = None
    conflict_summary: str | None = None


class MergePipeline:
    def __init__(
        self,
        repo_dir: Path,
        integration_worktree: Path,
        base_branch: str,
        integration_branch: str,
        merge_strategy: str,
        git_lock: AdvisoryLock,
        tasklist_lock: AdvisoryLock,
        policy: dict,
        loc_threshold: int,
        max_retries: int,
        tasklist: str = "docs/tasklist.md",
        skip_tasklist_mark: bool = False,
    ):
        self.repo_dir = Path(repo_dir)
        self.integration_worktree = Path(integration_worktree)
        self.base_branch = base_branch
        self.integration_branch = integration_branch
        self.merge_strategy = merge_strategy
        self.git_lock = git_lock
        self.tasklist_lock = tasklist_lock
        self.policy = policy or {}
        self.loc_threshold = int(loc_threshold)
        self.max_retries = int(max_retries)
        self.skip_tasklist_mark = skip_tasklist_mark

        # Bind tasklist operations to the integration checkout.
        self.tasklist_manager = TasklistManager(
            repo_dir=self.integration_worktree,
            tasklist=tasklist,
        )

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self.integration_worktree,
            capture_output=True,
            text=True,
            check=check,
        )

    def _git_out(self, *args: str) -> str:
        return self._git(*args).stdout

    def _rev_parse(self, ref: str) -> str:
        return self._git_out("rev-parse", ref).strip()

    def _reset_hard(self, ref: str) -> None:
        self._git("reset", "--hard", ref)

    def _conflict_summary(self) -> str:
        files = self._git_out("diff", "--name-only", "--diff-filter=U").strip()
        return files or "merge conflict"

    def _abort_integration(self) -> None:
        if self.merge_strategy == "merge":
            self._git("merge", "--abort", check=False)
        else:
            self._git("cherry-pick", "--abort", check=False)

    def integrate_eval_and_land(
        self,
        task_id: str,
        task_branch: str,
        base_ref_sha: str,
        commit_sha: str | None,
        task_risk: str | None,
        taskmap: dict,
        eval_manager_factory: Callable[..., object],
    ) -> IntegrationResult:
        """Integrate a task branch into integration, run gates, and land to base.

        Holds git_lock for the entire merge+eval+land pipeline.
        """
        with self.git_lock:
            current_branch = self._git_out("rev-parse", "--abbrev-ref", "HEAD").strip()
            if current_branch != self.integration_branch:
                self._git("checkout", self.integration_branch)

            for attempt in range(self.max_retries + 1):
                base_head = self._rev_parse(self.base_branch)

                # Ensure integration is clean and aligned with base head.
                self._abort_integration()
                self._reset_hard(base_head)

                # Integrate
                try:
                    if self.merge_strategy == "cherry-pick":
                        if commit_sha:
                            self._git("cherry-pick", "--no-edit", commit_sha)
                        else:
                            self._git(
                                "cherry-pick",
                                "--no-edit",
                                f"{base_ref_sha}..{task_branch}",
                            )
                    elif self.merge_strategy == "merge":
                        self._git("merge", "--no-ff", "--no-edit", task_branch)
                    else:
                        return IntegrationResult(
                            success=False,
                            status="land_fail",
                            error=f"unknown merge_strategy:{self.merge_strategy}",
                        )
                except subprocess.CalledProcessError:
                    summary = self._conflict_summary()
                    self._abort_integration()
                    self._reset_hard(base_head)
                    return IntegrationResult(
                        success=False,
                        status="conflict",
                        conflict_summary=summary,
                        error=summary,
                    )

                # Safety gates against the integration diff.
                passed, reason = run_safety_gates(
                    repo_dir=self.integration_worktree,
                    base_ref=base_head,
                    head_ref="HEAD",
                    policy=self.policy,
                    loc_threshold=self.loc_threshold,
                )
                if not passed:
                    self._reset_hard(base_head)
                    return IntegrationResult(
                        success=False,
                        status="safety_fail",
                        error=reason,
                    )

                # Eval gate (risk-based depth).
                mode = "full" if (task_risk or "medium").lower() == "high" else "smoke"
                eval_mgr = eval_manager_factory(
                    repo_dir=self.integration_worktree,
                    work_dir=self.integration_worktree / WORK_DIR_NAME,
                )
                eval_result = eval_mgr.run_eval(mode=mode)  # type: ignore[attr-defined]
                if not eval_result.get("_passed", False):
                    self._reset_hard(base_head)
                    return IntegrationResult(
                        success=False,
                        status="eval_fail",
                        error="eval_failed",
                    )

                # Mark task complete in the integration checkout.
                # Skipped for MCP providers — they handle completion externally.
                if not self.skip_tasklist_mark:
                    with self.tasklist_lock:
                        task_already_complete = False
                        ok = self.tasklist_manager.mark_task_complete_by_id(task_id, taskmap)
                        if not ok:
                            completion_state = self.tasklist_manager.task_completion_by_id(
                                task_id, taskmap
                            )
                            if completion_state is True:
                                task_already_complete = True
                            else:
                                self._reset_hard(base_head)
                                return IntegrationResult(
                                    success=False,
                                    status="land_fail",
                                    error="task_id_not_found_or_already_complete",
                                )
                        self._git("add", self.tasklist_manager.tasklist)
                        has_staged_changes = (
                            self._git("diff", "--cached", "--quiet", check=False).returncode != 0
                        )
                        if has_staged_changes:
                            msg = f"millstone: mark task {task_id} complete"
                            if task_already_complete:
                                msg = f"millstone: sync task {task_id} tasklist updates"
                            self._git(
                                "commit",
                                "-m",
                                msg,
                            )

                # Land: update base branch ref via local push.
                push = subprocess.run(
                    [
                        "git",
                        "push",
                        ".",
                        f"{self.integration_branch}:{self.base_branch}",
                    ],
                    cwd=self.integration_worktree,
                    capture_output=True,
                    text=True,
                )
                if push.returncode == 0:
                    return IntegrationResult(success=True, status="merged")

                out = (push.stderr or "") + "\n" + (push.stdout or "")
                out = out.strip()
                is_stale = (
                    "non-fast-forward" in out.lower()
                    or "fetch first" in out.lower()
                    or "rejected" in out.lower()
                )
                if is_stale and attempt < self.max_retries:
                    continue

                return IntegrationResult(
                    success=False,
                    status="land_fail",
                    error=out[:2000],
                )

            return IntegrationResult(
                success=False,
                status="land_fail",
                error="integration retries exhausted",
            )
