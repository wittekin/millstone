"""
Outer loop management for the millstone orchestrator.

This module contains the OuterLoopManager class which handles the self-direction
functionality (analyze, design, plan, cycle). The Orchestrator class holds an
instance and delegates via thin wrapper methods.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from millstone.artifact_providers.file import FileDesignProvider, FileOpportunityProvider
from millstone.artifact_providers.protocols import (
    DesignProvider,
    OpportunityProvider,
    TasklistProvider,
)
from millstone.artifact_providers.registry import (
    get_design_provider,
    get_opportunity_provider,
    get_tasklist_provider,
)
from millstone.artifacts.models import DesignStatus
from millstone.loops.engine import ArtifactReviewLoop
from millstone.policy.effects import EffectIntent, EffectPolicyGate, EffectRecord, EffectStatus
from millstone.policy.reference_integrity import (
    ReferenceIntegrityChecker,
    ReferenceIntegrityError,
)
from millstone.policy.schemas import DesignReviewResult
from millstone.prompts.utils import apply_provider_placeholders
from millstone.utils import progress

# Bootstrap MCP provider registration so that config-driven opt-in
# (tasklist_provider = "mcp" / design_provider = "mcp" in .millstone/config.toml)
# works without requiring callers to manually import the module.
with contextlib.suppress(Exception):
    import millstone.artifact_providers.mcp  # noqa: F401  (side-effect: registers "mcp" backends)
with contextlib.suppress(Exception):
    import millstone.artifact_providers.jira  # noqa: F401  (side-effect: registers "jira" backend)

if TYPE_CHECKING:
    # Avoid circular import - only used for type hints
    from millstone.artifacts.models import Opportunity

# For C2+ remote provider writes, construct intents like:
# EffectIntent(
#     effect_class=EffectClass.transactional,
#     description="<verb> <artifact_type> via <backend>",
#     idempotency_key=<artifact_id>,
#     rollback_plan="<how to undo>",
#     metadata={"backend": ..., "artifact_type": ..., "operation": ...},
# )


def _invalidate_tasklist_cache(provider: Any) -> None:
    """Call invalidate_cache() on the provider if it supports it (MCP providers do)."""
    invalidate = getattr(provider, "invalidate_cache", None)
    if callable(invalidate):
        invalidate()


class OuterLoopManager:
    """Manages outer loop operations for autonomous improvement cycles.

    This class handles operations related to:
    - Hard signal collection from automated tools
    - Analysis (finding improvement opportunities)
    - Design (creating implementation specs)
    - Design review
    - Planning (breaking designs into tasks)
    - Full autonomous cycle orchestration
    """

    def __init__(
        self,
        work_dir: Path,
        repo_dir: Path,
        tasklist: str,
        task_constraints: dict,
        roadmap: str | None = None,
        approve_opportunities: bool = True,
        approve_designs: bool = True,
        approve_plans: bool = True,
        review_designs: bool = True,
        max_cycles: int = 3,
        parse_task_metadata_callback: Callable[[str], dict] | None = None,
        opportunity_provider: OpportunityProvider | None = None,
        design_provider: DesignProvider | None = None,
        tasklist_provider: TasklistProvider | None = None,
        provider_config: dict | None = None,
        effect_gate: EffectPolicyGate | None = None,
        commit_opportunities: bool = False,
        commit_designs: bool = False,
    ):
        """Initialize the OuterLoopManager.

        Args:
            work_dir: Path to the work directory (.millstone/).
            repo_dir: Path to the repository root.
            tasklist: Path to the tasklist file (relative to repo_dir).
            task_constraints: Task atomizer constraints for run_plan().
            roadmap: Path to the roadmap file (relative to repo_dir).
            approve_opportunities: Pause after analyze for human review.
            approve_designs: Pause after design for human review.
            approve_plans: Pause after plan for human review.
            review_designs: Whether to review designs before implementation.
            max_cycles: Maximum review/fix cycles for outer-loop loops (default 3).
            parse_task_metadata_callback: Callback to parse task metadata from text.
                Should be TasklistManager._parse_task_metadata to avoid duplication.
            opportunity_provider: Optional pre-built provider override.
            design_provider: Optional pre-built provider override.
            tasklist_provider: Optional pre-built provider override.
            provider_config: Optional config source for provider backends/options.
            effect_gate: Optional effect-policy gate for C2+ provider writes.
        """
        self.work_dir = work_dir
        self.repo_dir = repo_dir
        self.tasklist = tasklist
        self.roadmap = roadmap
        self.task_constraints = task_constraints
        self.approve_opportunities = approve_opportunities
        self.approve_designs = approve_designs
        self.approve_plans = approve_plans
        self.review_designs = review_designs
        self.max_cycles = max_cycles
        self._parse_task_metadata_callback = parse_task_metadata_callback
        self._effect_gate = effect_gate
        config = provider_config or {}

        raw_opportunity_options = config.get("opportunity_provider_options", {})
        opportunity_options = (
            dict(raw_opportunity_options) if isinstance(raw_opportunity_options, dict) else {}
        )
        _opp_default = repo_dir / (
            "opportunities.md" if commit_opportunities else ".millstone/opportunities.md"
        )
        opportunity_options.setdefault("path", str(_opp_default))

        raw_design_options = config.get("design_provider_options", {})
        design_options = dict(raw_design_options) if isinstance(raw_design_options, dict) else {}
        _design_default = repo_dir / ("designs" if commit_designs else ".millstone/designs")
        design_options.setdefault("path", str(_design_default))

        raw_tasklist_options = config.get("tasklist_provider_options", {})
        tasklist_options = (
            dict(raw_tasklist_options) if isinstance(raw_tasklist_options, dict) else {}
        )
        tasklist_options.setdefault("path", str(repo_dir / tasklist))
        # Merge the provider-agnostic filter schema into tasklist options so that
        # each backend's from_config() can read it.  Explicit tasklist_provider_options
        # take precedence (setdefault only sets when absent).
        # Single-value shortcuts (label/assignee/status) expand to list forms when the
        # corresponding list key is absent or empty (empty list means "no constraint",
        # not "explicit empty filter"; use a non-empty list to suppress the shortcut).
        raw_filter = config.get("tasklist_filter", {})
        if isinstance(raw_filter, dict):
            labels = list(raw_filter.get("labels", []))
            label_shortcut = raw_filter.get("label", "")
            if not labels and isinstance(label_shortcut, str) and label_shortcut:
                labels = [label_shortcut]

            assignees = list(raw_filter.get("assignees", []))
            assignee_shortcut = raw_filter.get("assignee", "")
            if not assignees and isinstance(assignee_shortcut, str) and assignee_shortcut:
                assignees = [assignee_shortcut]

            statuses = list(raw_filter.get("statuses", []))
            status_shortcut = raw_filter.get("status", "")
            if not statuses and isinstance(status_shortcut, str) and status_shortcut:
                statuses = [status_shortcut]

            # Backend-specific keys forwarded as-is; each provider uses the keys it supports.
            milestone = raw_filter.get("milestone")  # GitHub
            cycles = list(raw_filter.get("cycles", []))  # Linear
            projects = list(raw_filter.get("projects", []))  # Linear
            project = raw_filter.get("project")  # Jira

            filter_dict: dict[str, Any] = {
                "labels": labels,
                "assignees": assignees,
                "statuses": statuses,
            }
            if milestone is not None:
                filter_dict["milestone"] = milestone
            if cycles:
                filter_dict["cycles"] = cycles
            if projects:
                filter_dict["projects"] = projects
            if project is not None:
                filter_dict["project"] = project

            tasklist_options.setdefault("filter", filter_dict)

        self.opportunity_provider: OpportunityProvider = (
            opportunity_provider
            if opportunity_provider is not None
            else get_opportunity_provider(
                backend=config.get("opportunity_provider", "file"),
                options=opportunity_options,
            )
        )
        self.design_provider: DesignProvider = (
            design_provider
            if design_provider is not None
            else get_design_provider(
                backend=config.get("design_provider", "file"),
                options=design_options,
            )
        )
        self.tasklist_provider: TasklistProvider = (
            tasklist_provider
            if tasklist_provider is not None
            else get_tasklist_provider(
                backend=config.get("tasklist_provider", "file"),
                options=tasklist_options,
            )
        )
        for provider in (
            self.opportunity_provider,
            self.design_provider,
            self.tasklist_provider,
        ):
            set_effect_applier = getattr(provider, "set_effect_applier", None)
            if callable(set_effect_applier):
                set_effect_applier(self._apply_provider_effect)
        # Cycle logging state
        self.cycle_log_file: Path | None = None
        self._cycle_start_time: datetime | None = None

    def _apply_provider_effect(self, intent: EffectIntent) -> EffectRecord:
        """Apply a provider-side effect with policy enforcement when configured."""
        if self._effect_gate is None:
            return EffectRecord(
                intent=intent,
                status=EffectStatus.skipped,
                timestamp=datetime.utcnow().isoformat() + "Z",
            )
        return self._effect_gate.apply(intent)

    def _inject_agent_callbacks(self, cb: Callable[..., str]) -> None:
        """Inject the agent callback into any MCP-backed providers that support it.

        Called at the top of each outer-loop method (run_analyze, run_design,
        run_plan) so that MCP providers receive the active agent callable before
        any write operation is attempted.  File and HTTP providers are unaffected
        since they do not expose ``set_agent_callback``.
        """
        for provider in (
            self.opportunity_provider,
            self.design_provider,
            self.tasklist_provider,
        ):
            set_cb = getattr(provider, "set_agent_callback", None)
            if callable(set_cb):
                set_cb(cb)

    # =========================================================================
    # Roadmap helpers
    # =========================================================================

    def _get_next_roadmap_goal(self) -> str | None:
        """Parse roadmap.md and return the first unchecked goal.

        Returns:
            String describing the goal, or None if roadmap doesn't exist or is empty.
        """
        if not self.roadmap:
            return None

        roadmap_path = self.repo_dir / self.roadmap
        if not roadmap_path.exists():
            return None

        content = roadmap_path.read_text()
        # Find the first line matching: - [ ] **Title**: Description
        # Or just - [ ] Task
        match = re.search(r"^- \[ \] (.*)", content, re.MULTILINE)
        if match:
            return match.group(1).strip()

        return None

    def _mark_roadmap_goal_complete(self, goal_text: str) -> None:
        """Mark a goal as complete in the roadmap file.

        Args:
            goal_text: The exact text of the goal to mark complete.
        """
        if not self.roadmap:
            return

        roadmap_path = self.repo_dir / self.roadmap
        if not roadmap_path.exists():
            return

        content = roadmap_path.read_text()
        # Escaping goal_text for regex
        escaped_goal = re.escape(goal_text)
        pattern = rf"^- \[ \] {escaped_goal}"
        replacement = f"- [x] {goal_text}"

        new_content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
        roadmap_path.write_text(new_content)
        progress(f"Marked roadmap goal as complete: {goal_text[:50]}...")

    # =========================================================================
    # Cycle logging helpers
    # =========================================================================

    def _setup_cycle_logging(self) -> None:
        """Set up cycle-specific logging for autonomous operation.

        Creates the cycles directory and initializes a new cycle log file.
        Called at the start of run_cycle() to track all decisions made during
        the autonomous cycle.
        """
        cycles_dir = self.work_dir / "cycles"
        cycles_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.cycle_log_file = cycles_dir / f"{timestamp}.log"
        self._cycle_start_time = datetime.now()

        # Write header
        with self.cycle_log_file.open("w") as f:
            f.write(f"=== Cycle Started: {self._cycle_start_time.isoformat()} ===\n\n")

    def _cycle_log(self, phase: str, message: str) -> None:
        """Log a cycle-level decision with timestamp.

        Args:
            phase: The phase of the cycle (e.g., "ANALYZE", "SELECT", "DESIGN")
            message: Description of what happened
        """
        if self.cycle_log_file is None:
            return

        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.cycle_log_file.open("a") as f:
            f.write(f"[{timestamp}] {phase}: {message}\n")

    def _cycle_log_complete(self, status: str) -> None:
        """Write the cycle completion footer.

        Args:
            status: Final status (e.g., "SUCCESS", "FAILED", "HALTED")
        """
        if self.cycle_log_file is None:
            return

        with self.cycle_log_file.open("a") as f:
            f.write(f"\n=== Cycle Completed: {status} ===\n")

    # =========================================================================
    # Hard signal collection
    # =========================================================================

    def collect_hard_signals(self) -> dict:
        """Collect deterministic signals from automated tools before LLM analysis.

        Runs a structured scan using available tools to gather concrete,
        reproducible signals that the analysis agent can use. These signals
        are high-confidence improvement opportunities because they come from
        deterministic tools rather than LLM interpretation.

        Returns:
            Dict with signal categories:
            {
                "timestamp": "ISO8601",
                "test_failures": [...],     # from last eval
                "coverage_gaps": [...],     # files with <80% coverage
                "todo_comments": [...],     # grep for TODO/FIXME/HACK
                "lint_errors": [...],       # from ruff
                "typing_errors": [...],     # from mypy
                "slow_tests": [...],        # tests taking >1s
                "complexity_hotspots": [...],  # high cyclomatic complexity
            }
        """
        signals: dict = {
            "timestamp": datetime.now().isoformat(),
            "test_failures": [],
            "coverage_gaps": [],
            "todo_comments": [],
            "lint_errors": [],
            "typing_errors": [],
            "slow_tests": [],
            "complexity_hotspots": [],
        }

        progress("Collecting hard signals...")

        # 1. Test failures from last eval
        evals_dir = self.work_dir / "evals"
        last_eval = None
        json_files = []
        if evals_dir.exists():
            json_files = sorted(f for f in evals_dir.glob("*.json") if f.name != "summary.json")
            if json_files:
                last_eval = json.loads(json_files[-1].read_text())
                signals["test_failures"] = last_eval.get("failed_tests", [])

                # Extract slow tests if duration info available
                # Note: pytest --durations output is not in JSON, so we skip this for now
                # unless we add duration tracking to eval

        # 2. Coverage gaps from last eval (files with <80% coverage)
        if evals_dir.exists() and json_files and last_eval:
            # coverage.json from pytest-cov has file-level data
            coverage_json_path = self.repo_dir / "coverage.json"
            if coverage_json_path.exists():
                try:
                    cov_data = json.loads(coverage_json_path.read_text())
                    files_data = cov_data.get("files", {})
                    for filepath, file_info in files_data.items():
                        summary = file_info.get("summary", {})
                        covered_lines = summary.get("covered_lines", 0)
                        num_statements = summary.get("num_statements", 0)
                        if num_statements > 0:
                            coverage_pct = covered_lines / num_statements
                            if coverage_pct < 0.80:
                                signals["coverage_gaps"].append(
                                    {
                                        "file": filepath,
                                        "coverage": round(coverage_pct * 100, 1),
                                        "missing_lines": summary.get("missing_lines", 0),
                                    }
                                )
                except (json.JSONDecodeError, KeyError):
                    pass

        # 3. TODO/FIXME/HACK comments
        try:
            result = subprocess.run(
                ["grep", "-rn", "-E", r"(TODO|FIXME|HACK|XXX):", "."],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.repo_dir,
            )
            for line in result.stdout.strip().split("\n")[:50]:  # Limit to 50
                if line and not line.startswith("./.git"):
                    # Parse format: ./file.py:123:# TODO: description
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        signals["todo_comments"].append(
                            {
                                "file": parts[0].lstrip("./"),
                                "line": int(parts[1]) if parts[1].isdigit() else 0,
                                "text": parts[2].strip()[:100],  # Truncate long comments
                            }
                        )
        except (subprocess.TimeoutExpired, Exception):
            pass

        # 4. Lint errors from ruff
        if shutil.which("ruff"):
            try:
                result = subprocess.run(
                    ["ruff", "check", ".", "--output-format=json"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=self.repo_dir,
                )
                try:
                    issues = json.loads(result.stdout)
                    for issue in issues[:30]:  # Limit to 30
                        signals["lint_errors"].append(
                            {
                                "file": issue.get("filename", ""),
                                "line": issue.get("location", {}).get("row", 0),
                                "code": issue.get("code", ""),
                                "message": issue.get("message", "")[:100],
                            }
                        )
                except json.JSONDecodeError:
                    pass
            except (subprocess.TimeoutExpired, Exception):
                pass

        # 5. Typing errors from mypy
        if shutil.which("mypy"):
            try:
                result = subprocess.run(
                    ["mypy", ".", "--no-error-summary"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=self.repo_dir,
                )
                # Parse mypy output: file.py:line: error: message
                for line in (result.stdout + result.stderr).strip().split("\n")[:30]:
                    match = re.match(r"([^:]+):(\d+): error: (.+)", line)
                    if match:
                        signals["typing_errors"].append(
                            {
                                "file": match.group(1),
                                "line": int(match.group(2)),
                                "message": match.group(3)[:100],
                            }
                        )
            except (subprocess.TimeoutExpired, Exception):
                pass

        # 6. Complexity hotspots from radon
        if shutil.which("radon"):
            try:
                result = subprocess.run(
                    ["radon", "cc", "-s", "-j", "."],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=self.repo_dir,
                )
                try:
                    data = json.loads(result.stdout)
                    for filepath, file_results in data.items():
                        for item in file_results:
                            complexity = item.get("complexity", 0)
                            if complexity >= 11:  # Grade C or worse
                                signals["complexity_hotspots"].append(
                                    {
                                        "file": filepath,
                                        "function": item.get("name", ""),
                                        "line": item.get("lineno", 0),
                                        "complexity": complexity,
                                        "rank": item.get("rank", ""),
                                    }
                                )
                except json.JSONDecodeError:
                    pass
            except (subprocess.TimeoutExpired, Exception):
                pass

        # Count total signals
        total_signals = sum(
            len(signals.get(key, []))
            for key in [
                "test_failures",
                "coverage_gaps",
                "todo_comments",
                "lint_errors",
                "typing_errors",
                "complexity_hotspots",
            ]
        )
        signals["total_signals"] = total_signals

        # Store signals
        signals_dir = self.work_dir / "signals"
        signals_dir.mkdir(exist_ok=True)
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        signals_file = signals_dir / f"{timestamp_str}.json"
        signals_file.write_text(json.dumps(signals, indent=2))

        progress(f"Collected {total_signals} hard signals -> {signals_file.name}")

        return signals

    def _format_signals_for_prompt(self, signals: dict) -> str:
        """Format collected signals as markdown for inclusion in analyze prompt.

        Args:
            signals: Dict of collected hard signals from collect_hard_signals().

        Returns:
            Markdown-formatted string summarizing the signals.
        """
        sections = []

        # Test failures
        if signals.get("test_failures"):
            lines = ["### Test Failures"]
            for test in signals["test_failures"][:10]:
                lines.append(f"- `{test}`")
            if len(signals["test_failures"]) > 10:
                lines.append(f"- ... and {len(signals['test_failures']) - 10} more")
            sections.append("\n".join(lines))

        # Coverage gaps
        if signals.get("coverage_gaps"):
            lines = ["### Coverage Gaps (<80%)"]
            for gap in signals["coverage_gaps"][:10]:
                lines.append(
                    f"- `{gap['file']}`: {gap['coverage']}% ({gap['missing_lines']} lines missing)"
                )
            if len(signals["coverage_gaps"]) > 10:
                lines.append(f"- ... and {len(signals['coverage_gaps']) - 10} more")
            sections.append("\n".join(lines))

        # TODO comments
        if signals.get("todo_comments"):
            lines = ["### TODO/FIXME/HACK Comments"]
            for todo in signals["todo_comments"][:10]:
                lines.append(f"- `{todo['file']}:{todo['line']}`: {todo['text']}")
            if len(signals["todo_comments"]) > 10:
                lines.append(f"- ... and {len(signals['todo_comments']) - 10} more")
            sections.append("\n".join(lines))

        # Lint errors
        if signals.get("lint_errors"):
            lines = ["### Lint Errors (ruff)"]
            for err in signals["lint_errors"][:10]:
                lines.append(f"- `{err['file']}:{err['line']}` [{err['code']}]: {err['message']}")
            if len(signals["lint_errors"]) > 10:
                lines.append(f"- ... and {len(signals['lint_errors']) - 10} more")
            sections.append("\n".join(lines))

        # Typing errors
        if signals.get("typing_errors"):
            lines = ["### Typing Errors (mypy)"]
            for err in signals["typing_errors"][:10]:
                lines.append(f"- `{err['file']}:{err['line']}`: {err['message']}")
            if len(signals["typing_errors"]) > 10:
                lines.append(f"- ... and {len(signals['typing_errors']) - 10} more")
            sections.append("\n".join(lines))

        # Complexity hotspots
        if signals.get("complexity_hotspots"):
            lines = ["### Complexity Hotspots (radon)"]
            for hotspot in signals["complexity_hotspots"][:10]:
                lines.append(
                    f"- `{hotspot['file']}:{hotspot['line']}` "
                    f"`{hotspot['function']}` - complexity {hotspot['complexity']} (rank {hotspot['rank']})"
                )
            if len(signals["complexity_hotspots"]) > 10:
                lines.append(f"- ... and {len(signals['complexity_hotspots']) - 10} more")
            sections.append("\n".join(lines))

        if not sections:
            return "*No hard signals detected. All automated checks passed.*"

        total = signals.get("total_signals", 0)
        header = f"**{total} issues detected by automated tools:**\n"
        return header + "\n\n".join(sections)

    # =========================================================================
    # Analysis helpers
    # =========================================================================

    def _get_opportunities_content(self) -> str:
        """Return current opportunities as a string for prompt injection."""
        if hasattr(self.opportunity_provider, "path"):
            path = self.opportunity_provider.path
            if path.exists():
                return path.read_text()
        # Fallback: format from list
        opps = self.opportunity_provider.list_opportunities()
        if not opps:
            return "(no opportunities)"
        lines = []
        for opp in opps:
            lines.append(f"- [ ] **{opp.title}** (ID: {opp.opportunity_id})")
            if opp.description:
                lines.append(f"  {opp.description}")
        return "\n".join(lines)

    def _parse_analyze_review_verdict(self, output: str) -> dict:
        """Parse JSON verdict from reviewer output.

        Returns a dict with at minimum ``verdict`` and ``feedback`` keys.
        If the output cannot be parsed, returns a NEEDS_REVISION verdict.
        """
        json_match = re.search(r"```json\s*(.*?)\s*```", output, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            pass
        return {
            "verdict": "NEEDS_REVISION",
            "score": 0,
            "strengths": [],
            "issues": [f"Could not parse reviewer output: {output[:200]}"],
            "feedback": output,
        }

    # =========================================================================
    # Analysis
    # =========================================================================

    def run_analyze(
        self,
        issues_file: str | None = None,
        load_prompt_callback: Callable[[str], str] | None = None,
        run_agent_callback: Callable[..., str] | None = None,
        reviewer_callback: Callable[..., str] | None = None,
        load_rollback_context_callback: Callable[[], dict | None] | None = None,
        log_callback: Callable[..., None] | None = None,
    ) -> dict:
        """Run analysis agent to scan for improvement opportunities.

        Invokes the analysis agent with the analyze prompt. The agent scans
        the codebase and writes findings to `opportunities.md` in the repo root.

        Before invoking the LLM, collects deterministic "hard signals" from
        automated tools (lint, typing, complexity, etc.) and injects them into
        the prompt. These high-confidence signals help the agent prioritize
        concrete, actionable improvements.

        If a `goals.md` file exists in the repo root, its contents are injected
        into the prompt to help prioritize opportunities that align with project goals.

        If an issues file is provided via the `issues_file` parameter, its contents
        are injected into the prompt so the agent can consider known issues alongside
        its codebase scan.

        Args:
            issues_file: Optional path to a file containing known issues to consider.
            load_prompt_callback: Callback to load prompt templates.
            run_agent_callback: Callback to invoke the LLM agent.
            reviewer_callback: Optional callback to invoke a reviewer agent. Accepted
                for API uniformity but not used; the human-approval gate in run_cycle
                is the appropriate quality checkpoint for analysis output.
            load_rollback_context_callback: Callback to load rollback context.
            log_callback: Optional callback for logging events.

        Returns:
            Dict with analysis results including:
            - opportunities_file: Path to created opportunities.md (if created)
            - success: Boolean indicating if analysis completed successfully
            - goals_used: Boolean indicating if goals.md was found and used
            - issues_used: Boolean indicating if issues file was provided and used
            - hard_signals: Dict of collected hard signals
        """
        progress("Running analysis agent...")

        # Collect hard signals before LLM analysis
        hard_signals = self.collect_hard_signals()

        if load_prompt_callback is None:
            raise ValueError("load_prompt_callback is required")
        if run_agent_callback is None:
            raise ValueError("run_agent_callback is required")
        self._inject_agent_callbacks(run_agent_callback)

        # Load the analyze prompt
        analyze_prompt = load_prompt_callback("analyze_prompt.md")

        # Inject hard signals into prompt
        if hard_signals.get("total_signals", 0) > 0:
            signals_section = f"""## Hard Signals (automated scan)

