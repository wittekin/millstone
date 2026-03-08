#!/usr/bin/env python3
"""
Builder-Reviewer Orchestrator

Wraps stochastic LLM calls in a deterministic builder-reviewer workflow.

Usage:
    python orchestrate.py [--max-cycles N] [--loc-threshold N]
"""

import argparse
import contextlib
import copy
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from millstone import __version__
from millstone.agent_providers import CLIProvider, CLIResult, get_provider, list_providers
from millstone.artifacts.eval_manager import EvalManager
from millstone.artifacts.evidence_store import (
    EvidenceStore,
    make_design_review_evidence,
    make_eval_evidence,
    make_review_evidence,
)
from millstone.artifacts.tasklist import TasklistManager
from millstone.config import (
    CONFIG_FILE_NAME,
    DEFAULT_CONFIG,
    DEFAULT_POLICY,
    DEFAULT_PROJECT_CONFIG,
    POLICY_FILE_NAME,
    PROJECT_FILE_NAME,
    STATE_FILE_NAME,
    # Constants
    WORK_DIR_NAME,
    detect_project_type,
    get_default_commands,
    load_config,
    load_policy,
    load_project_config,
)
from millstone.loops.engine import ArtifactReviewLoop
from millstone.loops.inner import InnerLoopManager
from millstone.loops.outer import OuterLoopManager
from millstone.loops.registry_adapter import LoopRegistryAdapter
from millstone.policy.capability import CapabilityPolicyGate, CapabilityTier
from millstone.policy.effects import EffectPolicyGate, NoOpEffectProvider
from millstone.policy.schemas import (
    ReviewDecision,
    parse_design_review,
)
from millstone.runtime.context import ContextManager
from millstone.runtime.profile import ProfileRegistry
from millstone.utils import (
    extract_claude_result,
    filter_reasoning_traces,
    is_empty_response,
    is_whitespace_or_comment_only_change,
    progress,
    summarize_diff,
    summarize_output,
)


@dataclass
class BuilderArtifact:
    """Artifact produced by the builder agent."""

    output: str
    git_status: str
    git_diff: str
    builder_committed: bool = False


@dataclass
class BuilderVerdict:
    """Verdict produced by the reviewer agent."""

    approved: bool
    decision: ReviewDecision | None
    raw_output: str
    feedback: str


class PreflightError(Exception):
    """Raised when pre-flight checks fail."""

    pass


class ConfigurationError(RuntimeError):
    """Raised when runtime wiring does not match configured loop contracts."""


DEFAULT_LOC_THRESHOLD: int = 1000
_ORCHESTRATOR_INTERNAL_ROLES = {"default", "sanity", "analyzer", "release_eng", "sre"}
__all__ = [
    "CONFIG_FILE_NAME",
    "ConfigurationError",
    "DEFAULT_CONFIG",
    "DEFAULT_POLICY",
    "DEFAULT_PROJECT_CONFIG",
    "Orchestrator",
    "POLICY_FILE_NAME",
    "PROJECT_FILE_NAME",
    "PreflightError",
    "STATE_FILE_NAME",
    "WORK_DIR_NAME",
    "detect_project_type",
    "filter_reasoning_traces",
    "get_default_commands",
    "is_empty_response",
    "load_config",
    "load_policy",
    "load_project_config",
    "summarize_output",
]


def _configure_python_logging_for_verbosity(log_verbosity: str) -> None:
    """Configure stdlib logging so DEBUG events are visible in verbose mode."""
    if log_verbosity != "verbose":
        return

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    if root_logger.handlers:
        for handler in root_logger.handlers:
            if handler.level > logging.DEBUG:
                handler.setLevel(logging.DEBUG)
        return

    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root_logger.addHandler(handler)


def _ensure_tasklist_file(tasklist_path: Path) -> None:
    """Create a minimal tasklist file if it does not exist."""
    if tasklist_path.exists():
        return
    tasklist_path.parent.mkdir(parents=True, exist_ok=True)
    tasklist_path.write_text("# Tasklist\n")


def _extract_backlog_items(backlog_text: str) -> list[tuple[str, str]]:
    """Extract backlog items from markdown/plain text into (status, task_text)."""
    items: list[tuple[str, str]] = []
    in_code_block = False

    # Prefer explicit task-like syntax when present.
    has_structured_tasks = bool(
        re.search(
            r"^\s*(?:[-*]\s+\[[ xX]\]\s+|[-*]\s+|\d+[.)]\s+|\[[ xX]\]\s+|TODO[:\-]\s+)",
            backlog_text,
            re.MULTILINE,
        )
    )

    for raw_line in backlog_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if stripped.startswith("#") or stripped.startswith("<!--"):
            continue

        # Checked/unchecked markdown checkbox list items.
        checkbox_match = re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.+?)\s*$", raw_line)
        if checkbox_match:
            status = "x" if checkbox_match.group(1).strip().lower() == "x" else " "
            items.append((status, checkbox_match.group(2).strip()))
            continue

        # Bare checkbox without markdown bullet.
        bare_checkbox_match = re.match(r"^\s*\[([ xX])\]\s+(.+?)\s*$", raw_line)
        if bare_checkbox_match:
            status = "x" if bare_checkbox_match.group(1).strip().lower() == "x" else " "
            items.append((status, bare_checkbox_match.group(2).strip()))
            continue

        # Bullet lists.
        bullet_match = re.match(r"^\s*[-*]\s+(.+?)\s*$", raw_line)
        if bullet_match:
            items.append((" ", bullet_match.group(1).strip()))
            continue

        # Numbered lists.
        numbered_match = re.match(r"^\s*\d+[.)]\s+(.+?)\s*$", raw_line)
        if numbered_match:
            items.append((" ", numbered_match.group(1).strip()))
            continue

        # TODO-prefixed lines.
        todo_match = re.match(r"^\s*TODO[:\-]\s+(.+?)\s*$", raw_line, re.IGNORECASE)
        if todo_match:
            items.append((" ", todo_match.group(1).strip()))
            continue

        # Plain-text migration fallback for line-oriented backlogs.
        if not has_structured_tasks:
            items.append((" ", stripped))

    return items


def _migrate_local_backlog(source_path: Path, output_path: Path) -> dict[str, Any]:
    """Convert a local backlog file into canonical markdown tasklist format."""
    if not source_path.exists():
        raise FileNotFoundError(f"Backlog file not found: {source_path}")

    backlog_text = source_path.read_text()
    items = _extract_backlog_items(backlog_text)
    if not items:
        raise ValueError(
            "No actionable backlog items found. Use bullet/numbered lines or one task per line."
        )

    output_lines = ["# Tasklist", ""]
    for status, task_text in items:
        output_lines.append(f"- [{status}] {task_text}")
    output_lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(output_lines))

    completed_count = sum(1 for status, _ in items if status == "x")
    pending_count = len(items) - completed_count
    return {
        "source_path": str(source_path),
        "output_path": str(output_path),
        "task_count": len(items),
        "pending_count": pending_count,
        "completed_count": completed_count,
    }


class _NoopLock:
    """Minimal lock protocol implementation for best-effort worker state writes."""

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False


