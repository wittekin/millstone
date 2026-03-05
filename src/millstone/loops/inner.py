"""
Inner loop management for the millstone orchestrator.

This module contains the InnerLoopManager class which handles the build-review-commit
core functionality. The Orchestrator class holds an instance and delegates via thin
wrapper methods.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from millstone.policy.schemas import (
    ReviewDecision,
    parse_review_decision,
    parse_sanity_result,
)
from millstone.utils import progress


class InnerLoopManager:
    """Manages inner loop operations for build-review-commit cycles.

    This class handles operations related to:
    - STOP file detection
    - Mechanical checks (LoC threshold, sensitive files, dangerous patterns)
    - Implementation and review sanity checks
    - Review approval decision parsing
    - Commit delegation
    - Single task execution through build-review cycle
    """

    def __init__(
        self,
        work_dir: Path,
        repo_dir: Path,
        loc_threshold: int = 1000,
        policy: dict | None = None,
        project_config: dict | None = None,
        loop_sensitive_patterns: list[str] | None = None,
    ):
        """Initialize the InnerLoopManager.

        Args:
            work_dir: Path to the work directory (.millstone/).
            repo_dir: Path to the repository root.
            loc_threshold: Maximum lines of code changed per task.
            policy: Policy configuration dict (from load_policy).
            project_config: Project configuration dict (from load_project_config).
            loop_sensitive_patterns: Optional sensitive-file patterns from loop registry.
        """
        self.work_dir = work_dir
        self.repo_dir = repo_dir
        self.loc_threshold = loc_threshold
        self.policy = policy or {}
        self.project_config = project_config or {}
        self.loop_sensitive_patterns = loop_sensitive_patterns

    def git(self, *args) -> str:
        """Run git command and return output."""
        import subprocess
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, cwd=self.repo_dir
        )
        return result.stdout

    # =========================================================================
    # STOP file detection
    # =========================================================================

    def check_stop(self) -> bool:
        """Check if a STOP.md file was created.

        Returns:
            True if STOP.md exists (should halt), False otherwise.
        """
        stop_file = self.work_dir / "STOP.md"
        if stop_file.exists():
            print()
            print("=== STOPPED ===")
            print("Reason:")
            print(stop_file.read_text())
            return True
        return False

    # =========================================================================
    # Sanity checks
    # =========================================================================

    def sanity_check_impl(
        self,
        agent_output: str,
        git_status: str,
        git_diff: str,
        load_prompt_callback: Callable[[str], str],
        run_agent_callback: Callable[..., str],
    ) -> bool:
        """Sanity check implementation before review.

        Uses structured output to get OK/HALT signal from the sanity check agent.
        Falls back to file-based STOP.md detection for compatibility.

        Args:
            agent_output: Output from the builder agent.
            git_status: Output of `git status --short`.
            git_diff: Output of `git diff`.
            load_prompt_callback: Callback to load prompt templates.
            run_agent_callback: Callback to invoke the LLM agent.

        Returns:
            True if sanity check passed, False if halted.
        """
        prompt = load_prompt_callback("sanity_check_impl.md")
        prompt = prompt.replace("{{ORCHESTRATOR_DIR}}", str(self.work_dir))
        prompt = prompt.replace("{{AGENT_OUTPUT}}", agent_output[:3000])
        prompt = prompt.replace("{{GIT_STATUS}}", git_status[:2000])
        prompt = prompt.replace("{{GIT_DIFF}}", git_diff[:6000])

        print("Running implementation sanity check...")
        output = run_agent_callback(
            prompt,
            role="sanity",
            output_schema="sanity_check",
        )

        # Try structured parsing first
        result = parse_sanity_result(output)
        if result is not None and result.should_halt:
            # Write STOP.md for consistency with existing mechanism
            stop_file = self.work_dir / "STOP.md"
            reason = result.reason or "Sanity check halted (no reason provided)"
            stop_file.write_text(f"Implementation sanity check failed:\n\n{reason}\n")
            print()
            print("=== STOPPED ===")
            print("Reason:")
            print(reason)
            return False

        # Fallback: check for file-based signal (compatibility)
        if self.check_stop():
            return False

        print("Implementation sanity check: OK")
        return True

    def sanity_check_review(
        self,
        review_output: str,
        load_prompt_callback: Callable[[str], str],
        run_agent_callback: Callable[..., str],
    ) -> bool:
        """Sanity check review before passing back to builder.

        Uses structured output to get OK/HALT signal from the sanity check agent.
        Falls back to file-based STOP.md detection for compatibility.

        Args:
            review_output: Output from the reviewer agent.
            load_prompt_callback: Callback to load prompt templates.
            run_agent_callback: Callback to invoke the LLM agent.

        Returns:
            True if sanity check passed, False if halted.
        """
        prompt = load_prompt_callback("sanity_check_review.md")
        prompt = prompt.replace("{{ORCHESTRATOR_DIR}}", str(self.work_dir))
        prompt = prompt.replace("{{REVIEW_OUTPUT}}", review_output[:6000])

        print("Running review sanity check...")
        output = run_agent_callback(
            prompt,
            role="sanity",
            output_schema="sanity_check",
        )

        # Try structured parsing first
        result = parse_sanity_result(output)
        if result is not None and result.should_halt:
            # Write STOP.md for consistency with existing mechanism
            stop_file = self.work_dir / "STOP.md"
            reason = result.reason or "Sanity check halted (no reason provided)"
            stop_file.write_text(f"Review sanity check failed:\n\n{reason}\n")
            print()
            print("=== STOPPED ===")
            print("Reason:")
            print(reason)
            return False

        # Fallback: check for file-based signal (compatibility)
        if self.check_stop():
            return False

        print("Review sanity check: OK")
        return True

    # =========================================================================
    # Mechanical checks
    # =========================================================================

    def mechanical_checks(
        self,
        loc_baseline_ref: str | None,
        skip_mechanical_checks: bool,
        no_tasklist_edits: bool = False,
        tasklist_path: str | None = None,
        tasklist_baseline: str | None = None,
        log_callback: Callable[..., None] | None = None,
        save_state_callback: Callable[[str], None] | None = None,
        git_callback: Callable[..., str] | None = None,
    ) -> tuple[bool, bool]:
        """Run mechanical sanity checks (no LLM needed).

        Uses policy configuration from .millstone/policy.toml for limits and patterns.
        When --continue is used after a halt, the first call to this method
        skips LoC and sensitive file checks (since the user has already reviewed).

        Policy rules checked:
        - limits.max_loc_per_task: Maximum lines of code changed
        - sensitive.paths: File patterns that require human approval (when sensitive.enabled is true)
        - dangerous.patterns: Content patterns that are blocked
        - tasklist.enforce_single_task: Only first unchecked task may be checked off

        Args:
            loc_baseline_ref: Git ref to diff against for LoC calculation.
            skip_mechanical_checks: If True, skip threshold/sensitive checks.
            tasklist_path: Path to tasklist file (relative to repo root).
            tasklist_baseline: Tasklist content captured before task execution.
            log_callback: Optional callback for logging events.
            save_state_callback: Optional callback to save state on halt.
            git_callback: Optional callback to run git commands. Falls back to self.git.

        Returns:
            Tuple of (passed, skip_mechanical_checks_consumed) where:
            - passed: True if all checks passed, False otherwise.
            - skip_mechanical_checks_consumed: True if skip was consumed.
        """
        # Use callback or fall back to internal git method
        git = git_callback if git_callback else self.git
        skip_consumed = False

        # Check for changes (staged or unstaged)
        status = git("status", "--porcelain").strip()
        if not status:
            self._log_policy_violation("no_changes", "No changes detected", log_callback)
            print("WARN: No changes detected. Proceeding to review (task may be read-only).")
            # We don't fail here anymore; we let the reviewer decide if changes were required.
            # return False, skip_consumed

        # Baseline for all diff-based checks (includes staged/unstaged + committed changes since baseline).
        baseline = loc_baseline_ref or "HEAD"
        changed_files = git("diff", baseline, "--name-only")
        changed_file_list = [f for f in changed_files.strip().split("\n") if f]

        # Worker safety: when running under --no-tasklist-edits, block any edits to the tasklist,
        # including committed edits (git diff <baseline> catches both committed and uncommitted changes).
        if no_tasklist_edits and tasklist_path and tasklist_path in changed_file_list:
            rule = "--no-tasklist-edits"
            self._log_policy_violation(
                "tasklist_edits_blocked",
                f"Tasklist modified under {rule}: {tasklist_path}",
                log_callback,
            )
            print(f"BLOCKED: Tasklist edits are not allowed ({rule}).")
            print(f"File modified: {tasklist_path}")
            if save_state_callback:
                save_state_callback(f"policy:no_tasklist_edits:{tasklist_path}")
            return False, skip_consumed

        # Skip threshold/sensitive checks if continuing from a halt
        if skip_mechanical_checks:
            skip_consumed = True
            print("Skipping LoC/sensitive file checks (--continue mode)")
            return True, skip_consumed

        # Check LoC threshold against baseline (per-task, not cumulative)
        # Use policy limit, falling back to CLI-provided loc_threshold
        total_loc = 0
        # Diff working directory against baseline (includes both staged and unstaged)
        numstat = git("diff", baseline, "--numstat")
        for line in numstat.strip().split("\n"):
            if line:
                parts = line.split("\t")
                if parts[0] != "-" and parts[1] != "-":  # Skip binary files
                    total_loc += int(parts[0]) + int(parts[1])

        # Enforce the stricter of policy and CLI limits.
        # This prevents accidental widening of safeguards from either source.
        policy_loc_limit = self.policy.get("limits", {}).get("max_loc_per_task", self.loc_threshold)
        effective_loc_threshold = min(int(policy_loc_limit), int(self.loc_threshold))
        if total_loc > effective_loc_threshold:
            rule = f"limits.max_loc_per_task ({effective_loc_threshold})"
            self._log_policy_violation("loc_threshold_exceeded", f"{total_loc} LoC exceeds {rule}", log_callback)
            print(f"Halted: {total_loc} lines changed (policy rule: {rule}).")
            print()
            print("Options:")
            print("  (1) Review with `git diff HEAD`, commit manually if satisfied, then re-run")
            print(f"  (2) Re-run with --loc-threshold={total_loc + 100}")
            print("  (3) Re-run with --continue to skip this check (after manual review)")
            if save_state_callback:
                save_state_callback(f"policy:loc_threshold_exceeded:{total_loc}")
            return False, skip_consumed

        sensitive_enabled = self.policy.get("sensitive", {}).get("enabled", False)
        if sensitive_enabled:
            # Check for sensitive files using policy patterns
            # Prefer policy.sensitive.paths, fall back to project_config.sensitive_paths
            policy_sensitive = self.policy.get("sensitive", {}).get("paths", [])
            config_patterns = policy_sensitive or self.project_config.get("sensitive_paths", {}).get("patterns", [])
            sensitive_patterns = []
            for pattern in config_patterns:
                # Escape regex special chars except *
                regex = re.escape(pattern).replace(r"\*", ".*")
                sensitive_patterns.append((pattern, regex))
            # Fallback to defaults if no patterns configured
            if not sensitive_patterns:
                if self.loop_sensitive_patterns:
                    default_patterns = self.loop_sensitive_patterns
                else:
                    default_patterns = [".env", "credentials", "secret", ".pem", ".key"]
                sensitive_patterns = [(p, re.escape(p).replace(r"\*", ".*")) for p in default_patterns]

            require_approval = self.policy.get("sensitive", {}).get("require_approval", True)
            for original_pattern, regex in sensitive_patterns:
                if re.search(regex, changed_files, re.IGNORECASE):
                    rule = f"sensitive.paths (matched '{original_pattern}')"
                    self._log_policy_violation("sensitive_file", f"File matched {rule}", log_callback)
                    if require_approval:
                        print(f"WARN: Sensitive files modified. Halting for human review (policy rule: {rule}).")
                        print(changed_files)
                        print()
                        print("Options:")
                        print("  (1) Review changes, then commit manually")
                        print("  (2) Re-run with --continue to skip this check (after manual review)")
                        if save_state_callback:
                            save_state_callback(f"policy:sensitive_files:{original_pattern}")
                        return False, skip_consumed
                    else:
                        # Log but don't block if require_approval is False
                        print(f"WARN: Sensitive file matched '{original_pattern}' (approval not required by policy)")

        # Check for dangerous patterns in diff content
        dangerous_patterns = self.policy.get("dangerous", {}).get("patterns", [])
        should_block = self.policy.get("dangerous", {}).get("block", True)
        if dangerous_patterns:
            # Get the actual diff content to scan for dangerous patterns
            diff_content = git("diff", baseline)
            for pattern in dangerous_patterns:
                if re.search(pattern, diff_content, re.IGNORECASE):
                    rule = f"dangerous.patterns (matched '{pattern}')"
                    self._log_policy_violation("dangerous_pattern", f"Diff content matched {rule}", log_callback)
                    if should_block:
                        print(f"BLOCKED: Dangerous pattern detected in changes (policy rule: {rule}).")
                        print()
                        print("This pattern is blocked by policy. The change cannot proceed.")
                        print("Review the diff and remove the dangerous content, or update the policy.")
                        if save_state_callback:
                            save_state_callback(f"policy:dangerous_pattern:{pattern}")
                        return False, skip_consumed
                    else:
                        # Log but don't block if block is False
                        print(f"WARN: Dangerous pattern '{pattern}' detected (blocking disabled by policy)")

        enforce_single_task = self.policy.get("tasklist", {}).get("enforce_single_task", False)
        if enforce_single_task and tasklist_path and tasklist_baseline is not None:
            tasklist_file = self.repo_dir / tasklist_path
            if tasklist_file.exists():
                from millstone.artifacts.tasklist import TasklistManager

                new_content = tasklist_file.read_text()
                manager = TasklistManager(self.repo_dir, tasklist=tasklist_path)
                valid, reason = manager.validate_single_task_completion(
                    tasklist_baseline,
                    new_content,
                )
                if not valid:
                    rule = "tasklist.enforce_single_task"
                    self._log_policy_violation("tasklist_scope", reason, log_callback)
                    if log_callback:
                        log_callback(
                            "tasklist_scope_violation",
                            rule=rule,
                            tasklist=tasklist_path,
                            reason=reason,
                        )
                    print(f"BLOCKED: Tasklist scope violation ({rule}).")
                    print(reason)
                    if save_state_callback:
                        save_state_callback(f"policy:tasklist_scope:{reason}")
                    return False, skip_consumed

        return True, skip_consumed

    def _log_policy_violation(
        self,
        violation_type: str,
        message: str,
        log_callback: Callable[..., None] | None = None,
    ) -> None:
        """Log a policy violation with the specific rule that triggered it.

        Args:
            violation_type: Type of violation (e.g., 'loc_threshold_exceeded', 'sensitive_file')
            message: Detailed message about the violation
            log_callback: Optional callback for logging events.
        """
        from millstone.config import POLICY_FILE_NAME, WORK_DIR_NAME

        if log_callback:
            log_callback(
                "policy_violation",
                violation_type=violation_type,
                message=message,
                policy_file=str(self.repo_dir / WORK_DIR_NAME / POLICY_FILE_NAME),
            )

    # =========================================================================
    # Review approval
    # =========================================================================

    def is_approved(self, review_output: str) -> tuple[bool, ReviewDecision | None]:
        """Check if review indicates approval.

        Uses structured output parsing from schemas module with fallback to
        regex patterns for compatibility with agents that don't follow schema.

        Args:
            review_output: Raw output from the reviewer agent.

        Returns:
            Tuple of (is_approved, parsed_decision).
            parsed_decision may be None if parsing failed but fallback matched.
        """
        # Try structured parsing first
        decision = parse_review_decision(review_output)
        if decision is not None:
            return decision.is_approved, decision

        return False, None

    # =========================================================================
    # Commit delegation
    # =========================================================================

    def delegate_commit(
        self,
        tasklist: str,
        session_id: str | None,
        load_prompt_callback: Callable[[str], str],
        run_agent_callback: Callable[..., str],
        log_callback: Callable[..., None] | None = None,
        update_loc_baseline_callback: Callable[[], None] | None = None,
        task_prefix: str = "",
        git_callback: Callable[..., str] | None = None,
    ) -> tuple[bool, dict | None]:
        """Ask the builder agent to commit its changes.

        The builder has full context of what it implemented and can write
        better commit messages than the orchestrator parsing task text.
        Uses the existing session if available, otherwise starts fresh.

        If the builder commits code but leaves the tasklist unstaged
        (a common oversight), we auto-commit the tasklist tick separately.

        Args:
            tasklist: Path to the tasklist file (relative to repo_dir).
            session_id: Session ID for resuming builder conversation.
            load_prompt_callback: Callback to load prompt templates.
            run_agent_callback: Callback to invoke the LLM agent.
            log_callback: Optional callback for logging events.
            update_loc_baseline_callback: Optional callback to update LoC baseline.
            task_prefix: Optional prefix for progress messages.
            git_callback: Optional callback to run git commands. Falls back to self.git.

        Returns:
            Tuple of (success, failure_info) where:
            - success: True if commit succeeded, False if failed.
            - failure_info: Dict with status and builder_output if failed, else None.
        """
        import subprocess

        # Use callback or fall back to internal git method
        git = git_callback if git_callback else self.git

        commit_prompt = load_prompt_callback("commit_prompt.md")

        progress(f"{task_prefix} Delegating commit to builder...")
        if session_id:
            builder_output = run_agent_callback(commit_prompt, resume=session_id, role="builder")
        else:
            builder_output = run_agent_callback(commit_prompt, role="builder")

        # Verify commit succeeded by checking for remaining changes
        status = git("status", "--porcelain").strip()
        if status:
            # Check if the only remaining change is the tasklist file
            # This happens when builder commits code but forgets to stage the tasklist tick
            remaining_files = [line.split()[-1] for line in status.split("\n") if line.strip()]
            tasklist_path = str(tasklist)

            if len(remaining_files) == 1 and remaining_files[0] == tasklist_path:
                # Auto-commit the tasklist tick
                if log_callback:
                    log_callback(
                        "auto_commit_tasklist",
                        reason="builder_forgot_to_stage_tasklist",
                        file=tasklist_path,
                    )
                subprocess.run(
                    ["git", "add", tasklist_path],
                    cwd=self.repo_dir,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", "Mark task complete in tasklist\n\nGenerated with millstone orchestrator"],
                    cwd=self.repo_dir,
                    capture_output=True,
                )
                progress(f"{task_prefix} Auto-committed tasklist tick (builder forgot to stage it)")
                # Verify again
                status = git("status", "--porcelain").strip()
                if not status:
                    if update_loc_baseline_callback:
                        update_loc_baseline_callback()
                    return True, None

            # There are still uncommitted changes - commit failed
            # Log detailed diagnostics for debugging
            if log_callback:
                log_callback(
                    "commit_failed",
                    reason="uncommitted_changes_remain",
                    status=status[:500],
                    builder_output=builder_output[:2000],
                )
            # Return failure info for caller to display
            return False, {
                "status": status,
                "builder_output": builder_output,
            }

        # Update LoC baseline so next task measures from this commit
        if update_loc_baseline_callback:
            update_loc_baseline_callback()
        return True, None