These issues were detected by automated tools. Consider them high-confidence opportunities.

{self._format_signals_for_prompt(hard_signals)}"""
            analyze_prompt = analyze_prompt.replace("{{HARD_SIGNALS}}", signals_section)
        else:
            signals_section = ""
            analyze_prompt = analyze_prompt.replace("{{HARD_SIGNALS}}", "")

        # Check for goals.md and inject if present
        goals_file_path = self.repo_dir / "goals.md"
        goals_used = False
        if goals_file_path.exists():
            goals_content = goals_file_path.read_text()
            goals_section = f"""## Project Goals

The following goals are defined for this project. Prioritize opportunities that advance these goals.

{goals_content}"""
            analyze_prompt = analyze_prompt.replace("{{PROJECT_GOALS}}", goals_section)
            goals_used = True
            progress("Found goals.md - incorporating project goals into analysis")
        else:
            goals_section = ""
            # Remove the placeholder if no goals file
            analyze_prompt = analyze_prompt.replace("{{PROJECT_GOALS}}", "")

        # Check for issues file and inject if provided
        issues_used = False
        if issues_file:
            issues_path = Path(issues_file)
            if not issues_path.is_absolute():
                issues_path = self.repo_dir / issues_path
            if issues_path.exists():
                issues_content = issues_path.read_text()
                issues_section = f"""## Known Issues