class Orchestrator:
    # Mapping of method names to manager attribute names for __getattr__ forwarding.
    # Methods listed here are pure delegations with identical signatures.
    _MANAGER_DELEGATIONS: dict[str, str] = {
        # TasklistManager methods
        # Note: has_remaining_tasks is NOT delegated here; it is overridden below
        # to support MCP-backed tasklist providers that have no local file.
        "extract_current_task_title": "_tasklist_manager",
        "extract_current_task_risk": "_tasklist_manager",
        "extract_current_task_context_file": "_tasklist_manager",
        "extract_current_task_group": "_tasklist_manager",
        "extract_current_task_acceptance_criteria": "_tasklist_manager",
        "count_completed_tasks": "_tasklist_manager",
        "_extract_unchecked_tasks": "_tasklist_manager",
        "_get_referenced_code_size": "_tasklist_manager",
        "_detect_dependencies": "_tasklist_manager",
        "_parse_task_metadata": "_tasklist_manager",
        "_print_tasklist_analysis": "_tasklist_manager",
        # ContextManager methods
        "_get_context_dir": "_context_manager",
        "_get_group_context_path": "_context_manager",
        # EvalManager methods
        "_parse_coverage_json": "_eval_manager",
        "_run_custom_eval_scripts": "_eval_manager",
        "_get_latest_eval": "_eval_manager",
        "_get_eval_before_task": "_eval_manager",
        "print_eval_summary": "_eval_manager",
        "print_metrics_report": "_eval_manager",
        "_parse_pytest_output": "_eval_manager",
        "_extract_failed_tests": "_eval_manager",
        "_generate_task_hash": "_eval_manager",
        "_extract_file_refs": "_eval_manager",
        "_extract_complexity_keywords": "_eval_manager",
        "get_task_summary": "_eval_manager",
        "_print_eval_summary": "_eval_manager",
        "run_category_evals": "_eval_manager",
        "_compute_eval_delta": "_eval_manager",
        # OuterLoopManager methods
        "collect_hard_signals": "_outer_loop_manager",
        "_select_opportunity": "_outer_loop_manager",
        "_format_signals_for_prompt": "_outer_loop_manager",
        "_extract_new_tasks": "_outer_loop_manager",
        # InnerLoopManager methods
        "check_stop": "_inner_loop_manager",
        "is_approved": "_inner_loop_manager",
    }

    def __getattr__(self, name: str):
        """Forward method calls to the appropriate manager for pure delegations.

        This reduces boilerplate for methods that are simple pass-throughs to
        manager classes. Only methods listed in _MANAGER_DELEGATIONS are forwarded.
        """
        if name in self._MANAGER_DELEGATIONS:
            manager_attr = self._MANAGER_DELEGATIONS[name]
            manager = object.__getattribute__(self, manager_attr)
            return getattr(manager, name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def __init__(
        self,
        max_cycles: int = 3,
        loc_threshold: int = DEFAULT_LOC_THRESHOLD,
        repo_dir: Path | None = None,
        task: str | None = None,
        tasklist: str = ".millstone/tasklist.md",
        roadmap: str | None = None,
        max_tasks: int = 5,
        dry_run: bool = False,
        research: bool = False,
        prompts_dir: str | Path | None = None,
        compact_threshold: int = 20,
        continue_run: bool = False,
        session_mode: str = "new",
        eval_on_commit: bool = False,
        auto_rollback: bool = False,
        retry_on_empty_response: bool | None = None,
        eval_scripts: list[str] | None = None,
        eval_on_task: str = "none",
        skip_eval: bool = False,
        review_designs: bool = True,
        approve_opportunities: bool = True,
        approve_designs: bool = True,
        approve_plans: bool = True,
        category_weights: dict[str, float] | None = None,
        category_thresholds: dict[str, int] | None = None,
        task_constraints: dict | None = None,
        risk_settings: dict | None = None,
        quiet: bool = False,
        min_response_length: int = 50,
        log_verbosity: str = "normal",
        log_diff_mode: str = "summary",
        profile: str = "dev_implementation",
        # CLI provider configuration
        cli: str = "claude",
        cli_builder: str | None = None,
        cli_reviewer: str | None = None,
        cli_sanity: str | None = None,
        cli_analyzer: str | None = None,
        cli_release_eng: str | None = None,
        cli_sre: str | None = None,
        # Worktree/parallel execution (control plane)
        parallel_enabled: bool = False,
        parallel_concurrency: int = 1,
        parallel_merge_strategy: str = "merge",
        parallel_integration_branch: str = "millstone/integration",
        parallel_worktree_root: str = ".millstone/worktrees",
        parallel_cleanup: str = "on_success",
        parallel_lock_git: str = ".millstone/locks/git.lock",
        parallel_lock_state: str = ".millstone/locks/state.lock",
        parallel_lock_tasklist: str = ".millstone/locks/tasklist.lock",
        parallel_heartbeat_interval: int = 30,
        parallel_heartbeat_ttl: int = 300,
        # Worktree CLI options / overrides
        base_branch: str | None = None,
        base_ref: str | None = None,
        integration_branch: str | None = None,
        merge_strategy: str | None = None,
        worktree_root: str | None = None,
        shared_state_dir: str | None = None,
        merge_max_retries: int = 2,
        worktree_cleanup: str | None = None,
        no_tasklist_edits: bool = False,
        high_risk_concurrency: int = 1,
    ):
        self.max_cycles = max_cycles
        self.base_max_cycles = max_cycles  # Store original for risk-based adjustments
        self.loc_threshold = loc_threshold
        self.cycle = 0
        # Session IDs for builder and reviewer agents (tracked separately for session continuity)
        self.builder_session_id: str | None = None
        self.reviewer_session_id: str | None = None
        # Legacy alias for backwards compatibility (points to builder_session_id)
        self.task = task
        self.tasklist = tasklist
        self.roadmap = roadmap
        self.max_tasks = max_tasks
        self.dry_run = dry_run
        self.research = research  # Research mode: skip no-changes check and commit
        self.completed_task_count: int = 0  # Count of - [x] entries in tasklist
        self.compact_threshold = compact_threshold
        self.continue_run = continue_run  # Whether to resume from saved state
        # Session persistence mode:
        # - "new" / "new_each_task": Fresh session for each task (default)
        # - "continue" / "continue_across_runs": Resume session from state file
        # - "continue_within_run": Preserve session for all tasks in single invocation
        # - Or a specific session ID string to resume
        valid_modes = (
            "new",
            "new_each_task",
            "continue",
            "continue_across_runs",
            "continue_within_run",
        )
        if session_mode not in valid_modes and not session_mode:
            raise ValueError(
                f"Invalid session_mode '{session_mode}'. Must be one of {valid_modes} or a session ID."
            )
        # Normalize legacy values to new config values
        if session_mode == "new":
            session_mode = "new_each_task"
        elif session_mode == "continue":
            session_mode = "continue_across_runs"
        self.session_mode = session_mode
        self.eval_on_commit = eval_on_commit  # Whether to run evals automatically after each commit
        self.auto_rollback = auto_rollback  # Whether to auto-revert on eval regression
        self.eval_scripts = eval_scripts or []  # Custom eval scripts to run
        # eval_on_task: "none", "smoke", "full", or path to custom suite
        self.eval_on_task = eval_on_task
        self.skip_eval = skip_eval  # Override to skip eval gate for specific runs
        self.baseline_eval: dict | None = (
            None  # Baseline eval for eval_on_commit/eval_on_task comparison
        )
        self.last_rollback_context: dict | None = None  # Context from last rollback for next cycle
        self.review_designs = review_designs  # Whether to review designs before implementation
        # Category scoring configuration
        default_category_weights = cast(dict[str, float], DEFAULT_CONFIG["category_weights"])
        default_category_thresholds = cast(dict[str, int], DEFAULT_CONFIG["category_thresholds"])
        default_task_constraints = cast(dict[str, Any], DEFAULT_CONFIG["task_constraints"])
        default_risk_settings = cast(dict[str, dict[str, Any]], DEFAULT_CONFIG["risk_settings"])
        self.category_weights = category_weights or default_category_weights.copy()
        self.category_thresholds = category_thresholds or default_category_thresholds.copy()
        # Task atomizer constraints for run_plan()
        self.task_constraints = (
            copy.deepcopy(task_constraints)
            if task_constraints is not None
            else copy.deepcopy(default_task_constraints)
        )
        # Risk level settings for verification requirements
        self.risk_settings = (
            copy.deepcopy(risk_settings)
            if risk_settings is not None
            else copy.deepcopy(default_risk_settings)
        )
        # Current task's risk level (set when task is parsed)
        self.current_task_risk: str | None = None
        # Current task's group name (from ## Group: <name> sections)
        self.current_task_group: str | None = None
        # Approval gates for cycle mode (pause for human review at each phase)
        self.approve_opportunities = approve_opportunities  # Pause after analyze
        self.approve_designs = approve_designs  # Pause after design
        self.approve_plans = approve_plans  # Pause after plan
        # Progress tracking
        self.current_task_num: int = 0  # Current task number (1-indexed)
        self.total_tasks: int = max_tasks  # Total tasks to process
        self.current_task_title: str = ""  # Title of current task for progress output
        # LoC baseline for per-task tracking (reset after each commit)
        self.loc_baseline_ref: str | None = None  # Commit hash to diff against
        # Diagnostics for commit failures
        self.last_commit_failure: dict | None = None
        # Skip mechanical checks for first task when continuing
        self._skip_mechanical_checks: bool = False
        self._tasklist_baseline: str | None = None
        # Per-task cost tracking
        self._task_start_time: datetime | None = None
        self._task_tokens_in: int = 0
        self._task_tokens_out: int = 0
        # Per-task review metrics tracking
        self._task_review_cycles: int = 0  # Count of REQUEST_CHANGES before APPROVED
        self._task_review_duration_ms: int = 0  # Total time spent in review calls
        self._task_findings_count: int = 0  # Total findings across all reviews
        self._task_findings_by_severity: dict[str, int] = {
            "critical": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
            "nit": 0,
        }  # Aggregated severity counts
        self._current_task_text: str = ""  # Current task text for review metrics
        self._task_previous_diff: str | None = (
            None  # Diff before REQUEST_CHANGES for false positive detection
        )
        # Quiet mode suppresses startup banner (for utility commands like --eval)
        self.quiet = quiet
        # Minimum response length to trigger retry logic
        self.min_response_length = min_response_length
        # Log verbosity: "minimal" (events only), "normal" (events + summaries), "verbose" (full output)
        if log_verbosity not in ("minimal", "normal", "verbose"):
            raise ValueError(
                f"Invalid log_verbosity '{log_verbosity}'. Must be 'minimal', 'normal', or 'verbose'."
            )
        self.log_verbosity = log_verbosity
        # Diff logging mode: "full" (complete diffs), "summary" (stats + truncated), "none" (suppress diffs)
        if log_diff_mode not in ("full", "summary", "none"):
            raise ValueError(
                f"Invalid log_diff_mode '{log_diff_mode}'. Must be 'full', 'summary', or 'none'."
            )
        self.log_diff_mode = log_diff_mode
        self.profile = ProfileRegistry().get(profile)
        self._capability_gate = CapabilityPolicyGate(self.profile.capability_tier)
        self._loop_adapter: LoopRegistryAdapter | None
        if self.profile.loop_id is not None:
            self._loop_adapter = LoopRegistryAdapter()
            loop_tier = self._loop_adapter.get_capability_tier(self.profile.loop_id)
            if loop_tier is not None:
                self._capability_gate.assert_permitted(CapabilityTier(loop_tier))
        else:
            self._loop_adapter = None
        self._effect_gate = EffectPolicyGate(
            capability_gate=self._capability_gate,
            permitted_effect_classes=self.profile.permitted_effect_classes,
            provider=NoOpEffectProvider(),
        )
        self._current_task_id: str | None = None

        # CLI provider configuration - use "claude", "codex", "gemini", or "opencode"
        # Can be set globally or per-role (builder, reviewer, sanity, analyzer, etc.)
        self._cli_default = cli
        self._cli_builder = cli_builder or cli
        self._cli_reviewer = cli_reviewer or cli
        self._cli_sanity = cli_sanity or cli
        self._cli_analyzer = cli_analyzer or cli
        self._cli_release_eng = cli_release_eng or cli
        self._cli_sre = cli_sre or cli
        # Cache for instantiated providers (lazy-loaded)
        self._providers: dict[str, CLIProvider] = {}

        # Paths - prompts dir can be custom (from config) or default (built-in)
        self.script_dir = Path(__file__).parent
        self._custom_prompts_dir: Path | None = None
        if prompts_dir:
            # Custom prompts directory (relative to repo, or absolute)
            self._custom_prompts_dir = Path(prompts_dir)
            if not self._custom_prompts_dir.is_absolute():
                # Resolve relative to repo directory
                repo_path = Path(repo_dir) if repo_dir else Path.cwd()
                self._custom_prompts_dir = repo_path / self._custom_prompts_dir

        # Work directory is in the target repo
        self.repo_dir = Path(repo_dir) if repo_dir else Path.cwd()
        self.work_dir = self.repo_dir / WORK_DIR_NAME
        self._evidence_store = EvidenceStore(self.work_dir)

        # Parallel/worktree mode settings (used by ParallelOrchestrator)
        self.parallel_enabled = parallel_enabled
        self.parallel_concurrency = parallel_concurrency
        self.parallel_merge_strategy = merge_strategy or parallel_merge_strategy
        self.parallel_integration_branch = integration_branch or parallel_integration_branch
        self.parallel_worktree_root = worktree_root or parallel_worktree_root
        self.parallel_cleanup = worktree_cleanup or parallel_cleanup
        self.parallel_lock_git = parallel_lock_git
        self.parallel_lock_state = parallel_lock_state
        self.parallel_lock_tasklist = parallel_lock_tasklist
        self.parallel_heartbeat_interval = parallel_heartbeat_interval
        self.parallel_heartbeat_ttl = parallel_heartbeat_ttl
        self.base_branch = base_branch
        self.base_ref = base_ref
        self.shared_state_dir = shared_state_dir
        self.merge_max_retries = merge_max_retries
        self.no_tasklist_edits = no_tasklist_edits
        self.high_risk_concurrency = high_risk_concurrency

        # Worker-mode guard: if a shared state dir is configured, this process is
        # a worktree worker and must never enter the control plane.
        if self.shared_state_dir:
            self.parallel_enabled = False

        # Initialize tasklist manager (delegates task parsing and management)
        self._tasklist_manager = TasklistManager(
            repo_dir=self.repo_dir,
            tasklist=self.tasklist,
            compact_threshold=self.compact_threshold,
        )

        # Initialize context manager (delegates cross-task context sharing)
        self._context_manager = ContextManager(work_dir=self.work_dir)

        # Load main configuration
        self.config = load_config(self.repo_dir)

        # Retry on empty response: use argument if provided, else config, else default True
        if retry_on_empty_response is not None:
            self.retry_on_empty_response = retry_on_empty_response
        else:
            self.retry_on_empty_response = self.config.get("retry_on_empty_response", True)

        # Load project configuration (for tests, lint, typing commands)
        self.project_config = load_project_config(self.repo_dir)

        # Load policy configuration (for safety checks and limits)
        self.policy = load_policy(self.repo_dir)

        # Initialize eval manager (delegates evaluation and metrics)
        self._eval_manager = EvalManager(
            work_dir=self.work_dir,
            repo_dir=self.repo_dir,
            project_config=self.project_config,
            policy=self.policy,
            category_weights=self.category_weights,
            category_thresholds=self.category_thresholds,
            eval_scripts=self.eval_scripts,
        )

        # Initialize outer loop manager (delegates analyze/design/plan/cycle)
        self._outer_loop_manager = OuterLoopManager(
            work_dir=self.work_dir,
            repo_dir=self.repo_dir,
            tasklist=self.tasklist,
            roadmap=self.roadmap,
            task_constraints=self.task_constraints,
            approve_opportunities=self.approve_opportunities,
            approve_designs=self.approve_designs,
            approve_plans=self.approve_plans,
            review_designs=self.review_designs,
            max_cycles=self.max_cycles,
            parse_task_metadata_callback=self._tasklist_manager._parse_task_metadata,
            provider_config=self.config,
            effect_gate=self._effect_gate,
            commit_opportunities=self.config.get("commit_opportunities", False),
            commit_designs=self.config.get("commit_designs", False),
        )

        loop_sensitive_patterns: list[str] = []
        if self._loop_adapter is not None and self.profile.loop_id is not None:
            for check in self._loop_adapter.get_checks(self.profile.loop_id):
                if check.id == "loc_threshold" and self.loc_threshold == DEFAULT_LOC_THRESHOLD:
                    threshold = check.get_threshold_value()
                    if isinstance(threshold, int):
                        self.loc_threshold = threshold
                elif check.id == "sensitive_files" and check.patterns is not None:
                    loop_sensitive_patterns = list(check.patterns)

        # Initialize inner loop manager (delegates build-review-commit core)
        self._inner_loop_manager = InnerLoopManager(
            work_dir=self.work_dir,
            repo_dir=self.repo_dir,
            loc_threshold=self.loc_threshold,
            policy=self.policy,
            project_config=self.project_config,
            loop_sensitive_patterns=loop_sensitive_patterns,
        )

        # Setup work directory and logging
        self._setup_work_dir()
        self._setup_logging()

        # Print startup banner (unless quiet mode for utility commands)
        if not self.quiet:
            print("=== Orchestrator Started ===")
            print(f"Repo: {self.repo_dir}")
            print(f"Work dir: {self.work_dir}")
            print(f"Log file: {self.log_file}")
            print(f"Max cycles per task: {self.max_cycles}")
            print(f"LoC threshold: {self.loc_threshold}")
            print(f"Profile: {self.profile.id} (tier: {self.profile.capability_tier.value})")
            if self.profile.permitted_effect_classes:
                print(
                    "Permitted effects: "
                    + ", ".join(
                        sorted(
                            effect_class.value
                            for effect_class in self.profile.permitted_effect_classes
                        )
                    )
                )
            if self.task:
                print(f"Task: {self.task}")
            else:
                if self.roadmap:
                    print(f"Roadmap: {self.roadmap}")
                from millstone.artifact_providers.mcp import MCPTasklistProvider

                tl_provider = self._outer_loop_manager.tasklist_provider
                if isinstance(tl_provider, MCPTasklistProvider):
                    label_str = ", ".join(tl_provider._labels) if tl_provider._labels else "none"
                    print(f"Tasklist: {tl_provider._mcp_server} (labels: {label_str})")
                else:
                    print(f"Tasklist: {self.tasklist}")
                print(f"Max tasks: {self.max_tasks}")
                if self.compact_threshold > 0:
                    print(f"Compact threshold: {self.compact_threshold}")
                else:
                    print("Compact threshold: disabled")
            if self.dry_run:
                print("Mode: DRY RUN (no agent invocations)")
            if self.session_mode != "new_each_task":
                print(f"Session mode: {self.session_mode}")
            # Show CLI configuration
            if (
                self._cli_builder
                == self._cli_reviewer
                == self._cli_sanity
                == self._cli_analyzer
                == self._cli_release_eng
                == self._cli_sre
            ):
                print(f"CLI: {self._cli_default}")
            else:
                print(f"CLI (default): {self._cli_default}")
                if self._cli_builder != self._cli_default:
                    print(f"CLI (builder): {self._cli_builder}")
                if self._cli_reviewer != self._cli_default:
                    print(f"CLI (reviewer): {self._cli_reviewer}")
                if self._cli_sanity != self._cli_default:
                    print(f"CLI (sanity): {self._cli_sanity}")
                if self._cli_analyzer != self._cli_default:
                    print(f"CLI (analyzer): {self._cli_analyzer}")
                if self._cli_release_eng != self._cli_default:
                    print(f"CLI (release_eng): {self._cli_release_eng}")
                if self._cli_sre != self._cli_default:
                    print(f"CLI (sre): {self._cli_sre}")
            # Show detected project type
            project_lang = self.project_config.get("project", {}).get("language", "unknown")
            print(f"Project type: {project_lang}")
            print()

    @property
    def session_id(self) -> str | None:
        """Legacy property for backwards compatibility. Returns builder_session_id."""
        return self.builder_session_id

    @session_id.setter
    def session_id(self, value: str | None) -> None:
        """Legacy property setter. Sets builder_session_id."""
        self.builder_session_id = value

    @property
    def loop_definition(self):
        if self._loop_adapter is None or self.profile.loop_id is None:
            return None
        return self._loop_adapter.get_loop(self.profile.loop_id)

    def has_remaining_tasks(self) -> bool:
        """Check whether there are pending tasks to process.

        When the configured provider is MCP (e.g. GitHub Issues, Linear), the
        local tasklist file is ignored entirely — the provider is always queried
        via the agent callback so that a stale or leftover local file does not
        shadow the remote backend.

        For file-backed tasklists the local file is read directly.
        """
        from millstone.artifact_providers.mcp import MCPTasklistProvider
        from millstone.artifacts.models import TaskStatus

        provider = self._outer_loop_manager.tasklist_provider

        if isinstance(provider, MCPTasklistProvider):
            # MCP provider: always query remote, ignore any local file.
            if provider._agent_callback is None:
                provider.set_agent_callback(lambda p, **k: self.run_agent(p, role="author", **k))
            provider.invalidate_cache()
            tasks = provider.list_tasks()
            return any(t.status in (TaskStatus.todo, TaskStatus.in_progress) for t in tasks)

        # File provider: use the local tasklist file.
        return self._tasklist_manager.has_remaining_tasks()

    def _setup_work_dir(self):
        """Create work directory and ensure it's gitignored."""
        self.work_dir.mkdir(exist_ok=True)

        # Add to .gitignore if not already there
        gitignore = self.repo_dir / ".gitignore"
        ignore_entry = f"/{WORK_DIR_NAME}/"

        if gitignore.exists():
            content = gitignore.read_text()
            if WORK_DIR_NAME not in content:
                with gitignore.open("a") as f:
                    f.write(f"\n# Dev orchestrator working directory\n{ignore_entry}\n")
                print(f"Added {ignore_entry} to .gitignore")
        else:
            gitignore.write_text(f"# Dev orchestrator working directory\n{ignore_entry}\n")
            print(f"Created .gitignore with {ignore_entry}")

    def _setup_logging(self):
        """Set up run logging path (lazy - file created on first log)."""
        runs_dir = self.work_dir / "runs"
        runs_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = runs_dir / f"{timestamp}.log"
        # Don't create file yet - wait until first log() call

    def log(self, event: str, **data):
        """Log an event with optional data to the run log file.

        Args:
            event: Name of the event (e.g., "prompt_sent", "response_received")
            **data: Key-value pairs to log (e.g., agent="builder", prompt="...")

        For 'minimal' and 'normal' verbosity levels, Codex "thinking" blocks
        (text between 'thinking' and 'codex' markers) are filtered from output.

        For 'normal' verbosity level, the 'output' key (agent responses) is
        additionally summarized to first 500 + last 200 chars. Full output is
        stored separately in .millstone/runs/<timestamp>_full/ if truncated.
        """
        timestamp = datetime.now().isoformat()

        # For 'minimal' and 'normal' verbosity, filter reasoning traces from output
        if self.log_verbosity in ("minimal", "normal") and "output" in data:
            output = data["output"]
            if isinstance(output, str):
                filtered = filter_reasoning_traces(output)
                if filtered != output:
                    data = dict(data)  # Don't mutate original
                    data["output"] = filtered

        # For 'normal' verbosity, summarize 'output' field and store full separately
        if self.log_verbosity == "normal" and "output" in data:
            output = data["output"]
            if isinstance(output, str) and len(output) > 700:
                # Store full output in separate file
                full_dir = self.log_file.parent / f"{self.log_file.stem}_full"
                full_dir.mkdir(exist_ok=True)
                # Use timestamp and event to create unique filename
                ts_short = datetime.now().strftime("%H%M%S_%f")
                full_path = full_dir / f"{ts_short}_{event}.txt"
                full_path.write_text(output)

                # Summarize for main log
                data = dict(data)  # Don't mutate original
                data["output"] = summarize_output(output)
                data["full_output_path"] = str(full_path)

        # Handle diff field based on log_diff_mode setting
        if "diff" in data:
            diff_content = data["diff"]
            if isinstance(diff_content, str):
                if self.log_diff_mode == "none":
                    # Suppress diffs entirely
                    data = dict(data) if data is not data else data  # Don't mutate original
                    del data["diff"]
                    data["diff_suppressed"] = True
                elif self.log_diff_mode == "summary":
                    # Store full diff in separate file
                    diff_file = self.log_file.with_suffix(".patch")
                    # Append to patch file if it exists (multiple diffs in one run)
                    with diff_file.open("a") as df:
                        df.write(f"# Diff from event: {event}\n")
                        df.write(f"# Time: {timestamp}\n\n")
                        df.write(diff_content)
                        df.write("\n\n")
                    # Summarize: show stats + truncated preview
                    data = dict(data)  # Don't mutate original
                    data["diff"] = summarize_diff(diff_content)
                    data["full_diff_path"] = str(diff_file)

        # File created on first write (append mode creates if not exists)
        with self.log_file.open("a") as f:
            f.write(f"\n{'=' * 60}\n")
            f.write(f"[{timestamp}] {event}\n")
            f.write(f"{'=' * 60}\n")
            for key, value in data.items():
                f.write(f"\n--- {key} ---\n")
                f.write(f"{value}\n")

    def _setup_cycle_logging(self) -> None:
        """Set up cycle-specific logging for autonomous operation."""
        self._outer_loop_manager._setup_cycle_logging()

    def _cycle_log(self, phase: str, message: str) -> None:
        """Log a cycle-level decision with timestamp."""
        self._outer_loop_manager._cycle_log(phase, message)

    def _cycle_log_complete(self, status: str) -> None:
        """Write the cycle completion footer."""
        self._outer_loop_manager._cycle_log_complete(status)

    def _init_loc_baseline(self) -> None:
        """Initialize the LoC baseline to current HEAD.

        Called at the start of a run to set the baseline for per-task LoC tracking.
        The baseline is updated after each successful commit.
        """
        self.loc_baseline_ref = self.git("rev-parse", "HEAD").strip()

    def _update_loc_baseline(self) -> None:
        """Update the LoC baseline to current HEAD after a successful commit.

        This ensures subsequent tasks are measured against the new baseline,
        not cumulative uncommitted changes.
        """
        self.loc_baseline_ref = self.git("rev-parse", "HEAD").strip()

    def _get_state_file_path(self) -> Path:
        """Return the path to the state file."""
        return self.work_dir / STATE_FILE_NAME

    def save_state(self, halt_reason: str = "") -> None:
        """Save current orchestration state to state.json.

        Called when the orchestrator halts mid-task (e.g., LoC threshold,
        sensitive file detection) to enable resumption with --continue.

        Args:
            halt_reason: Why the orchestrator halted (for user context)
        """
        state = {
            "current_task_num": self.current_task_num,
            "builder_session_id": self.builder_session_id,
            "reviewer_session_id": self.reviewer_session_id,
            # Legacy key for backwards compatibility with older state files
            "session_id": self.builder_session_id,
            "loc_baseline_ref": self.loc_baseline_ref,
            "cycle": self.cycle,
            "halt_reason": halt_reason,
            "timestamp": datetime.now().isoformat(),
        }
        state_file = self._get_state_file_path()
        state_file.write_text(json.dumps(state, indent=2))
        self.log("state_saved", **{k: str(v) for k, v in state.items()})

    def load_state(self) -> dict | None:
        """Load saved orchestration state from state.json.

        Returns:
            State dict if file exists and is valid, None otherwise.
            The returned dict always contains an ``outer_loop`` key (value is
            ``None`` when not present in the file, for backwards compatibility).
        """
        state_file = self._get_state_file_path()
        if not state_file.exists():
            return None
        try:
            state = json.loads(state_file.read_text())
            state.setdefault("outer_loop", None)
            self.log("state_loaded", **{k: str(v) for k, v in state.items()})
            return state
        except (json.JSONDecodeError, KeyError) as e:
            self.log("state_load_failed", error=str(e))
            return None

    def clear_state(self) -> None:
        """Remove saved state file after successful completion or manual clear."""
        state_file = self._get_state_file_path()
        if state_file.exists():
            state_file.unlink()
            self.log("state_cleared")

    def save_outer_loop_checkpoint(self, stage: str, **kwargs: object) -> None:
        """Persist an outer-loop stage checkpoint to state.json.

        Reads any existing state (to preserve inner-loop fields), merges in an
        ``outer_loop`` section, and writes the result back.

        Args:
            stage: The completed outer-loop stage name
                   (``"analyze_complete"``, ``"design_complete"``, or
                   ``"plan_complete"``).
            **kwargs: Extra fields to store alongside the stage (e.g.
                      ``opportunity``, ``design_path``, ``tasks_created``).
        """
        state_file = self._get_state_file_path()
        state: dict = {}
        if state_file.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                state = json.loads(state_file.read_text())

        state["outer_loop"] = {
            "stage": stage,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **kwargs,
        }
        state_file.write_text(json.dumps(state, indent=2))
        self.log(
            "outer_loop_checkpoint_saved", stage=stage, **{k: str(v) for k, v in kwargs.items()}
        )

    def has_saved_state(self) -> bool:
        """Check if there is a saved state file."""
        return self._get_state_file_path().exists()

    def clear_sessions(self) -> bool:
        """Clear stored session IDs from state file.

        Removes builder_session_id and reviewer_session_id from the state file,
        effectively resetting session continuity. The rest of the state (like
        current_task_num) is preserved if present.

        Returns:
            True if sessions were cleared, False if no state file exists.
        """
        state_file = self._get_state_file_path()
        if not state_file.exists():
            return False

        try:
            state = json.loads(state_file.read_text())
            # Check if there are any sessions to clear
            had_sessions = bool(
                state.get("builder_session_id")
                or state.get("reviewer_session_id")
                or state.get("session_id")
            )

            # Clear session IDs
            state["builder_session_id"] = None
            state["reviewer_session_id"] = None
            state["session_id"] = None  # Legacy key

            # Write back updated state
            state_file.write_text(json.dumps(state, indent=2))
            self.log("sessions_cleared", had_sessions=str(had_sessions))

            # Also clear in-memory session IDs
            self.builder_session_id = None
            self.reviewer_session_id = None

            return had_sessions
        except (json.JSONDecodeError, KeyError) as e:
            self.log("sessions_clear_failed", error=str(e))
            return False

    def auto_clear_stale_sessions(self, max_age_hours: int = 24) -> bool:
        """Auto-clear session IDs if the state file is older than max_age_hours.

        This prevents accumulation of stale sessions that may no longer be valid
        with the underlying CLI tools.

        Args:
            max_age_hours: Maximum age in hours before sessions are auto-cleared.
                          Defaults to 24 hours.

        Returns:
            True if stale sessions were cleared, False otherwise.
        """
        state_file = self._get_state_file_path()
        if not state_file.exists():
            return False

        try:
            state = json.loads(state_file.read_text())
            timestamp_str = state.get("timestamp")
            if not timestamp_str:
                return False

            # Parse the timestamp
            state_time = datetime.fromisoformat(timestamp_str)
            age = datetime.now() - state_time
            age_hours = age.total_seconds() / 3600

            if age_hours > max_age_hours and (
                state.get("builder_session_id")
                or state.get("reviewer_session_id")
                or state.get("session_id")
            ):
                self.log(
                    "auto_clearing_stale_sessions",
                    age_hours=f"{age_hours:.1f}",
                    max_age_hours=str(max_age_hours),
                )
                return self.clear_sessions()
            return False
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            self.log("stale_session_check_failed", error=str(e))
            return False

    def write_research_output(self, task_description: str, agent_output: str) -> Path:
        """Write research output to .millstone/research/ directory.

        Creates a timestamped markdown file containing the agent's full response
        and any structured data that can be extracted (sections like FINDINGS,
        RECOMMENDATIONS, AFFECTED_FILES, etc.).

        Args:
            task_description: The research task description (used for slug and header).
            agent_output: The full agent response to save.

        Returns:
            Path to the created research output file.
        """
        # Create research directory if it doesn't exist
        research_dir = self.work_dir / "research"
        research_dir.mkdir(parents=True, exist_ok=True)

        # Generate timestamp and slug for filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Create slug from task description: lowercase, replace spaces/special chars with hyphens
        slug = re.sub(r"[^a-z0-9]+", "-", task_description.lower())
        slug = slug.strip("-")[:50]  # Limit slug length
        if not slug:
            slug = "research"

        filename = f"{timestamp}_{slug}.md"
        output_file = research_dir / filename

        # Extract structured sections from agent output
        extracted_sections = self._extract_research_sections(agent_output)

        # Build markdown content
        content_parts = [
            f"# Research: {task_description}",
            "",
            f"**Timestamp:** {datetime.now().isoformat()}",
            f"**Task:** {task_description}",
            "",
        ]

        # Add extracted structured data if found
        if extracted_sections:
            content_parts.append("## Extracted Data")
            content_parts.append("")
            for section_name, section_content in extracted_sections.items():
                content_parts.append(f"### {section_name}")
                content_parts.append("")
                content_parts.append(section_content)
                content_parts.append("")

        # Add full agent response
        content_parts.append("## Full Agent Response")
        content_parts.append("")
        content_parts.append(agent_output)

        output_file.write_text("\n".join(content_parts))

        self.log(
            "research_output_saved",
            file=str(output_file),
            task=task_description[:200],
            sections_extracted=str(len(extracted_sections)),
        )

        return output_file

    def _extract_research_sections(self, agent_output: str) -> dict[str, str]:
        """Extract structured sections from agent research output.

        Looks for common section headers that research prompts might produce,
        such as FINDINGS, RECOMMENDATIONS, AFFECTED_FILES, SUMMARY, etc.

        Args:
            agent_output: The full agent response.

        Returns:
            Dict mapping section names to their content.
        """
        sections = {}

        # Common section patterns to look for (case-insensitive)
        section_patterns = [
            "FINDINGS",
            "RECOMMENDATIONS",
            "AFFECTED_FILES",
            "SUMMARY",
            "ANALYSIS",
            "CONCLUSIONS",
            "NEXT_STEPS",
            "RISKS",
            "DEPENDENCIES",
        ]

        for pattern in section_patterns:
            # Look for markdown headers or ALL CAPS section markers
            # Pattern 1: ## SECTION_NAME or # SECTION_NAME
            header_match = re.search(
                rf"^#+\s*{pattern}\s*$(.+?)(?=^#+\s|\Z)",
                agent_output,
                re.MULTILINE | re.IGNORECASE | re.DOTALL,
            )
            if header_match:
                content = header_match.group(1).strip()
                if content:
                    sections[pattern.title().replace("_", " ")] = content
                continue

            # Pattern 2: SECTION_NAME: or **SECTION_NAME:**
            inline_match = re.search(
                rf"(?:\*\*)?{pattern}(?:\*\*)?:\s*(.+?)(?=\n(?:\*\*)?[A-Z_]+(?:\*\*)?:|\Z)",
                agent_output,
                re.IGNORECASE | re.DOTALL,
            )
            if inline_match:
                content = inline_match.group(1).strip()
                if content:
                    sections[pattern.title().replace("_", " ")] = content

        return sections

    def preflight_checks(self) -> None:
        """Run pre-flight checks before starting orchestration.

        Verifies:
        1. claude CLI is installed and in PATH
        2. Current directory is a git repo
        3. Tasklist file exists (if not using --task)

        Raises:
            PreflightError: If any check fails
        """
        # Check 1: All configured CLIs are installed and working
        # Get unique set of CLIs configured for different roles
        configured_clis = {
            self._cli_default,
            self._cli_builder,
            self._cli_reviewer,
            self._cli_sanity,
            self._cli_analyzer,
        }
        for cli_name in configured_clis:
            provider = get_provider(cli_name)
            available, message = provider.check_available()
            if not available:
                raise PreflightError(message)

        # Check 2: Current directory is a git repo
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            cwd=self.repo_dir,
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise PreflightError(
                f"Not a git repository: {self.repo_dir}\nInitialize with: git init"
            )

        # Check 3: Tasklist file exists (if not using --task and not using MCP provider)
        if not self.task:
            from millstone.artifact_providers.mcp import MCPTasklistProvider

            tl_provider = self._outer_loop_manager.tasklist_provider
            if not isinstance(tl_provider, MCPTasklistProvider):
                tasklist_path = self.repo_dir / self.tasklist
                if not tasklist_path.exists():
                    raise PreflightError(
                        f"Tasklist file not found: {tasklist_path}\n"
                        "Create it or use --task to specify a task directly."
                    )

    def check_dirty_working_directory(self) -> None:
        """Check for uncommitted changes and warn if present.

        This is a non-blocking warning. Uncommitted changes from prior work
        will be included in the first task's diff and may trigger the LoC
        threshold. The operator may know what they're doing, so we just warn.
        """
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=self.repo_dir,
        )
        if result.returncode != 0:
            return  # Git command failed, skip warning

        status_lines = [line for line in result.stdout.strip().split("\n") if line]
        if not status_lines:
            return  # Working directory is clean

        file_count = len(status_lines)
        print()
        print("⚠️  WARNING: Working directory has uncommitted changes")
        print(f"   {file_count} file(s) modified")
        print("   These will be included in the first task's diff and may trigger LoC threshold.")
        print(
            f"   Consider committing first or running with --loc-threshold={self.loc_threshold * 2}"
        )
        print()

    def check_uncommitted_tasklist(self) -> None:
        """Check if the tasklist file has uncommitted changes and warn.

        This is a non-blocking warning. An uncommitted tasklist means that
        task completion markers (- [ ] → - [x]) from prior runs haven't been
        committed, which can cause confusion about which tasks are actually done.
        """
        if self.task:
            return  # No tasklist in --task mode

        tasklist_path = self.repo_dir / self.tasklist
        if not tasklist_path.exists():
            return  # Tasklist doesn't exist (will be caught by preflight_checks)

        # Check if tasklist has uncommitted changes
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", str(tasklist_path)],
            capture_output=True,
            text=True,
            cwd=self.repo_dir,
        )
        if result.returncode != 0:
            return  # Git command failed, skip warning

        status_output = result.stdout.rstrip("\n")  # Preserve leading spaces
        if not status_output:
            return  # Tasklist is clean

        # Determine if it's modified, staged, or both
        # Git porcelain format: XY filename
        # X = index status (staged), Y = worktree status (unstaged)
        status_code = status_output[:2] if len(status_output) >= 2 else ""
        is_staged = status_code[0] in "MADRCU"
        is_modified = status_code[1] in "MADRCU"

        print()
        print(f"⚠️  WARNING: Tasklist has uncommitted changes: {self.tasklist}")
        if is_staged and is_modified:
            print("   Changes are both staged and unstaged.")
        elif is_staged:
            print("   Changes are staged but not committed.")
        else:
            print("   Changes are not staged.")
        print("   Task completion markers may not reflect actual repository state.")
        print("   Consider committing the tasklist before running.")
        print()

    def cleanup(self):
        """Remove work directory contents (but keep the directory, runs/, evals/, and tasks/)."""
        import shutil

        persistent = {
            # Runtime history
            "runs",
            "evals",
            "tasks",
            "cycles",
            "parallel",
            "locks",
            "worktrees",
            # User-written config — never delete
            "config.toml",
            # Artifact files/dirs that are local-only by default
            "opportunities.md",
            "designs",
            # Pause/resume state
            "state.json",
            # Always preserve the default tasklist regardless of which tasklist this
            # Orchestrator instance was configured with.  Tests that pass a custom
            # tasklist path (e.g. "my/tasks.md") must not delete the real
            # .millstone/tasklist.md that exists on disk.
            Path(DEFAULT_CONFIG["tasklist"]).name,
        }
        # Also preserve a non-default tasklist name when it lives inside work_dir
        if Path(self.tasklist).parts[0] == WORK_DIR_NAME:
            persistent.add(Path(self.tasklist).name)
        if self.work_dir.exists():
            for item in self.work_dir.iterdir():
                if item.name in persistent:
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

    def _get_provider(self, role: str = "default") -> CLIProvider:
        """Get CLI provider for a given role.

        Args:
            role: Canonical roles include "author", "reviewer", "sanity", and "analyzer".
                "builder" is a backward-compatible alias resolved via the active profile.

        Returns:
            CLIProvider instance for the specified role.
        """
        canonical_role = self.profile.resolve_role(role)
        if (
            self._loop_adapter is not None
            and self.profile.loop_id is not None
            and canonical_role not in _ORCHESTRATOR_INTERNAL_ROLES
            and not self._loop_adapter.validate_role_id(self.profile.loop_id, canonical_role)
        ):
            raise ConfigurationError(
                f"Role {canonical_role!r} is not declared by loop {self.profile.loop_id!r}"
            )
        # Map role to CLI name
        cli_name = {
            "default": self._cli_default,
            "author": self._cli_builder,
            "builder": self._cli_builder,
            "reviewer": self._cli_reviewer,
            "sanity": self._cli_sanity,
            "analyzer": self._cli_analyzer,
            "release_eng": self._cli_release_eng,
            "sre": self._cli_sre,
        }.get(canonical_role, self._cli_default)

        # Return cached provider or create new one
        if cli_name not in self._providers:
            self._providers[cli_name] = get_provider(cli_name)
        return self._providers[cli_name]

    def _is_run_claude_patched(self) -> bool:
        """Check if run_claude method is patched/mocked for testing.

        This is used solely to maintain backward compatibility with legacy tests
        that mock run_claude directly.
        """
        # Check instance-level patch (most common in tests)
        if "run_claude" in self.__dict__:
            return True
        # Check if it's a Mock object (via mock_calls or side_effect)
        if hasattr(self.run_claude, "mock_calls") or hasattr(self.run_claude, "side_effect"):
            return True
        # Check if function code is different (class-level patch)
        return bool(
            hasattr(self.run_claude, "__func__")
            and self.run_claude.__func__ is not Orchestrator.run_claude
        )

    def _stage_untracked_files(self) -> None:
        """Ensure untracked files are included in diffs (intent-to-add).

        Runs 'git add -N -- <file>' for all untracked files that are not ignored.
        This allows 'git diff HEAD' to see them as new files.
        """
        try:
            untracked = self.git("ls-files", "--others", "--exclude-standard").strip()
            if untracked:
                for file_path in untracked.split("\n"):
                    if file_path.strip():
                        subprocess.run(
                            ["git", "add", "-N", "--", file_path.strip()],
                            cwd=self.repo_dir,
                            capture_output=True,
                        )
        except Exception as e:
            self.log("git_add_intent_failed", error=str(e))

    def run_agent(
        self,
        prompt: str,
        *,
        resume: str | None = None,
        model: str | None = None,
        role: str = "default",
        output_schema: str | None = None,
    ) -> str:
        """Run an agent with a prompt and return its output."""
        provider = self._get_provider(role)
        canonical_role = self.profile.resolve_role(role)

        # Determine if we should use specialized run_claude method
        # (which supports local execution and mocking in tests)
        cli_name = {
            "default": self._cli_default,
            "author": self._cli_builder,
            "builder": self._cli_builder,
            "reviewer": self._cli_reviewer,
            "sanity": self._cli_sanity,
            "analyzer": self._cli_analyzer,
            "release_eng": self._cli_release_eng,
            "sre": self._cli_sre,
        }.get(canonical_role, self._cli_default)

        def execute_run(p: str, r: str | None) -> CLIResult:
            kwargs = {}
            if r is not None:
                kwargs["resume"] = r

            if cli_name == "claude":
                # Use the mockable run_claude_result method
                return self.run_claude_result(
                    p,
                    model=model,
                    output_schema=output_schema,
                    schema_work_dir=str(self.work_dir) if output_schema else None,
                    **kwargs,
                )
            else:
                return provider.run(
                    p,
                    model=model,
                    cwd=str(self.repo_dir),
                    output_schema=output_schema,
                    schema_work_dir=str(self.work_dir) if output_schema else None,
                    **kwargs,
                )

        result = execute_run(prompt, resume)

        # Log response received
        self.log(
            "response_received",
            cli=cli_name,
            output=result.output,
            returncode=str(result.returncode),
            stderr=result.stderr if result.stderr and result.stderr != result.output else None,
        )

        # Check for CLI errors
        if result.returncode != 0 and not result.output.strip():
            error_msg = (
                result.stderr.strip()
                if result.stderr
                else f"CLI exited with code {result.returncode}"
            )
            self.log("cli_error", cli=cli_name, returncode=str(result.returncode), error=error_msg)
            result = CLIResult(
                output=f"CLI ERROR (exit {result.returncode}): {error_msg}",
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

        # Unwrapping and token tracking
        final_output = extract_claude_result(result.stdout)

        # Track tokens for non-Claude providers (Claude tracking is handled in run_claude)
        if cli_name != "claude":
            self._task_tokens_in += len(prompt) // 4
            self._task_tokens_out += len(final_output) // 4

        # Check if response is empty and retry once if so
        should_retry = False
        if self.retry_on_empty_response:
            should_retry = is_empty_response(
                final_output, expected_schema=output_schema, min_length=self.min_response_length
            )

        if (
            should_retry
            and canonical_role == "author"
            and self._tasklist_baseline is not None
            and not self.task
        ):
            tasklist_path = self.repo_dir / self.tasklist
            if tasklist_path.exists() and tasklist_path.read_text() != self._tasklist_baseline:
                should_retry = False

        if should_retry:
            self.log("empty_response_retry", role=role, output_schema=output_schema)

            # Account for retry tokens (only for non-Claude, as run_claude handles it)
            if cli_name != "claude":
                self._task_tokens_in += len(prompt) // 4

            retry_result = execute_run(prompt, resume)
            final_output = extract_claude_result(retry_result.stdout)

            if cli_name != "claude":
                self._task_tokens_out += len(final_output) // 4

            if is_empty_response(
                final_output, expected_schema=output_schema, min_length=self.min_response_length
            ):
                self.log("empty_response_fallback", role=role, output_schema=output_schema)
                if output_schema == "sanity_check":
                    return '{"status": "HALT", "reason": "Agent returned empty response"}'
                if output_schema == "review_decision":
                    return '{"status": "REQUEST_CHANGES", "review": "Reviewer returned empty response", "summary": "Empty review response", "findings": ["Reviewer returned empty response"], "findings_by_severity": {"critical": [], "high": ["Reviewer returned empty response"], "medium": [], "low": [], "nit": []}}'

        return final_output

    def run_claude_result(
        self,
        prompt: str,
        resume: str | None = None,
        model: str | None = None,
        output_schema: str | None = None,
        schema_work_dir: str | None = None,
    ) -> CLIResult:
        """Run Claude CLI agent and return full result object.

        This method handles the dual requirement of supporting full CLI results
        (error codes, stderr) in production, while supporting legacy string-only
        mocks in tests.
        """
        # Check if run_claude has been patched/mocked
        is_patched = self._is_run_claude_patched()

        if not is_patched:
            # Production path: Use provider directly to get real return codes and errors
            from millstone.agent_providers import get_provider

            provider = get_provider("claude")
            result = provider.run(
                prompt,
                resume=resume,
                model=model,
                cwd=str(self.repo_dir),
                output_schema=output_schema,
                schema_work_dir=schema_work_dir,
            )
            # Track tokens for production path
            self._task_tokens_in += len(prompt) // 4
            self._task_tokens_out += len(result.output) // 4
            return result

        # Test/Mock path: Call the patched method which returns a string
        # Construct kwargs to avoid passing None values to fragile mocks
        kwargs = {}
        if resume is not None:
            kwargs["resume"] = resume
        if model is not None:
            kwargs["model"] = model
        if output_schema is not None:
            kwargs["output_schema"] = output_schema
        if schema_work_dir is not None:
            kwargs["schema_work_dir"] = schema_work_dir

        output = self.run_claude(prompt, **kwargs)

        # Track tokens for mock path (using string length)
        # Note: If mock returned CLIResult, we use its output length
        out_str = output.output if isinstance(output, CLIResult) else str(output)
        self._task_tokens_in += len(prompt) // 4
        self._task_tokens_out += len(out_str) // 4

        # If the mock returned a CLIResult object (some do), pass it through
        if isinstance(output, CLIResult):
            return output

        # Wrap string output in a success result
        return CLIResult(output=str(output), returncode=0, stdout=str(output), stderr="")

    def run_claude(
        self,
        prompt: str,
        resume: str | None = None,
        model: str | None = None,
        output_schema: str | None = None,
        schema_work_dir: str | None = None,
    ) -> str:
        """Run Claude CLI agent and return output string.

        This method is kept for backwards compatibility and to allow tests to
        patch agent execution with a simple string return.
        """
        # Instantiate/get the Claude provider directly to avoid recursion
        from millstone.agent_providers import get_provider

        provider = get_provider("claude")

        result = provider.run(
            prompt,
            resume=resume,
            model=model,
            cwd=str(self.repo_dir),
            output_schema=output_schema,
            schema_work_dir=schema_work_dir,
        )

        # Track token estimates (roughly 4 chars per token)
        self._task_tokens_in += len(prompt) // 4
        self._task_tokens_out += len(result.output) // 4

        return result.output

    def git(self, *args, check: bool = False) -> str:
        """Run git command and return output."""
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=check,
            cwd=self.repo_dir,
        )
        return result.stdout

    def delegate_commit(self) -> bool:
        """Ask the builder agent to commit its changes.

        The builder has full context of what it implemented and can write
        better commit messages than the orchestrator parsing task text.
        Uses the existing session if available, otherwise starts fresh.

        If the builder commits code but leaves the tasklist unstaged
        (a common oversight), we auto-commit the tasklist tick separately.

        Returns:
            True if commit succeeded (no uncommitted changes remain),
            False if commit failed (changes still present).
        """
        success, failure_info = self._inner_loop_manager.delegate_commit(
            tasklist=self.tasklist,
            session_id=self.session_id,
            load_prompt_callback=self.load_prompt,
            run_agent_callback=self.run_agent,
            log_callback=self.log,
            update_loc_baseline_callback=self._update_loc_baseline,
            task_prefix=self._task_prefix(),
            git_callback=self.git,
        )
        if not success and failure_info:
            self.last_commit_failure = failure_info
        return success

    def _auto_commit_tasklist_if_needed(self) -> bool:
        """Auto-commit the tasklist checkbox if the builder left it unstaged.

        When the builder commits code early but forgets to include the tasklist
        tick, this ensures the task is still marked complete so it won't be
        re-selected on the next run. Only applies to file-backed tasklists.

        Returns True if no action was needed or the commit succeeded.
        Returns False if the commit failed (task not fully completed).
        """
        import subprocess

        from millstone.artifact_providers.mcp import MCPTasklistProvider

        provider = self._outer_loop_manager.tasklist_provider
        if isinstance(provider, MCPTasklistProvider):
            return True  # MCP tasks are marked done via API, not file commits

        status = self.git("status", "--porcelain").strip()
        if not status:
            return True

        remaining_files = [line.split()[-1] for line in status.split("\n") if line.strip()]
        tasklist_path = str(self.tasklist)

        if len(remaining_files) == 1 and remaining_files[0] == tasklist_path:
            self.log(
                "auto_commit_tasklist",
                reason="builder_early_commit_forgot_tasklist",
                file=tasklist_path,
            )
            add_result = subprocess.run(
                ["git", "add", tasklist_path],
                cwd=self.repo_dir,
                capture_output=True,
            )
            if add_result.returncode != 0:
                progress(
                    f"{self._task_prefix()} ERROR: git add for tasklist failed "
                    f"(rc={add_result.returncode})"
                )
                return False
            commit_result = subprocess.run(
                [
                    "git",
                    "commit",
                    "-m",
                    "Mark task complete in tasklist\n\nGenerated with millstone orchestrator",
                ],
                cwd=self.repo_dir,
                capture_output=True,
            )
            if commit_result.returncode != 0:
                progress(
                    f"{self._task_prefix()} ERROR: git commit for tasklist failed "
                    f"(rc={commit_result.returncode})"
                )
                return False
            progress(
                f"{self._task_prefix()} Auto-committed tasklist tick "
                "(builder committed code but forgot to stage tasklist)"
            )

        return True

    def load_prompt(self, name: str) -> str:
        """Load a prompt file from custom dir or package resources."""
        if self._custom_prompts_dir:
            content = (self._custom_prompts_dir / name).read_text()
        else:
            # Default: load from package resources (works with pip/pipx install)
            content = files("millstone.prompts").joinpath(name).read_text()

        # Append hidden tag for debugging and stable testing
        return f"{content}\n\n<!-- prompt_name: {name} -->"

    def _apply_provider_placeholders(self, prompt: str, placeholders: dict[str, str]) -> str:
        """Replace provider-specific placeholder tokens in a prompt.

        Only tokens present in *placeholders* are touched. Raises ``ValueError``
        if a provider key appears in the template but its resolved value is the
        empty string.
        """
        for key, value in placeholders.items():
            token = f"{{{{{key}}}}}"
            if token in prompt:
                if not value:
                    raise ValueError(f"Provider placeholder {token} resolved to empty string")
                prompt = prompt.replace(token, value)
        return prompt

    def get_task_prompt(self) -> str:
        """Generate prompt for a direct task (when --task is used)."""
        prompt = self.load_prompt("task_prompt.md").replace("{{TASK}}", self.task or "")
        prompt = self._apply_provider_placeholders(
            prompt,
            self._outer_loop_manager.tasklist_provider.get_prompt_placeholders(),
        )
        if self.shared_state_dir:
            prompt += (
                "\n\n---\n\n"
                "## Worktree Worker Notes\n\n"
                "- This run is a worktree worker (`--shared-state-dir` is set).\n"
                "- You MAY update tasklist task text/metadata for coherence if your implementation requires it.\n"
                "- Do NOT mark task checkboxes complete. The control plane handles completion marking.\n"
            )
        if self.no_tasklist_edits:
            prompt += (
                "\n\n---\n\n"
                "## Hard Constraint\n\n"
                f"- `--no-tasklist-edits` is active. Do NOT edit `{self.tasklist}`.\n"
                "- Do NOT check off tasks or rewrite task text.\n"
                "- The worktree control plane owns tasklist updates.\n"
            )
        return prompt

    def _task_prefix(self) -> str:
        """Return the task prefix for progress messages, e.g., '[Task 2/5]'."""
        return f"[Task {self.current_task_num}/{self.total_tasks}]"

    def get_task_context_file_content(self) -> str | None:
        # Delegates to TasklistManager
        return self._tasklist_manager.get_task_context_file_content(log_callback=self.log)

    def get_group_context(self, group_name: str | None = None) -> str | None:
        # Delegates to ContextManager
        return self._context_manager.get_group_context(
            group_name=group_name,
            current_task_group=self.current_task_group,
        )

    def accumulate_group_context(
        self,
        task_text: str,
        group_name: str | None = None,
        git_diff: str | None = None,
    ) -> bool:
        # Delegates to ContextManager
        return self._context_manager.accumulate_group_context(
            task_text=task_text,
            group_name=group_name,
            git_diff=git_diff,
            current_task_group=self.current_task_group,
            log_callback=self.log,
            extract_context_callback=self.extract_context_summary,
        )

    def extract_context_summary(
        self,
        task_text: str,
        git_diff: str,
    ) -> dict | None:
        # Delegates to ContextManager
        return self._context_manager.extract_context_summary(
            task_text=task_text,
            git_diff=git_diff,
            load_prompt_callback=self.load_prompt,
            run_agent_callback=self.run_agent,
            log_callback=self.log,
        )

    def apply_risk_settings(self, risk_level: str | None) -> None:
        """Apply risk-based settings for the current task.

        Adjusts max_cycles and stores the risk level for later use in
        approval gates and verification requirements.

        Args:
            risk_level: The risk level ('low', 'medium', 'high') or None for default.
        """
        self.current_task_risk = risk_level

        if risk_level and risk_level in self.risk_settings:
            settings = self.risk_settings[risk_level]
            # Adjust max_cycles based on risk
            self.max_cycles = settings.get("max_cycles", self.base_max_cycles)
        else:
            # Use base max_cycles if no risk level or unknown
            self.max_cycles = self.base_max_cycles

    def requires_high_risk_approval(self) -> bool:
        """Check if current task requires approval due to high risk.

        Returns:
            True if task is high-risk and require_approval is set.
        """
        if self.current_task_risk != "high":
            return False
        settings = self.risk_settings.get("high", {})
        return settings.get("require_approval", False)

    def mark_task_complete(self) -> bool:
        # Delegates to TasklistManager
        return self._tasklist_manager.mark_task_complete(log_callback=self.log)

    def should_compact(self) -> bool:
        # Delegates to TasklistManager (syncs completed_task_count first)
        self._tasklist_manager.completed_task_count = self.completed_task_count
        return self._tasklist_manager.should_compact()

    def verify_compaction(
        self,
        original_content: str,
        new_content: str,
        original_unchecked: list[str],
    ) -> tuple[bool, str]:
        # Delegates to TasklistManager
        return self._tasklist_manager.verify_compaction(
            original_content, new_content, original_unchecked
        )

    def run_compaction(self) -> bool:
        # Delegates to TasklistManager (syncs completed_task_count before and after)
        self._tasklist_manager.completed_task_count = self.completed_task_count
        result = self._tasklist_manager.run_compaction(
            run_agent_callback=lambda p, schema_work_dir=None, **k: self.run_agent(
                p, role="author", **k
            ),
            get_prompt_callback=self.get_compact_prompt,
            log_callback=self.log,
        )
        self.completed_task_count = self._tasklist_manager.completed_task_count
        return result

    def run_eval(self, coverage: bool = False, mode: str | None = None) -> dict:
        def _emit_eval_evidence(eval_result: dict) -> None:
            record = make_eval_evidence(
                eval_result=eval_result,
                work_item_id=self._current_task_id,
                capability_tier=self.profile.capability_tier.value,
            )
            self._evidence_store.emit(record)

        return self._eval_manager.run_eval(
            coverage=coverage,
            mode=mode,
            log_callback=self.log,
            run_custom_eval_scripts_callback=self._run_custom_eval_scripts,
            emit_evidence_callback=_emit_eval_evidence,
        )

    def compare_evals(self) -> dict:
        # Delegates to EvalManager
        return self._eval_manager.compare_evals(log_callback=self.log)

    def _run_typing(self, cmd: str = "") -> dict:
        # Delegates to EvalManager (syncs thresholds first)
        self._eval_manager.category_thresholds = self.category_thresholds
        return self._eval_manager._run_typing(cmd)

    def _run_lint(self, cmd: str = "") -> dict:
        # Delegates to EvalManager (syncs thresholds first)
        self._eval_manager.category_thresholds = self.category_thresholds
        return self._eval_manager._run_lint(cmd)

    def _run_bandit(self) -> dict:
        # Delegates to EvalManager (syncs thresholds first)
        self._eval_manager.category_thresholds = self.category_thresholds
        return self._eval_manager._run_bandit()

    def _run_radon(self) -> dict:
        # Delegates to EvalManager (syncs thresholds first)
        self._eval_manager.category_thresholds = self.category_thresholds
        return self._eval_manager._run_radon()

    def _compute_composite_score(self, categories: dict) -> float:
        # Delegates to EvalManager (syncs weights first)
        self._eval_manager.category_weights = self.category_weights
        return self._eval_manager._compute_composite_score(categories)

    def _print_eval_trend_warnings(
        self, previous_eval: dict, current_eval: dict, delta: dict
    ) -> bool:
        # Delegates to EvalManager
        return self._eval_manager._print_eval_trend_warnings(
            previous_eval, current_eval, delta, log_callback=self.log
        )

    def _update_eval_summary(self, evals_dir: Path, timestamp_str: str, eval_result: dict) -> None:
        # Delegates to EvalManager
        return self._eval_manager._update_eval_summary(evals_dir, timestamp_str, eval_result)

    def save_task_metrics(
        self,
        task_text: str,
        outcome: str,
        cycles_used: int,
        eval_before: dict | None = None,
        eval_after: dict | None = None,
    ) -> Path:
        result = self._eval_manager.save_task_metrics(
            task_text=task_text,
            outcome=outcome,
            cycles_used=cycles_used,
            task_start_time=self._task_start_time,
            task_tokens_in=self._task_tokens_in,
            task_tokens_out=self._task_tokens_out,
            task_review_cycles=self._task_review_cycles,
            task_review_duration_ms=self._task_review_duration_ms,
            task_findings_count=self._task_findings_count,
            task_findings_by_severity=self._task_findings_by_severity,
            current_task_group=self.current_task_group,
            eval_before=eval_before,
            eval_after=eval_after,
            log_callback=self.log,
        )
        # Only emit review evidence for reviewer verdicts. Operational failure reasons
        # (eval_gate_failed, commit_failed, etc.) are not review verdicts and must not
        # be recorded as EvidenceKind.review to preserve the outcome contract.
        if outcome == "approved":
            record = make_review_evidence(
                task_text=task_text,
                outcome=outcome,
                cycles=cycles_used,
                findings_count=self._task_findings_count,
                findings_by_severity=dict(self._task_findings_by_severity),
                duration_ms=self._task_review_duration_ms,
                capability_tier=self.profile.capability_tier.value,
                work_item_id=self._current_task_id,
            )
            self._evidence_store.emit(record)
        return result

    def append_review_metric(
        self,
        task_text: str,
        verdict: str,
        findings: list[str] | None,
        findings_by_severity: dict[str, list[str]] | None,
        duration_ms: int,
        false_positive_indicator: bool = False,
    ) -> None:
        # Delegates to EvalManager
        return self._eval_manager.append_review_metric(
            task_text=task_text,
            verdict=verdict,
            findings=findings,
            findings_by_severity=findings_by_severity,
            duration_ms=duration_ms,
            cli_reviewer=self._cli_reviewer,
            false_positive_indicator=false_positive_indicator,
            log_callback=self.log,
        )

    def get_duration_by_complexity(self, limit: int = 100) -> dict:
        # Delegates to EvalManager
        return self._eval_manager.get_duration_by_complexity(
            limit=limit,
            parse_task_metadata_callback=self._tasklist_manager._parse_task_metadata,
        )

    def estimate_remaining_time(self, pending_tasks: list[dict]) -> dict:
        # Delegates to EvalManager
        return self._eval_manager.estimate_remaining_time(
            pending_tasks,
            parse_task_metadata_callback=self._tasklist_manager._parse_task_metadata,
        )

    def analyze_tasklist(self) -> dict:
        from millstone.artifact_providers.mcp import MCPTasklistProvider

        provider = self._outer_loop_manager.tasklist_provider
        if isinstance(provider, MCPTasklistProvider):
            label_str = ", ".join(provider._labels) if provider._labels else "none"
            print(f"Remote tasklist provider: {provider._mcp_server}")
            print(f"  Labels: {label_str}")

            # Fetch live task counts from the remote provider
            pending_count = 0
            completed_count = 0
            total_count = 0
            try:
                if provider._agent_callback is None:
                    provider.set_agent_callback(
                        lambda p, **k: self.run_agent(p, role="author", **k)
                    )
                provider.invalidate_cache()
                tasks = provider.list_tasks()
                from millstone.artifacts.models import TaskStatus

                for t in tasks:
                    if t.status == TaskStatus.done:
                        completed_count += 1
                    else:
                        pending_count += 1
                total_count = len(tasks)
                print(f"  Open tasks: {pending_count}")
            except Exception:
                print("  Open tasks: unable to fetch task count")

            print()
            print("Detailed task analysis is not available for remote providers.")
            print("Use --dry-run to inspect prompts that would be sent.")
            return {
                "pending_count": pending_count,
                "completed_count": completed_count,
                "total_count": total_count,
                "tasks": [],
                "dependencies": [],
                "suggested_order": [],
            }
        # Delegates to TasklistManager
        return self._tasklist_manager.analyze_tasklist(
            estimate_time_callback=self.estimate_remaining_time,
            log_callback=self.log,
        )

    def _estimate_complexity(
        self,
        file_refs: list[str],
        keywords: list[tuple[str, str]],
        est_loc: int | None,
        ref_loc: int | None = None,
    ) -> str:
        # Delegates to TasklistManager
        return self._tasklist_manager._estimate_complexity(file_refs, keywords, est_loc, ref_loc)

    def split_task(self, task_number: int) -> dict:
        # Delegates to TasklistManager
        return self._tasklist_manager.split_task(
            task_number=task_number,
            run_agent_callback=lambda p, schema_work_dir=None, **k: self.run_agent(
                p, role="author", **k
            ),
            load_prompt_callback=self.load_prompt,
            log_callback=self.log,
        )

    def _run_eval_on_commit(self, task_text: str = "") -> bool:
        # Delegates to EvalManager (syncs baseline_eval first)
        self._eval_manager.baseline_eval = self.baseline_eval
        cycle_log_callback = self._cycle_log if getattr(self, "cycle_log_file", None) else None
        return self._eval_manager._run_eval_on_commit(
            task_text=task_text,
            task_prefix=self._task_prefix(),
            auto_rollback=self.auto_rollback,
            cycle_log_callback=cycle_log_callback,
            log_callback=self.log,
            run_eval_callback=lambda: self.run_eval(),
        )

    def _run_eval_on_task(self, task_text: str = "") -> bool:
        # Delegates to EvalManager (syncs baseline_eval first)
        self._eval_manager.baseline_eval = self.baseline_eval
        cycle_log_callback = self._cycle_log if getattr(self, "cycle_log_file", None) else None
        return self._eval_manager._run_eval_on_task(
            eval_on_task=self.eval_on_task,
            task_text=task_text,
            task_prefix=self._task_prefix(),
            auto_rollback=self.auto_rollback,
            cycle_log_callback=cycle_log_callback,
            log_callback=self.log,
            run_eval_callback=lambda mode: self.run_eval(mode=mode),
        )

    def _run_eval_gate(self, task_text: str = "") -> tuple[bool, dict | None]:
        # Delegates to EvalManager (syncs baseline_eval first)
        self._eval_manager.baseline_eval = self.baseline_eval
        return self._eval_manager._run_eval_gate(
            eval_on_task=self.eval_on_task,
            skip_eval=self.skip_eval,
            task_text=task_text,
            task_prefix=self._task_prefix(),
            log_callback=self.log,
            run_eval_callback=lambda mode: self.run_eval(mode=mode),
        )

    def _print_category_comparison(self, current_eval: dict) -> None:
        # Delegates to EvalManager (syncs baseline_eval first)
        self._eval_manager.baseline_eval = self.baseline_eval
        return self._eval_manager._print_category_comparison(current_eval)

    def _handle_eval_regression(
        self,
        current_eval: dict,
        task_text: str,
        reason: str,
        details: dict,
    ) -> bool:
        # Delegates to EvalManager
        cycle_log_callback = None
        if getattr(self, "cycle_log_file", None):
            cycle_log_callback = self._cycle_log
        return self._eval_manager._handle_eval_regression(
            current_eval=current_eval,
            task_text=task_text,
            reason=reason,
            details=details,
            auto_rollback=self.auto_rollback,
            cycle_log_callback=cycle_log_callback,
            log_callback=self.log,
        )

    def _perform_rollback(
        self,
        commit_hash: str,
        task_text: str,
        reason: str,
        details: dict,
    ) -> bool:
        # Delegates to EvalManager (syncs rollback context after)
        success = self._eval_manager._perform_rollback(
            commit_hash=commit_hash,
            task_text=task_text,
            reason=reason,
            details=details,
            log_callback=self.log,
        )
        if success and self._eval_manager.last_rollback_context:
            self.last_rollback_context = self._eval_manager.last_rollback_context
        return success

    def _load_rollback_context(self) -> dict | None:
        # Delegates to EvalManager (checks in-memory context first)
        if self.last_rollback_context:
            return self.last_rollback_context
        return self._eval_manager._load_rollback_context()

    def clear_rollback_context(self) -> None:
        # Delegates to EvalManager (clears both in-memory and file-based)
        self.last_rollback_context = None
        self._eval_manager.clear_rollback_context()

    def run_analyze(self, issues_file: str | None = None) -> dict:
        # Delegates to OuterLoopManager
        self._capability_gate.assert_permitted(CapabilityTier.C1_LOCAL_WRITE)
        return self._outer_loop_manager.run_analyze(
            issues_file=issues_file,
            load_prompt_callback=self.load_prompt,
            run_agent_callback=lambda p, schema_work_dir=None, **k: self.run_agent(
                p, role="analyzer", **k
            ),
            reviewer_callback=lambda p, schema_work_dir=None, **k: self.run_agent(
                p, role="reviewer", **k
            ),
            load_rollback_context_callback=self._load_rollback_context,
            log_callback=self.log,
        )

    def run_design(self, opportunity: str, opportunity_id: str | None = None) -> dict:
        # Delegates to OuterLoopManager
        self._capability_gate.assert_permitted(CapabilityTier.C1_LOCAL_WRITE)
        return self._outer_loop_manager.run_design(
            opportunity=opportunity,
            opportunity_id=opportunity_id,
            load_prompt_callback=self.load_prompt,
            run_agent_callback=lambda p, schema_work_dir=None, **k: self.run_agent(
                p, role="author", **k
            ),
            reviewer_callback=lambda p, schema_work_dir=None, **k: self.run_agent(
                p, role="reviewer", **k
            ),
            log_callback=self.log,
        )

    def review_design(self, design_path: str) -> dict:
        result = self._outer_loop_manager.review_design(
            design_path=design_path,
            load_prompt_callback=self.load_prompt,
            run_agent_callback=self.run_agent,
            is_empty_response_callback=is_empty_response,
            parse_design_review_callback=parse_design_review,
            log_callback=self.log,
        )
        verdict = result.get("verdict", "unknown")
        record = make_design_review_evidence(
            design_path=design_path,
            outcome=verdict,
            strengths_count=len(result.get("strengths") or []),
            issues_count=len(result.get("issues") or []),
            capability_tier=self.profile.capability_tier.value,
        )
        self._evidence_store.emit(record)
        return result

    def run_review_diff(self, diff_content: str) -> dict:
        """Perform pre-merge QA review on a diff."""
        progress("Running pre-merge QA review...")
        prompt = self.load_prompt("review_diff_prompt.md")
        prompt = prompt.replace("{{DIFF_CONTENT}}", diff_content)
        prompt = prompt.replace("{{TASKLIST_SUMMARY}}", self.extract_current_task_title())

        output = self.run_agent(prompt, role="reviewer")

        # Robust extraction using regex
        approved = False
        try:
            match = re.search(r"\{[\s\S]*?\}", output)
            if match:
                data = json.loads(match.group(0))
                approved = data.get("verdict") == "APPROVED"
        except (json.JSONDecodeError, AttributeError):
            # Fallback to simple check if JSON fails
            approved = '"verdict": "APPROVED"' in output or '"verdict":"APPROVED"' in output

        return {"approved": approved, "output": output}

    def run_prepare_release(self) -> dict:
        """Prepare a new release based on completed tasks."""
        progress("Preparing release...")
        changelog_path = self.repo_dir / "CHANGELOG.md"
        changelog_content = changelog_path.read_text() if changelog_path.exists() else ""

        prompt = self.load_prompt("release_prompt.md")
        # Build completed-tasks string from the actual tasklist or git log fallback.
        tasklist_path = self.repo_dir / self.tasklist
        if tasklist_path.exists():
            content = tasklist_path.read_text()
            completed_lines = [
                line for line in content.splitlines() if line.strip().startswith("- [x]")
            ]
            completed_tasks_str = "\n".join(completed_lines) or "(no completed tasks)"
        else:
            # MCP provider or missing file: use git log since last tag.
            try:
                last_tag = self.git("describe", "--tags", "--abbrev=0").strip()
                ref = last_tag if last_tag else "HEAD~20"
                log_output = self.git("log", "--oneline", f"{ref}..HEAD")
                completed_tasks_str = log_output.strip() or "(no recent commits)"
            except Exception:
                completed_tasks_str = "(could not determine completed tasks)"
        prompt = prompt.replace("{{COMPLETED_TASKS}}", completed_tasks_str)
        prompt = prompt.replace("{{CHANGELOG_CONTENT}}", changelog_content)

        output = self.run_agent(prompt, role="release_eng")

        # 1. Update Changelog File
        changelog_path.parent.mkdir(parents=True, exist_ok=True)
        changelog_path.write_text(output)

        # 2. Extract Version and Tag
        version_match = re.search(r"\[(\d+\.\d+\.\d+)\]", output)
        tag_name = None
        if version_match:
            version = version_match.group(1)
            tag_name = f"v{version}"
            progress(f"Creating git tag {tag_name}...")
            try:
                self.git("tag", "-a", tag_name, "-m", f"Release {version}", check=True)
            except Exception as e:
                progress(f"Warning: Failed to create tag {tag_name}: {e}")
                tag_name = None

        return {"changelog_update": output, "tag": tag_name}

    def run_sre_diagnose(self) -> dict:
        """Diagnose and mitigate an incident based on alerts."""
        progress("SRE: Diagnosing incident...")
        alerts_path = self.repo_dir / "alerts.json"
        alerts_json = alerts_path.read_text() if alerts_path.exists() else "[]"

        infra_path = self.repo_dir / "docs/maintainer/infrastructure/manifest.md"
        # Backward-compatible fallback for older repos.
        if not infra_path.exists():
            legacy_path = self.repo_dir / "docs/infrastructure/manifest.md"
            infra_path = legacy_path if legacy_path.exists() else infra_path
        infra_manifest = infra_path.read_text() if infra_path.exists() else "N/A"

        prompt = self.load_prompt("sre_prompt.md")
        prompt = prompt.replace("{{ALERTS_JSON}}", alerts_json)
        prompt = prompt.replace("{{INFRA_MANIFEST}}", infra_manifest)

        output = self.run_agent(prompt, role="sre")
        return {"mitigation_plan": output}

    def _validate_task(self, task_metadata: dict) -> dict:
        # Delegates to OuterLoopManager (syncs constraints first)
        self._outer_loop_manager.task_constraints = self.task_constraints
        return self._outer_loop_manager._validate_task(task_metadata)

    def _validate_generated_tasks(
        self,
        old_content: str,
        new_content: str,
        *,
        new_task_ids: list[str] | None = None,
    ) -> dict:
        # Delegates to OuterLoopManager (syncs constraints first)
        self._outer_loop_manager.task_constraints = self.task_constraints
        return self._outer_loop_manager._validate_generated_tasks(
            old_content, new_content, new_task_ids=new_task_ids
        )

    def run_plan(self, design_path: str) -> dict:
        # Delegates to OuterLoopManager
        self._capability_gate.assert_permitted(CapabilityTier.C1_LOCAL_WRITE)
        return self._outer_loop_manager.run_plan(
            design_path=design_path,
            load_prompt_callback=self.load_prompt,
            run_agent_callback=lambda p, schema_work_dir=None, **k: self.run_agent(
                p, role="author", **k
            ),
            log_callback=self.log,
            task_constraints=self.task_constraints,
        )

    def _persist_pending_mcp_syncs(self, pending_syncs: list[dict]) -> None:
        """Write updated pending_mcp_syncs back to state.json.

        Called after each successful MCP write during sync so that
        ``last_synced_index`` is durably persisted for retry recovery.
        """
        state_file = self._get_state_file_path()
        state: dict = {}
        if state_file.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                state = json.loads(state_file.read_text())
        outer = state.get("outer_loop", {})
        outer["pending_mcp_syncs"] = pending_syncs
        state["outer_loop"] = outer
        state_file.write_text(json.dumps(state, indent=2))

    def _clear_pending_mcp_syncs(self) -> None:
        """Remove pending_mcp_syncs from state.json after successful sync."""
        state_file = self._get_state_file_path()
        if not state_file.exists():
            return
        state: dict = {}
        with contextlib.suppress(json.JSONDecodeError, OSError):
            state = json.loads(state_file.read_text())
        outer = state.get("outer_loop", {})
        outer.pop("pending_mcp_syncs", None)
        state["outer_loop"] = outer
        state_file.write_text(json.dumps(state, indent=2))

    def _sync_pending_mcp_writes(self, pending_syncs: list[dict]) -> None:
        """Process pending MCP syncs from a previous run's staging files.

        Reads each staging file via the appropriate file provider and writes
        the content to the corresponding MCP provider.  After successful sync,
        archives the staging file to ``<path>.synced``.

        Args:
            pending_syncs: List of pending sync entries from state.json.
        """
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifact_providers.mcp import MCPOpportunityProvider

        for entry in pending_syncs:
            sync_type = entry.get("type")
            staging_file = entry.get("staging_file")
            if not sync_type or not staging_file:
                continue

            staging_path = Path(staging_file)
            if not staging_path.is_absolute():
                staging_path = self.repo_dir / staging_file
            if not staging_path.exists():
                print(f"[staging] Skipping sync: staging file not found: {staging_path}")
                continue

            # Stale sync warning (>24h)
            created_at = entry.get("created_at", "")
            if created_at:
                try:
                    created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
                    if age_hours > 24:
                        print(
                            f"[staging] WARNING: pending MCP sync for {sync_type} is "
                            f"{age_hours:.0f}h old — this may indicate an abandoned run. "
                            "Proceeding with sync."
                        )
                except (ValueError, TypeError):
                    pass

            if sync_type == "opportunities":
                opp_provider = self._outer_loop_manager.opportunity_provider
                if not isinstance(opp_provider, MCPOpportunityProvider):
                    print("[staging] Skipping sync: opportunity provider is not MCP-backed")
                    continue

                progress(f"[staging] Syncing pending MCP write: {sync_type} from {staging_file}")

                # Inject agent callback for MCP writes during sync
                opp_provider.set_agent_callback(
                    lambda p, **k: self.run_agent(p, role="analyzer", **k)
                )

                file_prov = FileOpportunityProvider(staging_path)
                opportunities = file_prov.list_opportunities()
                last_synced = entry.get("last_synced_index", 0)
                synced_count = 0

                for i, opp in enumerate(opportunities):
                    if i < last_synced:
                        continue
                    opp_provider.write_opportunity(opp)
                    synced_count += 1
                    # Persist progress after each successful write so that a
                    # mid-sync failure can resume without replaying items.
                    entry["last_synced_index"] = i + 1
                    self._persist_pending_mcp_syncs(pending_syncs)

                # Archive the staging file
                archived_path = Path(str(staging_path) + ".synced")
                staging_path.rename(archived_path)
                # Clear pending syncs from state now that sync is complete
                self._clear_pending_mcp_syncs()
                progress(
                    f"[staging] Sync complete: {synced_count} item(s) written to MCP; "
                    f"staging file archived to {archived_path}"
                )
            elif sync_type == "designs":
                from millstone.artifact_providers.file import FileDesignProvider
                from millstone.artifact_providers.mcp import MCPDesignProvider

                design_provider = self._outer_loop_manager.design_provider
                if not isinstance(design_provider, MCPDesignProvider):
                    print("[staging] Skipping sync: design provider is not MCP-backed")
                    continue

                progress(f"[staging] Syncing pending MCP write: {sync_type} from {staging_file}")

                # Inject agent callback for MCP writes during sync
                design_provider.set_agent_callback(
                    lambda p, **k: self.run_agent(p, role="author", **k)
                )

                file_design_prov = FileDesignProvider(staging_path)
                designs = file_design_prov.list_designs()
                last_synced = entry.get("last_synced_index", 0)
                synced_count = 0

                for i, design in enumerate(designs):
                    if i < last_synced:
                        continue
                    design_provider.write_design(design)
                    synced_count += 1
                    entry["last_synced_index"] = i + 1
                    self._persist_pending_mcp_syncs(pending_syncs)

                # Archive the staging directory
                archived_path = Path(str(staging_path) + ".synced")
                staging_path.rename(archived_path)
                self._clear_pending_mcp_syncs()
                progress(
                    f"[staging] Sync complete: {synced_count} design(s) written to MCP; "
                    f"staging directory archived to {archived_path}"
                )
            elif sync_type == "tasks":
                from millstone.artifact_providers.file import FileTasklistProvider
                from millstone.artifact_providers.mcp import MCPTasklistProvider

                task_provider = self._outer_loop_manager.tasklist_provider
                if not isinstance(task_provider, MCPTasklistProvider):
                    print("[staging] Skipping sync: tasklist provider is not MCP-backed")
                    continue

                progress(f"[staging] Syncing pending MCP write: {sync_type} from {staging_file}")

                # Inject agent callback for MCP writes during sync
                task_provider.set_agent_callback(
                    lambda p, **k: self.run_agent(p, role="author", **k)
                )

                file_task_prov = FileTasklistProvider(staging_path)
                tasks = file_task_prov.list_tasks()
                last_synced = entry.get("last_synced_index", 0)
                synced_count = 0

                for i, task in enumerate(tasks):
                    if i < last_synced:
                        continue
                    task_provider.append_tasks([task])
                    synced_count += 1
                    entry["last_synced_index"] = i + 1
                    self._persist_pending_mcp_syncs(pending_syncs)

                # Archive the staging file
                archived_path = Path(str(staging_path) + ".synced")
                staging_path.rename(archived_path)
                self._clear_pending_mcp_syncs()
                progress(
                    f"[staging] Sync complete: {synced_count} task(s) written to MCP; "
                    f"staging file archived to {archived_path}"
                )
            else:
                print(f"[staging] Skipping unknown sync type: {sync_type}")

    def _resume_from_stage(
        self, stage: str, outer: dict, *, enforce_gates: bool = True
    ) -> int | None:
        """Resume outer-loop from a checkpoint stage.

        Args:
            stage: The checkpoint stage name (e.g. "analyze_complete").
            outer: The outer_loop section from state.json.
            enforce_gates: When True, approval gates (approve_designs,
                approve_plans) are respected and may halt the resume.
                When False (e.g. --continue), gates are skipped since the
                user explicitly approved by re-running.

        Returns an exit code if the stage was handled (including halting at a
        gate), or None if the stage is unknown and the caller should fall
        through to a full cycle.
        """
        progress(f"Resuming cycle from checkpoint: {stage}")

        if stage == "analyze_complete":
            design_result = self.run_design(opportunity=outer.get("opportunity", ""))
            if not design_result.get("success"):
                return 1
            design_ref = design_result.get("design_file") or design_result.get("design_id") or ""
            # Respect approve_designs gate when enforcement is active
            if enforce_gates and self.approve_designs:
                progress("")
                progress("=" * 60)
                progress("APPROVAL GATE: Design created")
                progress("=" * 60)
                progress("")
                progress(f"Review the design: {design_ref}")
                progress("Then re-run with:")
                progress(f"  millstone --plan {design_ref}")
                progress("")
                progress("Or run with --no-approve for fully autonomous operation.")
                # Build pending syncs for design if staged
                pending_syncs: list[dict] = []
                if design_result.get("staged"):
                    pending_syncs.append(
                        {
                            "type": "designs",
                            "staging_file": design_result["staging_file"],
                            "last_synced_index": 0,
                            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        }
                    )
                self.save_outer_loop_checkpoint(
                    "design_complete",
                    design_path=design_ref,
                    opportunity=outer.get("opportunity", ""),
                    pending_mcp_syncs=pending_syncs if pending_syncs else None,
                )
                return 0
            # No gate — continue to plan
            plan_result = self.run_plan(design_path=design_ref)
            if not plan_result.get("success"):
                return 1
            # Respect approve_plans gate when enforcement is active
            if enforce_gates and self.approve_plans:
                progress("")
                progress("=" * 60)
                progress("APPROVAL GATE: Tasks added to tasklist")
                progress("=" * 60)
                progress("")
                progress("Review the new tasks, then re-run to execute:")
                progress("  millstone")
                progress("")
                progress("Or run with --no-approve for fully autonomous operation.")
                plan_pending_syncs: list[dict] = []
                if plan_result.get("staged"):
                    plan_pending_syncs.append(
                        {
                            "type": "tasks",
                            "staging_file": plan_result["staging_file"],
                            "last_synced_index": 0,
                            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        }
                    )
                self.save_outer_loop_checkpoint(
                    "plan_complete",
                    design_path=design_ref,
                    tasks_created=plan_result.get("tasks_added", 0),
                    pending_mcp_syncs=plan_pending_syncs if plan_pending_syncs else None,
                )
                return 0
            self.clear_state()
            return self.run()

        elif stage == "design_complete":
            plan_result = self.run_plan(design_path=outer.get("design_path", ""))
            if not plan_result.get("success"):
                return 1
            # Respect approve_plans gate when enforcement is active
            if enforce_gates and self.approve_plans:
                progress("")
                progress("=" * 60)
                progress("APPROVAL GATE: Tasks added to tasklist")
                progress("=" * 60)
                progress("")
                progress("Review the new tasks, then re-run to execute:")
                progress("  millstone")
                progress("")
                progress("Or run with --no-approve for fully autonomous operation.")
                design_plan_pending_syncs: list[dict] = []
                if plan_result.get("staged"):
                    design_plan_pending_syncs.append(
                        {
                            "type": "tasks",
                            "staging_file": plan_result["staging_file"],
                            "last_synced_index": 0,
                            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        }
                    )
                self.save_outer_loop_checkpoint(
                    "plan_complete",
                    design_path=outer.get("design_path", ""),
                    tasks_created=plan_result.get("tasks_added", 0),
                    pending_mcp_syncs=design_plan_pending_syncs
                    if design_plan_pending_syncs
                    else None,
                )
                return 0
            self.clear_state()
            return self.run()

        elif stage == "plan_complete":
            self.clear_state()
            return self.run()

        else:
            progress(f"Warning: unknown outer_loop stage '{stage}', running full cycle.")
            return None

    def run_cycle(self) -> int:
        # Process any pending MCP syncs from a previous halted run.
        # When syncs exist, resume from the saved outer-loop stage
        # (e.g. analyze_complete → design → plan → execute) instead of
        # restarting the full cycle — the analysis output has already been
        # produced and just needed to be flushed to MCP.
        state = self.load_state()
        if state:
            outer = state.get("outer_loop") or {}
            pending_syncs = outer.get("pending_mcp_syncs")
            if pending_syncs:
                self._sync_pending_mcp_writes(pending_syncs)
                # Resume from the checkpoint stage after syncing,
                # respecting downstream approval gates.
                stage = outer.get("stage")
                if stage:
                    result = self._resume_from_stage(stage, outer)
                    if result is not None:
                        return result
        # No pending syncs or no checkpoint — run full cycle from scratch.
        self._capability_gate.assert_permitted(CapabilityTier.C1_LOCAL_WRITE)
        return self._outer_loop_manager.run_cycle(
            has_remaining_tasks_callback=self.has_remaining_tasks,
            run_callback=self.run,
            run_analyze_callback=self.run_analyze,
            run_design_callback=self.run_design,
            review_design_callback=self.review_design,
            run_plan_callback=self.run_plan,
            run_eval_callback=self.run_eval,
            eval_on_commit=self.eval_on_commit,
            log_callback=self.log,
            save_checkpoint_callback=self.save_outer_loop_checkpoint,
        )

    def get_tasklist_prompt(self) -> str:
        """Generate prompt for tasklist-based task execution.

        If the current task is part of a group and there is accumulated context
        from previous tasks in that group, it is appended to the prompt.

        If the task has a context file annotation (<!-- context: path -->), the
        content of that file is also appended to the prompt.
        """
        prompt = self.load_prompt("tasklist_prompt.md")
        prompt = prompt.replace("{{WORKING_DIRECTORY}}", str(self.repo_dir))
        prompt = self._apply_provider_placeholders(
            prompt,
            self._outer_loop_manager.tasklist_provider.get_prompt_placeholders(),
        )
        # Backward-compat: custom --prompts-dir templates may still use {{TASKLIST_PATH}}
        prompt = prompt.replace("{{TASKLIST_PATH}}", self.tasklist)

        # Inject acceptance criteria for the builder
        acceptance_criteria = self.extract_current_task_acceptance_criteria()
        if acceptance_criteria:
            criteria_lines = "\n".join(f"- {c}" for c in acceptance_criteria)
            criteria_blurb = f"\nYour implementation must satisfy:\n{criteria_lines}\n"
        else:
            criteria_blurb = ""
        prompt = prompt.replace("{{ACCEPTANCE_CRITERIA}}", criteria_blurb)

        # Append group context if available
        group_context = self.get_group_context()
        if group_context:
            prompt += "\n\n---\n\n## Group Context\n\n"
            prompt += f"This task is part of the **{self.current_task_group}** group. "
            prompt += "Below is context from previously completed tasks in this group. "
            prompt += "Use this to understand patterns, decisions, and approaches established earlier.\n\n"
            prompt += group_context

        # Append task-specific context file if specified
        context_file_content = self.get_task_context_file_content()
        if context_file_content:
            context_file_path = self.extract_current_task_context_file()
            prompt += "\n\n---\n\n## Task Context\n\n"
            prompt += f"The following context has been provided for this task from `{context_file_path}`:\n\n"
            prompt += context_file_content

        return prompt

    def get_review_prompt(self, builder_output: str = "", git_diff: str | None = None) -> str:
        """Generate prompt for review."""
        prompt = self.load_prompt("review_prompt.md")
        prompt = prompt.replace("{{WORKING_DIRECTORY}}", str(self.repo_dir))
        # Backward-compat: custom --prompts-dir templates may still contain the
        # literal default path instead of a provider token.  Rewrite before
        # injecting dynamic content so we never mutate diff/author payloads.
        prompt = prompt.replace(".millstone/tasklist.md", self.tasklist)
        prompt = self._apply_provider_placeholders(
            prompt,
            self._outer_loop_manager.tasklist_provider.get_prompt_placeholders(),
        )
        if "{{GIT_DIFF}}" in prompt:
            prompt = prompt.replace("{{GIT_DIFF}}", git_diff or "")
        if "{{AUTHOR_OUTPUT}}" in prompt:
            prompt = prompt.replace("{{AUTHOR_OUTPUT}}", builder_output)
        # Inject acceptance criteria for the reviewer
        acceptance_criteria = self.extract_current_task_acceptance_criteria()
        if acceptance_criteria:
            criteria_lines = "\n".join(f"- {c}" for c in acceptance_criteria)
            criteria_blurb = f"Verify each criterion is met:\n{criteria_lines}\n\n"
        else:
            criteria_blurb = ""
        prompt = prompt.replace("{{ACCEPTANCE_CRITERIA}}", criteria_blurb)
        return prompt

    def get_compact_prompt(self) -> str:
        """Generate prompt for tasklist compaction."""
        prompt = self.load_prompt("compact_tasklist.md")
        prompt = self._apply_provider_placeholders(
            prompt,
            self._outer_loop_manager.tasklist_provider.get_prompt_placeholders(),
        )
        return prompt

    def get_research_prompt(self) -> str:
        """Generate prompt for research/analysis mode.

        Uses research_prompt.md template with the task description.
        Research mode is for exploration tasks that don't produce code changes.
        """
        task_description = self.task if self.task else self.extract_current_task_title()
        return self.load_prompt("research_prompt.md").replace("{{TASK}}", task_description)

    def sanity_check_impl(self, agent_output: str, git_status: str, git_diff: str) -> bool:
        # Delegates to InnerLoopManager
        return self._inner_loop_manager.sanity_check_impl(
            agent_output=agent_output,
            git_status=git_status,
            git_diff=git_diff,
            load_prompt_callback=self.load_prompt,
            run_agent_callback=self.run_agent,
        )

    def sanity_check_review(self, review_output: str) -> bool:
        # Delegates to InnerLoopManager
        return self._inner_loop_manager.sanity_check_review(
            review_output=review_output,
            load_prompt_callback=self.load_prompt,
            run_agent_callback=self.run_agent,
        )

    def mechanical_checks(self) -> bool:
        # Delegates to InnerLoopManager (manages skip_mechanical_checks state)
        passed, skip_consumed = self._inner_loop_manager.mechanical_checks(
            loc_baseline_ref=self.loc_baseline_ref,
            skip_mechanical_checks=self._skip_mechanical_checks,
            no_tasklist_edits=self.no_tasklist_edits,
            tasklist_path=None if (self.task and not self.no_tasklist_edits) else self.tasklist,
            tasklist_baseline=self._tasklist_baseline,
            log_callback=self.log,
            save_state_callback=self.save_state,
            git_callback=self.git,
        )
        if skip_consumed:
            self._skip_mechanical_checks = False
        return passed

    def _log_policy_violation(self, violation_type: str, message: str) -> None:
        # Delegates to InnerLoopManager
        self._inner_loop_manager._log_policy_violation(
            violation_type=violation_type,
            message=message,
            log_callback=self.log,
        )

    def _analyze_task_complexity(self, task_text: str) -> dict:
        """Analyze task complexity using the analyzer agent.

        Returns:
            Dict with 'complexity' (simple/medium/complex) and 'reasoning'.
        """
        # Check if enabled (skip if disabled to save tokens/time)
        if not self.config.get("model_selection", {}).get("enabled", False):
            return {}

        progress(f"{self._task_prefix()} Analyzing task complexity...")

        # Extract referenced files
        file_refs = self._extract_file_refs(task_text)

        # Get context file content if available
        context_content = self.get_task_context_file_content()

        # Prepare files section
        files_info = []
        if file_refs:
            files_info.append(f"Referenced files: {', '.join(file_refs)}")
        if context_content:
            context_path = self.extract_current_task_context_file()
            files_info.append(f"Context file ({context_path}):\n{context_content[:2000]}...")

        files_str = "\n\n".join(files_info) if files_info else "No specific files referenced."

        prompt = (
            self.load_prompt("complexity_prompt.md")
            .replace("{{TASK}}", task_text)
            .replace("{{FILES}}", files_str)
        )

        # Run analyzer
        response = self.run_agent(prompt, role="analyzer", output_schema="complexity_analysis")

        # Parse JSON
        result = {"complexity": "medium", "reasoning": "Failed to parse response"}
        try:
            import re

            # Use non-greedy match to find the first valid JSON object
            json_match = re.search(r"\{[\s\S]*?\}", response)
            if json_match:
                parsed = json.loads(json_match.group(0))
                if "complexity" in parsed:
                    result = parsed
        except Exception as e:
            self.log("complexity_analysis_failed", error=str(e), response=response)

        self.log("task_complexity_analysis", **result)
        progress(
            f"{self._task_prefix()} Task Complexity: {result.get('complexity', 'unknown').upper()}"
        )
        return result

    def run_single_task(self) -> bool:
        """Run a single task through the build-review cycle.

        Returns:
            True if task was approved and committed, False otherwise.
        """
        self._capability_gate.assert_permitted(CapabilityTier.C1_LOCAL_WRITE)

        # Reset per-task state
        self.cycle = 0
        if self.session_mode == "new_each_task":
            self.builder_session_id = None
            self.reviewer_session_id = None

        # Initialize tracking
        self._task_start_time = datetime.now()
        self._task_tokens_in = 0
        self._task_tokens_out = 0
        self._task_review_cycles = 0
        self._task_review_duration_ms = 0
        self._task_findings_count = 0
        self._task_findings_by_severity = {
            "critical": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
            "nit": 0,
        }
        self._task_previous_diff = None

        # Determine task text and metadata
        _mcp_task_item: Any = None  # Set when MCP provider supplies the current task
        if self.task:
            task_display = self.task[:50] + "..." if len(self.task) > 50 else self.task
            self.current_task_title = task_display
            task_text = self.task
            # --task mode still supports metadata parsing (e.g., "  - Risk: high")
            task_metadata = self._tasklist_manager._parse_task_metadata(task_text)
            self.apply_risk_settings(task_metadata.get("risk"))
            self.current_task_group = None
        else:
            # When the tasklist is MCP-backed, derive task title and ID from the
            # remote provider's cached task list instead of reading a local file
            # that may not exist or may be stale.
            from millstone.artifact_providers.mcp import MCPTasklistProvider
            from millstone.artifacts.models import TaskStatus

            provider = self._outer_loop_manager.tasklist_provider
            if isinstance(provider, MCPTasklistProvider):
                cached = provider.list_tasks()
                pending = [
                    t for t in cached if t.status in (TaskStatus.todo, TaskStatus.in_progress)
                ]
                if pending:
                    _mcp_task_item = pending[0]
                    self.current_task_title = _mcp_task_item.title or "task"
                    task_text = self.current_task_title
                    self.apply_risk_settings(_mcp_task_item.risk)
                    self.current_task_group = None
                else:
                    self.current_task_title = "task"
                    task_text = self.current_task_title
                    self.apply_risk_settings(None)
                    self.current_task_group = None
            else:
                self.current_task_title = self.extract_current_task_title() or "task"
                task_text = self.current_task_title
                self.apply_risk_settings(self.extract_current_task_risk())
                self.current_task_group = self.extract_current_task_group()

        self._current_task_text = task_text
        # Prefer canonical task_id from metadata (stable, ontology-compliant identity).
        # When an MCP provider supplied the task, use its remote ID directly.
        if _mcp_task_item is not None:
            self._current_task_id = _mcp_task_item.task_id
        elif self.task:
            _task_meta = self._tasklist_manager._parse_task_metadata(task_text)
            self._current_task_id = _task_meta.get("task_id") or (
                re.sub(r"[^a-z0-9]+", "-", task_text.lower()).strip("-")[:30] or None
            )
        else:
            # In tasklist mode, use the raw block (title + indented metadata) so that
            # "- ID: my-task" lines are visible to the parser.
            _task_meta = self._tasklist_manager.extract_current_task_metadata()
            self._current_task_id = _task_meta.get("task_id") or (
                re.sub(r"[^a-z0-9]+", "-", task_text.lower()).strip("-")[:30] or None
            )

        # Worker mode: when --shared-state-dir is set, emit result.json + heartbeats
        # for the worktree control plane to consume.
        worker_task_id: str | None = None
        worker_state = None
        worker_hb_stop = None
        worker_started_at = time.time()
        if self.shared_state_dir:
            import threading

            from millstone.runtime.parallel_state import ParallelState

            worker_state = ParallelState(Path(self.shared_state_dir), state_lock=_NoopLock())
            meta = self._tasklist_manager._parse_task_metadata(task_text)
            task_id_value = meta.get("task_id")
            if isinstance(task_id_value, str) and task_id_value:
                worker_task_id = task_id_value
            else:
                worker_task_id = self._tasklist_manager.generate_task_id(task_text)

            worker_hb_stop = threading.Event()

            def _hb_loop() -> None:
                while not worker_hb_stop.is_set():
                    try:
                        worker_state.write_heartbeat(worker_task_id)
                    except Exception as exc:
                        logging.debug(
                            "Worker heartbeat write failed for task_id=%s: %s",
                            worker_task_id,
                            exc,
                        )
                    worker_hb_stop.wait(self.parallel_heartbeat_interval)

            try:
                worker_state.write_heartbeat(worker_task_id)
            except Exception as exc:
                logging.debug(
                    "Worker heartbeat write failed for task_id=%s: %s",
                    worker_task_id,
                    exc,
                )

            threading.Thread(target=_hb_loop, daemon=True).start()

        def _worker_finish(
            status: str, review_summary: str | None = None, error: str | None = None
        ) -> None:
            if worker_hb_stop is not None:
                worker_hb_stop.set()
            if worker_state is None or worker_task_id is None:
                return
            try:
                branch = self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
                commit_sha = self.git("rev-parse", "HEAD").strip()
            except Exception:
                branch = None
                commit_sha = None
            try:
                worker_state.write_task_result(
                    worker_task_id,
                    {
                        "task_id": worker_task_id,
                        "status": status,
                        "branch": branch,
                        "commit_sha": commit_sha,
                        "risk": self.current_task_risk,
                        "review_summary": review_summary,
                        "error": error,
                        "started_at": worker_started_at,
                        "completed_at": time.time(),
                    },
                )
            except Exception as exc:
                # Best-effort; worker results are helpful but must not break the run.
                logging.debug(
                    "Worker result write failed for task_id=%s: %s",
                    worker_task_id,
                    exc,
                )

        self._tasklist_baseline = None
        if not self.task:
            tasklist_path = self.repo_dir / self.tasklist
            if tasklist_path.exists():
                self._tasklist_baseline = tasklist_path.read_text()

        # Progress info
        info_parts = []
        if self.current_task_risk:
            info_parts.append(f"risk: {self.current_task_risk}")
        if self.current_task_group:
            info_parts.append(f"group: {self.current_task_group}")
        progress(
            f'{self._task_prefix()} Starting: "{self.current_task_title}"'
            + (f" [{', '.join(info_parts)}]" if info_parts else "")
        )

        # High-risk gate
        if self.requires_high_risk_approval():
            print(f"\n=== HIGH-RISK TASK APPROVAL REQUIRED ===\nTask: {self.current_task_title}\n")
            if input("Proceed? [y/N]: ").strip().lower() != "y":
                self.log("high_risk_declined", task=task_text)
                _worker_finish("failed", error="high_risk_declined")
                return False

        # Research mode (Short circuit)
        if self.research:
            progress(f"{self._task_prefix()} Running researcher...")
            builder_output = self.run_agent(self.get_research_prompt(), role="author")
            output_file = self.write_research_output(task_text, builder_output)
            self.log("research_completed", task=task_text[:200], output_file=str(output_file))
            if not self.task:
                self.mark_task_complete()
                # Close remote task for MCP providers — mark_task_complete()
                # only updates the local tasklist file, which is a no-op when
                # the task lives on a remote backend (GitHub Issues, Linear, …).
                from millstone.artifact_providers.mcp import MCPTasklistProvider
                from millstone.artifacts.models import TaskStatus

                provider = self._outer_loop_manager.tasklist_provider
                if isinstance(provider, MCPTasklistProvider) and self._current_task_id:
                    provider.update_task_status(self._current_task_id, TaskStatus.done)
            progress(f"{self._task_prefix()} Research completed -> {output_file}")
            _worker_finish("success", review_summary=builder_output[:4000])
            return True

        # Capture baseline for eval
        eval_enabled = self.eval_on_commit or (self.eval_on_task != "none" and not self.skip_eval)
        eval_before = self._get_eval_before_task() if eval_enabled else None

        # Track empty-diff retry state
        empty_diff_retried = False

        # Record HEAD before builder runs so we can detect early commits
        pre_build_head = self.git("rev-parse", "HEAD").strip()

        # Define Loop components
        def builder_producer(feedback: str | None = None) -> BuilderArtifact:
            if feedback:
                progress(
                    f"{self._task_prefix()} Cycle {self.cycle + 1}/{self.max_cycles}: Applying fixes..."
                )
                msg = f"Address this review feedback:\n\n{feedback}"
                output = self.run_agent(msg, resume=self.builder_session_id, role="author")
            else:
                progress(f"{self._task_prefix()} Running builder...")
                self._analyze_task_complexity(task_text)
                prompt = self.get_task_prompt() if self.task else self.get_tasklist_prompt()
                output = self.run_agent(prompt, role="author")

            # Extract session
            match = re.search(r"session_id[\":\s]+([a-f0-9-]+)", output)
            if match:
                self.builder_session_id = match.group(1)

            # Ensure untracked files are included in diff (for reviewer visibility)
            self._stage_untracked_files()

            # Detect if builder committed its own changes (HEAD advanced)
            nonlocal pre_build_head
            current_head = self.git("rev-parse", "HEAD").strip()
            builder_committed = current_head != pre_build_head

            if builder_committed:
                progress(
                    f"{self._task_prefix()} Builder committed changes directly "
                    f"(HEAD advanced from {pre_build_head[:8]} to {current_head[:8]}). "
                    "Using committed diff for review."
                )
                self.log(
                    "builder_early_commit",
                    old_head=pre_build_head,
                    new_head=current_head,
                )
                # Use the diff from the builder's commit(s) for review.
                # Also include any uncommitted changes on top of the builder's commits.
                git_diff = self.git("diff", pre_build_head)
                git_status = self.git("diff", "--stat", pre_build_head)
            else:
                git_diff = self.git("diff", "HEAD")
                git_status = self.git("status", "--short")

            # Update pre_build_head so the next cycle (if reviewer rejects)
            # correctly detects whether the builder commits again.
            if builder_committed:
                pre_build_head = current_head

            return BuilderArtifact(
                output, git_status, git_diff, builder_committed=builder_committed
            )

        def builder_validator(artifact: BuilderArtifact) -> tuple[bool, str | None]:
            nonlocal empty_diff_retried

            if not self.mechanical_checks():
                return False, "Mechanical checks failed"

            self.log(
                "git_state",
                cycle=str(self.cycle + 1),
                status=artifact.git_status,
                diff=artifact.git_diff,
            )

            # Retry once on empty diff before sanity check fails
            # This catches cases where the builder didn't make changes but should have
            # Skip nudge if builder already committed (diff extracted from commits)
            if (
                not artifact.git_status.strip()
                and not empty_diff_retried
                and not artifact.builder_committed
            ):
                empty_diff_retried = True
                progress(f"{self._task_prefix()} No changes detected, nudging builder to retry...")
                nudge_msg = (
                    "No file changes were detected. If this task requires creating or modifying files, "
                    "please make the necessary changes now. If the task is truly read-only (verification, "
                    "research, or analysis), confirm that no changes are needed."
                )
                retry_output = self.run_agent(
                    nudge_msg, resume=self.builder_session_id, role="author"
                )

                # Update artifact with new state
                artifact.output = retry_output
                self._stage_untracked_files()  # Re-check for new untracked files
                artifact.git_status = self.git("status", "--short")
                artifact.git_diff = self.git("diff", "HEAD")

                # Re-log git state after retry
                self.log(
                    "git_state",
                    cycle=str(self.cycle + 1),
                    status=artifact.git_status,
                    diff=artifact.git_diff,
                    note="after_empty_diff_retry",
                )

            if not self.sanity_check_impl(artifact.output, artifact.git_status, artifact.git_diff):
                return False, "Sanity check failed"

            return True, None

        def builder_reviewer(artifact: BuilderArtifact) -> BuilderVerdict:
            review_start = time.time()
            reviewer_resume = (
                self.reviewer_session_id if self.session_mode != "new_each_task" else None
            )

            review_output = self.run_agent(
                self.get_review_prompt(artifact.output, artifact.git_diff),
                role="reviewer",
                output_schema="review_decision",
                resume=reviewer_resume,
            )

            self._task_review_duration_ms += int((time.time() - review_start) * 1000)

            match = re.search(r"session_id[\":\s]+([a-f0-9-]+)", review_output)
            if match:
                self.reviewer_session_id = match.group(1)

            # Try to parse verdict first - if parseable, review is actionable
            approved, decision = self.is_approved(review_output)

            # Only sanity check if we couldn't parse a valid verdict
            # This allows reviewers like Codex that output just JSON without markdown
            if decision is None:
                is_empty_fallback = (
                    "Reviewer returned empty response" in review_output
                    and "REQUEST_CHANGES" in review_output
                )
                if not is_empty_fallback and not self.sanity_check_review(review_output):
                    raise Exception("Review sanity check failed")

            # Update metrics and diff for false positive detection
            if decision:
                self._task_findings_count += decision.findings_count
                if decision.findings_by_severity:
                    for s, f in decision.findings_by_severity.items():
                        self._task_findings_by_severity[s] += len(f)

            if not approved:
                self._task_review_cycles += 1
                self._task_previous_diff = artifact.git_diff

            return BuilderVerdict(approved, decision, review_output, review_output[:4000])

        # State tracking for the loop
        loop_state: dict[str, str | None] = {"failure_reason": None}

        def builder_on_success(artifact: BuilderArtifact, verdict: BuilderVerdict) -> bool:
            # Detect false positive
            is_false_positive = False
            if self._task_previous_diff is not None:
                is_false_positive = is_whitespace_or_comment_only_change(
                    self._task_previous_diff, artifact.git_diff
                )

            self.append_review_metric(
                task_text=self._current_task_text,
                verdict=verdict.decision.status.value if verdict.decision else "APPROVED",
                findings=verdict.decision.findings if verdict.decision else [],
                findings_by_severity=verdict.decision.findings_by_severity
                if verdict.decision
                else {},
                duration_ms=self._task_review_duration_ms,
                false_positive_indicator=is_false_positive,
            )

            if self.eval_on_task != "none" and not self.skip_eval:
                gate_passed, _ = self._run_eval_gate(task_text=task_text)
                if not gate_passed:
                    loop_state["failure_reason"] = "eval_gate_failed"
                    return False

            if artifact.builder_committed:
                # Builder committed during this cycle. Check if there are
                # remaining uncommitted changes (e.g. from a review-fix cycle
                # where the builder edited files without committing again).
                worktree_status = self.git("status", "--porcelain").strip()
                tasklist_path = str(self.tasklist)
                non_tasklist_dirty = any(
                    line.split()[-1] != tasklist_path
                    for line in worktree_status.split("\n")
                    if line.strip()
                )

                if non_tasklist_dirty:
                    # There are uncommitted changes beyond the tasklist —
                    # delegate a commit for the remaining edits.
                    progress(
                        f"{self._task_prefix()} Builder committed earlier but "
                        "uncommitted changes remain — delegating commit."
                    )
                    if not self.delegate_commit():
                        loop_state["failure_reason"] = "commit_failed"
                        return False
                else:
                    progress(
                        f"{self._task_prefix()} Skipping commit delegation (builder already committed)."
                    )
                    # Auto-commit the tasklist checkbox if it's the only
                    # remaining dirty file (file-backed tasklist only).
                    if not self._auto_commit_tasklist_if_needed():
                        loop_state["failure_reason"] = "tasklist_commit_failed"
                        return False

                # Update baseline so next task measures LoC from this commit
                self._update_loc_baseline()

                if self.eval_on_commit and not self._run_eval_on_commit(task_text=task_text):
                    loop_state["failure_reason"] = "eval_regression"
                    return False

                self.accumulate_group_context(task_text, git_diff=artifact.git_diff)
                return True

            if self.delegate_commit():
                if self.eval_on_commit and not self._run_eval_on_commit(task_text=task_text):
                    loop_state["failure_reason"] = "eval_regression"
                    return False

                self.accumulate_group_context(task_text, git_diff=artifact.git_diff)
                return True

            loop_state["failure_reason"] = "commit_failed"
            return False

        # Run the loop
        loop = ArtifactReviewLoop(
            name="Builder",
            producer=builder_producer,
            validator=builder_validator,
            reviewer=builder_reviewer,
            is_approved=lambda v: v.approved,
            max_cycles=self.max_cycles,
            on_cycle_start=lambda c: setattr(
                self, "cycle", c - 1
            ),  # Keep self.cycle aligned with cycle body
            on_success=builder_on_success,
        )

        result = loop.run()
        self.cycle = result.cycles  # Final cycle count

        # Logging final state
        if result.success:
            self.save_task_metrics(
                task_text,
                "approved",
                self.cycle,
                eval_before,
                self._get_latest_eval() if eval_enabled else None,
            )
            progress(f"{self._task_prefix()} Completed and committed after {self.cycle} cycle(s)")
            review_summary = result.verdict.feedback if result.verdict else None
            _worker_finish("success", review_summary=review_summary)
            return True
        else:
            reason = loop_state["failure_reason"] or result.error or "Unknown failure"
            self.save_task_metrics(task_text, reason.lower().replace(" ", "_"), self.cycle)
            self.log("run_completed", result=reason.upper(), cycles=str(self.cycle))
            review_summary = result.verdict.feedback if result.verdict else None
            _worker_finish("failed", review_summary=review_summary, error=reason)
            self._print_failure_summary(
                self.current_task_title,
                result.artifact.output if result.artifact else None,
                result.verdict,
                error=result.error,
            )
            return False

    def _print_failure_summary(
        self,
        task_title: str,
        last_builder_output: str | None,
        last_verdict: "BuilderVerdict | None",
        *,
        error: str | None = None,
    ) -> None:
        """Print a structured failure summary to stdout when a task is abandoned."""
        if self.quiet:
            return

        sep = "━" * 42
        print(f"\n{sep}")
        print(f'━━━ Task Failed: "{task_title}" ━━━')
        print()

        if last_builder_output is not None:
            lines = last_builder_output.splitlines()
            truncated = lines[:20]
            print("Last builder output (truncated to 20 lines):")
            for line in truncated:
                print(f"  {line}")
            if len(lines) > 20:
                print(f"  ... ({len(lines) - 20} more lines)")
        else:
            print("Last builder output: (none)")
        print()

        if last_verdict is None:
            print("Reviewer verdict: N/A")
        else:
            verdict_label = "REJECTED" if not last_verdict.approved else "APPROVED"
            print(f"Reviewer verdict: {verdict_label}")
            print("Reviewer feedback:")
            feedback = last_verdict.feedback.strip() if last_verdict.feedback else ""
            if feedback:
                for line in feedback.splitlines():
                    print(f"  {line}")
            else:
                print("  (none)")
        print()

        print(
            "Suggestion: Revise the task description to clarify requirements,\n"
            'or add acceptance criteria so the builder knows what "done" looks like.'
        )
        print()
        print(f"Log: {self.log_file}")
        print(sep)

    def run_dry_run(self) -> int:
        """Show what would be executed without invoking claude. Returns exit code 0."""
        print("=== DRY RUN MODE ===")
        print("Showing prompts and files that would be used (no claude invocations)")
        print()

        # Show prompt files that would be used
        print("--- Prompt Files ---")
        prompt_files = [
            "tasklist_prompt.md" if not self.task else "task_prompt.md",
            "review_prompt.md",
            "sanity_check_impl.md",
            "sanity_check_review.md",
            "commit_prompt.md",
        ]
        for pf in prompt_files:
            if self._custom_prompts_dir:
                path = self._custom_prompts_dir / pf
                exists = "✓" if path.exists() else "✗"
                print(f"  {exists} {path}")
            else:
                # Using package resources - check if loadable
                try:
                    files("millstone.prompts").joinpath(pf).read_text()
                    print(f"  ✓ [package] millstone/prompts/{pf}")
                except FileNotFoundError:
                    print(f"  ✗ [package] millstone/prompts/{pf}")
        print()

        # Show the builder prompt that would be sent
        print("--- Builder Prompt ---")
        prompt = self.get_task_prompt() if self.task else self.get_tasklist_prompt()
        print(prompt)
        print()

        # Show the review prompt that would be sent
        print("--- Review Prompt ---")
        review_prompt = self.get_review_prompt()
        print(review_prompt)
        print()

        # Show tasklist file info if applicable
        if not self.task:
            print("--- Tasklist Info ---")
            from millstone.artifact_providers.mcp import MCPTasklistProvider

            provider = self._outer_loop_manager.tasklist_provider
            if isinstance(provider, MCPTasklistProvider):
                print(f"  Provider: {provider._mcp_server} (remote)")
                label_str = ", ".join(provider._labels) if provider._labels else "none"
                print(f"  Labels: {label_str}")
                project_str = ", ".join(provider._projects) if provider._projects else "none"
                print(f"  Projects: {project_str}")
            else:
                tasklist_path = self.repo_dir / self.tasklist
                print(f"  File: {tasklist_path}")
                print(f"  Exists: {tasklist_path.exists()}")
                if tasklist_path.exists():
                    content = tasklist_path.read_text()
                    unchecked = len(re.findall(r"^- \[ \]", content, re.MULTILINE))
                    self.completed_task_count = self.count_completed_tasks()
                    print(f"  Unchecked tasks: {unchecked}")
                    print(f"  Completed tasks: {self.completed_task_count}")
                    print(f"  Compact threshold: {self.compact_threshold}")
                    if self.should_compact():
                        print("  Compaction: WOULD TRIGGER (completed >= threshold)")
                    elif self.compact_threshold <= 0:
                        print("  Compaction: DISABLED")
                    else:
                        print("  Compaction: not needed (completed < threshold)")
            print()

        # Show work directory info
        print("--- Work Directory ---")
        print(f"  Path: {self.work_dir}")
        print(f"  Exists: {self.work_dir.exists()}")
        print()

        print("=== END DRY RUN ===")
        return 0

    def run(self) -> int:
        """Run the orchestration loop. Returns exit code."""
        # Worktree control plane: must run before dry-run handling so it can
        # provide its own dry-run behavior (worktree layout plan).
        if self.parallel_enabled:
            from millstone.runtime.parallel import ParallelOrchestrator

            return ParallelOrchestrator(self).run()

        # Handle dry-run mode
        if self.dry_run:
            return self.run_dry_run()

        # Handle --continue mode: restore state and skip mechanical checks
        if self.continue_run:
            state = self.load_state()
            if state:
                print("=== CONTINUING FROM SAVED STATE ===")
                print(f"Halted at: {state.get('timestamp', 'unknown')}")
                print(f"Reason: {state.get('halt_reason', 'unknown')}")
                print()
                # Restore relevant state
                self.loc_baseline_ref = state.get("loc_baseline_ref")
                # Load both session IDs (with fallback to legacy session_id for old state files)
                self.builder_session_id = state.get("builder_session_id") or state.get("session_id")
                self.reviewer_session_id = state.get("reviewer_session_id")
                # Route to the correct outer-loop stage if a checkpoint exists.
                # Stages: analyze_complete -> design_complete -> plan_complete -> inner loop.
                outer = state.get("outer_loop")
                if outer and outer.get("stage"):
                    # Outer-loop resume: builder hasn't run yet, so mechanical
                    # checks must remain active for the first task.
                    stage = outer["stage"]
                    print(f"Outer-loop stage checkpoint: {stage}")
                    print()
                    # Process any pending MCP syncs from the previous run.
                    pending_syncs = outer.get("pending_mcp_syncs")
                    if pending_syncs:
                        self._sync_pending_mcp_writes(pending_syncs)
                    # --continue is explicit user approval, so skip gates.
                    result = self._resume_from_stage(stage, outer, enforce_gates=False)
                    if result is not None:
                        return result
                else:
                    # Inner-loop resume (LoC/sensitive-file halt): user has
                    # already reviewed the diff, so skip mechanical checks.
                    self._skip_mechanical_checks = True
            else:
                print("Warning: --continue specified but no saved state found.")
                print("Running normally...")
                print()

        # Auto-clear stale sessions older than 24 hours (unless explicitly continuing)
        if not self.continue_run and self.auto_clear_stale_sessions(max_age_hours=24):
            print("Auto-cleared stale session IDs (older than 24 hours).")

        # Handle --session mode (unless already set by --continue)
        if (
            self.session_mode not in ("new_each_task", "continue_within_run")
            and not self.builder_session_id
        ):
            if self.session_mode == "continue_across_runs":
                # Load session IDs from saved state
                state = self.load_state()
                if state and (state.get("builder_session_id") or state.get("session_id")):
                    self.builder_session_id = state.get("builder_session_id") or state.get(
                        "session_id"
                    )
                    self.reviewer_session_id = state.get("reviewer_session_id")
                    print(f"Resuming builder session: {self.builder_session_id}")
                    if self.reviewer_session_id:
                        print(f"Resuming reviewer session: {self.reviewer_session_id}")
                else:
                    print(
                        "Warning: --session continue_across_runs specified but no saved session found."
                    )
                    print("Starting fresh session...")
            else:
                # Treat session_mode as an explicit session ID (for builder only)
                self.builder_session_id = self.session_mode
                print(f"Using builder session: {self.builder_session_id}")

        # Run pre-flight checks before starting
        self.preflight_checks()

        # Initialize LoC baseline for per-task tracking (unless continuing)
        if not self.continue_run or not self.loc_baseline_ref:
            self._init_loc_baseline()

        # Warn about uncommitted changes (non-blocking, but skip if continuing)
        if not self.continue_run:
            self.check_dirty_working_directory()
            self.check_uncommitted_tasklist()

        # Count completed tasks in tasklist (for compaction tracking)
        if not self.task:
            self.completed_task_count = self.count_completed_tasks()

            # Check if compaction is needed before starting (skip if continuing)
            if not self.continue_run and self.should_compact():
                self.run_compaction()

        # Capture baseline eval if eval_on_commit or eval_on_task is enabled
        # Note: skip_eval only affects eval_on_task gate, not eval_on_commit
        eval_on_task_enabled = self.eval_on_task != "none" and not self.skip_eval
        eval_enabled = self.eval_on_commit or eval_on_task_enabled
        if eval_enabled and not self.continue_run:
            # Determine which mode to use for baseline
            if self.eval_on_commit:
                progress("Running baseline eval (--eval-on-commit)...")
                self.baseline_eval = self.run_eval()
            else:
                mode_display = f"(--eval-on-task {self.eval_on_task})"
                progress(f"Running baseline eval {mode_display}...")
                self.baseline_eval = self.run_eval(mode=self.eval_on_task)
            if not self.baseline_eval.get("_passed", False):
                progress("Warning: Baseline tests already failing. Will only halt on NEW failures.")
            self.log(
                "baseline_eval_captured",
                passed=str(self.baseline_eval.get("_passed", False)),
                failed_count=str(len(self.baseline_eval.get("failed_tests", []))),
                mode=self.eval_on_task if not self.eval_on_commit else "default",
            )

        # Log run start
        self.log(
            "run_started",
            task=self.task or "(from tasklist)",
            tasklist=self.tasklist,
            max_cycles=str(self.max_cycles),
            max_tasks=str(self.max_tasks),
            loc_threshold=str(self.loc_threshold),
            compact_threshold=str(self.compact_threshold),
            completed_task_count=str(self.completed_task_count),
            continue_run=str(self.continue_run),
        )

        try:
            # For --task mode, only run once
            if self.task:
                self.current_task_num = 1
                self.total_tasks = 1
                success = self.run_single_task()
                if success:
                    progress("=== SUCCESS ===")
                    self.log("run_completed", result="SUCCESS", tasks_completed="1")
                    self.clear_state()  # Clear state on success
                    return 0
                else:
                    self.log("run_completed", result="FAILED", tasks_completed="0")
                    return 1

            # For tasklist mode, run up to max_tasks
            tasks_completed = 0
            for task_num in range(1, self.max_tasks + 1):
                # Check if there are remaining tasks before starting
                if not self.has_remaining_tasks():
                    progress(
                        f"=== NO MORE TASKS === All tasklist tasks complete ({tasks_completed} this run)"
                    )
                    self.log(
                        "run_completed",
                        result="SUCCESS",
                        reason="no_remaining_tasks",
                        tasks_completed=str(tasks_completed),
                    )
                    self.clear_state()  # Clear state on success
                    return 0

                # Set task tracking for progress output
                self.current_task_num = task_num
                self.total_tasks = self.max_tasks

                success = self.run_single_task()
                if success:
                    tasks_completed += 1
                    self.clear_state()  # Clear state after each successful task
                else:
                    # Stop on first failure
                    progress(f"=== HALTED after {tasks_completed} task(s) ===")
                    self.log(
                        "run_completed",
                        result="HALTED",
                        tasks_completed=str(tasks_completed),
                    )
                    return 1

            progress(
                f"=== MAX TASKS REACHED === Completed {tasks_completed} task(s), returning control."
            )
            self.log(
                "run_completed",
                result="SUCCESS",
                tasks_completed=str(tasks_completed),
            )
            self.clear_state()  # Clear state on success
            return 0

        finally:
            self.cleanup()


