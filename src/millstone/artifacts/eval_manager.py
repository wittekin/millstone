"""
Evaluation and metrics management for the millstone orchestrator.

This module contains the EvalManager class which handles all evaluation
and metrics functionality. The Orchestrator class holds an instance
and delegates via thin wrapper methods.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from millstone.utils import progress

if TYPE_CHECKING:
    # Avoid circular import - only used for type hints
    pass


class EvalManager:
    """Manages evaluation, metrics tracking, and rollback functionality.

    This class handles all operations related to:
    - Running tests and capturing results
    - Category-based scoring (tests, typing, lint, security, complexity)
    - Composite score calculation
    - Eval comparison and trend tracking
    - Task metrics and review metrics
    - Eval gating and rollback on regression
    """

    def __init__(
        self,
        work_dir: Path,
        repo_dir: Path,
        project_config: dict,
        policy: dict,
        category_weights: dict[str, float],
        category_thresholds: dict[str, int],
        eval_scripts: list[str] | None = None,
    ):
        """Initialize the EvalManager.

        Args:
            work_dir: Path to the work directory (.millstone/).
            repo_dir: Path to the repository root.
            project_config: Project configuration dict (from load_project_config).
            policy: Policy configuration dict (from load_policy).
            category_weights: Dict of category name to weight for composite score.
            category_thresholds: Dict of category name to error threshold.
            eval_scripts: List of custom eval scripts to run.
        """
        self.work_dir = work_dir
        self.repo_dir = repo_dir
        self.project_config = project_config
        self.policy = policy
        self.category_weights = category_weights
        self.category_thresholds = category_thresholds
        self.eval_scripts = eval_scripts or []
        # Baseline eval for comparison (set by orchestrator at run start)
        self.baseline_eval: dict | None = None
        # Context from last rollback for next cycle
        self.last_rollback_context: dict | None = None

    def git(self, *args) -> str:
        """Run git command and return output."""
        result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=self.repo_dir)
        return result.stdout

    # =========================================================================
    # Core eval methods
    # =========================================================================

    def run_eval(
        self,
        coverage: bool = False,
        mode: str | None = None,
        log_callback: Callable[..., None] | None = None,
        run_custom_eval_scripts_callback: Callable[[], list] | None = None,
        emit_evidence_callback: Callable[[dict], None] | None = None,
    ) -> dict:
        """Run tests and capture results.

        Uses configured test command from project.toml, falling back to
        auto-detected commands based on project type.
        Stores structured results in `.millstone/evals/<timestamp>.json`.

        Args:
            coverage: If True, run with coverage enabled.
            mode: Eval mode - "smoke" (quick tests), "full" (all tests + coverage),
                  or a path to a custom test suite/script. If None, uses standard
                  behavior based on coverage flag.
            log_callback: Optional callback for logging events.
            run_custom_eval_scripts_callback: Optional callback to run custom eval
                scripts (defaults to self._run_custom_eval_scripts).

        Returns:
            Dict with eval results including test counts and pass/fail status.
        """
        timestamp = datetime.now()
        timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S")

        # Capture git HEAD
        git_head = self.git("rev-parse", "HEAD").strip()

        # Determine test command based on mode
        tests_config = self.project_config.get("tests", {})
        test_cmd_str = ""

        if mode == "smoke":
            # Smoke mode: use smoke_command from config, or fast pytest subset
            test_cmd_str = tests_config.get("smoke_command", "")
            if not test_cmd_str:
                # Default smoke: run with -x (fail fast) and no coverage
                test_cmd_str = "pytest tests/ --tb=short -q -x"
            coverage = False  # Smoke mode never runs coverage
        elif mode == "full":
            # Full mode: run with coverage
            test_cmd_str = tests_config.get("coverage_command", "")
            if not test_cmd_str:
                test_cmd_str = "pytest tests/ --cov=. --cov-report=json --tb=short -q"
            coverage = True
        elif mode and mode not in ("none", "smoke", "full"):
            # Custom path mode: treat mode as a path to custom test suite/script
            custom_path = Path(mode)
            if custom_path.suffix in (".sh", ".bash"):
                # Shell script
                test_cmd_str = f"bash {mode}"
            elif custom_path.suffix == ".py":
                # Python script
                test_cmd_str = f"python {mode}"
            elif custom_path.is_dir() or "/" in mode:
                # Directory path - run pytest on it
                test_cmd_str = f"pytest {mode} --tb=short -q"
            else:
                # Assume it's a command
                test_cmd_str = mode
        else:
            # Standard mode (None or legacy)
            if coverage:
                test_cmd_str = tests_config.get("coverage_command", "")
            else:
                test_cmd_str = tests_config.get("command", "")

            # Fall back to hardcoded pytest if no command configured
            if not test_cmd_str:
                test_cmd_str = "pytest tests/ --tb=short -q"
                if coverage:
                    test_cmd_str = "pytest tests/ --cov=. --cov-report=json --tb=short -q"

        # Run test command and capture timing
        start_time = time.time()
        result = subprocess.run(
            test_cmd_str,
            shell=True,
            capture_output=True,
            text=True,
            cwd=self.repo_dir,
        )
        duration = time.time() - start_time

        # Parse output to extract test counts (works for pytest-style output)
        output = result.stdout + result.stderr
        test_results = self._parse_pytest_output(output)

        # Build eval result
        eval_result: dict[str, Any] = {
            "timestamp": timestamp.isoformat(),
            "git_head": git_head,
            "duration_seconds": round(duration, 2),
            "tests": test_results,
            "failed_tests": self._extract_failed_tests(output),
        }

        # Add coverage if requested and available
        if coverage:
            coverage_data = self._parse_coverage_json()
            if coverage_data:
                eval_result["coverage"] = coverage_data

        # Run custom eval scripts if configured
        custom_scripts_results = []
        all_scripts_passed = True
        if self.eval_scripts:
            if run_custom_eval_scripts_callback:
                custom_scripts_results = run_custom_eval_scripts_callback()
            else:
                custom_scripts_results = self._run_custom_eval_scripts()
            eval_result["custom_scripts"] = custom_scripts_results
            # Check if any custom script failed
            for script_result in custom_scripts_results:
                if script_result.get("exit_code", 0) != 0:
                    all_scripts_passed = False
                    break

        # Run category evaluations and compute composite score
        coverage_data_value = eval_result.get("coverage")
        coverage_data = coverage_data_value if isinstance(coverage_data_value, dict) else None
        category_results = self.run_category_evals(test_results, coverage_data)
        eval_result["categories"] = category_results["categories"]
        eval_result["composite_score"] = category_results["composite_score"]

        # Store results
        evals_dir = self.work_dir / "evals"
        evals_dir.mkdir(exist_ok=True)

        # Find previous eval for delta tracking (exclude summary.json)
        json_files = sorted(f for f in evals_dir.glob("*.json") if f.name != "summary.json")
        previous_eval = None
        previous_eval_file = None
        if json_files:
            previous_eval_file = json_files[-1]
            previous_eval = json.loads(previous_eval_file.read_text())
            eval_result["previous_eval"] = previous_eval_file.name

        # Compute delta from previous eval
        if previous_eval:
            eval_result["delta"] = self._compute_eval_delta(previous_eval, eval_result)

        eval_file = evals_dir / f"{timestamp_str}.json"
        eval_file.write_text(json.dumps(eval_result, indent=2))
        eval_result["_eval_file"] = eval_file.name

        # Update summary.json for time-series tracking
        self._update_eval_summary(evals_dir, timestamp_str, eval_result)

        if log_callback:
            log_callback(
                "eval_completed",
                timestamp=timestamp_str,
                tests_total=str(test_results.get("total", 0)),
                tests_passed=str(test_results.get("passed", 0)),
                tests_failed=str(test_results.get("failed", 0)),
                duration=f"{duration:.2f}s",
                custom_scripts_count=str(len(custom_scripts_results)),
                custom_scripts_passed=str(all_scripts_passed),
                composite_score=str(category_results["composite_score"]),
            )

        # Print human-readable summary
        self._print_eval_summary(eval_result)

        # Print trend warnings if there's a previous eval to compare against
        delta = eval_result.get("delta")
        if previous_eval and isinstance(delta, dict):
            self._print_eval_trend_warnings(previous_eval, eval_result, delta, log_callback)

        # Store pass/fail status for exit code (pytest must pass AND all custom scripts must pass)
        pytest_passed = test_results.get("failed", 0) == 0 and test_results.get("errors", 0) == 0
        eval_result["_passed"] = pytest_passed and all_scripts_passed

        if emit_evidence_callback:
            emit_evidence_callback(eval_result)

        return eval_result

    def compare_evals(self, log_callback: Callable[..., None] | None = None) -> dict:
        """Compare the two most recent eval results.

        Finds the two most recent JSON files in `.millstone/evals/`, compares them,
        and reports what changed (new failures, new passes, coverage delta, etc.).

        Args:
            log_callback: Optional callback for logging events.

        Returns:
            Dict with comparison results including:
            - older_file, newer_file: Filenames compared
            - new_failures: Tests that started failing
            - new_passes: Tests that started passing
            - coverage_delta: Coverage change (if both have coverage)
            - duration_delta: Duration change
            - status: "REGRESSION", "IMPROVEMENT", or "NO_CHANGE"
            - _has_regressions: Boolean for exit code

        Raises:
            FileNotFoundError: If fewer than 2 eval files exist.
        """
        evals_dir = self.work_dir / "evals"
        if not evals_dir.exists():
            raise FileNotFoundError("No evals directory found. Run --eval first.")

        # Find all JSON files, sorted by filename (exclude summary.json)
        json_files = sorted(f for f in evals_dir.glob("*.json") if f.name != "summary.json")
        if len(json_files) < 2:
            raise FileNotFoundError(
                f"Need at least 2 eval files to compare, found {len(json_files)}. "
                "Run --eval multiple times first."
            )

        # Get the two most recent
        older_file = json_files[-2]
        newer_file = json_files[-1]

        older_data = json.loads(older_file.read_text())
        newer_data = json.loads(newer_file.read_text())

        # Extract test data
        older_tests = older_data.get("tests", {})
        newer_tests = newer_data.get("tests", {})
        older_failed = set(older_data.get("failed_tests", []))
        newer_failed = set(newer_data.get("failed_tests", []))

        # Compute deltas
        new_failures = newer_failed - older_failed
        new_passes = older_failed - newer_failed

        # Coverage delta (if both have coverage)
        coverage_delta = None
        older_cov = older_data.get("coverage", {})
        newer_cov = newer_data.get("coverage", {})
        if older_cov and newer_cov:
            older_rate = older_cov.get("line_rate", 0)
            newer_rate = newer_cov.get("line_rate", 0)
            coverage_delta = round((newer_rate - older_rate) * 100, 1)

        # Duration delta
        older_duration = older_data.get("duration_seconds", 0)
        newer_duration = newer_data.get("duration_seconds", 0)
        duration_delta = round(newer_duration - older_duration, 1)

        # Composite score delta
        composite_delta = None
        older_composite = older_data.get("composite_score")
        newer_composite = newer_data.get("composite_score")
        if older_composite is not None and newer_composite is not None:
            composite_delta = round(newer_composite - older_composite, 4)

        # Category-by-category breakdown
        category_deltas = {}
        older_cats = older_data.get("categories", {})
        newer_cats = newer_data.get("categories", {})
        all_categories = set(older_cats.keys()) | set(newer_cats.keys())
        for cat in sorted(all_categories):
            older_score = older_cats.get(cat, {}).get("score")
            newer_score = newer_cats.get(cat, {}).get("score")
            if older_score is not None and newer_score is not None:
                category_deltas[cat] = {
                    "older": round(older_score, 4),
                    "newer": round(newer_score, 4),
                    "delta": round(newer_score - older_score, 4),
                }

        # Determine overall status
        if new_failures:
            status = "REGRESSION"
        elif new_passes:
            status = "IMPROVEMENT"
        else:
            status = "NO_CHANGE"

        result = {
            "older_file": older_file.name,
            "newer_file": newer_file.name,
            "older_tests": older_tests,
            "newer_tests": newer_tests,
            "new_failures": sorted(new_failures),
            "new_passes": sorted(new_passes),
            "coverage_delta": coverage_delta,
            "duration_delta": duration_delta,
            "composite_delta": composite_delta,
            "category_deltas": category_deltas,
            "status": status,
            "_has_regressions": len(new_failures) > 0,
        }

        # Print comparison
        self._print_eval_comparison(result)

        # Log the comparison
        if log_callback:
            log_callback(
                "eval_comparison",
                older_file=older_file.name,
                newer_file=newer_file.name,
                new_failures=str(len(new_failures)),
                new_passes=str(len(new_passes)),
                status=status,
            )

        return result

    def _print_eval_comparison(self, result: dict) -> None:
        """Print human-readable eval comparison to stdout.

        Args:
            result: The comparison result dict from compare_evals().
        """
        older_tests = result["older_tests"]
        newer_tests = result["newer_tests"]

        older_passed = older_tests.get("passed", 0)
        older_total = older_tests.get("total", 0)
        newer_passed = newer_tests.get("passed", 0)
        newer_total = newer_tests.get("total", 0)

        passed_delta = newer_passed - older_passed

        print()
        print(f"Comparing: {result['older_file']} → {result['newer_file']}")
        print()

        # Tests summary
        delta_str = f"({passed_delta:+d})" if passed_delta != 0 else "(no change)"
        print(
            f"Tests: {older_passed}/{older_total} passed → {newer_passed}/{newer_total} passed {delta_str}"
        )

        # Coverage (if available)
        if result["coverage_delta"] is not None:
            # Calculate actual percentages from test data if available
            # For display, we show the delta
            delta = result["coverage_delta"]
            delta_str = f"({delta:+.1f}%)" if delta != 0 else "(no change)"
            print(f"Coverage: {delta_str}")

        # Duration
        duration_delta = result["duration_delta"]
        delta_str = f"({duration_delta:+.1f}s)" if duration_delta != 0 else "(no change)"
        print(f"Duration: {delta_str}")

        # Composite score (if available)
        composite_delta = result.get("composite_delta")
        if composite_delta is not None:
            delta_str = f"({composite_delta:+.4f})" if composite_delta != 0 else "(no change)"
            print(f"Composite Score: {delta_str}")

        # Category-by-category breakdown
        category_deltas = result.get("category_deltas", {})
        if category_deltas:
            print()
            print("Category Breakdown:")
            for cat, data in sorted(category_deltas.items()):
                delta = data["delta"]
                delta_str = f"({delta:+.4f})" if delta != 0 else "(no change)"
                print(f"  {cat}: {data['older']:.4f} → {data['newer']:.4f} {delta_str}")

        # New failures
        print()
        new_failures = result["new_failures"]
        if new_failures:
            print("New failures:")
            for test in new_failures[:10]:
                print(f"  - {test}")
            if len(new_failures) > 10:
                print(f"  ... and {len(new_failures) - 10} more")
        else:
            print("New failures:")
            print("  (none)")

        # New passes
        print()
        new_passes = result["new_passes"]
        if new_passes:
            print("New passes:")
            for test in new_passes[:10]:
                print(f"  - {test}")
            if len(new_passes) > 10:
                print(f"  ... and {len(new_passes) - 10} more")
        else:
            print("New passes:")
            print("  (none)")

        # JSON summary line for programmatic parsing
        print()
        summary = {
            "status": result["status"],
            "new_failures": len(new_failures),
            "new_passes": len(new_passes),
        }
        if result["coverage_delta"] is not None:
            summary["coverage_delta"] = result["coverage_delta"]
        if result.get("composite_delta") is not None:
            summary["composite_delta"] = result["composite_delta"]
        if result.get("category_deltas"):
            summary["category_deltas"] = {
                cat: data["delta"] for cat, data in result["category_deltas"].items()
            }
        print(json.dumps(summary))

    def _parse_pytest_output(self, output: str) -> dict:
        """Parse pytest output to extract test counts.

        Args:
            output: Combined stdout/stderr from pytest.

        Returns:
            Dict with total, passed, failed, errors, skipped counts.
        """
        # Default values
        results = {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
        }

        # Look for pytest summary line like "5 passed, 2 failed, 1 error in 1.23s"
        # or "10 passed in 0.50s"
        summary_pattern = r"(\d+)\s+passed"
        failed_pattern = r"(\d+)\s+failed"
        error_pattern = r"(\d+)\s+error"
        skipped_pattern = r"(\d+)\s+skipped"

        passed_match = re.search(summary_pattern, output)
        if passed_match:
            results["passed"] = int(passed_match.group(1))

        failed_match = re.search(failed_pattern, output)
        if failed_match:
            results["failed"] = int(failed_match.group(1))

        error_match = re.search(error_pattern, output)
        if error_match:
            results["errors"] = int(error_match.group(1))

        skipped_match = re.search(skipped_pattern, output)
        if skipped_match:
            results["skipped"] = int(skipped_match.group(1))

        results["total"] = (
            results["passed"] + results["failed"] + results["errors"] + results["skipped"]
        )

        return results

    def _extract_failed_tests(self, output: str) -> list[str]:
        """Extract list of failed test names from pytest output.

        Args:
            output: Combined stdout/stderr from pytest.

        Returns:
            List of failed test names (e.g., ["test_foo.py::test_bar"]).
        """
        failed_tests = []

        # Match FAILED lines like "FAILED tests/test_foo.py::test_bar - AssertionError"
        pattern = r"FAILED\s+([\w/._:]+)"
        for match in re.finditer(pattern, output):
            failed_tests.append(match.group(1))

        return failed_tests

    def _parse_coverage_json(self) -> dict | None:
        """Parse coverage.json if it exists.

        Returns:
            Dict with line_rate and branch_rate, or None if no coverage data.
        """
        coverage_file = self.repo_dir / "coverage.json"
        if not coverage_file.exists():
            return None

        try:
            data = json.loads(coverage_file.read_text())
            totals = data.get("totals", {})
            return {
                "line_rate": round(totals.get("percent_covered", 0) / 100, 2),
                "branch_rate": (
                    round(totals.get("percent_covered_branches", 0) / 100, 2)
                    if "percent_covered_branches" in totals
                    else None
                ),
            }
        except (json.JSONDecodeError, KeyError):
            return None

    def _run_custom_eval_scripts(self) -> list[dict]:
        """Run custom eval scripts and return results.

        Iterates through self.eval_scripts, running each command via subprocess
        with a 60-second timeout. Captures exit code, stdout, stderr, and duration.

        Returns:
            List of dicts with command, exit_code, duration, stdout, stderr for each script.
        """
        results = []
        for command in self.eval_scripts:
            start_time = time.time()
            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=self.repo_dir,
                )
                duration = time.time() - start_time
                results.append(
                    {
                        "command": command,
                        "exit_code": result.returncode,
                        "duration": round(duration, 2),
                        "stdout": result.stdout[:5000],  # Limit output size
                        "stderr": result.stderr[:5000],
                    }
                )
            except subprocess.TimeoutExpired:
                duration = time.time() - start_time
                results.append(
                    {
                        "command": command,
                        "exit_code": -1,  # Use -1 to indicate timeout
                        "duration": round(duration, 2),
                        "stdout": "",
                        "stderr": "Command timed out after 60 seconds",
                    }
                )
            except Exception as e:
                duration = time.time() - start_time
                results.append(
                    {
                        "command": command,
                        "exit_code": -2,  # Use -2 to indicate other error
                        "duration": round(duration, 2),
                        "stdout": "",
                        "stderr": str(e),
                    }
                )

        return results

    # =========================================================================
    # Category scoring methods
    # =========================================================================

    def run_category_evals(self, test_results: dict, coverage_data: dict | None) -> dict:
        """Run category evaluations and compute scores.

        Runs available tools (mypy, ruff, bandit, radon) and computes
        0.0-1.0 scores for each category. Categories without available
        tools are skipped.

        Args:
            test_results: Dict with test pass/fail counts.
            coverage_data: Dict with line_rate, or None if no coverage.

        Returns:
            Dict with categories and composite_score:
            {
                "categories": {
                    "tests": {"score": 0.96, "passed": 48, "failed": 2},
                    "typing": {"score": 1.0, "errors": 0},
                    ...
                },
                "composite_score": 0.94
            }
        """
        categories = {}

        # Tests category (always available)
        total = test_results.get("total", 0)
        passed = test_results.get("passed", 0)
        failed = test_results.get("failed", 0)
        errors = test_results.get("errors", 0)
        tests_score = passed / total if total > 0 else 1.0  # No tests = perfect score
        categories["tests"] = {
            "score": round(tests_score, 2),
            "passed": passed,
            "failed": failed + errors,
        }

        # Coverage category (if coverage data available)
        if coverage_data:
            line_rate = coverage_data.get("line_rate", 0)
            categories["coverage"] = {
                "score": round(line_rate, 2),
                "line_rate": line_rate,
            }

        # Typing category - use configured command or fall back to mypy
        typing_cmd = self.project_config.get("typing", {}).get("command", "")
        if typing_cmd or shutil.which("mypy"):
            typing_result = self._run_typing(typing_cmd)
            categories["typing"] = typing_result

        # Lint category - use configured command or fall back to ruff
        lint_cmd = self.project_config.get("lint", {}).get("command", "")
        if lint_cmd or shutil.which("ruff"):
            lint_result = self._run_lint(lint_cmd)
            categories["lint"] = lint_result

        # Security category (bandit) - optional
        if shutil.which("bandit"):
            security_result = self._run_bandit()
            categories["security"] = security_result

        # Complexity category (radon) - optional
        if shutil.which("radon"):
            complexity_result = self._run_radon()
            categories["complexity"] = complexity_result

        # Compute composite score
        composite_score = self._compute_composite_score(categories)

        return {
            "categories": categories,
            "composite_score": composite_score,
        }

    def _run_typing(self, cmd: str = "") -> dict:
        """Run typing check command and return scoring data.

        Args:
            cmd: Custom typing command. If empty, falls back to mypy.

        Returns:
            Dict with score (0.0-1.0) and error count.
        """
        try:
            if cmd:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=self.repo_dir,
                )
            else:
                result = subprocess.run(
                    ["mypy", "."],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=self.repo_dir,
                )
            # Count errors from output (lines with ": error:" for mypy-style output)
            # This heuristic works for mypy and tsc
            error_count = len(re.findall(r": error[:\[]", result.stdout + result.stderr))
            threshold = self.category_thresholds.get("typing", 50)
            score = max(0.0, 1.0 - (error_count / threshold))
            return {
                "score": round(score, 2),
                "errors": error_count,
            }
        except (subprocess.TimeoutExpired, Exception):
            return {"score": 0.0, "errors": -1}  # -1 indicates tool failure

    def _run_lint(self, cmd: str = "") -> dict:
        """Run lint command and return scoring data.

        Args:
            cmd: Custom lint command. If empty, falls back to ruff.

        Returns:
            Dict with score (0.0-1.0), error count, and warning count.
        """
        try:
            if cmd:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=self.repo_dir,
                )
                # Exit code 0 means the linter found no issues.
                # For non-zero exit, count non-empty output lines as a proxy for issue count.
                if result.returncode == 0:
                    error_count = 0
                else:
                    error_count = len([line for line in result.stdout.split("\n") if line.strip()])
            else:
                result = subprocess.run(
                    ["ruff", "check", ".", "--output-format=json"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=self.repo_dir,
                )
                try:
                    issues = json.loads(result.stdout)
                    error_count = len(issues)
                except json.JSONDecodeError:
                    # Fallback: count lines in output
                    error_count = result.stdout.count("\n")
            threshold = self.category_thresholds.get("lint", 100)
            score = max(0.0, 1.0 - (error_count / threshold))
            return {
                "score": round(score, 2),
                "errors": error_count,
                "warnings": 0,  # Most linters don't distinguish warnings by default
            }
        except (subprocess.TimeoutExpired, Exception):
            return {"score": 0.0, "errors": -1, "warnings": 0}

    def _run_bandit(self) -> dict:
        """Run bandit security scanner and return scoring data.

        Returns:
            Dict with score (0.0-1.0) and issue count.
        """
        try:
            result = subprocess.run(
                ["bandit", "-r", ".", "-f", "json", "-q"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=self.repo_dir,
            )
            try:
                data = json.loads(result.stdout)
                issue_count = len(data.get("results", []))
            except json.JSONDecodeError:
                issue_count = 0 if result.returncode == 0 else 1
            threshold = self.category_thresholds.get("security", 10)
            score = max(0.0, 1.0 - (issue_count / threshold))
            return {
                "score": round(score, 2),
                "issues": issue_count,
            }
        except (subprocess.TimeoutExpired, Exception):
            return {"score": 0.0, "issues": -1}

    def _run_radon(self) -> dict:
        """Run radon complexity analysis and return scoring data.

        Returns:
            Dict with score (0.0-1.0) and count of high-complexity functions.
        """
        try:
            result = subprocess.run(
                ["radon", "cc", "-s", "-j", "."],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=self.repo_dir,
            )
            # Parse JSON output and count functions with grade C or worse (complexity >= 11)
            high_complexity = 0
            try:
                data = json.loads(result.stdout)
                for file_results in data.values():
                    for item in file_results:
                        complexity = item.get("complexity", 0)
                        if complexity >= 11:  # Grade C or worse
                            high_complexity += 1
            except json.JSONDecodeError:
                pass
            threshold = self.category_thresholds.get("complexity", 20)
            score = max(0.0, 1.0 - (high_complexity / threshold))
            return {
                "score": round(score, 2),
                "high_complexity_functions": high_complexity,
            }
        except (subprocess.TimeoutExpired, Exception):
            return {"score": 0.0, "high_complexity_functions": -1}

    def _compute_composite_score(self, categories: dict) -> float:
        """Compute weighted average of category scores.

        Only includes categories that have scores. Weights are normalized
        to sum to 1.0 for the available categories.

        Args:
            categories: Dict of category name to result dict with 'score' key.

        Returns:
            Weighted average score (0.0-1.0).
        """
        if not categories:
            return 0.0

        total_weight = 0.0
        weighted_sum = 0.0

        for name, result in categories.items():
            weight = self.category_weights.get(name, 0.0)
            if weight > 0 and "score" in result:
                total_weight += weight
                weighted_sum += weight * result["score"]

        if total_weight == 0:
            return 0.0

        # Normalize to available weights
        return round(weighted_sum / total_weight, 2)

    def _print_eval_summary(self, eval_result: dict) -> None:
        """Print human-readable eval summary to stdout.

        Args:
            eval_result: The eval result dict.
        """
        tests = eval_result.get("tests", {})
        total = tests.get("total", 0)
        passed = tests.get("passed", 0)
        failed = tests.get("failed", 0)
        errors = tests.get("errors", 0)
        skipped = tests.get("skipped", 0)
        duration = eval_result.get("duration_seconds", 0)

        print()
        print("=== Eval Results ===")
        print(f"Git HEAD: {eval_result.get('git_head', 'unknown')[:8]}")
        print(f"Duration: {duration:.2f}s")
        print()
        print(f"Tests: {passed}/{total} passed", end="")
        if failed:
            print(f", {failed} failed", end="")
        if errors:
            print(f", {errors} errors", end="")
        if skipped:
            print(f", {skipped} skipped", end="")
        print()

        # Show coverage if available
        coverage = eval_result.get("coverage")
        if coverage:
            line_rate = coverage.get("line_rate", 0)
            print(f"Coverage: {line_rate * 100:.1f}%")

        # Show failed tests if any
        failed_tests = eval_result.get("failed_tests", [])
        if failed_tests:
            print()
            print("Failed tests:")
            for test in failed_tests[:10]:  # Limit to first 10
                print(f"  - {test}")
            if len(failed_tests) > 10:
                print(f"  ... and {len(failed_tests) - 10} more")

        # Show custom script results if any
        custom_scripts = eval_result.get("custom_scripts", [])
        if custom_scripts:
            print()
            print("Custom scripts:")
            for script in custom_scripts:
                status = "PASS" if script.get("exit_code", 0) == 0 else "FAIL"
                duration = script.get("duration", 0)
                command = script.get("command", "unknown")
                print(f"  [{status}] {command} ({duration:.2f}s)")

        # Show category scores if available
        categories = eval_result.get("categories", {})
        if categories:
            print()
            print("Category Scores:")
            for name, data in sorted(categories.items()):
                score = data.get("score", 0.0)
                # Format details based on category type
                details = []
                if name == "tests":
                    details.append(
                        f"passed={data.get('passed', 0)}, failed={data.get('failed', 0)}"
                    )
                elif name == "coverage":
                    details.append(f"line_rate={data.get('line_rate', 0):.0%}")
                elif name == "typing" or name == "lint":
                    errors = data.get("errors", 0)
                    details.append(f"errors={errors}" if errors >= 0 else "tool failed")
                elif name == "security":
                    issues = data.get("issues", 0)
                    details.append(f"issues={issues}" if issues >= 0 else "tool failed")
                elif name == "complexity":
                    funcs = data.get("high_complexity_functions", 0)
                    details.append(f"high_complexity={funcs}" if funcs >= 0 else "tool failed")
                detail_str = f" ({', '.join(details)})" if details else ""
                print(f"  {name}: {score:.2f}{detail_str}")

        # Show composite score if available
        composite_score = eval_result.get("composite_score")
        if composite_score is not None:
            print()
            print(f"Composite Score: {composite_score:.2f}")

        # Calculate overall status
        pytest_passed = failed == 0 and errors == 0
        scripts_passed = (
            all(s.get("exit_code", 0) == 0 for s in custom_scripts) if custom_scripts else True
        )

        print()
        if pytest_passed and scripts_passed:
            print("Status: PASSED")
        else:
            print("Status: FAILED")

    # =========================================================================
    # Trend tracking methods
    # =========================================================================

    def _compute_eval_delta(self, previous: dict, current: dict) -> dict:
        """Compute delta between two eval results.

        Calculates changes in tests, categories, and composite score
        between the previous and current eval results.

        Args:
            previous: The previous eval result dict.
            current: The current eval result dict.

        Returns:
            Dict with delta values for composite score, tests, and categories.
        """
        delta: dict = {}

        # Composite score delta
        prev_composite = previous.get("composite_score")
        curr_composite = current.get("composite_score")
        if prev_composite is not None and curr_composite is not None:
            delta["composite"] = round(curr_composite - prev_composite, 4)

        # Tests delta
        prev_tests = previous.get("tests", {})
        curr_tests = current.get("tests", {})
        if prev_tests and curr_tests:
            delta["tests"] = {
                "passed": curr_tests.get("passed", 0) - prev_tests.get("passed", 0),
                "failed": curr_tests.get("failed", 0) - prev_tests.get("failed", 0),
            }

        # Coverage delta (as a value, not percentage points)
        prev_cov = previous.get("coverage", {})
        curr_cov = current.get("coverage", {})
        if prev_cov and curr_cov:
            prev_rate = prev_cov.get("line_rate", 0)
            curr_rate = curr_cov.get("line_rate", 0)
            delta["coverage"] = round(curr_rate - prev_rate, 4)

        # Category score deltas
        prev_cats = previous.get("categories", {})
        curr_cats = current.get("categories", {})
        if prev_cats and curr_cats:
            cat_deltas = {}
            all_categories = set(prev_cats.keys()) | set(curr_cats.keys())
            for cat in all_categories:
                prev_score = prev_cats.get(cat, {}).get("score")
                curr_score = curr_cats.get(cat, {}).get("score")
                if prev_score is not None and curr_score is not None:
                    cat_deltas[cat] = round(curr_score - prev_score, 4)
            if cat_deltas:
                delta["categories"] = cat_deltas

        return delta

    def _print_eval_trend_warnings(
        self,
        previous_eval: dict,
        current_eval: dict,
        delta: dict,
        log_callback: Callable[..., None] | None = None,
    ) -> bool:
        """Print warnings if eval trends show regressions.

        Compares current eval to the previous run and warns if:
        - Pass rate decreased (more failures)
        - New test failures appeared
        - Composite score decreased

        Args:
            previous_eval: The previous eval result dict.
            current_eval: The current eval result dict.
            delta: The computed delta between previous and current.
            log_callback: Optional callback for logging events.

        Returns:
            True if any warnings were printed (regressions detected), False otherwise.
        """
        warnings_printed = False

        # Check for new test failures
        prev_failed = set(previous_eval.get("failed_tests", []))
        curr_failed = set(current_eval.get("failed_tests", []))
        new_failures = curr_failed - prev_failed

        if new_failures:
            warnings_printed = True
            print()
            print("WARNING: New test failures detected!")
            for test in sorted(new_failures)[:10]:
                print(f"  - {test}")
            if len(new_failures) > 10:
                print(f"  ... and {len(new_failures) - 10} more")

        # Check for pass rate decrease
        tests_delta = delta.get("tests", {})
        passed_delta = tests_delta.get("passed", 0)
        failed_delta = tests_delta.get("failed", 0)

        if passed_delta < 0:
            warnings_printed = True
            print()
            print(f"WARNING: Pass rate decreased ({passed_delta:+d} passed)")

        if failed_delta > 0:
            warnings_printed = True
            print()
            print(f"WARNING: Failure count increased ({failed_delta:+d} failed)")

        # Check for composite score decrease
        composite_delta = delta.get("composite")
        if composite_delta is not None and composite_delta < 0:
            warnings_printed = True
            prev_score = previous_eval.get("composite_score", 0)
            curr_score = current_eval.get("composite_score", 0)
            print()
            print(
                f"WARNING: Composite score decreased ({prev_score:.4f} -> {curr_score:.4f}, {composite_delta:+.4f})"
            )

        # Check for category regressions
        cat_deltas = delta.get("categories", {})
        regressed_categories = [(cat, d) for cat, d in cat_deltas.items() if d < 0]
        if regressed_categories:
            warnings_printed = True
            print()
            print("WARNING: Category score regressions:")
            for cat, d in sorted(regressed_categories, key=lambda x: x[1]):
                print(f"  - {cat}: {d:+.4f}")

        # Log trend warning event if any warnings
        if warnings_printed and log_callback:
            log_callback(
                "eval_trend_warning",
                new_failures=str(len(new_failures)),
                passed_delta=str(passed_delta),
                failed_delta=str(failed_delta),
                composite_delta=(str(composite_delta) if composite_delta is not None else "none"),
                regressed_categories=str([cat for cat, _ in regressed_categories]),
            )

        return warnings_printed

    def _update_eval_summary(self, evals_dir: Path, timestamp_str: str, eval_result: dict) -> None:
        """Update summary.json with time-series data for trend analysis.

        Appends the current eval's composite score and key metrics to
        a summary file that accumulates data across all evals.

        Args:
            evals_dir: Path to the evals directory.
            timestamp_str: Timestamp string for the current eval.
            eval_result: The current eval result dict.
        """
        summary_file = evals_dir / "summary.json"

        # Load existing summary or create new one
        summary = json.loads(summary_file.read_text()) if summary_file.exists() else {"evals": []}

        # Extract key metrics for the time series
        entry = {
            "timestamp": eval_result.get("timestamp"),
            "file": f"{timestamp_str}.json",
            "git_head": eval_result.get("git_head", "")[:8],
            "composite_score": eval_result.get("composite_score"),
            "tests": {
                "passed": eval_result.get("tests", {}).get("passed", 0),
                "failed": eval_result.get("tests", {}).get("failed", 0),
            },
        }

        # Include category scores if available
        categories = eval_result.get("categories", {})
        if categories:
            entry["category_scores"] = {
                name: data.get("score") for name, data in categories.items()
            }

        summary["evals"].append(entry)
        summary_file.write_text(json.dumps(summary, indent=2))

    # =========================================================================
    # Metrics methods
    # =========================================================================

    def _generate_task_hash(self, task_text: str) -> str:
        """Generate a short hash for a task based on its text.

        Args:
            task_text: The task description text.

        Returns:
            8-character hex hash.
        """
        return hashlib.sha256(task_text.encode()).hexdigest()[:8]

    def _get_latest_eval(self) -> dict | None:
        """Get the most recent eval result.

        Returns:
            The eval result dict, or None if no evals exist.
        """
        evals_dir = self.work_dir / "evals"
        if not evals_dir.exists():
            return None

        json_files = sorted(evals_dir.glob("*.json"))
        if not json_files:
            return None

        return json.loads(json_files[-1].read_text())

    def _get_eval_before_task(self) -> dict | None:
        """Get the eval result from before the current task started.

        This is used to compute eval deltas for the task.

        Returns:
            The eval result dict, or None if no baseline available.
        """
        # Use baseline_eval if available (set at run start)
        if self.baseline_eval:
            return self.baseline_eval
        return None

    def save_task_metrics(
        self,
        task_text: str,
        outcome: str,
        cycles_used: int,
        task_start_time: datetime | None = None,
        task_tokens_in: int = 0,
        task_tokens_out: int = 0,
        task_review_cycles: int = 0,
        task_review_duration_ms: int = 0,
        task_findings_count: int = 0,
        task_findings_by_severity: dict[str, int] | None = None,
        current_task_group: str | None = None,
        eval_before: dict | None = None,
        eval_after: dict | None = None,
        log_callback: Callable[..., None] | None = None,
    ) -> Path:
        """Save per-task cost and outcome metrics to a JSON file.

        Stores metrics in `.millstone/tasks/<task_hash>.json` to enable
        cost-normalized improvement analysis.

        Args:
            task_text: The task description.
            outcome: Task outcome (approved, rejected, loop_detected, etc.).
            cycles_used: Number of build-review cycles used.
            task_start_time: When the task started.
            task_tokens_in: Total input tokens used.
            task_tokens_out: Total output tokens used.
            task_review_cycles: Count of REQUEST_CHANGES before APPROVED.
            task_review_duration_ms: Total time spent in review calls.
            task_findings_count: Total findings across all reviews.
            task_findings_by_severity: Aggregated severity counts.
            current_task_group: Task group name if applicable.
            eval_before: Eval result before task (for delta calculation).
            eval_after: Eval result after task (for delta calculation).
            log_callback: Optional callback for logging events.

        Returns:
            Path to the saved task metrics file.
        """
        tasks_dir = self.work_dir / "tasks"
        tasks_dir.mkdir(exist_ok=True)

        task_hash = self._generate_task_hash(task_text)
        timestamp = datetime.now()

        # Calculate duration
        duration_seconds = 0.0
        if task_start_time:
            duration_seconds = (timestamp - task_start_time).total_seconds()

        # Build task metrics
        task_metrics = {
            "task": task_text[:500],  # Truncate long task descriptions
            "task_hash": task_hash,
            "timestamp": timestamp.isoformat(),
            "duration_seconds": round(duration_seconds, 2),
            "cycles": cycles_used,
            "tokens": {
                "input": task_tokens_in,
                "output": task_tokens_out,
            },
            "outcome": outcome,
            # Task group (from ## Group: <name> sections in tasklist)
            "group": current_task_group,
            # Review quality metrics
            "review": {
                "review_cycles": task_review_cycles,
                "review_duration_ms": task_review_duration_ms,
                "findings_count": task_findings_count,
                "findings_by_severity": task_findings_by_severity
                or {"critical": 0, "high": 0, "medium": 0, "low": 0, "nit": 0},
            },
        }

        # Add eval delta if both before and after are available
        if eval_before and eval_after:
            delta = {}
            before_score = eval_before.get("composite_score")
            after_score = eval_after.get("composite_score")
            if before_score is not None and after_score is not None:
                delta["composite"] = round(after_score - before_score, 3)

            # Add test delta
            before_tests = eval_before.get("tests", {})
            after_tests = eval_after.get("tests", {})
            if before_tests and after_tests:
                delta["tests"] = {
                    "passed": after_tests.get("passed", 0) - before_tests.get("passed", 0),
                    "failed": after_tests.get("failed", 0) - before_tests.get("failed", 0),
                }

            # Add coverage delta
            before_cov = eval_before.get("coverage", {})
            after_cov = eval_after.get("coverage", {})
            if before_cov and after_cov:
                before_rate = before_cov.get("line_rate", 0)
                after_rate = after_cov.get("line_rate", 0)
                delta["coverage"] = round(after_rate - before_rate, 3)

            if delta:
                task_metrics["eval_delta"] = delta

        # Use timestamp + hash for unique filename to preserve history
        filename = f"{timestamp.strftime('%Y%m%d_%H%M%S')}_{task_hash}.json"
        task_file = tasks_dir / filename
        task_file.write_text(json.dumps(task_metrics, indent=2))

        if log_callback:
            log_callback(
                "task_metrics_saved",
                task_hash=task_hash,
                duration=f"{duration_seconds:.2f}s",
                cycles=str(cycles_used),
                tokens_in=str(task_tokens_in),
                tokens_out=str(task_tokens_out),
                outcome=outcome,
                review_cycles=str(task_review_cycles),
                review_duration_ms=str(task_review_duration_ms),
                findings_count=str(task_findings_count),
            )

        return task_file

    def append_review_metric(
        self,
        task_text: str,
        verdict: str,
        findings: list[str] | None,
        findings_by_severity: dict[str, list[str]] | None,
        duration_ms: int,
        cli_reviewer: str = "unknown",
        false_positive_indicator: bool = False,
        log_callback: Callable[..., None] | None = None,
    ) -> None:
        """Append a review metric entry to the JSONL log.

        Writes one JSON line per review to `.millstone/metrics/reviews.jsonl`
        for tracking reviewer effectiveness over time.

        Args:
            task_text: The task description being reviewed.
            verdict: Review verdict ("APPROVED" or "REQUEST_CHANGES").
            findings: List of finding strings from the review.
            findings_by_severity: Findings grouped by severity level.
            duration_ms: Duration of the review call in milliseconds.
            cli_reviewer: The CLI used for reviewing.
            false_positive_indicator: True if this approval came after REQUEST_CHANGES
                but without meaningful code changes (only whitespace/comments changed).
            log_callback: Optional callback for logging events.
        """
        metrics_dir = self.work_dir / "metrics"
        metrics_dir.mkdir(exist_ok=True)

        reviews_file = metrics_dir / "reviews.jsonl"

        task_hash = self._generate_task_hash(task_text)
        timestamp = datetime.now()

        # Build findings list from either source
        all_findings = []
        if findings:
            all_findings.extend(findings)
        if findings_by_severity:
            for severity_findings in findings_by_severity.values():
                all_findings.extend(severity_findings)

        review_entry = {
            "task_hash": task_hash,
            "reviewer_cli": cli_reviewer,
            "verdict": verdict,
            "findings": all_findings,
            "findings_count": len(all_findings),
            "findings_by_severity": findings_by_severity or {},
            "duration_ms": duration_ms,
            "timestamp": timestamp.isoformat(),
            "false_positive_indicator": false_positive_indicator,
        }

        # Append as single JSON line
        with reviews_file.open("a") as f:
            f.write(json.dumps(review_entry) + "\n")

        if log_callback:
            log_callback(
                "review_metric_appended",
                task_hash=task_hash,
                verdict=verdict,
                findings_count=str(len(all_findings)),
                duration_ms=str(duration_ms),
            )

    def get_task_summary(self, limit: int = 20) -> list[dict]:
        """Get summary of recent tasks with cost and outcome data.

        Args:
            limit: Maximum number of tasks to return.

        Returns:
            List of task metrics dicts, sorted by timestamp (newest first).
        """
        tasks_dir = self.work_dir / "tasks"
        if not tasks_dir.exists():
            return []

        # Load all task JSON files
        task_files = sorted(tasks_dir.glob("*.json"), reverse=True)
        tasks = []
        for task_file in task_files[:limit]:
            try:
                task_data = json.loads(task_file.read_text())
                tasks.append(task_data)
            except (OSError, json.JSONDecodeError):
                continue

        return tasks

    def get_duration_by_complexity(
        self,
        limit: int = 100,
        parse_task_metadata_callback: Callable[[str], dict] | None = None,
    ) -> dict:
        """Calculate average task duration grouped by complexity level.

        Loads completed task metrics and estimates their complexity based on
        task text, then calculates average duration for each complexity level.
        Only approved tasks are included since they represent successful completions.

        Args:
            limit: Maximum number of historical tasks to analyze.
            parse_task_metadata_callback: Callback to parse task metadata.

        Returns:
            Dict with complexity levels as keys and stats as values:
            {
                "simple": {"count": N, "avg_seconds": X, "total_seconds": Y},
                "medium": {"count": N, "avg_seconds": X, "total_seconds": Y},
                "complex": {"count": N, "avg_seconds": X, "total_seconds": Y},
            }
        """
        tasks = self.get_task_summary(limit=limit)

        # Filter to approved tasks only
        approved_tasks = [t for t in tasks if t.get("outcome") == "approved"]

        # Initialize stats for each complexity level
        stats: dict = {
            "simple": {"count": 0, "total_seconds": 0.0},
            "medium": {"count": 0, "total_seconds": 0.0},
            "complex": {"count": 0, "total_seconds": 0.0},
        }

        for task in approved_tasks:
            task_text = task.get("task", "")
            duration = task.get("duration_seconds", 0)

            # Re-analyze complexity from task text
            metadata = {}
            if parse_task_metadata_callback:
                metadata = parse_task_metadata_callback(task_text)
            file_refs = self._extract_file_refs(task_text)
            keywords = self._extract_complexity_keywords(task_text)

            complexity = self._estimate_complexity(
                file_refs=file_refs,
                keywords=keywords,
                est_loc=metadata.get("est_loc"),
                ref_loc=None,  # Don't re-check file sizes for historical tasks
            )

            stats[complexity]["count"] += 1
            stats[complexity]["total_seconds"] += duration

        # Calculate averages
        for level in stats:
            count = stats[level]["count"]
            if count > 0:
                stats[level]["avg_seconds"] = stats[level]["total_seconds"] / count
            else:
                stats[level]["avg_seconds"] = 0.0

        return stats

    def _extract_file_refs(self, task_text: str) -> list[str]:
        """Extract file references from task text.

        Helper method that extracts file paths from task descriptions.
        Used for complexity estimation.

        Args:
            task_text: The task description text.

        Returns:
            List of file paths found in the text.
        """
        file_pattern = r'[`"\']?([a-zA-Z0-9_/.-]+\.[a-zA-Z0-9]+)[`"\']?'
        dir_pattern = r'[`"\']?([a-zA-Z0-9_/.-]+/)[`"\']?'

        file_matches = re.findall(file_pattern, task_text)
        dir_matches = re.findall(dir_pattern, task_text)

        # Filter out common false positives
        excluded = {
            "e.g.",
            "i.e.",
            "etc.",
            "vs.",
            "ex.",
            ".md",
            ".json",
            ".py",
            ".ts",
            ".js",
            ".tsx",
            ".jsx",
            "1.0",
            "2.0",
            "3.0",
        }
        file_refs = [
            ref for ref in file_matches if ref not in excluded and not ref.startswith("http")
        ]
        dir_refs = [ref for ref in dir_matches if len(ref) > 2]

        return list(set(file_refs + dir_refs))

    def _extract_complexity_keywords(self, task_text: str) -> list[tuple[str, str]]:
        """Extract complexity keywords from task text.

        Helper method that finds keywords indicating task complexity.

        Args:
            task_text: The task description text.

        Returns:
            List of (keyword, complexity_level) tuples.
        """
        complexity_keywords = {
            "simple": ["fix typo", "rename", "update comment", "remove unused"],
            "medium": ["add", "implement", "create", "update", "extend", "integrate"],
            "complex": [
                "refactor",
                "migrate",
                "redesign",
                "overhaul",
                "rewrite",
                "architect",
            ],
        }

        text_lower = task_text.lower()
        found = []
        for level, keywords in complexity_keywords.items():
            for kw in keywords:
                if kw in text_lower:
                    found.append((kw, level))

        return found

    def _estimate_complexity(
        self,
        file_refs: list[str],
        keywords: list[tuple[str, str]],
        est_loc: int | None,
        ref_loc: int | None = None,
    ) -> str:
        """Estimate task complexity based on various factors.

        Args:
            file_refs: List of file/directory references found.
            keywords: List of (keyword, level) tuples found.
            est_loc: Estimated lines of code from task metadata, or None.
            ref_loc: Actual lines of code in referenced files, or None.

        Returns:
            "simple", "medium", or "complex"
        """
        score = 0

        # File references contribute to complexity
        ref_count = len(file_refs)
        if ref_count >= 5:
            score += 2
        elif ref_count >= 2:
            score += 1

        # Keywords contribute based on their level
        for _, level in keywords:
            if level == "complex":
                score += 2
            elif level == "medium":
                score += 1
            # simple keywords don't add to score

        # Estimated LoC contributes (explicit metadata takes priority)
        if est_loc is not None:
            if est_loc >= 200:
                score += 2
            elif est_loc >= 50:
                score += 1
        elif ref_loc is not None and ref_loc > 0:
            # Use referenced code size as fallback when no explicit estimate
            # Large files suggest more complex changes
            if ref_loc >= 500:
                score += 2
            elif ref_loc >= 150:
                score += 1

        # Map score to complexity level
        if score >= 4:
            return "complex"
        elif score >= 2:
            return "medium"
        else:
            return "simple"

    def estimate_remaining_time(
        self,
        pending_tasks: list[dict],
        parse_task_metadata_callback: Callable[[str], dict] | None = None,
    ) -> dict:
        """Estimate remaining time to complete pending tasks.

        Uses historical task duration data grouped by complexity to estimate
        how long pending tasks will take.

        Args:
            pending_tasks: List of task analysis dicts from analyze_tasklist().
            parse_task_metadata_callback: Callback to parse task metadata.

        Returns:
            Dict with estimation details:
            {
                "total_seconds": Estimated total time,
                "total_formatted": Human-readable total time,
                "by_complexity": {
                    "simple": {"count": N, "estimated_seconds": X},
                    ...
                },
                "has_data": True if historical data is available,
                "confidence": "high"/"medium"/"low" based on data quality,
            }
        """
        duration_stats = self.get_duration_by_complexity(
            parse_task_metadata_callback=parse_task_metadata_callback
        )

        # Check if we have enough historical data
        total_historical = sum(s["count"] for s in duration_stats.values())
        has_data = total_historical > 0

        # Default durations (fallback when no historical data)
        default_durations = {
            "simple": 120.0,  # 2 minutes
            "medium": 300.0,  # 5 minutes
            "complex": 600.0,  # 10 minutes
        }

        # Count pending tasks by complexity
        by_complexity: dict = {
            "simple": {"count": 0, "estimated_seconds": 0.0},
            "medium": {"count": 0, "estimated_seconds": 0.0},
            "complex": {"count": 0, "estimated_seconds": 0.0},
        }

        for task in pending_tasks:
            complexity = task.get("complexity", "medium")
            by_complexity[complexity]["count"] += 1

            # Use historical average if available, otherwise use default
            if duration_stats[complexity]["count"] > 0:
                avg = duration_stats[complexity]["avg_seconds"]
            else:
                avg = default_durations[complexity]

            by_complexity[complexity]["estimated_seconds"] += avg

        # Calculate total
        total_seconds = sum(c["estimated_seconds"] for c in by_complexity.values())

        # Determine confidence level
        if total_historical >= 10:
            confidence = "high"
        elif total_historical >= 3:
            confidence = "medium"
        else:
            confidence = "low"

        # Format total time as human-readable
        if total_seconds < 60:
            total_formatted = f"{total_seconds:.0f} seconds"
        elif total_seconds < 3600:
            minutes = total_seconds / 60
            total_formatted = f"{minutes:.1f} minutes"
        else:
            hours = total_seconds / 3600
            total_formatted = f"{hours:.1f} hours"

        return {
            "total_seconds": total_seconds,
            "total_formatted": total_formatted,
            "by_complexity": by_complexity,
            "has_data": has_data,
            "confidence": confidence,
            "historical_tasks": total_historical,
        }

    def print_eval_summary(self) -> None:
        """Print summary of recent tasks with cost-normalized improvements.

        Shows tasks sorted by improvement/cost ratio to help identify
        which types of tasks produce the best ROI.
        """
        tasks = self.get_task_summary()

        if not tasks:
            print("No task history found in .millstone/tasks/")
            print("Run some tasks with the orchestrator to collect metrics.")
            return

        print()
        print("=== Task Cost Summary ===")
        print()

        # Calculate totals
        total_duration = 0.0
        total_tokens_in = 0
        total_tokens_out = 0
        total_cycles = 0
        approved_count = 0
        composite_deltas = []

        for task in tasks:
            total_duration += task.get("duration_seconds", 0)
            tokens = task.get("tokens", {})
            total_tokens_in += tokens.get("input", 0)
            total_tokens_out += tokens.get("output", 0)
            total_cycles += task.get("cycles", 0)
            if task.get("outcome") == "approved":
                approved_count += 1
            delta = task.get("eval_delta", {})
            if "composite" in delta:
                composite_deltas.append(delta["composite"])

        print(f"Tasks analyzed: {len(tasks)}")
        print(
            f"Approved: {approved_count}/{len(tasks)} ({100 * approved_count / len(tasks) if tasks else 0:.0f}%)"
        )
        print(f"Total duration: {total_duration / 60:.1f} minutes")
        print(
            f"Total tokens: {total_tokens_in + total_tokens_out:,} (in: {total_tokens_in:,}, out: {total_tokens_out:,})"
        )
        print(f"Total cycles: {total_cycles}")
        print()

        # Cost-per-approved-task
        if approved_count > 0:
            avg_duration = total_duration / approved_count
            avg_tokens = (total_tokens_in + total_tokens_out) / approved_count
            avg_cycles = total_cycles / approved_count
            print("Average per approved task:")
            print(f"  Duration: {avg_duration:.1f}s")
            print(f"  Tokens: {avg_tokens:.0f}")
            print(f"  Cycles: {avg_cycles:.1f}")
            print()

        # Show improvement trends if we have eval deltas
        if composite_deltas:
            avg_delta = sum(composite_deltas) / len(composite_deltas)
            print("Eval impact:")
            print(f"  Tasks with deltas: {len(composite_deltas)}")
            print(f"  Avg composite delta: {avg_delta:+.3f}")
            if total_tokens_in + total_tokens_out > 0 and composite_deltas:
                # Improvement per 1000 tokens
                improvement_per_1k = (
                    sum(composite_deltas) / (total_tokens_in + total_tokens_out)
                ) * 1000
                print(f"  Improvement per 1K tokens: {improvement_per_1k:+.4f}")
            print()

        # Show recent tasks table
        print("Recent tasks:")
        print("-" * 80)
        print(f"{'Task':<40} {'Outcome':<10} {'Duration':>8} {'Tokens':>10} {'Delta':>8}")
        print("-" * 80)

        for task in tasks[:10]:
            task_text = task.get("task", "")[:37]
            if len(task.get("task", "")) > 37:
                task_text += "..."
            outcome = task.get("outcome", "unknown")[:10]
            duration = task.get("duration_seconds", 0)
            tokens = task.get("tokens", {})
            total_tok = tokens.get("input", 0) + tokens.get("output", 0)
            delta = task.get("eval_delta", {}).get("composite")
            delta_str = f"{delta:+.3f}" if delta is not None else "n/a"

            print(
                f"{task_text:<40} {outcome:<10} {duration:>7.1f}s {total_tok:>10,} {delta_str:>8}"
            )

        if len(tasks) > 10:
            print(f"... and {len(tasks) - 10} more tasks")
        print()

    def print_metrics_report(self) -> None:
        """Print summary report of review metrics.

        Generates a report from .millstone/metrics/reviews.jsonl showing:
        - Approval rate
        - Average cycles to approval (per task)
        - Common finding categories
        - Reviewer comparison (if multiple CLIs used)
        """
        reviews_file = self.work_dir / "metrics" / "reviews.jsonl"

        if not reviews_file.exists():
            print("No review metrics found in .millstone/metrics/reviews.jsonl")
            print("Run some tasks with the orchestrator to collect review metrics.")
            return

        # Load all review entries
        reviews = []
        with reviews_file.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        reviews.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        if not reviews:
            print("No valid review entries found in .millstone/metrics/reviews.jsonl")
            return

        print()
        print("=== Review Metrics Report ===")
        print()

        # Basic counts
        total_reviews = len(reviews)
        approved_count = sum(1 for r in reviews if r.get("verdict") == "APPROVED")
        changes_count = sum(1 for r in reviews if r.get("verdict") == "REQUEST_CHANGES")

        approval_rate = (approved_count / total_reviews * 100) if total_reviews > 0 else 0
        print(f"Total reviews: {total_reviews}")
        print(f"Approved: {approved_count} ({approval_rate:.1f}%)")
        print(f"Request changes: {changes_count} ({100 - approval_rate:.1f}%)")
        print()

        # Calculate cycles to approval per task
        # Group reviews by task_hash to find how many reviews per task
        task_reviews: dict[str, list[dict]] = {}
        for review in reviews:
            task_hash = review.get("task_hash", "unknown")
            if task_hash not in task_reviews:
                task_reviews[task_hash] = []
            task_reviews[task_hash].append(review)

        # Count cycles (reviews) per task that eventually got approved
        cycles_list = []
        for _task_hash, task_review_list in task_reviews.items():
            # Sort by timestamp to ensure order
            sorted_reviews = sorted(task_review_list, key=lambda r: r.get("timestamp", ""))
            # Count total reviews for this task (cycles)
            cycle_count = len(sorted_reviews)
            # Check if task was eventually approved
            last_verdict = sorted_reviews[-1].get("verdict") if sorted_reviews else None
            if last_verdict == "APPROVED":
                cycles_list.append(cycle_count)

        if cycles_list:
            avg_cycles = sum(cycles_list) / len(cycles_list)
            print(f"Tasks with approval: {len(cycles_list)}")
            print(f"Average cycles to approval: {avg_cycles:.2f}")
            print(f"Min cycles: {min(cycles_list)}, Max cycles: {max(cycles_list)}")
            print()

        # Total duration stats
        durations = [r.get("duration_ms", 0) for r in reviews]
        if durations:
            total_duration_ms = sum(durations)
            avg_duration_ms = total_duration_ms / len(durations)
            print(f"Total review time: {total_duration_ms / 1000:.1f}s")
            print(f"Average review duration: {avg_duration_ms:.0f}ms")
            print()

        # Finding categories (extract from findings)
        all_findings = []
        for review in reviews:
            findings = review.get("findings", [])
            all_findings.extend(findings)

        if all_findings:
            # Simple categorization based on common keywords
            categories: dict[str, int] = {}
            keywords = {
                "security": ["security", "vulnerability", "injection", "xss", "sql"],
                "error handling": ["error", "exception", "try", "catch", "handle"],
                "testing": ["test", "coverage", "mock", "assert"],
                "documentation": ["doc", "comment", "readme", "docstring"],
                "performance": ["performance", "slow", "optimize", "efficiency"],
                "style": ["style", "format", "naming", "convention"],
                "logic": ["logic", "bug", "incorrect", "wrong", "fix"],
                "type": ["type", "typing", "annotation"],
            }

            for finding in all_findings:
                finding_lower = finding.lower()
                categorized = False
                for category, kws in keywords.items():
                    if any(kw in finding_lower for kw in kws):
                        categories[category] = categories.get(category, 0) + 1
                        categorized = True
                        break
                if not categorized:
                    categories["other"] = categories.get("other", 0) + 1

            print(f"Total findings: {len(all_findings)}")
            print("Finding categories:")
            for category, count in sorted(categories.items(), key=lambda x: -x[1]):
                print(f"  {category}: {count}")
            print()

        # Findings by severity (if available)
        severity_totals: dict[str, int] = {}
        for review in reviews:
            findings_by_severity = review.get("findings_by_severity", {})
            for severity, findings in findings_by_severity.items():
                severity_totals[severity] = severity_totals.get(severity, 0) + len(findings)

        if severity_totals:
            print("Findings by severity:")
            severity_order = ["critical", "high", "medium", "low"]
            for severity in severity_order:
                if severity in severity_totals:
                    print(f"  {severity}: {severity_totals[severity]}")
            # Print any other severities not in the order list
            for severity, count in severity_totals.items():
                if severity not in severity_order:
                    print(f"  {severity}: {count}")
            print()

        # False positive indicators
        false_positive_count = sum(1 for r in reviews if r.get("false_positive_indicator", False))
        if false_positive_count > 0:
            print(f"Potential false positives: {false_positive_count}")
            print("  (Tasks approved on retry without meaningful code changes)")
            print()

        # Reviewer comparison (if multiple CLIs used)
        reviewers: dict[str, dict] = {}
        for review in reviews:
            cli = review.get("reviewer_cli", "unknown")
            if cli not in reviewers:
                reviewers[cli] = {
                    "total": 0,
                    "approved": 0,
                    "changes": 0,
                    "findings": 0,
                    "duration_ms": 0,
                }
            reviewers[cli]["total"] += 1
            if review.get("verdict") == "APPROVED":
                reviewers[cli]["approved"] += 1
            else:
                reviewers[cli]["changes"] += 1
            reviewers[cli]["findings"] += review.get("findings_count", 0)
            reviewers[cli]["duration_ms"] += review.get("duration_ms", 0)

        if len(reviewers) > 1:
            print("Reviewer comparison:")
            print("-" * 70)
            print(f"{'CLI':<15} {'Reviews':>8} {'Approved':>10} {'Findings':>10} {'Avg Time':>12}")
            print("-" * 70)
            for cli, stats in sorted(reviewers.items()):
                approval_pct = (
                    (stats["approved"] / stats["total"] * 100) if stats["total"] > 0 else 0
                )
                avg_time = stats["duration_ms"] / stats["total"] if stats["total"] > 0 else 0
                print(
                    f"{cli:<15} {stats['total']:>8} "
                    f"{stats['approved']:>5} ({approval_pct:>4.0f}%) "
                    f"{stats['findings']:>10} "
                    f"{avg_time:>10.0f}ms"
                )
            print()
        elif reviewers:
            # Single reviewer - still show stats
            cli = list(reviewers.keys())[0]
            stats = reviewers[cli]
            print(f"Reviewer: {cli}")
            print(f"  Total findings: {stats['findings']}")
            print()

    # =========================================================================
    # Eval gating and rollback methods
    # =========================================================================

    def _run_eval_on_commit(
        self,
        task_text: str = "",
        task_prefix: str = "",
        auto_rollback: bool = False,
        cycle_log_callback: Callable[[str, str], None] | None = None,
        log_callback: Callable[..., None] | None = None,
        run_eval_callback: Callable[..., dict] | None = None,
    ) -> bool:
        """Run eval after a commit and compare against baseline.

        Called after each successful commit when eval_on_commit is enabled.
        Compares the current eval against the baseline captured at run start.
        Checks both for NEW test failures and composite score regression.

        If composite_score drops by more than policy.eval.max_regression,
        offers to revert the commit (or auto-reverts with --auto-rollback).

        Args:
            task_text: The task description (used for rollback context).
            task_prefix: Prefix for progress messages, e.g., '[Task 2/5]'.
            auto_rollback: Whether to auto-revert on regression.
            cycle_log_callback: Optional callback for cycle logging.
            log_callback: Optional callback for logging events.
            run_eval_callback: Optional callback to run eval (defaults to self.run_eval).

        Returns:
            True if no regression, False if regression detected and halted/reverted.
        """
        progress(f"{task_prefix} Running post-commit eval...")
        if run_eval_callback:
            current_eval = run_eval_callback()
        else:
            current_eval = self.run_eval(log_callback=log_callback)

        # Get failed tests from baseline and current
        baseline_failed = set(
            self.baseline_eval.get("failed_tests", []) if self.baseline_eval else []
        )
        current_failed = set(current_eval.get("failed_tests", []))

        # Find new failures (in current but not in baseline)
        new_failures = current_failed - baseline_failed

        # Check composite score regression
        baseline_score = self.baseline_eval.get("composite_score") if self.baseline_eval else None
        current_score = current_eval.get("composite_score")
        max_regression = self.policy.get("eval", {}).get("max_regression", 0.05)
        score_regression = 0.0
        has_score_regression = False

        if baseline_score is not None and current_score is not None:
            score_regression = baseline_score - current_score
            if score_regression > max_regression:
                has_score_regression = True

        # Log the eval results
        if log_callback:
            log_callback(
                "eval_on_commit",
                baseline_failed_count=str(len(baseline_failed)),
                current_failed_count=str(len(current_failed)),
                new_failures_count=str(len(new_failures)),
                new_failures=str(list(new_failures)[:10]),
                baseline_score=(str(baseline_score) if baseline_score is not None else "none"),
                current_score=(str(current_score) if current_score is not None else "none"),
                score_regression=str(round(score_regression, 4)),
                has_score_regression=str(has_score_regression),
            )

        # Handle new test failures
        if new_failures:
            progress(
                f"{task_prefix} EVAL REGRESSION: {len(new_failures)} new test failure(s) introduced"
            )
            print()
            print("New test failures:")
            for test in sorted(new_failures)[:10]:
                print(f"  - {test}")
            if len(new_failures) > 10:
                print(f"  ... and {len(new_failures) - 10} more")
            print()
            return self._handle_eval_regression(
                current_eval=current_eval,
                task_text=task_text,
                reason="test_failures",
                details={"new_failures": list(new_failures)[:10]},
                auto_rollback=auto_rollback,
                cycle_log_callback=cycle_log_callback,
                log_callback=log_callback,
            )

        # Handle composite score regression
        if has_score_regression:
            progress(
                f"{task_prefix} EVAL REGRESSION: composite score dropped by {score_regression:.4f}"
            )
            print()
            print("Composite score regression detected:")
            print(f"  Baseline: {baseline_score:.4f}")
            print(f"  Current:  {current_score:.4f}")
            print(f"  Delta:    {-score_regression:.4f}")
            print(f"  Max allowed regression: {max_regression:.4f}")
            print()

            # Show category breakdown
            self._print_category_comparison(current_eval)

            return self._handle_eval_regression(
                current_eval=current_eval,
                task_text=task_text,
                reason="composite_score_regression",
                details={
                    "baseline_score": baseline_score,
                    "current_score": current_score,
                    "regression": score_regression,
                    "max_regression": max_regression,
                },
                auto_rollback=auto_rollback,
                cycle_log_callback=cycle_log_callback,
                log_callback=log_callback,
            )

        # No regression - report success
        if current_failed:
            progress(
                f"{task_prefix} Post-commit eval: {len(current_failed)} pre-existing failure(s), no new failures"
            )
        else:
            progress(f"{task_prefix} Post-commit eval: All tests passing")

        if current_score is not None:
            if baseline_score is not None:
                delta = current_score - baseline_score
                delta_str = f"+{delta:.4f}" if delta >= 0 else f"{delta:.4f}"
                progress(f"{task_prefix} Composite score: {current_score:.4f} ({delta_str})")
            else:
                progress(f"{task_prefix} Composite score: {current_score:.4f}")

        return True

    def _run_eval_on_task(
        self,
        eval_on_task: str,
        task_text: str = "",
        task_prefix: str = "",
        auto_rollback: bool = False,
        cycle_log_callback: Callable[[str, str], None] | None = None,
        log_callback: Callable[..., None] | None = None,
        run_eval_callback: Callable[..., dict] | None = None,
    ) -> bool:
        """Run eval after an approved task using the configured eval_on_task mode.

        Called after each approved task when eval_on_task is not "none".
        Uses the eval mode specified in eval_on_task config:
        - "smoke": Quick tests only (fail-fast, no coverage)
        - "full": All tests with coverage
        - Custom path: Run custom test suite/script

        Compares the current eval against the baseline captured at run start.
        Checks both for NEW test failures and composite score regression.

        Args:
            eval_on_task: The eval mode to use.
            task_text: The task description (used for rollback context).
            task_prefix: Prefix for progress messages.
            auto_rollback: Whether to auto-revert on regression.
            cycle_log_callback: Optional callback for cycle logging.
            log_callback: Optional callback for logging events.
            run_eval_callback: Optional callback to run eval (defaults to self.run_eval).

        Returns:
            True if no regression, False if regression detected and halted/reverted.
        """
        mode = eval_on_task
        if mode == "none":
            return True  # Eval disabled

        mode_display = f"({mode} mode)" if mode in ("smoke", "full") else f"(custom: {mode})"
        progress(f"{task_prefix} Running post-task eval {mode_display}...")

        # Run eval with the configured mode (use callback if provided)
        if run_eval_callback:
            current_eval = run_eval_callback(mode=mode)
        else:
            current_eval = self.run_eval(mode=mode, log_callback=log_callback)

        # Get failed tests from baseline and current
        baseline_failed = set(
            self.baseline_eval.get("failed_tests", []) if self.baseline_eval else []
        )
        current_failed = set(current_eval.get("failed_tests", []))

        # Find new failures (in current but not in baseline)
        new_failures = current_failed - baseline_failed

        # Check composite score regression
        baseline_score = self.baseline_eval.get("composite_score") if self.baseline_eval else None
        current_score = current_eval.get("composite_score")
        max_regression = self.policy.get("eval", {}).get("max_regression", 0.05)
        score_regression = 0.0
        has_score_regression = False

        if baseline_score is not None and current_score is not None:
            score_regression = baseline_score - current_score
            if score_regression > max_regression:
                has_score_regression = True

        # Log the eval results
        if log_callback:
            log_callback(
                "eval_on_task",
                mode=mode,
                baseline_failed_count=str(len(baseline_failed)),
                current_failed_count=str(len(current_failed)),
                new_failures_count=str(len(new_failures)),
                new_failures=str(list(new_failures)[:10]),
                baseline_score=(str(baseline_score) if baseline_score is not None else "none"),
                current_score=(str(current_score) if current_score is not None else "none"),
                score_regression=str(round(score_regression, 4)),
                has_score_regression=str(has_score_regression),
            )

        # Handle new test failures
        if new_failures:
            progress(
                f"{task_prefix} EVAL REGRESSION: {len(new_failures)} new test failure(s) introduced"
            )
            print()
            print("New test failures:")
            for test in sorted(new_failures)[:10]:
                print(f"  - {test}")
            if len(new_failures) > 10:
                print(f"  ... and {len(new_failures) - 10} more")
            print()
            return self._handle_eval_regression(
                current_eval=current_eval,
                task_text=task_text,
                reason="test_failures",
                details={"new_failures": list(new_failures)[:10]},
                auto_rollback=auto_rollback,
                cycle_log_callback=cycle_log_callback,
                log_callback=log_callback,
            )

        # Handle composite score regression
        if has_score_regression:
            progress(
                f"{task_prefix} EVAL REGRESSION: composite score dropped by {score_regression:.4f}"
            )
            print()
            print("Composite score regression detected:")
            print(f"  Baseline: {baseline_score:.4f}")
            print(f"  Current:  {current_score:.4f}")
            print(f"  Delta:    {-score_regression:.4f}")
            print(f"  Max allowed regression: {max_regression:.4f}")
            print()

            # Show category breakdown
            self._print_category_comparison(current_eval)

            return self._handle_eval_regression(
                current_eval=current_eval,
                task_text=task_text,
                reason="composite_score_regression",
                details={
                    "baseline_score": baseline_score,
                    "current_score": current_score,
                    "regression": score_regression,
                    "max_regression": max_regression,
                },
                auto_rollback=auto_rollback,
                cycle_log_callback=cycle_log_callback,
                log_callback=log_callback,
            )

        # No regression - report success
        if current_failed:
            progress(
                f"{task_prefix} Post-task eval: {len(current_failed)} pre-existing failure(s), no new failures"
            )
        else:
            progress(f"{task_prefix} Post-task eval: All tests passing")

        if current_score is not None:
            if baseline_score is not None:
                delta = current_score - baseline_score
                delta_str = f"+{delta:.4f}" if delta >= 0 else f"{delta:.4f}"
                progress(f"{task_prefix} Composite score: {current_score:.4f} ({delta_str})")
            else:
                progress(f"{task_prefix} Composite score: {current_score:.4f}")

        return True

    def _run_eval_gate(
        self,
        eval_on_task: str,
        skip_eval: bool = False,
        task_text: str = "",
        task_prefix: str = "",
        log_callback: Callable[..., None] | None = None,
        run_eval_callback: Callable[..., dict] | None = None,
    ) -> tuple[bool, dict | None]:
        """Run eval gate before commit to prevent commits that break tests.

        Called after review approval when eval_on_task is not "none".
        Runs the eval suite before committing to gate the commit on test results.

        Args:
            eval_on_task: The eval mode to use.
            skip_eval: Whether to skip the eval gate.
            task_text: The task description (for logging context).
            task_prefix: Prefix for progress messages.
            log_callback: Optional callback for logging events.
            run_eval_callback: Optional callback to run eval (defaults to self.run_eval).

        Returns:
            Tuple of (gate_passed, eval_result). gate_passed is True if commit
            should proceed, False if commit should be blocked.
        """
        # Skip eval gate if --skip-eval flag is set
        if skip_eval:
            return True, None  # Eval gate bypassed by --skip-eval

        mode = eval_on_task
        if mode == "none":
            return True, None  # Eval gating disabled

        mode_display = f"({mode} mode)" if mode in ("smoke", "full") else f"(custom: {mode})"
        progress(f"{task_prefix} Running eval gate {mode_display}...")

        # Run eval with the configured mode (use callback if provided)
        if run_eval_callback:
            current_eval = run_eval_callback(mode=mode)
        else:
            current_eval = self.run_eval(mode=mode, log_callback=log_callback)

        # Get failed tests from baseline and current
        baseline_failed = set(
            self.baseline_eval.get("failed_tests", []) if self.baseline_eval else []
        )
        current_failed = set(current_eval.get("failed_tests", []))

        # Find new failures (in current but not in baseline)
        new_failures = current_failed - baseline_failed

        # Check composite score regression
        baseline_score = self.baseline_eval.get("composite_score") if self.baseline_eval else None
        current_score = current_eval.get("composite_score")
        max_regression = self.policy.get("eval", {}).get("max_regression", 0.05)
        score_regression = 0.0
        has_score_regression = False

        if baseline_score is not None and current_score is not None:
            score_regression = baseline_score - current_score
            if score_regression > max_regression:
                has_score_regression = True

        # Determine if gate should fail
        gate_failed = bool(new_failures) or has_score_regression

        if gate_failed:
            # Log the eval gate failure with details
            if log_callback:
                log_callback(
                    "eval_gate_failed",
                    mode=mode,
                    baseline_failed_count=str(len(baseline_failed)),
                    current_failed_count=str(len(current_failed)),
                    new_failures_count=str(len(new_failures)),
                    new_failures=str(list(new_failures)[:10]),
                    baseline_score=(str(baseline_score) if baseline_score is not None else "none"),
                    current_score=(str(current_score) if current_score is not None else "none"),
                    score_regression=str(round(score_regression, 4)),
                    has_score_regression=str(has_score_regression),
                    task_text=task_text[:200] if task_text else "",
                )

            # Print failure details
            if new_failures:
                progress(f"{task_prefix} EVAL GATE FAILED: {len(new_failures)} new test failure(s)")
                print()
                print("New test failures preventing commit:")
                for test in sorted(new_failures)[:10]:
                    print(f"  - {test}")
                if len(new_failures) > 10:
                    print(f"  ... and {len(new_failures) - 10} more")
                print()

            if has_score_regression:
                progress(
                    f"{task_prefix} EVAL GATE FAILED: composite score dropped by {score_regression:.4f}"
                )
                print()
                print("Composite score regression preventing commit:")
                print(f"  Baseline: {baseline_score:.4f}")
                print(f"  Current:  {current_score:.4f}")
                print(f"  Delta:    {-score_regression:.4f}")
                print(f"  Max allowed regression: {max_regression:.4f}")
                print()
                self._print_category_comparison(current_eval)

            print("Commit blocked by eval gate. Fix the failures and retry.")
            return False, current_eval

        # Gate passed - log success
        if log_callback:
            log_callback(
                "eval_gate_passed",
                mode=mode,
                current_failed_count=str(len(current_failed)),
                current_score=(str(current_score) if current_score is not None else "none"),
            )

        if current_failed:
            progress(
                f"{task_prefix} Eval gate passed: {len(current_failed)} pre-existing failure(s), no new failures"
            )
        else:
            progress(f"{task_prefix} Eval gate passed: All tests passing")

        return True, current_eval

    def _print_category_comparison(self, current_eval: dict) -> None:
        """Print category-by-category breakdown comparing current to baseline.

        Args:
            current_eval: The current eval result dict.
        """
        baseline_cats = self.baseline_eval.get("categories", {}) if self.baseline_eval else {}
        current_cats = current_eval.get("categories", {})

        if not baseline_cats and not current_cats:
            return

        print("Category breakdown:")
        all_cats = set(baseline_cats.keys()) | set(current_cats.keys())
        for cat in sorted(all_cats):
            baseline_score = baseline_cats.get(cat, {}).get("score")
            current_score = current_cats.get(cat, {}).get("score")

            if baseline_score is not None and current_score is not None:
                delta = current_score - baseline_score
                delta_str = f"+{delta:.2f}" if delta >= 0 else f"{delta:.2f}"
                status = "↑" if delta > 0.01 else ("↓" if delta < -0.01 else "→")
                print(f"  {cat}: {baseline_score:.2f} → {current_score:.2f} ({delta_str}) {status}")
            elif current_score is not None:
                print(f"  {cat}: {current_score:.2f} (new)")
            elif baseline_score is not None:
                print(f"  {cat}: {baseline_score:.2f} → (missing)")
        print()

    def _handle_eval_regression(
        self,
        current_eval: dict,
        task_text: str,
        reason: str,
        details: dict,
        auto_rollback: bool = False,
        cycle_log_callback: Callable[[str, str], None] | None = None,
        log_callback: Callable[..., None] | None = None,
    ) -> bool:
        """Handle eval regression by prompting for or auto-performing revert.

        Args:
            current_eval: The current eval result dict.
            task_text: The task description.
            reason: Why the regression occurred (test_failures, composite_score_regression).
            details: Additional details about the regression.
            auto_rollback: Whether to auto-revert on regression.
            cycle_log_callback: Optional callback for cycle logging.
            log_callback: Optional callback for logging events.

        Returns:
            False always (indicates regression was detected and handled).
        """
        commit_hash = self.git("rev-parse", "HEAD").strip()
        short_hash = commit_hash[:8]

        # Log rollback decision point (only in cycle mode when cycle_log_callback is set)
        if cycle_log_callback:
            cycle_log_callback("EVAL_REGRESSION", f"Detected {reason}: {details}")

        if auto_rollback:
            # Auto-revert mode
            print(f"Auto-reverting commit {short_hash} due to eval regression...")
            success = self._perform_rollback(commit_hash, task_text, reason, details, log_callback)
            if success:
                print(f"Commit {short_hash} has been reverted.")
                if cycle_log_callback:
                    cycle_log_callback("AUTO_ROLLBACK", f"Reverted commit {short_hash}")
            else:
                print(f"Failed to revert commit {short_hash}. Manual intervention required.")
                if cycle_log_callback:
                    cycle_log_callback("ROLLBACK_FAILED", f"Failed to revert {short_hash}")
            return False
        else:
            # Interactive mode - prompt user
            print(
                f"Eval regression detected. Revert commit {short_hash}? [y/N] ",
                end="",
                flush=True,
            )
            try:
                response = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                response = ""
                print()

            if response in ("y", "yes"):
                success = self._perform_rollback(
                    commit_hash, task_text, reason, details, log_callback
                )
                if success:
                    print(f"Commit {short_hash} has been reverted.")
                    if cycle_log_callback:
                        cycle_log_callback(
                            "USER_ROLLBACK", f"User requested revert of {short_hash}"
                        )
                else:
                    print(f"Failed to revert commit {short_hash}. Manual intervention required.")
            else:
                print("Commit kept. Halting for manual intervention.")
                if cycle_log_callback:
                    cycle_log_callback("ROLLBACK_DECLINED", f"User declined revert of {short_hash}")

            return False

    def _perform_rollback(
        self,
        commit_hash: str,
        task_text: str,
        reason: str,
        details: dict,
        log_callback: Callable[..., None] | None = None,
    ) -> bool:
        """Perform git revert on the specified commit and store context.

        Args:
            commit_hash: The commit hash to revert.
            task_text: The task description.
            reason: Why the rollback is happening.
            details: Additional details about the regression.
            log_callback: Optional callback for logging events.

        Returns:
            True if revert succeeded, False otherwise.
        """
        try:
            # Perform git revert (non-interactive, auto-commit)
            result = subprocess.run(
                ["git", "revert", "--no-edit", commit_hash],
                capture_output=True,
                text=True,
                cwd=self.repo_dir,
            )

            if result.returncode != 0:
                if log_callback:
                    log_callback(
                        "rollback_failed",
                        commit=commit_hash[:8],
                        error=result.stderr[:500],
                    )
                return False

            # Store rollback context for next cycle
            self.last_rollback_context = {
                "timestamp": datetime.now().isoformat(),
                "reverted_commit": commit_hash,
                "task": task_text,
                "reason": reason,
                "details": details,
            }

            # Save rollback context to file for persistence across runs
            rollback_file = self.work_dir / "last_rollback.json"
            rollback_file.write_text(json.dumps(self.last_rollback_context, indent=2))

            if log_callback:
                log_callback(
                    "rollback_completed",
                    commit=commit_hash[:8],
                    reason=reason,
                    task=task_text[:100],
                )

            return True
        except Exception as e:
            if log_callback:
                log_callback(
                    "rollback_error",
                    commit=commit_hash[:8],
                    error=str(e)[:500],
                )
            return False

    def _load_rollback_context(self) -> dict | None:
        """Load rollback context from last_rollback.json if it exists.

        This context is used to inform the next analysis cycle about what
        previously failed, so the agent can try a different approach.

        Returns:
            Dict with rollback context, or None if no rollback context exists.
        """
        # First check in-memory context
        if self.last_rollback_context:
            return self.last_rollback_context

        # Then check persisted file
        rollback_file = self.work_dir / "last_rollback.json"
        if rollback_file.exists():
            try:
                return json.loads(rollback_file.read_text())
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def clear_rollback_context(self) -> None:
        """Clear the rollback context after it has been used.

        Called after a successful cycle completion to prevent stale context
        from affecting future cycles.
        """
        self.last_rollback_context = None
        rollback_file = self.work_dir / "last_rollback.json"
        if rollback_file.exists():
            with contextlib.suppress(IOError):
                rollback_file.unlink()