The following issues have been reported. Consider these alongside your codebase scan. If a known issue matches something you find during the scan, note "Confirms reported issue" rather than listing it as a separate finding.

{issues_content}"""
                analyze_prompt = analyze_prompt.replace("{{KNOWN_ISSUES}}", issues_section)
                issues_used = True
                progress(f"Found issues file - incorporating {issues_path.name} into analysis")
            else:
                progress(f"Warning: Issues file not found: {issues_path}")
                analyze_prompt = analyze_prompt.replace("{{KNOWN_ISSUES}}", "")
        else:
            # Remove the placeholder if no issues file
            analyze_prompt = analyze_prompt.replace("{{KNOWN_ISSUES}}", "")

        # Check for rollback context and inject if present
        rollback_context = None
        if load_rollback_context_callback:
            rollback_context = load_rollback_context_callback()
        if rollback_context:
            rollback_section = f"""## Previous Rollback Context

A previous attempt at a task was rolled back due to eval regression. Consider this when suggesting approaches:

- **Task that failed**: {rollback_context.get("task", "Unknown")}
- **Reason for rollback**: {rollback_context.get("reason", "Unknown")}
- **Details**: {json.dumps(rollback_context.get("details", {}), indent=2)}

When addressing similar areas, try a different approach than what caused the regression."""
            analyze_prompt = analyze_prompt.replace("{{ROLLBACK_CONTEXT}}", rollback_section)
            progress("Found rollback context - incorporating into analysis")
        else:
            analyze_prompt = analyze_prompt.replace("{{ROLLBACK_CONTEXT}}", "")

        # Apply provider placeholders (static substitutions must precede this call).
        opp_placeholders = self.opportunity_provider.get_prompt_placeholders()
        analyze_prompt = apply_provider_placeholders(analyze_prompt, opp_placeholders)

        if reviewer_callback is not None:
            # Wrap analyze generation in ArtifactReviewLoop with explicit reviewer role.
            def produce_opportunities(feedback=None):
                if feedback is None:
                    run_agent_callback(analyze_prompt)
                else:
                    ops_content = self._get_opportunities_content()
                    fix_prompt = load_prompt_callback("analyze_fix_prompt.md")
                    fix_prompt = fix_prompt.replace("{{OPPORTUNITIES_CONTENT}}", ops_content)
                    fix_prompt = fix_prompt.replace("{{FEEDBACK}}", feedback)
                    fix_prompt = fix_prompt.replace("{{HARD_SIGNALS}}", signals_section)
                    fix_prompt = fix_prompt.replace("{{PROJECT_GOALS}}", goals_section)
                    fix_prompt = apply_provider_placeholders(fix_prompt, opp_placeholders)
                    run_agent_callback(fix_prompt)
                return self._get_opportunities_content()

            def review_opportunities(ops_content):
                review_prompt = load_prompt_callback("analyze_review_prompt.md")
                review_prompt = review_prompt.replace("{{OPPORTUNITIES_CONTENT}}", ops_content)
                review_prompt = review_prompt.replace("{{HARD_SIGNALS}}", signals_section)
                review_prompt = review_prompt.replace("{{PROJECT_GOALS}}", goals_section)
                output = reviewer_callback(review_prompt)
                return self._parse_analyze_review_verdict(output)

            loop = ArtifactReviewLoop(
                name="Analyzer",
                producer=produce_opportunities,
                reviewer=review_opportunities,
                is_approved=lambda v: isinstance(v, dict) and v.get("verdict") == "APPROVED",
                max_cycles=self.max_cycles,
            )
            loop_result = loop.run()

            opportunities = self.opportunity_provider.list_opportunities()
            opportunities_file = (
                str(self.opportunity_provider.path)
                if isinstance(self.opportunity_provider, FileOpportunityProvider)
                else None
            )

            if not loop_result.success:
                if log_callback:
                    log_callback(
                        "analyze_failed",
                        reason="Review loop failed without approval",
                        cycles=str(loop_result.cycles),
                    )
                return {
                    "success": False,
                    "opportunities_file": opportunities_file,
                    "cycles": loop_result.cycles,
                    "error": loop_result.error or "Review loop failed without approval",
                    "goals_used": goals_used,
                    "issues_used": issues_used,
                    "hard_signals": hard_signals,
                }

            opportunity_count = len(opportunities)
            result = {
                "success": True,
                "opportunities_file": opportunities_file,
                "goals_used": goals_used,
                "issues_used": issues_used,
                "hard_signals": hard_signals,
                "opportunity_count": opportunity_count,
            }
            if log_callback:
                log_callback(
                    "analyze_completed",
                    opportunities_file=opportunities_file,
                    opportunity_count=str(opportunity_count),
                    goals_used=str(goals_used),
                    issues_used=str(issues_used),
                    hard_signals_count=str(hard_signals.get("total_signals", 0)),
                )
            print()
            print("=== Analysis Complete ===")
            print(f"Hard signals collected: {hard_signals.get('total_signals', 0)}")
            print(f"Opportunities file: {opportunities_file or 'provider-managed'}")
            print(f"Opportunities found: {opportunity_count}")
            if goals_used:
                print("Project goals: incorporated from goals.md")
            if issues_used:
                print("Known issues: incorporated from issues file")
            return result

        # Single-pass path: reviewer_callback is None
        output = run_agent_callback(analyze_prompt)

        opportunities = self.opportunity_provider.list_opportunities()
        success = len(opportunities) > 0
        opportunities_file = (
            str(self.opportunity_provider.path)
            if success and isinstance(self.opportunity_provider, FileOpportunityProvider)
            else None
        )

        result = {
            "success": success,
            "opportunities_file": opportunities_file,
            "goals_used": goals_used,
            "issues_used": issues_used,
            "hard_signals": hard_signals,
        }

        if success:
            opportunity_count = len(opportunities)
            result["opportunity_count"] = opportunity_count

            if log_callback:
                log_callback(
                    "analyze_completed",
                    opportunities_file=opportunities_file,
                    opportunity_count=str(opportunity_count),
                    goals_used=str(goals_used),
                    issues_used=str(issues_used),
                    hard_signals_count=str(hard_signals.get("total_signals", 0)),
                )

            # Print summary
            print()
            print("=== Analysis Complete ===")
            print(f"Hard signals collected: {hard_signals.get('total_signals', 0)}")
            print(f"Opportunities file: {opportunities_file or 'provider-managed'}")
            print(f"Opportunities found: {opportunity_count}")
            if goals_used:
                print("Project goals: incorporated from goals.md")
            if issues_used:
                print("Known issues: incorporated from issues file")
        else:
            if log_callback:
                log_callback(
                    "analyze_failed",
                    reason="opportunities.md not created",
                    output=output[:2000],
                    hard_signals_count=str(hard_signals.get("total_signals", 0)),
                )
            print()
            print("=== Analysis Failed ===")
            print("The analysis agent did not create opportunities.md")

        return result

    # =========================================================================
    # Design
    # =========================================================================

    def run_design(
        self,
        opportunity: str,
        opportunity_id: str | None = None,
        load_prompt_callback: Callable[[str], str] | None = None,
        run_agent_callback: Callable[..., str] | None = None,
        reviewer_callback: Callable[..., str] | None = None,
        log_callback: Callable[..., None] | None = None,
    ) -> dict:
        """Run design agent to create an implementation spec for an opportunity.

        Invokes the design agent with the design prompt. The agent analyzes the
        opportunity and writes a design document to `designs/<slug>.md`.

        Args:
            opportunity: Description of the opportunity to design a solution for.
            opportunity_id: Optional canonical opportunity ID for opportunity_ref.
            load_prompt_callback: Callback to load prompt templates.
            run_agent_callback: Callback to invoke the LLM agent.
            reviewer_callback: Optional callback to invoke a reviewer agent. Accepted
                for API uniformity; automated design review is handled by the separate
                review_design() method.
            log_callback: Optional callback for logging events.

        Returns:
            Dict with design results including:
            - design_file: Path to created design file (if created)
            - design_id: ID of created design (if created)
            - success: Boolean indicating if design was created successfully
        """
        progress(f"Running design agent for: {opportunity}")

        if load_prompt_callback is None:
            raise ValueError("load_prompt_callback is required")
        if run_agent_callback is None:
            raise ValueError("run_agent_callback is required")
        self._inject_agent_callbacks(run_agent_callback)

        # Load the design prompt and substitute opportunity
        design_prompt = load_prompt_callback("design_prompt.md")
        design_prompt = design_prompt.replace("{{OPPORTUNITY}}", opportunity)
        design_prompt = design_prompt.replace("{{OPPORTUNITY_ID}}", opportunity_id or "")

        # Apply provider placeholders (static substitutions must precede this call).
        design_placeholders = self.design_provider.get_prompt_placeholders()
        design_prompt = apply_provider_placeholders(design_prompt, design_placeholders)

        if isinstance(self.design_provider, FileDesignProvider):
            self.design_provider.path.mkdir(parents=True, exist_ok=True)

        # Snapshot existing designs (IDs and bodies) before invoking agent so we can
        # detect both newly-created designs and in-place revisions to existing ones.
        existing_designs_before = self.design_provider.list_designs()
        existing_design_ids = {d.design_id for d in existing_designs_before}
        existing_design_bodies = {d.design_id: d.body for d in existing_designs_before}

        if reviewer_callback is not None:
            # Wrap design generation in ArtifactReviewLoop with explicit reviewer role.
            detected_design_id: list[str | None] = [None]

            def _find_new_or_revised_design() -> object:
                current = self.design_provider.list_designs()
                new = [d for d in current if d.design_id not in existing_design_ids]
                if not new and opportunity_id:
                    rev = self.design_provider.get_design(opportunity_id)
                    if rev is not None and rev.body != existing_design_bodies.get(opportunity_id):
                        new = [rev]
                if not new:
                    for d in current:
                        if (
                            d.design_id in existing_design_ids
                            and d.body != existing_design_bodies.get(d.design_id)
                        ):
                            new = [d]
                            break
                return new[0] if new else None

            def produce_design(feedback=None):
                if feedback is None:
                    run_agent_callback(design_prompt)
                else:
                    design_content = ""
                    if detected_design_id[0]:
                        d = self.design_provider.get_design(detected_design_id[0])
                        if d:
                            design_content = d.body
                    fix_prompt = load_prompt_callback("design_fix_prompt.md")
                    fix_prompt = fix_prompt.replace("{{OPPORTUNITY}}", opportunity)
                    fix_prompt = fix_prompt.replace("{{DESIGN_CONTENT}}", design_content)
                    fix_prompt = fix_prompt.replace("{{FEEDBACK}}", feedback)
                    fix_prompt = apply_provider_placeholders(fix_prompt, design_placeholders)
                    run_agent_callback(fix_prompt)
                # Detect design only on first pass; fix passes reuse the cached ID.
                if detected_design_id[0] is None:
                    found = _find_new_or_revised_design()
                    if found is not None:
                        detected_design_id[0] = found.design_id
                if detected_design_id[0] is None:
                    return None
                if isinstance(self.design_provider, FileDesignProvider):
                    return str(self.design_provider.path / f"{detected_design_id[0]}.md")
                return detected_design_id[0]

            def review_design_for_loop(design_path_or_id):
                if design_path_or_id is None:
                    return {
                        "approved": False,
                        "verdict": "NEEDS_REVISION",
                        "feedback": "No design was created or detected",
                    }
                review_result = self.review_design(
                    design_path=design_path_or_id,
                    load_prompt_callback=load_prompt_callback,
                    run_agent_callback=reviewer_callback,
                )
                # Ensure a 'feedback' key exists for the loop's feedback extraction.
                if "feedback" not in review_result:
                    issues = review_result.get("issues", [])
                    questions = review_result.get("questions", [])
                    parts = [f"Issue: {i}" for i in issues] + [f"Question: {q}" for q in questions]
                    review_result = dict(review_result)
                    review_result["feedback"] = (
                        "\n".join(parts) if parts else review_result.get("output", "")
                    )
                return review_result

            loop = ArtifactReviewLoop(
                name="Designer",
                producer=produce_design,
                reviewer=review_design_for_loop,
                is_approved=lambda v: isinstance(v, dict) and v.get("approved") is True,
                max_cycles=self.max_cycles,
            )
            loop_result = loop.run()

            if loop_result.success and detected_design_id[0]:
                new_design = self.design_provider.get_design(detected_design_id[0])
                if new_design:
                    checker = ReferenceIntegrityChecker(
                        opportunity_provider=self.opportunity_provider,
                        design_provider=self.design_provider,
                    )
                    try:
                        checker.check_design(new_design)
                    except ReferenceIntegrityError as exc:
                        if log_callback:
                            log_callback(
                                "design_failed",
                                opportunity=opportunity[:200],
                                reason="reference_integrity_failed",
                                integrity_error=str(exc),
                            )
                        print()
                        print("=== Design Failed ===")
                        print("Reference integrity check failed:")
                        for violation in exc.violations:
                            print(f"  - {violation}")
                        return {
                            "success": False,
                            "design_file": None,
                            "design_id": new_design.design_id,
                            "integrity_error": str(exc),
                        }

                    design_file = (
                        str(self.design_provider.path / f"{new_design.design_id}.md")
                        if isinstance(self.design_provider, FileDesignProvider)
                        else None
                    )
                    result = {
                        "success": True,
                        "design_file": design_file,
                        "design_id": new_design.design_id,
                    }
                    if log_callback:
                        log_callback(
                            "design_completed",
                            opportunity=opportunity[:200],
                            design_file=design_file,
                            design_id=new_design.design_id,
                        )
                    print()
                    print("=== Design Complete ===")
                    if design_file:
                        print(f"Design file: {design_file}")
                    else:
                        print(f"Design created: {new_design.design_id}")
                    return result

            # Loop failed or no design detected after loop.
            error_msg = loop_result.error or "Review loop failed without approval"
            if log_callback:
                log_callback(
                    "design_failed",
                    opportunity=opportunity[:200],
                    reason=error_msg,
                )
            print()
            print("=== Design Failed ===")
            print(error_msg)
            return {
                "success": False,
                "design_file": None,
                "design_id": detected_design_id[0],
                "error": error_msg,
            }

        # Single-pass path: reviewer_callback is None
        output = run_agent_callback(design_prompt)

        # Find new or in-place-revised designs by comparing provider snapshots.
        # New: IDs that appeared after the agent ran.
        # In-place (opportunity_id known): the named design is accepted as revised even if
        #   its ID did not change — the agent edited the existing file per prompt instructions.
        # In-place (opportunity_id=None, e.g. --design CLI path): fall back to body-content
        #   comparison across all pre-existing designs; any change counts as a revision.
        current_designs = self.design_provider.list_designs()
        new_designs = [d for d in current_designs if d.design_id not in existing_design_ids]
        if not new_designs and opportunity_id:
            revised = self.design_provider.get_design(opportunity_id)
            if revised is not None and revised.body != existing_design_bodies.get(opportunity_id):
                new_designs = [revised]
        if not new_designs:
            # opportunity_id is None or design not found via it: detect in-place edit by
            # comparing body content of pre-existing designs.
            for d in current_designs:
                if d.design_id in existing_design_ids and d.body != existing_design_bodies.get(
                    d.design_id
                ):
                    new_designs = [d]
                    break

        if new_designs:
            new_design = new_designs[0]
            checker = ReferenceIntegrityChecker(
                opportunity_provider=self.opportunity_provider,
                design_provider=self.design_provider,
            )
            try:
                checker.check_design(new_design)
            except ReferenceIntegrityError as exc:
                if log_callback:
                    log_callback(
                        "design_failed",
                        opportunity=opportunity[:200],
                        reason="reference_integrity_failed",
                        integrity_error=str(exc),
                    )
                print()
                print("=== Design Failed ===")
                print("Reference integrity check failed:")
                for violation in exc.violations:
                    print(f"  - {violation}")
                return {
                    "success": False,
                    "design_file": None,
                    "design_id": new_design.design_id,
                    "integrity_error": str(exc),
                }

            design_file = (
                str(self.design_provider.path / f"{new_design.design_id}.md")
                if isinstance(self.design_provider, FileDesignProvider)
                else None
            )
            result = {
                "success": True,
                "design_file": design_file,
                "design_id": new_design.design_id,
            }

            if log_callback:
                log_callback(
                    "design_completed",
                    opportunity=opportunity[:200],
                    design_file=design_file,
                    design_id=new_design.design_id,
                )

            # Print summary
            print()
            print("=== Design Complete ===")
            if design_file:
                print(f"Design file: {design_file}")
            else:
                print(f"Design created: {new_design.design_id}")
        else:
            result = {
                "success": False,
                "design_file": None,
                "design_id": None,
            }

            if log_callback:
                log_callback(
                    "design_failed",
                    opportunity=opportunity[:200],
                    reason="no design file created or revised",
                    output=output[:2000],
                )

            print()
            print("=== Design Failed ===")
            print("The design agent did not create or revise a design artifact")

        return result

    def review_design(
        self,
        design_path: str,
        load_prompt_callback: Callable[[str], str] | None = None,
        run_agent_callback: Callable[..., str] | None = None,
        is_empty_response_callback: Callable[[str, str], bool] | None = None,
        parse_design_review_callback: Callable[[str], DesignReviewResult | None] | None = None,
        log_callback: Callable[..., None] | None = None,
    ) -> dict:
        """Review a design document for quality and completeness.

        Invokes the review agent with the design review prompt. The agent evaluates
        the design against criteria like measurable success criteria, completeness,
        alternatives considered, and appropriate scoping.

        Args:
            design_path: Path to the design file to review.
            load_prompt_callback: Callback to load prompt templates.
            run_agent_callback: Callback to invoke the LLM agent.
            is_empty_response_callback: Callback to check for empty/malformed response.
            parse_design_review_callback: Callback to parse structured review response.
            log_callback: Optional callback for logging events.

        Returns:
            Dict with review results including:
            - approved: Boolean indicating if design was approved
            - verdict: "APPROVED" or "NEEDS_REVISION"
            - strengths: List of design strengths (if parsed)
            - issues: List of issues to address (if parsed)
            - questions: List of questions for author (if parsed)
            - output: The full review output text
        """
        progress(f"Reviewing design: {design_path}")

        if load_prompt_callback is None:
            raise ValueError("load_prompt_callback is required")
        if run_agent_callback is None:
            raise ValueError("run_agent_callback is required")

        # Inject before any provider reads so MCP-backed design providers
        # have an active callback when get_design() is called below.
        self._inject_agent_callbacks(run_agent_callback)

        # Read the design file
        design_file = Path(design_path)
        if not design_file.is_absolute():
            design_file = self.repo_dir / design_file
        if not design_file.exists():
            result = {
                "approved": False,
                "verdict": "ERROR",
                "output": f"Design file not found: {design_path}",
            }
            design = self.design_provider.get_design(design_file.stem)
            if design is not None:
                design_content = design.body
            else:
                if log_callback:
                    log_callback(
                        "design_review_failed",
                        design_path=design_path,
                        reason="file_not_found",
                    )
                print()
                print("=== Design Review Failed ===")
                print(f"Design file not found: {design_path}")
                return result
        else:
            design_content = design_file.read_text()

        # Load and substitute the review prompt
        review_prompt = load_prompt_callback("review_design_prompt.md")
        review_prompt = review_prompt.replace("{{DESIGN_CONTENT}}", design_content)

        # Use default model for design reviews (deeper analysis than sanity checks)
        output = run_agent_callback(review_prompt)

        # Log full response before attempting verdict extraction
        if log_callback:
            log_callback(
                "design_review_response",
                design_path=design_path,
                response=output,
            )

        # Try to validate response against schema
        schema_valid = True
        if is_empty_response_callback:
            schema_valid = not is_empty_response_callback(output, "design_review")

        # Try structured parsing first
        parsed = None
        if parse_design_review_callback:
            parsed = parse_design_review_callback(output)

        # Retry once if response is empty or malformed
        if parsed is None and (not schema_valid or not output or not output.strip()):
            if log_callback:
                log_callback(
                    "design_review_retry",
                    design_path=design_path,
                    reason="empty_or_malformed",
                    original_response=output,
                )

            # Retry with explicit format instruction
            retry_prompt = (
                review_prompt
                + "\n\n---\n\n"
                + "IMPORTANT: Your previous response did not contain the required JSON format. "
                + "You MUST respond with a JSON block containing exactly these fields:\n\n"
                + '```json\n{\n  "verdict": "APPROVED" or "NEEDS_REVISION",\n'
                + '  "strengths": ["..."],\n  "issues": ["..."],\n  "questions": ["..."]\n}\n```\n\n'
                + "All fields are required. Respond with ONLY the JSON block, no other text."
            )

            output = run_agent_callback(retry_prompt)

            # Log retry response
            if log_callback:
                log_callback(
                    "design_review_retry_response",
                    design_path=design_path,
                    response=output,
                )

            # Re-validate after retry
            if is_empty_response_callback:
                schema_valid = not is_empty_response_callback(output, "design_review")
            if parse_design_review_callback:
                parsed = parse_design_review_callback(output)

        if parsed is not None:
            # Successfully parsed structured response
            approved = parsed.is_approved
            verdict = parsed.verdict.value
            result = {
                "approved": approved,
                "verdict": verdict,
                "strengths": parsed.strengths,
                "issues": parsed.issues,
                "questions": parsed.questions or [],
                "output": output,
            }
            if log_callback:
                log_callback(
                    "design_review_completed",
                    design_path=design_path,
                    verdict=verdict,
                    schema_valid=schema_valid,
                    parsed=True,
                )
        else:
            # Fallback to keyword-based parsing for backwards compatibility
            has_approved = "APPROVED" in output
            has_needs_revision = "NEEDS_REVISION" in output

            # Detect extraction failure: no verdict keywords found or empty response
            if not output or not output.strip():
                if log_callback:
                    log_callback(
                        "design_review_extraction_failed",
                        design_path=design_path,
                        reason="empty_response",
                        response=output,
                    )
                approved = False
                verdict = "NEEDS_REVISION"
            elif not has_approved and not has_needs_revision:
                if log_callback:
                    log_callback(
                        "design_review_extraction_failed",
                        design_path=design_path,
                        reason="no_verdict_keywords",
                        response=output,
                        schema_valid=schema_valid,
                    )
                # Default to NEEDS_REVISION when extraction fails
                approved = False
                verdict = "NEEDS_REVISION"
            else:
                approved = has_approved and not has_needs_revision
                verdict = "APPROVED" if approved else "NEEDS_REVISION"

            result = {
                "approved": approved,
                "verdict": verdict,
                "output": output,
            }

            if log_callback:
                log_callback(
                    "design_review_completed",
                    design_path=design_path,
                    verdict=verdict,
                    schema_valid=schema_valid,
                    parsed=False,
                )

        if result["approved"]:
            design_id = design_file.stem
            try:
                self.design_provider.update_design_status(design_id, DesignStatus.reviewed)
            except Exception as e:
                progress(
                    f"Warning: Failed to update design status to reviewed for {design_id}: {e}"
                )

        # Print summary
        print()
        print("=== Design Review ===")
        print(f"Design: {design_path}")
        print(f"Verdict: {verdict}")
        strengths = result.get("strengths")
        if isinstance(strengths, list) and strengths:
            print("Strengths:")
            for s in strengths:
                print(f"  - {s}")
        issues = result.get("issues")
        if isinstance(issues, list) and issues:
            print("Issues:")
            for i in issues:
                print(f"  - {i}")

        return result

    def review_plan(
        self,
        design_path: str,
        plan_content: str,
        load_prompt_callback: Callable[[str], str] | None = None,
        run_agent_callback: Callable[..., str] | None = None,
        log_callback: Callable[..., None] | None = None,
    ) -> dict:
        """Review a proposed plan for structural integrity and quality.

        Invokes the review agent with the plan_review_prompt.

        Args:
            design_path: Path to the design file (for context).
            plan_content: The content of the plan (tasklist tasks).
            load_prompt_callback: Callback to load prompt templates.
            run_agent_callback: Callback to invoke the LLM agent.
            log_callback: Optional callback for logging events.

        Returns:
            Dict with review results:
            - approved: Boolean
            - verdict: "APPROVED" | "NEEDS_REVISION"
            - feedback: List of strings
            - score: Number
        """
        progress(f"Reviewing plan for: {design_path}")

        if load_prompt_callback is None:
            raise ValueError("load_prompt_callback is required")
        if run_agent_callback is None:
            raise ValueError("run_agent_callback is required")

        self._inject_agent_callbacks(run_agent_callback)

        # Read design content for context
        design_file = Path(design_path)
        if not design_file.is_absolute():
            design_file = self.repo_dir / design_file
        if design_file.exists():
            design_content = design_file.read_text()
        else:
            design = self.design_provider.get_design(design_file.stem)
            design_content = design.body if design is not None else "Design file not found."

        # Load and substitute the review prompt
        review_prompt = load_prompt_callback("plan_review_prompt.md")
        review_prompt = review_prompt.replace("{{DESIGN_CONTENT}}", design_content)
        review_prompt = review_prompt.replace("{{PROPOSED_PLAN}}", plan_content)

        output = run_agent_callback(review_prompt)

        # Parse JSON output
        try:
            # Extract JSON block
            match = re.search(r"```json\s*(\{.*?\})\s*```", output, re.DOTALL)
            json_str = match.group(1) if match else output

            result_data = json.loads(json_str)
            verdict = result_data.get("verdict", "NEEDS_REVISION")
            approved = verdict == "APPROVED"
            feedback = result_data.get("feedback", [])
            score = result_data.get("score", 0)

        except (json.JSONDecodeError, AttributeError):
            # Fallback
            approved = "APPROVED" in output and "NEEDS_REVISION" not in output
            verdict = "APPROVED" if approved else "NEEDS_REVISION"
            feedback = ["Failed to parse review output."]
            score = 0
            if log_callback:
                log_callback("plan_review_parse_failed", output=output)

        result = {
            "approved": approved,
            "verdict": verdict,
            "feedback": feedback,
            "score": score,
            "output": output,
        }

        if log_callback:
            log_callback(
                "plan_review_completed",
                verdict=verdict,
                score=str(score),
                feedback_count=str(len(feedback)),
            )

        print()
        print("=== Plan Review ===")
        print(f"Verdict: {verdict} (Score: {score}/10)")
        if feedback:
            print("Feedback:")
            for item in feedback:
                print(f"  - {item}")

        return result

    # =========================================================================
    # Task validation helpers (for planning)
    # =========================================================================

    def _parse_task_metadata(self, task_text: str) -> dict:
        """Parse metadata from a task description.

        Delegates to the parse_task_metadata_callback (from TasklistManager)
        to avoid code duplication. Falls back to a minimal implementation if
        no callback is provided.

        Args:
            task_text: The full task text including any metadata lines.

        Returns:
            Dict with extracted metadata (see TasklistManager._parse_task_metadata).
        """
        if self._parse_task_metadata_callback is not None:
            return self._parse_task_metadata_callback(task_text)

        # Minimal fallback if no callback provided (for testing or standalone use)
        return {
            "title": "",
            "description": task_text.strip(),
            "est_loc": None,
            "tests": None,
            "risk": None,
            "criteria": None,
            "context_file": None,
            "raw": task_text,
        }

    def _extract_new_tasks(self, old_content: str, new_content: str) -> list[str]:
        """Extract newly added tasks from tasklist content.

        Compares old and new tasklist content to find tasks that were added.

        Args:
            old_content: Tasklist content before agent ran.
            new_content: Tasklist content after agent ran.

        Returns:
            List of task text strings (everything after '- [ ] ').
        """
        # Find all unchecked tasks in both versions
        old_tasks = set(re.findall(r"^- \[ \] (.+(?:\n(?:  .+))*)", old_content, re.MULTILINE))
        new_tasks = re.findall(r"^- \[ \] (.+(?:\n(?:  .+))*)", new_content, re.MULTILINE)

        # Return tasks that are in new but not in old (preserving order)
        new_task_set = set(new_tasks) - old_tasks
        return [t for t in new_tasks if t in new_task_set]

    def _validate_task(self, task_metadata: dict) -> dict:
        """Validate a task against atomizer constraints.

        Checks that the task meets the configured constraints for:
        - Maximum estimated lines of code
        - Test specification requirement
        - Success criteria requirement

        Args:
            task_metadata: Parsed task metadata from _parse_task_metadata().

        Returns:
            Dict with validation results:
            - valid: Boolean indicating if all constraints are met
            - violations: List of constraint violation messages
        """
        violations = []
        constraints = self.task_constraints

        # Check estimated LoC
        max_loc = constraints.get("max_loc", 200)
        if task_metadata["est_loc"] is not None:
            if task_metadata["est_loc"] > max_loc:
                violations.append(
                    f"Estimated LoC ({task_metadata['est_loc']}) exceeds maximum ({max_loc})"
                )
        else:
            # No explicit estimate - try to infer from description
            # Heuristic: if description mentions "system", "full", "complete", etc. it's likely large
            desc_lower = (task_metadata["description"] + " " + task_metadata["title"]).lower()
            large_indicators = ["system", "full implementation", "complete", "entire", "all of"]
            if any(indicator in desc_lower for indicator in large_indicators):
                violations.append(
                    "Task appears large (no Est. LoC provided, description suggests significant scope). "
                    "Add 'Est. LoC: N' metadata or split into smaller tasks."
                )

        # Check test requirement
        if constraints.get("require_tests", True) and not task_metadata["tests"]:
            # Check if tests are mentioned in the description
            desc_lower = (task_metadata["description"] or "").lower()
            if "test" not in desc_lower:
                violations.append(
                    "Task does not specify tests. Add 'Tests: filename' metadata or mention tests in description."
                )

        # Check success criteria requirement
        if constraints.get("require_criteria", True) and not task_metadata["criteria"]:
            # Check if criteria are mentioned in the description
            desc_lower = (task_metadata["description"] or "").lower()
            criteria_keywords = ["success", "done when", "complete when", "criteria", "should"]
            if not any(kw in desc_lower for kw in criteria_keywords):
                violations.append(
                    "Task does not have explicit success criteria. Add 'Criteria: ...' metadata."
                )

        # Check risk level requirement
        if constraints.get("require_risk", True):
            if not task_metadata.get("risk"):
                violations.append(
                    "Task does not have risk level assigned. Add 'Risk: low|medium|high' metadata."
                )
            elif task_metadata["risk"] not in ("low", "medium", "high"):
                violations.append(
                    f"Invalid risk level '{task_metadata['risk']}'. Must be 'low', 'medium', or 'high'."
                )

        # Check context requirement
        if (
            constraints.get("require_context", True)
            and not task_metadata.get("context")
            and not task_metadata.get("context_file")
        ):
            violations.append(
                "Task does not have explicit context metadata. Add 'Context: ...' line or '<!-- context: path -->' annotation."
            )

        return {
            "valid": len(violations) == 0,
            "violations": violations,
        }

    def _validate_generated_tasks(self, old_content: str, new_content: str) -> dict:
        """Validate all newly generated tasks against constraints.

        Args:
            old_content: Tasklist content before planning.
            new_content: Tasklist content after planning.

        Returns:
            Dict with validation results:
            - valid: Boolean indicating if all tasks pass validation
            - tasks: List of dicts with task metadata and validation results
            - violations_summary: Human-readable summary of all violations
        """
        new_tasks = self._extract_new_tasks(old_content, new_content)

        results = []
        all_violations = []

        for task_text in new_tasks:
            metadata = self._parse_task_metadata(task_text)
            validation = self._validate_task(metadata)

            results.append(
                {
                    "metadata": metadata,
                    "validation": validation,
                }
            )

            if not validation["valid"]:
                title = metadata["title"] or metadata["description"][:50]
                for v in validation["violations"]:
                    all_violations.append(f"- **{title}**: {v}")

        return {
            "valid": len(all_violations) == 0,
            "tasks": results,
            "violations_summary": "\n".join(all_violations) if all_violations else "",
        }

    # =========================================================================
    # Planning
    # =========================================================================

    def run_plan(
        self,
        design_path: str,
        load_prompt_callback: Callable[[str], str] | None = None,
        run_agent_callback: Callable[..., str] | None = None,
        log_callback: Callable[..., None] | None = None,
        task_constraints: dict | None = None,
    ) -> dict:
        """Break a design document into executable tasks for the tasklist.

        Invokes the planning agent with the plan prompt. The agent reads the design
        and current tasklist, then appends a sequence of atomic, testable tasks
        to the tasklist.

        After initial task generation, validates tasks against atomizer constraints
        (max LoC, test requirements, success criteria). If tasks violate constraints,
        prompts the agent to split/fix them up to max_split_attempts times.

        Args:
            design_path: Path to the design file to break into tasks.
            load_prompt_callback: Callback to load prompt templates.
            run_agent_callback: Callback to invoke the LLM agent.
            log_callback: Optional callback for logging events.

        Returns:
            Dict with plan results including:
            - success: Boolean indicating if tasks were added successfully
            - tasks_added: Number of new tasks added to the tasklist
            - validation: Task validation results (if tasks were added)
        """
        if load_prompt_callback is None:
            raise ValueError("load_prompt_callback is required")
        if run_agent_callback is None:
            raise ValueError("run_agent_callback is required")
        self._inject_agent_callbacks(run_agent_callback)

        # Use passed constraints or fall back to instance variable
        if task_constraints is not None:
            saved_constraints = self.task_constraints
            self.task_constraints = task_constraints
        else:
            saved_constraints = None

        try:
            return self._run_plan_impl(
                design_path=design_path,
                load_prompt_callback=load_prompt_callback,
                run_agent_callback=run_agent_callback,
                log_callback=log_callback,
            )
        finally:
            if saved_constraints is not None:
                self.task_constraints = saved_constraints

    def _run_plan_impl(
        self,
        design_path: str,
        load_prompt_callback: Callable[[str], str] | None = None,
        run_agent_callback: Callable[..., str] | None = None,
        log_callback: Callable[..., None] | None = None,
    ) -> dict:
        """Internal implementation of run_plan."""
        progress(f"Running planning agent for: {design_path}")

        # Read the design file
        design_file = Path(design_path)
        if not design_file.is_absolute():
            design_file = self.repo_dir / design_file
        design_id = design_file.stem
        design = self.design_provider.get_design(design_id)
        if design is None:
            error_msg = f"Design file not found: {design_path}"
            if log_callback:
                log_callback("plan_failed", design_path=design_path, reason="file_not_found")
            print(f"\n=== Planning Failed ===\n{error_msg}")
            return {"success": False, "tasks_added": 0, "error": error_msg}
        if design.status == DesignStatus.draft:
            progress(f"Warning: Planning design {design_id} with draft status (unreviewed)")
        if load_prompt_callback is None:
            raise ValueError("load_prompt_callback is required")
        if run_agent_callback is None:
            raise ValueError("run_agent_callback is required")

        design_content = design_file.read_text() if design_file.exists() else design.body

        # Reset rollback baseline so this session's initial state is captured,
        # not a stale baseline from a previous planning run.
        reset_baseline = getattr(self.tasklist_provider, "reset_snapshot_baseline", None)
        if callable(reset_baseline):
            reset_baseline()

        # Read current tasklist via provider snapshot for backend-agnostic rollback.
        try:
            original_tasklist_content = self.tasklist_provider.get_snapshot()
        except FileNotFoundError:
            error_msg = f"Tasklist file not found: {self.tasklist}"
            if log_callback:
                log_callback("plan_failed", design_path=design_path, reason="tasklist_not_found")
            print(f"\n=== Planning Failed ===\n{error_msg}")
            return {"success": False, "tasks_added": 0, "error": error_msg}
        tasks_before_ids = {t.task_id for t in self.tasklist_provider.list_tasks()}

        # Callbacks are validated and injected by the run_plan() entry point.

        # State tracking for the loop
        state: dict[str, Any] = {
            "split_attempts": 0,
            "validation": {"valid": False, "violations_summary": ""},
        }

        tasklist_placeholders = self.tasklist_provider.get_prompt_placeholders()

        def produce_tasks(feedback: str | None = None) -> list[str]:
            """Inner producer that handles both initial generation and refinement."""
            if feedback:
                # Get tasks the agent added so far (by ID diff) for the fix prompt.
                # No restore here — agent edits the existing tasks in place.
                current_tasks = [
                    t
                    for t in self.tasklist_provider.list_tasks()
                    if t.task_id not in tasks_before_ids
                ]
                current_added = "\n".join(f"- [ ] {t.raw or t.title}" for t in current_tasks)

                fix_prompt = load_prompt_callback("plan_fix_prompt.md")
                fix_prompt = fix_prompt.replace("{{DESIGN_CONTENT}}", design_content)
                fix_prompt = fix_prompt.replace("{{PLAN_CONTENT}}", current_added)
                fix_prompt = fix_prompt.replace("{{FEEDBACK}}", feedback)
                fix_prompt = apply_provider_placeholders(fix_prompt, tasklist_placeholders)
                # Backward-compat: custom --prompts-dir templates may still use {{TASKLIST_PATH}}
                fix_prompt = fix_prompt.replace("{{TASKLIST_PATH}}", self.tasklist)
                run_agent_callback(fix_prompt)
                _invalidate_tasklist_cache(self.tasklist_provider)
            else:
                # Initial generation
                plan_prompt = load_prompt_callback("plan_prompt.md")
                plan_prompt = plan_prompt.replace("{{DESIGN_CONTENT}}", design_content)
                plan_prompt = plan_prompt.replace("{{TASKLIST_CONTENT}}", original_tasklist_content)
                max_loc = self.task_constraints.get("max_loc", 200)
                plan_prompt = plan_prompt.replace("{{MAX_LOC}}", str(max_loc))
                plan_prompt = apply_provider_placeholders(plan_prompt, tasklist_placeholders)
                # Backward-compat: custom --prompts-dir templates may still use {{TASKLIST_PATH}}
                plan_prompt = plan_prompt.replace("{{TASKLIST_PATH}}", self.tasklist)
                run_agent_callback(plan_prompt)
                _invalidate_tasklist_cache(self.tasklist_provider)

            # Mechanical validation loop (Atomizer)
            new_content = self.tasklist_provider.get_snapshot()
            validation = self._validate_generated_tasks(original_tasklist_content, new_content)

            max_split_attempts = self.task_constraints.get("max_split_attempts", 2)
            split_attempt = 0
            while not validation["valid"] and split_attempt < max_split_attempts:
                split_attempt += 1
                state["split_attempts"] += 1
                progress(
                    f"Task validation failed (attempt {split_attempt}/{max_split_attempts}), requesting fixes..."
                )

                split_prompt = load_prompt_callback("task_split_prompt.md")
                split_prompt = split_prompt.replace(
                    "{{VIOLATIONS}}", validation["violations_summary"]
                )
                split_prompt = split_prompt.replace(
                    "{{TASKLIST_CONTENT}}", self.tasklist_provider.get_snapshot()
                )
                split_prompt = apply_provider_placeholders(split_prompt, tasklist_placeholders)
                # Backward-compat: custom --prompts-dir templates may still use {{TASKLIST_PATH}}
                split_prompt = split_prompt.replace("{{TASKLIST_PATH}}", self.tasklist)

                run_agent_callback(split_prompt)
                _invalidate_tasklist_cache(self.tasklist_provider)
                validation = self._validate_generated_tasks(
                    original_tasklist_content,
                    self.tasklist_provider.get_snapshot(),
                )

            state["validation"] = validation
            tasks_after = self.tasklist_provider.list_tasks()
            return [t.title for t in tasks_after if t.task_id not in tasks_before_ids]

        def review_tasks(tasks: list[str]) -> dict:
            added_content = "\n".join(f"- [ ] {t}" for t in tasks)
            return self.review_plan(
                design_path=design_path,
                plan_content=added_content,
                load_prompt_callback=load_prompt_callback,
                run_agent_callback=run_agent_callback,
                log_callback=log_callback,
            )

        # Run the generic loop
        loop = ArtifactReviewLoop(
            name="Planner",
            producer=produce_tasks,
            reviewer=review_tasks,
            is_approved=lambda v: v.get("approved", False),
            max_cycles=self.max_cycles,
        )

        loop_result = loop.run()

        # Revert on loop failure to ensure rejected tasks don't persist
        if not loop_result.success:
            self.tasklist_provider.restore_snapshot(original_tasklist_content)

        final_tasks = loop_result.artifact or []
        tasks_added = len(final_tasks)

        if loop_result.success and tasks_added > 0:
            checker = ReferenceIntegrityChecker(
                opportunity_provider=self.opportunity_provider,
                design_provider=self.design_provider,
            )
            try:
                checker.check_tasks(self.tasklist_provider.list_tasks())
            except ReferenceIntegrityError as exc:
                self.tasklist_provider.restore_snapshot(original_tasklist_content)
                if log_callback:
                    log_callback(
                        "plan_failed",
                        design_path=design_path,
                        reason="reference_integrity_failed",
                        integrity_error=str(exc),
                    )
                print()
                print("=== Planning Failed ===")
                print("Reference integrity check failed:")
                for violation in exc.violations:
                    print(f"  - {violation}")
                return {
                    "success": False,
                    "tasks_added": 0,
                    "error": "Reference integrity check failed",
                    "integrity_error": str(exc),
                }

            result = {
                "success": True,
                "tasks_added": tasks_added,
                "validation": state["validation"],
            }
            try:
                self.design_provider.update_design_status(design_id, DesignStatus.approved)
            except Exception as e:
                progress(
                    f"Warning: Failed to update design status to approved for {design_id}: {e}"
                )

            if log_callback:
                log_callback(
                    "plan_completed",
                    design_path=design_path,
                    tasks_added=str(tasks_added),
                    validation_passed=str(state["validation"]["valid"]),
                    split_attempts=str(state["split_attempts"]),
                    plan_attempts=str(loop_result.cycles),
                )

            print()
            print("=== Planning Complete ===")
            print(f"Design: {design_path}")
            print(f"Tasks added: {tasks_added}")

            if not state["validation"]["valid"]:
                print("Warning: Some tasks still have constraint violations after attempts:")
                print(state["validation"]["violations_summary"])
            elif state["split_attempts"] > 0 or loop_result.cycles > 1:
                print(
                    f"Tasks finalized after {loop_result.cycles} review cycle(s) and {state['split_attempts']} split attempt(s)"
                )
        else:
            result = {
                "success": False,
                "tasks_added": 0,
                "error": loop_result.error or "No tasks added",
            }
            if log_callback:
                log_callback("plan_failed", design_path=design_path, reason=result["error"])
            print(f"\n=== Planning Failed ===\n{result['error']}")

        return result

    # =========================================================================
    # Opportunity selection
    # =========================================================================

    def _select_opportunity(self) -> Opportunity | None:
        """Return the top identified opportunity by ROI score.

        Calls opportunity_provider.list_opportunities(), filters to identified status
        only, sorts by roi_score descending (None treated as 0.0), and returns the
        selected Opportunity record.

        Returns:
            Opportunity or None if no identified opportunities found.
        """
        from millstone.artifacts.models import OpportunityStatus

        opportunities = self.opportunity_provider.list_opportunities()
        identified = [o for o in opportunities if o.status == OpportunityStatus.identified]

        if not identified:
            return None

        ranked = sorted(identified, key=lambda o: o.roi_score or 0.0, reverse=True)
        return ranked[0]

    # =========================================================================
    # Full cycle
    # =========================================================================

    def run_cycle(
        self,
        has_remaining_tasks_callback: Callable[[], bool] | None = None,
        run_callback: Callable[[], int] | None = None,
        run_analyze_callback: Callable[[str | None], dict] | None = None,
        run_design_callback: Callable[..., dict] | None = None,
        review_design_callback: Callable[[str], dict] | None = None,
        run_plan_callback: Callable[[str], dict] | None = None,
        run_eval_callback: Callable[[], dict] | None = None,
        eval_on_commit: bool = False,
        log_callback: Callable[..., None] | None = None,
        save_checkpoint_callback: Callable[..., None] | None = None,
    ) -> int:
        """Run the full autonomous cycle: analyze -> design -> plan -> build -> eval.

        This is the entry point for "just improve this project" mode. Chains together
        all the outer loops with the existing inner loop (build-review-commit).

        If there are pending tasks in the tasklist, skips analysis/design/plan and
        goes directly to executing tasks. If no pending tasks, runs analysis to find
        opportunities, designs a solution for the top opportunity, breaks it into
        tasks, then executes those tasks.

        Approval gates (approve_opportunities, approve_designs, approve_plans) pause
        the cycle at each phase for human review. Set these to False (or use --no-approve)
        for fully autonomous operation.

        Args:
            has_remaining_tasks_callback: Callback to check if tasklist has tasks.
            run_callback: Callback to run the inner loop (task execution).
            run_analyze_callback: Callback to run analysis.
            run_design_callback: Callback to run design.
            review_design_callback: Callback to review design.
            run_plan_callback: Callback to run planning.
            run_eval_callback: Callback to run evaluation.
            eval_on_commit: Whether eval_on_commit is enabled.
            log_callback: Optional callback for logging events.

        Returns:
            Exit code: 0 on success, 1 on failure or halt.
        """
        # Initialize cycle logging
        self._setup_cycle_logging()

        # Step 1: Check for pending tasks first
        if has_remaining_tasks_callback and has_remaining_tasks_callback():
            if run_callback is None:
                raise ValueError("run_callback is required")
            self._cycle_log("SKIP", "Pending tasks found in tasklist, skipping to execution")
            progress("Pending tasks found in tasklist. Executing tasks...")
            result = run_callback()
            self._cycle_log_complete("SUCCESS" if result == 0 else "FAILED")
            return result

        # Step 2: No pending tasks - Determine objective
        # Priority: 1. Roadmap goal, 2. Highest ROI opportunity
        roadmap_goal = self._get_next_roadmap_goal()
        selected = None

        if roadmap_goal:
            progress(f"Found goal in roadmap: {roadmap_goal[:50]}...")
            self._cycle_log("SELECT", f"Roadmap Goal: {roadmap_goal}")
        else:
            # No pending tasks and no roadmap - run analysis to find opportunities
            if run_analyze_callback is None:
                raise ValueError("run_analyze_callback is required")
            progress("No pending tasks or roadmap goals. Running analysis...")
            analyze_result = run_analyze_callback(None)

            if not analyze_result.get("success", False):
                self._cycle_log("ANALYZE", "Failed - opportunities.md not created")
                self._cycle_log_complete("FAILED")
                progress("Analysis failed. Cannot continue cycle.")
                return 1

            opportunity_count = analyze_result.get("opportunity_count", 0)
            self._cycle_log("ANALYZE", f"Found {opportunity_count} opportunities")

            selected = self._select_opportunity()
            if not selected:
                self._cycle_log("SELECT", "No opportunities found")
                self._cycle_log_complete("SUCCESS")
                progress("No opportunities found. Cycle complete.")
                return 0
            self._cycle_log(
                "SELECT",
                json.dumps(
                    {
                        "opportunity_id": selected.opportunity_id,
                        "title": selected.title,
                        "roi_score": selected.roi_score,
                        "requires_design": selected.requires_design,
                    }
                ),
            )
            from millstone.artifacts.models import OpportunityStatus

            self.opportunity_provider.update_opportunity_status(
                selected.opportunity_id, OpportunityStatus.adopted
            )
            self._cycle_log(
                "ADOPT",
                json.dumps(
                    {
                        "opportunity_id": selected.opportunity_id,
                        "title": selected.title,
                        "status": OpportunityStatus.adopted.value,
                    }
                ),
            )

        # Approval gate: pause after analyze for human to pick opportunity
        # (Skip if using roadmap, as roadmap is human-curated)
        if not roadmap_goal and self.approve_opportunities:
            self._cycle_log("GATE", "Paused at opportunities approval gate")
            self._cycle_log_complete("HALTED")
            progress("")
            progress("=" * 60)
            progress("APPROVAL GATE: Opportunities identified")
            progress("=" * 60)
            progress("")
            if selected is not None:
                progress(f"Selected opportunity: {selected.opportunity_id} ({selected.title})")
            progress("Review opportunities.md and re-run with:")
            progress("  millstone --design '<opportunity description>'")
            progress("")
            progress("Or run with --no-approve for fully autonomous operation.")
            if save_checkpoint_callback is not None:
                opportunity_text = selected.title if selected is not None else ""
                save_checkpoint_callback("analyze_complete", opportunity=opportunity_text)
            return 0

        # Step 4: Design
        if run_design_callback is None:
            raise ValueError("run_design_callback is required")
        if roadmap_goal:
            objective = roadmap_goal
        else:
            assert selected is not None
            objective = selected.title
        progress(f"Designing solution for: {objective}")
        if selected is not None:
            design_result = run_design_callback(
                objective,
                opportunity_id=selected.opportunity_id,
            )
        else:
            design_result = run_design_callback(objective)

        if not design_result.get("success", False):
            self._cycle_log("DESIGN", "Failed - no design artifact created")
            self._cycle_log_complete("FAILED")
            progress("Design failed. Halting for human review.")
            return 1

        design_path = design_result.get("design_file")
        design_id = design_result.get("design_id")
        design_ref = design_path or design_id
        if not design_ref:
            self._cycle_log("DESIGN", "Failed - no design reference returned")
            self._cycle_log_complete("FAILED")
            progress("Design failed. Halting for human review.")
            return 1
        self._cycle_log("DESIGN", f"Created {design_ref}")

        # Step 5: Review design (if configured)
        if self.review_designs:
            if review_design_callback is None:
                raise ValueError("review_design_callback is required")
            review_result = review_design_callback(design_ref)
            verdict = review_result.get("verdict", "UNKNOWN")
            self._cycle_log("REVIEW", verdict)
            if not review_result.get("approved", False):
                self._cycle_log_complete("FAILED")
                progress("Design needs revision. Halting for human review.")
                return 1

        # Approval gate: pause after design for human review
        if self.approve_designs:
            self._cycle_log("GATE", "Paused at design approval gate")
            self._cycle_log_complete("HALTED")
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
            if save_checkpoint_callback is not None:
                save_checkpoint_callback(
                    "design_complete", design_path=design_ref, opportunity=objective
                )
            return 0

        # Step 6: Plan
        if run_plan_callback is None:
            raise ValueError("run_plan_callback is required")
        progress("Breaking design into tasks...")
        plan_result = run_plan_callback(design_ref)

        if not plan_result.get("success", False):
            self._cycle_log("PLAN", "Failed - no tasks added")
            self._cycle_log_complete("FAILED")
            progress("Planning failed. Halting for human review.")
            return 1

        tasks_added = plan_result.get("tasks_added", 0)
        self._cycle_log("PLAN", f"Added {tasks_added} tasks to tasklist")

        # Approval gate: pause after plan for human review
        if self.approve_plans:
            self._cycle_log("GATE", "Paused at plan approval gate")
            self._cycle_log_complete("HALTED")
            progress("")
            progress("=" * 60)
            progress("APPROVAL GATE: Tasks added to tasklist")
            progress("=" * 60)
            progress("")
            progress(f"Review the new tasks in: {self.tasklist}")
            progress("Then re-run to execute:")
            progress("  millstone")
            progress("")
            progress("Or run with --no-approve for fully autonomous operation.")
            if save_checkpoint_callback is not None:
                save_checkpoint_callback(
                    "plan_complete", design_path=design_ref, tasks_created=tasks_added
                )
            return 0

        # Step 7: Run inner loop (existing task execution)
        if run_callback is None:
            raise ValueError("run_callback is required")
        progress("Executing tasks...")
        self._cycle_log("EXECUTE", "Starting task execution")
        result = run_callback()

        if result == 0:
            self._cycle_log("EXECUTE", "All tasks completed successfully")
            if roadmap_goal:
                self._mark_roadmap_goal_complete(roadmap_goal)
        else:
            self._cycle_log("EXECUTE", "Task execution failed or halted")

        # Step 8: Final eval (if eval_on_commit is enabled)
        # Note: eval is already run during the inner loop if eval_on_commit is True
        # This is just for logging the final state
        if result == 0 and eval_on_commit and run_eval_callback:
            # Run a final eval and log the result
            eval_result = run_eval_callback()
            tests = eval_result.get("tests", {})
            passed = tests.get("passed", 0)
            total = tests.get("total", 0)
            coverage = eval_result.get("coverage", {})
            if coverage:
                cov_pct = round(coverage.get("line_rate", 0) * 100, 1)
                self._cycle_log("EVAL", f"{passed}/{total} tests passed, coverage {cov_pct}%")
            else:
                self._cycle_log("EVAL", f"{passed}/{total} tests passed")
            progress("Cycle completed successfully with evals.")

        self._cycle_log_complete("SUCCESS" if result == 0 else "FAILED")
        return result