def main():
    # Load config file first (if it exists) to use as defaults
    config = load_config()
    # commit_tasklist=True provides the legacy tracked path as the default,
    # but only when the user has not explicitly configured a tasklist path.
    if (
        config.get("commit_tasklist", False)
        and config.get("tasklist") == DEFAULT_CONFIG["tasklist"]
    ):
        config["tasklist"] = "docs/tasklist.md"

    parser = argparse.ArgumentParser(
        description="Builder-Reviewer Orchestrator: Wraps stochastic LLM calls in a deterministic "
        "builder-reviewer workflow. Reads tasks from a tasklist file or accepts direct task input, "
        "then iterates through build-review cycles until approval or max cycles reached.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Common workflows:
  1) One task now
     %(prog)s --task "add login page"
  2) Existing backlog (local)
     %(prog)s --migrate-tasklist backlog.md
     %(prog)s
  3) Design -> plan -> execute (skip analyze)
     %(prog)s --deliver "Add retry logic to API client"
  4) New project / fresh app
     %(prog)s --init
     %(prog)s --deliver "Build a CLI app for ..."
  5) Full autonomous improvement loop
     %(prog)s --cycle

Examples:
  %(prog)s                           Run using default tasklist (.millstone/tasklist.md)
  %(prog)s --tasklist TODO.md        Use custom tasklist file
  %(prog)s --max-cycles 5            Allow up to 5 build-review iterations
  %(prog)s --dry-run                 Preview prompts without invoking the configured CLI

Configuration:
  Settings can be stored in .millstone/config.toml. CLI flags override config file values.
  Example config.toml:
    max_cycles = 5
    loc_threshold = 1000
    tasklist = "TODO.md"
    max_tasks = 10
    prompts_dir = "my_prompts"

Remote backlog scoping (Jira / Linear / GitHub):
  Narrow the working task set without changing backend credentials or project scope.
  Single-value shortcut (simplest):
    [tasklist_filter]
    label = "sprint-1"        # equivalent to labels = ["sprint-1"]
    assignee = "alice"        # equivalent to assignees = ["alice"]
    status = "Todo"           # equivalent to statuses = ["Todo"]
  Multi-value (explicit list form, takes precedence over shortcut):
    [tasklist_filter]
    labels   = ["sprint-1", "backend"]
    assignees = ["alice", "bob"]
    statuses  = ["Todo", "In Progress"]
  Backend-specific extras (via tasklist_provider_options):
    # GitHub milestone:
    [tasklist_provider_options]
    filter = {milestone = "v2.0"}
    # Linear cycle / project:
    [tasklist_provider_options]
    filter = {cycles = ["Sprint 5"], projects = ["Platform"]}
""",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=config["max_cycles"],
        metavar="N",
        help="Maximum write/review iterations before halting. Applies to both the inner "
        "build-review loop (code tasks) and the outer-loop authoring steps (analyze, design, "
        "plan). If the reviewer requests changes N times without approval, the orchestrator "
        "stops for human intervention. Higher values allow more automated fixes but risk "
        f"infinite loops. (default: {config['max_cycles']})",
    )
    parser.add_argument(
        "--loc-threshold",
        type=int,
        default=config["loc_threshold"],
        metavar="N",
        help="Maximum lines of code changed (additions + deletions) before requiring "
        "human review. Large changes bypass automated approval as a safety measure. "
        f"Set higher for refactoring tasks, lower for security-sensitive repos. (default: {config['loc_threshold']})",
    )
    parser.add_argument(
        "--task",
        type=str,
        default=None,
        metavar="DESCRIPTION",
        help="Execute a specific task directly instead of reading from the tasklist. "
        "Useful for one-off tasks or testing. When set, --tasklist is ignored. "
        "Example: --task 'add user authentication endpoint'",
    )
    parser.add_argument(
        "--tasklist",
        type=str,
        default=config["tasklist"],
        metavar="PATH",
        help="Path to the markdown tasklist file containing tasks. The builder picks "
        "the first unchecked task (- [ ]) and marks it complete (- [x]) when done. "
        f"Ignored if --task is provided. (default: {config['tasklist']})",
    )
    parser.add_argument(
        "--migrate-tasklist",
        type=str,
        default=None,
        metavar="PATH",
        help="Convert a local backlog file into canonical tasklist format and write it to "
        "--tasklist (default: .millstone/tasklist.md). Accepts markdown checklists, bullet "
        "lists, numbered lists, TODO-prefixed lines, or one-task-per-line text files.",
    )
    parser.add_argument(
        "--roadmap",
        type=str,
        default=config.get("roadmap"),
        metavar="PATH",
        help="Path to the markdown roadmap file containing high-level goals. "
        "When using --cycle, the orchestrator pulls the next goal from here if the tasklist is empty.",
    )
    parser.add_argument(
        "--prompts-dir",
        type=str,
        default=config.get("prompts_dir"),
        metavar="PATH",
        help="Path to custom prompts directory. Overrides built-in prompts with "
        "project-specific versions. The directory should contain prompt files like "
        "tasklist_prompt.md, review_prompt.md, etc. If not specified, uses "
        "built-in prompts from the millstone package.",
    )
    parser.add_argument(
        "--repo-dir",
        type=str,
        default=None,
        metavar="PATH",
        help="Repository directory to operate on. Useful for running in a git worktree. "
        "Defaults to current working directory.",
    )
    parser.add_argument(
        "--shared-state-dir",
        type=str,
        default=None,
        metavar="PATH",
        help="Shared state directory used for worktree workers (result.json, heartbeats). "
        "When set, millstone runs in worker mode and will not start the worktree control plane.",
    )
    parser.add_argument(
        "--worktrees",
        action="store_true",
        default=bool(config.get("parallel_enabled", False)),
        help="Enable worktree execution mode (control plane). Defaults to parallel_enabled from config.toml.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(config.get("parallel_concurrency", 1)),
        metavar="N",
        help="Maximum number of in-flight task worktrees in worktree mode. "
        "Defaults to parallel_concurrency from config.toml.",
    )
    parser.add_argument(
        "--base-branch",
        type=str,
        default=None,
        metavar="NAME",
        help="Base branch to land onto in worktree mode (e.g., main). "
        "If omitted, defaults to the current branch name.",
    )
    parser.add_argument(
        "--base-ref",
        type=str,
        default=None,
        metavar="REF",
        help="Base ref/commit SHA to fork task branches from in worktree mode. "
        "If omitted, defaults to the tip of --base-branch.",
    )
    parser.add_argument(
        "--integration-branch",
        type=str,
        default=str(config.get("parallel_integration_branch", "millstone/integration")),
        metavar="NAME",
        help="Integration branch used as the serialized merge queue target in worktree mode.",
    )
    parser.add_argument(
        "--merge-strategy",
        type=str,
        choices=("merge", "cherry-pick"),
        default=str(config.get("parallel_merge_strategy", "merge")),
        help="Integration strategy for worktree mode: merge or cherry-pick.",
    )
    parser.add_argument(
        "--worktree-root",
        type=str,
        default=str(config.get("parallel_worktree_root", ".millstone/worktrees")),
        metavar="PATH",
        help="Root directory for git worktrees in worktree mode.",
    )
    parser.add_argument(
        "--merge-max-retries",
        type=int,
        default=2,
        metavar="N",
        help="Maximum retries for integrate+land when base advances mid-run (default: 2).",
    )
    parser.add_argument(
        "--worktree-cleanup",
        type=str,
        choices=("always", "on_success", "never"),
        default=str(config.get("parallel_cleanup", "on_success")),
        help="Cleanup policy for worktree mode (default from config).",
    )
    parser.add_argument(
        "--no-tasklist-edits",
        action="store_true",
        default=False,
        help="Disallow editing the tasklist file during a run (used for worktree workers).",
    )
    parser.add_argument(
        "--high-risk-concurrency",
        type=int,
        default=1,
        metavar="N",
        help="Maximum number of concurrent high-risk tasks in worktree mode (default: 1).",
    )
    parser.add_argument(
        "--max-tasks",
        "-n",
        type=int,
        default=config["max_tasks"],
        metavar="N",
        help="Maximum number of tasklist tasks to process before returning control. "
        "After each task is approved and committed, picks the next unchecked task. "
        f"Stops early if no unchecked tasks remain. Ignored when using --task. (default: {config['max_tasks']})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show prompts and files that would be used without invoking the agent. "
        "Displays the builder prompt, review prompt, and tasklist info. "
        "Useful for debugging prompt issues or verifying configuration.",
    )
    parser.add_argument(
        "--research",
        action="store_true",
        help="Run in research/analysis mode. The agent explores and analyzes without "
        "making code changes. Skips the 'no changes' mechanical check and commit "
        "delegation. Output is saved to .millstone/research/ as timestamped markdown. "
        "Useful for codebase exploration, infrastructure evaluation, or documentation tasks.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging output. Overrides log_verbosity config to 'verbose', "
        "showing full agent output including reasoning traces. Useful for debugging "
        "specific runs. Without this flag, output is summarized per log_verbosity setting.",
    )
    parser.add_argument(
        "--full-diff",
        action="store_true",
        help="Show full diffs in logs. Overrides log_diff_mode config to 'full', "
        "showing complete diffs inline instead of summaries. Useful for debugging "
        "specific runs when you need to see exact changes without checking .patch files.",
    )
    # CLI provider arguments
    available_clis = ", ".join(list_providers())
    parser.add_argument(
        "--cli",
        type=str,
        default=config.get("cli", "claude"),
        metavar="NAME",
        help=f"CLI tool to use for all agent roles. Available: {available_clis}. "
        f"(default: {config.get('cli', 'claude')})",
    )
    parser.add_argument(
        "--cli-builder",
        type=str,
        default=config.get("cli_builder"),
        metavar="NAME",
        help="CLI tool for builder role. Overrides --cli for build tasks. "
        f"Available: {available_clis}.",
    )
    parser.add_argument(
        "--cli-reviewer",
        type=str,
        default=config.get("cli_reviewer"),
        metavar="NAME",
        help="CLI tool for reviewer role. Overrides --cli for code review. "
        f"Available: {available_clis}.",
    )
    parser.add_argument(
        "--cli-sanity",
        type=str,
        default=config.get("cli_sanity"),
        metavar="NAME",
        help="CLI tool for sanity checks. Overrides --cli for sanity validation. "
        f"Available: {available_clis}.",
    )
    parser.add_argument(
        "--cli-analyzer",
        type=str,
        default=config.get("cli_analyzer"),
        metavar="NAME",
        help="CLI tool for analyzer role. Overrides --cli for analyze/design/plan. "
        f"Available: {available_clis}.",
    )
    parser.add_argument(
        "--cli-release-eng",
        type=str,
        default=config.get("cli_release_eng"),
        metavar="NAME",
        help="CLI tool for release engineering role. Overrides --cli for release preparation. "
        f"Available: {available_clis}.",
    )
    parser.add_argument(
        "--cli-sre",
        type=str,
        default=config.get("cli_sre"),
        metavar="NAME",
        help="CLI tool for SRE role. Overrides --cli for incident diagnosis. "
        f"Available: {available_clis}.",
    )
    parser.add_argument(
        "--compact-threshold",
        type=int,
        default=config["compact_threshold"],
        metavar="N",
        help="Number of completed tasks that triggers automatic tasklist compaction. "
        "When the count of completed tasks (- [x]) reaches this threshold, "
        "runs a compaction step before the next build cycle to reduce token usage. "
        f"Set to 0 to disable automatic compaction. (default: {config['compact_threshold']})",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Force immediate compaction of completed tasks in the tasklist "
        "to reduce token usage, regardless of --compact-threshold. Useful for manual cleanup "
        "of long-running tasklists.",
    )
    parser.add_argument(
        "--review-diff",
        type=str,
        metavar="PATH",
        help="Perform a pre-merge QA review on a diff file. Outputs verdict and feedback.",
    )
    parser.add_argument(
        "--prepare-release",
        action="store_true",
        help="Automatically prepare a new release based on completed tasks. Updates changelog and creates a git tag.",
    )
    parser.add_argument(
        "--sre",
        action="store_true",
        help="Run SRE diagnosis loop. Analyzes alerts.json and infrastructure manifest to propose mitigation.",
    )
    parser.add_argument(
        "--continue",
        dest="continue_run",
        action="store_true",
        help="Resume from saved state after a halt. Handles two cases: (1) inner-loop halts "
        "due to LoC threshold or sensitive file detection — resumes the paused task without "
        "re-running mechanical checks; (2) outer-loop interruptions — resumes an interrupted "
        "analyze/design/plan cycle from the last completed stage (e.g. if design finished but "
        "planning was interrupted, --continue picks up at the plan step). State is stored in "
        ".millstone/state.json and cleared after successful completion.",
    )
    parser.add_argument(
        "--session",
        type=str,
        default=config.get("session_mode", "new_each_task"),
        metavar="MODE",
        help="Session persistence mode for builder/reviewer. Accepts: "
        "'new' or 'new_each_task' (fresh session each task, default), "
        "'continue_within_run' (preserve session for all tasks in single invocation), "
        "'continue' or 'continue_across_runs' (resume session from .millstone/state.json), "
        "or a specific session ID to resume. Config file uses session_mode key.",
    )
    parser.add_argument(
        "--clear-sessions",
        action="store_true",
        help="Clear stored session IDs from .millstone/state.json and exit. "
        "Use this to reset session continuity when sessions become invalid or stale. "
        "Sessions are also auto-cleared if they are older than 24 hours.",
    )
    parser.add_argument(
        "--eval",
        action="store_true",
        help="Run tests and capture results. Executes pytest, parses output, and stores "
        "structured results in .millstone/evals/<timestamp>.json. Prints a human-readable "
        "summary to stdout. Exits 0 if all tests pass, 1 if any tests fail.",
    )
    parser.add_argument(
        "--cov",
        action="store_true",
        help="When used with --eval, also collect coverage data. Runs pytest with "
        "--cov and --cov-report=json, then includes coverage metrics in the eval results.",
    )
    parser.add_argument(
        "--eval-compare",
        action="store_true",
        help="Compare the two most recent eval results and report changes. "
        "Shows tests that started failing, tests that started passing, coverage delta, "
        "and duration delta. Exits 0 if no new failures, 1 if regressions detected. "
        "Requires at least 2 eval files in .millstone/evals/.",
    )
    parser.add_argument(
        "--eval-summary",
        action="store_true",
        help="Show cost-normalized improvement summary across recent tasks. "
        "Displays per-task metrics including duration, token usage, cycles, and eval deltas. "
        "Useful for understanding ROI of different task types. "
        "Reads data from .millstone/tasks/.",
    )
    parser.add_argument(
        "--metrics-report",
        action="store_true",
        help="Generate summary report from review metrics. Shows approval rate, average cycles "
        "to approval, common finding categories, and reviewer comparison (if multiple CLIs used). "
        "Reads data from .millstone/metrics/reviews.jsonl.",
    )
    parser.add_argument(
        "--analyze-tasklist",
        action="store_true",
        help="Analyze the tasklist and report: task count, estimated complexity per task "
        "(simple/medium/complex based on file references and keywords), suggested task ordering, "
        "and potential dependencies between tasks. Useful for planning and understanding "
        "the scope of pending work.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show tasklist status with progress estimation. Displays pending task count, "
        "complexity breakdown, and estimated remaining time based on historical task metrics. "
        "This is an alias for --analyze-tasklist that focuses on progress tracking.",
    )
    parser.add_argument(
        "--split-task",
        type=int,
        default=None,
        metavar="N",
        help="Interactive task splitting. Analyzes task number N from the tasklist and suggests "
        "how to break it down into smaller, more atomic subtasks based on file/component boundaries. "
        "The agent examines the task description and referenced files, then proposes a breakdown. "
        "Task numbers are 1-indexed and match the order shown by --analyze-tasklist. "
        "Example: --split-task 3",
    )
    parser.add_argument(
        "--eval-on-commit",
        action="store_true",
        default=config.get("eval_on_commit", False),
        help="Run evals automatically after each commit. Before starting, captures a baseline "
        "eval. After each successful commit, runs eval and compares against the baseline. "
        "If new test failures are introduced, halts with an error message. Pre-existing test "
        "failures do not block operation. (default: from config or False)",
    )
    parser.add_argument(
        "--auto-rollback",
        action="store_true",
        default=config.get("auto_rollback", False),
        help="Auto-revert commits when eval regression is detected. When used with --eval-on-commit, "
        "if the composite score drops by more than policy.eval.max_regression (default 0.05), "
        "the commit is automatically reverted. Without this flag, a prompt is shown asking whether "
        "to revert. Rollback context is saved for the next cycle. (default: from config or False)",
    )
    parser.add_argument(
        "--eval-on-task",
        type=str,
        default=config.get("eval_on_task", "none"),
        metavar="MODE",
        help="Run eval suite after each approved task. MODE can be: 'none' (disabled, default), "
        "'smoke' (quick tests with fail-fast, no coverage), 'full' (all tests with coverage), "
        "or a path to a custom test suite/script (e.g., 'tests/smoke/', 'scripts/eval.sh'). "
        "Before starting, captures a baseline eval using the specified mode. After each task approval, "
        "runs eval and compares against baseline. If new test failures are introduced, halts. "
        "(default: from config or 'none')",
    )
    parser.add_argument(
        "--skip-eval",
        action="store_true",
        default=False,
        help="Skip the eval gate for this run, even if --eval-on-task is configured. "
        "Useful for documentation-only changes or other cases where running evals is unnecessary. "
        "When set, no baseline eval is captured and the eval gate check is bypassed.",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Run the analysis agent to scan for improvement opportunities. "
        "The agent examines the codebase for code quality issues, test gaps, "
        "documentation gaps, architecture issues, performance issues, and security issues. "
        "The output is gated through an iterative write/review/fix loop: a reviewer checks "
        "the analysis and requests revisions until it approves or --max-cycles is exhausted. "
        "Results are written to opportunities.md in the repo root. Exits 0 on success. "
        "(Supersedes prior behavior: --analyze previously ran once with no review step; "
        "it now runs the same iterative write/review/fix loop as --design and --plan.)",
    )
    parser.add_argument(
        "--issues",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to a file containing known issues to consider during analysis. "
        "Used with --analyze to incorporate external issue reports (bug reports, feature "
        "requests) into the analysis. The file format is flexible (markdown with issue "
        "descriptions). The analysis agent will deduplicate findings that match known issues.",
    )
    parser.add_argument(
        "--design",
        type=str,
        default=None,
        metavar="OPPORTUNITY",
        help="Run the design agent to create an implementation spec for an opportunity. "
        "The agent analyzes the opportunity, explores options, and writes a design document "
        "to designs/<slug>.md. The slug is a kebab-case version of the opportunity title. "
        "The output is gated through an iterative write/review/fix loop: a reviewer checks "
        "the design and requests revisions until it approves or --max-cycles is exhausted. "
        "Example: --design 'Add retry logic to API calls'",
    )
    parser.add_argument(
        "--review-design",
        type=str,
        default=None,
        metavar="PATH",
        help="Review a design document for quality and completeness. "
        "Evaluates the design against criteria like measurable success criteria, "
        "completeness, alternatives considered, and appropriate scoping. "
        "Uses the haiku model for cost efficiency. "
        "Exits 0 if APPROVED, 1 if NEEDS_REVISION. "
        "Example: --review-design designs/add-retry-logic.md",
    )
    parser.add_argument(
        "--plan",
        type=str,
        default=None,
        metavar="PATH",
        help="Run the planning agent to break a design into executable tasks. "
        "Reads the design document and current tasklist, then appends a sequence "
        "of atomic, testable tasks to the tasklist. Tasks are ordered by dependency "
        "and self-contained so the builder can implement each independently. "
        "The output is gated through an iterative write/review/fix loop: a reviewer checks "
        "the task breakdown and requests revisions until it approves or --max-cycles is exhausted. "
        "Exits 0 if tasks were added, 1 if no tasks were added. "
        "Example: --plan designs/add-retry-logic.md",
    )
    parser.add_argument(
        "--deliver",
        type=str,
        default=None,
        metavar="OBJECTIVE",
        help="Run design -> optional design review -> plan -> execute for one objective. "
        "This explicitly skips analyze, creates --tasklist if missing for file-backed tasklists, "
        "and halts if pending tasks already exist to avoid mixing scopes. "
        "Example: --deliver 'Add retry logic to API client'",
    )
    parser.add_argument(
        "--cycle",
        action="store_true",
        help="Run the full autonomous cycle: analyze → design → plan → build → eval. "
        "If there are pending tasks in the tasklist, skips straight to executing them. "
        "If no pending tasks, runs analysis to find opportunities, designs a solution "
        "for the top High Priority opportunity, breaks it into tasks, then executes. "
        "Exits 0 on success, 1 on failure or if halted for human review.",
    )
    parser.add_argument(
        "--no-approve",
        action="store_true",
        help="Disable approval gates for fully autonomous operation. By default, --cycle "
        "pauses at each phase (after analyze, design, plan) for human review. This flag "
        "sets approve_opportunities, approve_designs, and approve_plans to False, allowing "
        "the cycle to run without human intervention. Use with caution in trusted/low-risk scenarios.",
    )
    parser.add_argument(
        "--complete",
        action="store_true",
        help="When combined with --plan, --design, or --analyze, continue executing all "
        "remaining outer-loop stages through to task implementation. "
        "--plan file.md --complete runs plan then executes all resulting tasks. "
        "--design 'objective' --complete runs design, plan, then executes. "
        "--analyze --complete runs analyze, selects top opportunity, then design, plan, execute.",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Scaffold a new millstone project. Detects project type (Python/Node/Go/Rust), "
        "prompts for test command and CLI tool, then writes .millstone/config.toml and "
        ".millstone/tasklist.md. Refuses to overwrite an existing config unless --force is set.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Non-interactive mode for --init. Accepts all detected defaults without prompting.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force overwrite of existing .millstone/config.toml when using --init.",
    )
    args = parser.parse_args()

    # Handle --init: scaffold project and exit (before any tasklist checks)
    if args.init:
        from millstone.commands.init import run_init

        repo_dir = Path(args.repo_dir) if args.repo_dir else None
        sys.exit(run_init(yes=args.yes, force=args.force, repo_dir=repo_dir))

    # Validate --compact is not used with --task
    if args.compact and args.task:
        parser.error("--compact cannot be used with --task. Compaction operates on tasklist files.")

    # Validate --cov requires --eval
    if args.cov and not args.eval:
        parser.error("--cov requires --eval. Use: --eval --cov")

    # Validate --issues requires --analyze
    if args.issues and not args.analyze:
        parser.error("--issues requires --analyze. Use: --analyze --issues PATH")

    # Validate --deliver exclusivity with other outer-loop entrypoints
    if args.deliver and (
        args.analyze or args.design or args.plan or args.cycle or args.review_design
    ):
        parser.error(
            "--deliver cannot be combined with --analyze, --design, --review-design, --plan, or --cycle."
        )

    # Validate --complete usage
    if args.complete and not (args.plan or args.design or args.analyze):
        parser.error("--complete requires --plan, --design, or --analyze")
    if args.complete and (args.deliver or args.cycle):
        parser.error("--complete cannot be used with --deliver or --cycle")

    # Preserve config values not exposed as CLI args (CLI overrides config)
    prompts_dir = args.prompts_dir if args.prompts_dir else config.get("prompts_dir")
    eval_scripts = config.get("eval_scripts", [])
    # Compute effective log_verbosity: --verbose flag overrides config
    log_verbosity = "verbose" if args.verbose else config.get("log_verbosity", "normal")
    _configure_python_logging_for_verbosity(log_verbosity)
    # Compute effective log_diff_mode: --full-diff flag overrides config
    log_diff_mode = "full" if args.full_diff else config.get("log_diff_mode", "summary")

    # Handle --migrate-tasklist: normalize a local backlog into markdown checklist format
    if args.migrate_tasklist:
        try:
            source_path = Path(args.migrate_tasklist)
            output_path = Path(args.tasklist)
            result = _migrate_local_backlog(source_path=source_path, output_path=output_path)
            print("Tasklist migration complete:")
            print(f"  Source: {result['source_path']}")
            print(f"  Output: {result['output_path']}")
            print(
                f"  Tasks: {result['task_count']} total "
                f"({result['pending_count']} pending, {result['completed_count']} completed)"
            )
            print()
            print("Next step:")
            print("  millstone")
            sys.exit(0)
        except Exception as e:
            print(f"Error migrating tasklist: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle --clear-sessions: clear stored session IDs and exit
    if args.clear_sessions:
        # Minimal orchestrator for clear-sessions - just needs work directory
        orchestrator = Orchestrator(
            task="clear-sessions",  # Dummy task to avoid tasklist requirement
            dry_run=False,
            quiet=True,  # Suppress startup banner for utility command
            log_verbosity=log_verbosity,
            log_diff_mode=log_diff_mode,
        )
        try:
            if orchestrator.clear_sessions():
                print("Session IDs cleared from .millstone/state.json")
            else:
                print("No session IDs to clear (state file not found or no sessions stored).")
            sys.exit(0)
        except Exception as e:
            print(f"Error clearing sessions: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle --eval: run tests and exit (before tasklist check since it doesn't need tasklist)
    if args.eval:
        # Minimal orchestrator for eval - just needs work directory
        orchestrator = Orchestrator(
            task="eval",  # Dummy task to avoid tasklist requirement
            dry_run=False,
            eval_scripts=eval_scripts,
            quiet=True,  # Suppress startup banner for utility command
            log_verbosity=log_verbosity,
            log_diff_mode=log_diff_mode,
        )
        try:
            eval_result = orchestrator.run_eval(coverage=args.cov)
            sys.exit(0 if eval_result.get("_passed", False) else 1)
        except Exception as e:
            print(f"Error running eval: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle --eval-compare: compare eval results and exit
    if args.eval_compare:
        # Minimal orchestrator for eval-compare - just needs work directory
        orchestrator = Orchestrator(
            task="eval-compare",  # Dummy task to avoid tasklist requirement
            dry_run=False,
            quiet=True,  # Suppress startup banner for utility command
            log_verbosity=log_verbosity,
            log_diff_mode=log_diff_mode,
        )
        try:
            result = orchestrator.compare_evals()
            sys.exit(1 if result.get("_has_regressions", False) else 0)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error comparing evals: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle --eval-summary: show cost-normalized improvement summary
    if args.eval_summary:
        # Minimal orchestrator for eval-summary - just needs work directory
        orchestrator = Orchestrator(
            task="eval-summary",  # Dummy task to avoid tasklist requirement
            dry_run=False,
            quiet=True,  # Suppress startup banner for utility command
            log_verbosity=log_verbosity,
            log_diff_mode=log_diff_mode,
        )
        try:
            orchestrator.print_eval_summary()
            sys.exit(0)
        except Exception as e:
            print(f"Error showing eval summary: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle --metrics-report: generate review metrics summary
    if args.metrics_report:
        # Minimal orchestrator for metrics-report - just needs work directory
        orchestrator = Orchestrator(
            task="metrics-report",  # Dummy task to avoid tasklist requirement
            dry_run=False,
            quiet=True,  # Suppress startup banner for utility command
            log_verbosity=log_verbosity,
            log_diff_mode=log_diff_mode,
        )
        try:
            orchestrator.print_metrics_report()
            sys.exit(0)
        except Exception as e:
            print(f"Error showing metrics report: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle --analyze-tasklist or --status: analyze tasklist and report statistics
    if args.analyze_tasklist or args.status:
        # Orchestrator needs tasklist path to analyze
        orchestrator = Orchestrator(
            tasklist=args.tasklist,
            dry_run=False,
            quiet=True,  # Suppress startup banner for utility command
            log_verbosity=log_verbosity,
            log_diff_mode=log_diff_mode,
        )
        try:
            orchestrator.analyze_tasklist()
            sys.exit(0)
        except Exception as e:
            print(f"Error analyzing tasklist: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle --split-task: analyze a task and suggest subtasks
    if args.split_task is not None:
        using_remote_provider = config.get("tasklist_provider", "file") != "file"
        if using_remote_provider:
            print(
                "Task splitting is not supported for remote providers.",
                file=sys.stderr,
            )
            print(
                "Use your provider's native interface to manage tasks.",
                file=sys.stderr,
            )
            sys.exit(1)
        orchestrator = Orchestrator(
            tasklist=args.tasklist,
            dry_run=False,
            cli=args.cli,
            cli_analyzer=args.cli_analyzer,
            log_verbosity=log_verbosity,
            log_diff_mode=log_diff_mode,
        )
        try:
            result = orchestrator.split_task(task_number=args.split_task)
            sys.exit(0 if result.get("success", False) else 1)
        except Exception as e:
            print(f"Error splitting task: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle --analyze: run analysis agent and exit (or continue with --complete)
    if args.analyze:
        # Minimal orchestrator for analyze - just needs work directory
        orchestrator = Orchestrator(
            task="analyze",  # Dummy task to avoid tasklist requirement
            dry_run=False,
            max_cycles=args.max_cycles,
            profile=config.get("profile", "dev_implementation"),
            cli=args.cli,
            cli_analyzer=args.cli_analyzer,
            log_verbosity=log_verbosity,
            log_diff_mode=log_diff_mode,
        )
        try:
            result = orchestrator.run_analyze(issues_file=args.issues)
            if not result.get("success", False):
                sys.exit(1)
            if not args.complete:
                sys.exit(0)
            # --complete: select top opportunity and chain through design → plan → execute
            selected = orchestrator._select_opportunity()
            if not selected:
                print("No opportunities found. Nothing to do.")
                sys.exit(0)
            # Resolve approval gates (same logic as --cycle)
            if args.no_approve:
                _approve_opportunities = False
                _approve_designs = False
                _approve_plans = False
            else:
                _approve_opportunities = config.get("approve_opportunities", True)
                _approve_designs = config.get("approve_designs", True)
                _approve_plans = config.get("approve_plans", True)
            # Approval gate: pause after analyze for human to pick opportunity
            if _approve_opportunities:
                print("")
                print("=" * 60)
                print("APPROVAL GATE: Opportunities identified")
                print("=" * 60)
                print("")
                print(f"Selected opportunity: {selected.title}")
                orchestrator.save_outer_loop_checkpoint(
                    "analyze_complete", opportunity=selected.title
                )
                print("Review opportunities.md and re-run with:")
                print("  millstone --continue")
                print("")
                print("Or run with --no-approve for fully autonomous operation.")
                sys.exit(0)
            review_designs = config.get("review_designs", True)
            using_remote_provider = config.get("tasklist_provider", "file") != "file"
            if not using_remote_provider:
                _ensure_tasklist_file(Path.cwd() / args.tasklist)
            full_orchestrator = Orchestrator(
                max_cycles=args.max_cycles,
                loc_threshold=args.loc_threshold,
                tasklist=args.tasklist,
                max_tasks=args.max_tasks,
                dry_run=args.dry_run,
                prompts_dir=prompts_dir,
                compact_threshold=args.compact_threshold,
                eval_on_commit=args.eval_on_commit,
                auto_rollback=args.auto_rollback,
                eval_scripts=eval_scripts,
                eval_on_task=args.eval_on_task,
                skip_eval=args.skip_eval,
                review_designs=review_designs,
                profile=config.get("profile", "dev_implementation"),
                cli=args.cli,
                cli_builder=args.cli_builder,
                cli_reviewer=args.cli_reviewer,
                cli_sanity=args.cli_sanity,
                cli_analyzer=args.cli_analyzer,
                log_verbosity=log_verbosity,
                log_diff_mode=log_diff_mode,
            )
            design_result = full_orchestrator.run_design(opportunity=selected.title)
            if not design_result.get("success", False):
                sys.exit(1)
            design_ref = design_result.get("design_file") or design_result.get("design_id")
            if not design_ref:
                print("Error: design did not return a usable design reference.", file=sys.stderr)
                sys.exit(1)
            if review_designs:
                review_result = full_orchestrator.review_design(str(design_ref))
                if not review_result.get("approved", False):
                    sys.exit(1)
            # Approval gate: pause after design for human review
            if _approve_designs:
                print("")
                print("=" * 60)
                print("APPROVAL GATE: Design created")
                print("=" * 60)
                print("")
                print(f"Review the design: {design_ref}")
                full_orchestrator.save_outer_loop_checkpoint(
                    "design_complete",
                    design_path=str(design_ref),
                    opportunity=selected.title,
                )
                print("Then re-run with:")
                print("  millstone --continue")
                print("")
                print("Or run with --no-approve for fully autonomous operation.")
                sys.exit(0)
            plan_result = full_orchestrator.run_plan(design_path=str(design_ref))
            if not plan_result.get("success", False):
                sys.exit(1)
            if not plan_result.get("tasks_added", 0):
                print("No tasks were created by the planning agent.")
                sys.exit(0)
            # Approval gate: pause after plan for human review
            if _approve_plans:
                print("")
                print("=" * 60)
                print("APPROVAL GATE: Tasks added to tasklist")
                print("=" * 60)
                print("")
                print(f"Review the new tasks in: {args.tasklist}")
                full_orchestrator.save_outer_loop_checkpoint(
                    "plan_complete",
                    design_path=str(design_ref),
                    tasks_created=plan_result.get("tasks_added", 0),
                )
                print("Then re-run to execute:")
                print("  millstone --continue")
                print("")
                print("Or run with --no-approve for fully autonomous operation.")
                sys.exit(0)
            sys.exit(full_orchestrator.run())
        except Exception as e:
            print(f"Error running analysis: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle --design: run design agent and exit (or continue with --complete)
    if args.design:
        review_designs = config.get("review_designs", True)
        # Minimal orchestrator for design - just needs work directory
        orchestrator = Orchestrator(
            task="design",  # Dummy task to avoid tasklist requirement
            dry_run=False,
            max_cycles=args.max_cycles,
            review_designs=review_designs,
            profile=config.get("profile", "dev_implementation"),
            cli=args.cli,
            cli_analyzer=args.cli_analyzer,
            cli_sanity=args.cli_sanity,
            log_verbosity=log_verbosity,
            log_diff_mode=log_diff_mode,
        )
        try:
            result = orchestrator.run_design(opportunity=args.design)
            if not result.get("success", False):
                sys.exit(1)
            # If design was created and review_designs is enabled, automatically review it
            if review_designs and result.get("design_file"):
                review_result = orchestrator.review_design(result["design_file"])
                if not review_result.get("approved", False):
                    sys.exit(1)
            if not args.complete:
                sys.exit(0)
            # --complete: chain through plan → execute
            # Resolve approval gates (same logic as --cycle)
            if args.no_approve:
                _approve_designs = False
                _approve_plans = False
            else:
                _approve_designs = config.get("approve_designs", True)
                _approve_plans = config.get("approve_plans", True)
            design_ref = result.get("design_file") or result.get("design_id")
            if not design_ref:
                print("Error: design did not return a usable design reference.", file=sys.stderr)
                sys.exit(1)
            # Approval gate: pause after design for human review
            if _approve_designs:
                print("")
                print("=" * 60)
                print("APPROVAL GATE: Design created")
                print("=" * 60)
                print("")
                print(f"Review the design: {design_ref}")
                orchestrator.save_outer_loop_checkpoint(
                    "design_complete",
                    design_path=str(design_ref),
                    opportunity=args.design,
                )
                print("Then re-run with:")
                print("  millstone --continue")
                print("")
                print("Or run with --no-approve for fully autonomous operation.")
                sys.exit(0)
            using_remote_provider = config.get("tasklist_provider", "file") != "file"
            if not using_remote_provider:
                _ensure_tasklist_file(Path.cwd() / args.tasklist)
            full_orchestrator = Orchestrator(
                max_cycles=args.max_cycles,
                loc_threshold=args.loc_threshold,
                tasklist=args.tasklist,
                max_tasks=args.max_tasks,
                dry_run=args.dry_run,
                prompts_dir=prompts_dir,
                compact_threshold=args.compact_threshold,
                eval_on_commit=args.eval_on_commit,
                auto_rollback=args.auto_rollback,
                eval_scripts=eval_scripts,
                eval_on_task=args.eval_on_task,
                skip_eval=args.skip_eval,
                review_designs=review_designs,
                profile=config.get("profile", "dev_implementation"),
                cli=args.cli,
                cli_builder=args.cli_builder,
                cli_reviewer=args.cli_reviewer,
                cli_sanity=args.cli_sanity,
                cli_analyzer=args.cli_analyzer,
                log_verbosity=log_verbosity,
                log_diff_mode=log_diff_mode,
            )
            plan_result = full_orchestrator.run_plan(design_path=str(design_ref))
            if not plan_result.get("success", False):
                sys.exit(1)
            if not plan_result.get("tasks_added", 0):
                print("No tasks were created by the planning agent.")
                sys.exit(0)
            # Approval gate: pause after plan for human review
            if _approve_plans:
                print("")
                print("=" * 60)
                print("APPROVAL GATE: Tasks added to tasklist")
                print("=" * 60)
                print("")
                print(f"Review the new tasks in: {args.tasklist}")
                full_orchestrator.save_outer_loop_checkpoint(
                    "plan_complete",
                    design_path=str(design_ref),
                    tasks_created=plan_result.get("tasks_added", 0),
                )
                print("Then re-run to execute:")
                print("  millstone --continue")
                print("")
                print("Or run with --no-approve for fully autonomous operation.")
                sys.exit(0)
            sys.exit(full_orchestrator.run())
        except Exception as e:
            print(f"Error running design: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle --review-design: review a design document and exit
    if args.review_design:
        # Minimal orchestrator for review-design - just needs work directory
        orchestrator = Orchestrator(
            task="review-design",  # Dummy task to avoid tasklist requirement
            dry_run=False,
            profile=config.get("profile", "dev_implementation"),
            cli=args.cli,
            cli_sanity=args.cli_sanity,
            log_verbosity=log_verbosity,
            log_diff_mode=log_diff_mode,
        )
        try:
            result = orchestrator.review_design(design_path=args.review_design)
            sys.exit(0 if result.get("approved", False) else 1)
        except Exception as e:
            print(f"Error reviewing design: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle --plan: run planning agent and exit (or continue with --complete)
    if args.plan:
        # Plan needs tasklist, so use the configured one
        orchestrator = Orchestrator(
            tasklist=args.tasklist,
            dry_run=False,
            max_cycles=args.max_cycles,
            profile=config.get("profile", "dev_implementation"),
            cli=args.cli,
            cli_analyzer=args.cli_analyzer,
            log_verbosity=log_verbosity,
            log_diff_mode=log_diff_mode,
        )
        try:
            result = orchestrator.run_plan(design_path=args.plan)
            if not result.get("success", False):
                sys.exit(1)
            if not args.complete:
                sys.exit(0)
            # --complete: chain through execute
            if not result.get("tasks_added", 0):
                print("No tasks were created by the planning agent.")
                sys.exit(0)
            # Resolve approval gates (same logic as --cycle)
            _approve_plans = False if args.no_approve else config.get("approve_plans", True)
            # Approval gate: pause after plan for human review
            if _approve_plans:
                print("")
                print("=" * 60)
                print("APPROVAL GATE: Tasks added to tasklist")
                print("=" * 60)
                print("")
                print(f"Review the new tasks in: {args.tasklist}")
                orchestrator.save_outer_loop_checkpoint(
                    "plan_complete",
                    design_path=args.plan,
                    tasks_created=result.get("tasks_added", 0),
                )
                print("Then re-run to execute:")
                print("  millstone --continue")
                print("")
                print("Or run with --no-approve for fully autonomous operation.")
                sys.exit(0)
            using_remote_provider = config.get("tasklist_provider", "file") != "file"
            if not using_remote_provider:
                _ensure_tasklist_file(Path.cwd() / args.tasklist)
            full_orchestrator = Orchestrator(
                max_cycles=args.max_cycles,
                loc_threshold=args.loc_threshold,
                tasklist=args.tasklist,
                max_tasks=args.max_tasks,
                dry_run=args.dry_run,
                prompts_dir=prompts_dir,
                compact_threshold=args.compact_threshold,
                eval_on_commit=args.eval_on_commit,
                auto_rollback=args.auto_rollback,
                eval_scripts=eval_scripts,
                eval_on_task=args.eval_on_task,
                skip_eval=args.skip_eval,
                profile=config.get("profile", "dev_implementation"),
                cli=args.cli,
                cli_builder=args.cli_builder,
                cli_reviewer=args.cli_reviewer,
                cli_sanity=args.cli_sanity,
                cli_analyzer=args.cli_analyzer,
                log_verbosity=log_verbosity,
                log_diff_mode=log_diff_mode,
            )
            sys.exit(full_orchestrator.run())
        except Exception as e:
            print(f"Error running plan: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle --deliver: design -> review(optional) -> plan -> execute
    if args.deliver:
        review_designs = config.get("review_designs", True)
        using_remote_provider = config.get("tasklist_provider", "file") != "file"
        if not using_remote_provider:
            _ensure_tasklist_file(Path.cwd() / args.tasklist)

        orchestrator = Orchestrator(
            max_cycles=args.max_cycles,
            loc_threshold=args.loc_threshold,
            tasklist=args.tasklist,
            max_tasks=args.max_tasks,
            dry_run=args.dry_run,
            prompts_dir=prompts_dir,
            compact_threshold=args.compact_threshold,
            eval_on_commit=args.eval_on_commit,
            auto_rollback=args.auto_rollback,
            eval_scripts=eval_scripts,
            eval_on_task=args.eval_on_task,
            skip_eval=args.skip_eval,
            review_designs=review_designs,
            profile=config.get("profile", "dev_implementation"),
            cli=args.cli,
            cli_builder=args.cli_builder,
            cli_reviewer=args.cli_reviewer,
            cli_sanity=args.cli_sanity,
            cli_analyzer=args.cli_analyzer,
            log_verbosity=log_verbosity,
            log_diff_mode=log_diff_mode,
        )
        try:
            orchestrator.preflight_checks()

            if not using_remote_provider and orchestrator.has_remaining_tasks():
                print(
                    "Error: --deliver requires an empty pending tasklist so the new objective "
                    "does not mix with existing backlog tasks.",
                    file=sys.stderr,
                )
                print(
                    "Run `millstone` to finish existing tasks first, or point `--tasklist` "
                    "to a fresh file for this objective.",
                    file=sys.stderr,
                )
                sys.exit(1)

            design_result = orchestrator.run_design(opportunity=args.deliver)
            if not design_result.get("success", False):
                sys.exit(1)

            design_ref = design_result.get("design_file") or design_result.get("design_id")
            if not design_ref:
                print("Error: design did not return a usable design reference.", file=sys.stderr)
                sys.exit(1)

            if review_designs:
                review_result = orchestrator.review_design(str(design_ref))
                if not review_result.get("approved", False):
                    sys.exit(1)

            plan_result = orchestrator.run_plan(design_path=str(design_ref))
            if not plan_result.get("success", False):
                sys.exit(1)

            sys.exit(orchestrator.run())
        except PreflightError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error running deliver flow: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle --cycle: run full autonomous cycle and exit
    if args.cycle:
        review_designs = config.get("review_designs", True)
        # Approval gates: use config values unless --no-approve is set
        if args.no_approve:
            approve_opportunities = False
            approve_designs = False
            approve_plans = False
        else:
            approve_opportunities = config.get("approve_opportunities", True)
            approve_designs = config.get("approve_designs", True)
            approve_plans = config.get("approve_plans", True)
        orchestrator = Orchestrator(
            max_cycles=args.max_cycles,
            loc_threshold=args.loc_threshold,
            tasklist=args.tasklist,
            max_tasks=args.max_tasks,
            dry_run=args.dry_run,
            prompts_dir=prompts_dir,
            compact_threshold=args.compact_threshold,
            eval_on_commit=args.eval_on_commit,
            auto_rollback=args.auto_rollback,
            eval_scripts=eval_scripts,
            eval_on_task=args.eval_on_task,
            skip_eval=args.skip_eval,
            review_designs=review_designs,
            approve_opportunities=approve_opportunities,
            approve_designs=approve_designs,
            approve_plans=approve_plans,
            profile=config.get("profile", "dev_implementation"),
            cli=args.cli,
            cli_builder=args.cli_builder,
            cli_reviewer=args.cli_reviewer,
            cli_sanity=args.cli_sanity,
            cli_analyzer=args.cli_analyzer,
            log_verbosity=log_verbosity,
            log_diff_mode=log_diff_mode,
        )
        try:
            orchestrator.preflight_checks()
            sys.exit(orchestrator.run_cycle())
        except PreflightError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error running cycle: {e}", file=sys.stderr)
            sys.exit(1)

    # Early check for missing tasklist when no task is specified (or when --compact is used)
    # This provides a helpful usage message before any setup occurs.
    # Skip when using a remote tasklist provider (e.g. MCP) — no local file required.
    _using_remote_provider = config.get("tasklist_provider", "file") != "file"
    if (not args.task or args.compact) and not _using_remote_provider:
        tasklist_path = Path.cwd() / args.tasklist
        if not tasklist_path.exists():
            print(f"Error: Tasklist file not found: {tasklist_path}", file=sys.stderr)
            print(file=sys.stderr)
            print("To get started, either:", file=sys.stderr)
            print(
                f"  1. Create {args.tasklist} with tasks in markdown checkbox format:",
                file=sys.stderr,
            )
            print("       - [ ] First task to complete", file=sys.stderr)
            print("       - [ ] Second task to complete", file=sys.stderr)
            print(file=sys.stderr)
            print("  2. Specify a task directly:", file=sys.stderr)
            print("       python orchestrate.py --task 'implement feature X'", file=sys.stderr)
            print(file=sys.stderr)
            print("  3. Use a different tasklist file:", file=sys.stderr)
            print("       python orchestrate.py --tasklist path/to/tasks.md", file=sys.stderr)
            sys.exit(1)

    # Worktree control-plane enablement: if --shared-state-dir is set, this process
    # is a worker and must not re-enter the control plane even if config enables it.
    parallel_enabled = bool(args.worktrees)
    if args.shared_state_dir:
        parallel_enabled = False

    _review_designs = config.get("review_designs", True)
    if args.no_approve:
        _approve_opportunities = False
        _approve_designs = False
        _approve_plans = False
    else:
        _approve_opportunities = config.get("approve_opportunities", True)
        _approve_designs = config.get("approve_designs", True)
        _approve_plans = config.get("approve_plans", True)

    orchestrator = Orchestrator(
        max_cycles=args.max_cycles,
        loc_threshold=args.loc_threshold,
        repo_dir=args.repo_dir,
        task=args.task,
        tasklist=args.tasklist,
        roadmap=args.roadmap,
        max_tasks=args.max_tasks,
        dry_run=args.dry_run,
        research=args.research,
        prompts_dir=prompts_dir,
        compact_threshold=args.compact_threshold,
        continue_run=args.continue_run,
        session_mode=args.session,
        eval_on_commit=args.eval_on_commit,
        auto_rollback=args.auto_rollback,
        eval_scripts=eval_scripts,
        eval_on_task=args.eval_on_task,
        skip_eval=args.skip_eval,
        review_designs=_review_designs,
        approve_opportunities=_approve_opportunities,
        approve_designs=_approve_designs,
        approve_plans=_approve_plans,
        parallel_enabled=parallel_enabled,
        parallel_concurrency=args.concurrency,
        base_branch=args.base_branch,
        base_ref=args.base_ref,
        integration_branch=args.integration_branch,
        merge_strategy=args.merge_strategy,
        worktree_root=args.worktree_root,
        shared_state_dir=args.shared_state_dir,
        merge_max_retries=args.merge_max_retries,
        worktree_cleanup=args.worktree_cleanup,
        no_tasklist_edits=args.no_tasklist_edits,
        high_risk_concurrency=args.high_risk_concurrency,
        profile=config.get("profile", "dev_implementation"),
        cli=args.cli,
        cli_builder=args.cli_builder,
        cli_reviewer=args.cli_reviewer,
        cli_sanity=args.cli_sanity,
        cli_analyzer=args.cli_analyzer,
        cli_release_eng=args.cli_release_eng,
        cli_sre=args.cli_sre,
        log_verbosity=log_verbosity,
        log_diff_mode=log_diff_mode,
    )
    try:
        # Handle --compact: force compaction and exit
        if args.compact:
            orchestrator.preflight_checks()
            orchestrator.completed_task_count = orchestrator.count_completed_tasks()
            if orchestrator.completed_task_count == 0:
                print("No completed tasks to compact.")
                sys.exit(0)
            success = orchestrator.run_compaction()
            sys.exit(0 if success else 1)

        # Handle --review-diff
        if args.review_diff:
            diff_path = Path(args.review_diff)
            if not diff_path.exists():
                print(f"Error: Diff file not found: {diff_path}", file=sys.stderr)
                sys.exit(1)
            diff_content = diff_path.read_text()
            result = orchestrator.run_review_diff(diff_content)
            print(result["output"])
            sys.exit(0 if result["approved"] else 1)

        # Handle --prepare-release
        if args.prepare_release:
            result = orchestrator.run_prepare_release()
            print("Release prepared successfully.")
            if result.get("tag"):
                print(f"Created git tag: {result['tag']}")
            sys.exit(0)

        # Handle --sre
        if args.sre:
            result = orchestrator.run_sre_diagnose()
            print(result["mitigation_plan"])
            sys.exit(0)

        sys.exit(orchestrator.run())
    except PreflightError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
