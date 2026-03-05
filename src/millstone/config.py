"""
Configuration loading and constants for the millstone orchestrator.

This module contains configuration functions and constants extracted from
orchestrate.py for better modularity. All items are re-exported from
orchestrate.py for backward compatibility.
"""

import copy
import importlib
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_toml_library() -> ModuleType | None:
    """Return a TOML parsing module, preferring tomllib but falling back to tomli."""
    for module_name in ("tomllib", "tomli"):
        try:
            return importlib.import_module(module_name)
        except ImportError:
            continue
    return None


tomllib = _load_toml_library()

# Directory name for orchestrator files in target repo
WORK_DIR_NAME = ".millstone"
CONFIG_FILE_NAME = "config.toml"
STATE_FILE_NAME = "state.json"
PROJECT_FILE_NAME = "project.toml"
POLICY_FILE_NAME = "policy.toml"

# Default policy configuration (can be overridden by .millstone/policy.toml)
DEFAULT_POLICY = {
    "limits": {
        "max_loc_per_task": 2000,
        "max_cycles": 3,
    },
    "sensitive": {
        "enabled": False,
        "paths": [".env", "credentials.*", "*.key", "*.pem", "secret*"],
        "require_approval": True,
    },
    "dangerous": {
        "patterns": ["rm -rf", "DROP TABLE", "force push", "git push --force", "DELETE FROM.*WHERE 1", "truncate table"],
        "block": True,
    },
    "tasklist": {
        "enforce_single_task": True,
    },
    "eval": {
        "min_composite_score": 0.0,  # Minimum acceptable composite score (0.0 = disabled)
        "max_regression": 0.05,      # Maximum acceptable regression in composite score
    },
}

# Default project configuration (can be overridden by .millstone/project.toml)
DEFAULT_PROJECT_CONFIG = {
    "project": {
        "name": "",
        "language": "auto",  # auto-detect if not specified
    },
    "tests": {
        "command": "",  # Auto-detected based on language
        "coverage_command": "",  # Auto-detected based on language
    },
    "lint": {
        "command": "",  # Auto-detected based on language
    },
    "typing": {
        "command": "",  # Auto-detected based on language
    },
    "sensitive_paths": {
        "patterns": [".env", "*.key", "*.pem", "credentials.*", "secret*"],
    },
    "tasklist": {
        "path": "",  # Defaults to config's tasklist setting
    },
}

# Default configuration values
DEFAULT_CONFIG = {
    "max_cycles": 3,
    "loc_threshold": 1_000_000,
    "tasklist": ".millstone/tasklist.md",
    "max_tasks": 5,
    "opportunity_provider": "file",
    "design_provider": "file",
    "tasklist_provider": "file",
    "opportunity_provider_options": {},
    "design_provider_options": {},
    "tasklist_provider_options": {},
    # Provider-agnostic filter schema for remote tasklist backends (Jira/Linear/GitHub).
    # These filters narrow the working task set without changing backend credentials or
    # project scope.  Each key accepts a list of strings; an empty list means no filter.
    # Individual backends may support additional native keys via tasklist_provider_options.
    #
    # UX shortcuts: single-string forms that expand to their list equivalents.
    # Use `label = "sprint-1"` instead of `labels = ["sprint-1"]` for quick scoping.
    # If both the shortcut and its list form are set, the list form takes precedence.
    "tasklist_filter": {
        "labels": [],      # Restrict to tasks tagged with ALL of these labels
        "assignees": [],   # Restrict to tasks assigned to ANY of these users
        "statuses": [],    # Restrict to tasks in ANY of these status names
        # Shortcuts for single-value narrowing (expand to the list forms above):
        "label": "",       # Single-label shortcut  →  labels = ["<value>"]
        "assignee": "",    # Single-assignee shortcut  →  assignees = ["<value>"]
        "status": "",      # Single-status shortcut  →  statuses = ["<value>"]
    },
    "commit_tasklist": False,  # If True, tasklist defaults to docs/tasklist.md (tracked path)
    "commit_designs": False,   # If True, designs default to designs/ (tracked path)
    "commit_opportunities": False,  # If True, opportunities default to opportunities.md (tracked path)
    "prompts_dir": None,  # None means use built-in prompts
    "compact_threshold": 20,  # Trigger compaction when completed tasks >= this
    "eval_on_commit": False,  # Run evals automatically after each commit
    "retry_on_empty_response": True,  # Retry when agent returns empty/malformed response
    "auto_rollback": False,  # Auto-revert commits when eval regresses beyond threshold
    "eval_scripts": [],  # Custom eval scripts to run (e.g., ["mypy .", "ruff check ."])
    # Parallel/worktree execution (worktree-based isolated task execution).
    # Flat keys are required because load_config() only reads known top-level keys.
    "parallel_enabled": False,
    "parallel_concurrency": 1,
    "parallel_merge_strategy": "merge",
    "parallel_integration_branch": "millstone/integration",
    "parallel_worktree_root": ".millstone/worktrees",
    "parallel_cleanup": "on_success",
    "parallel_lock_git": ".millstone/locks/git.lock",
    "parallel_lock_state": ".millstone/locks/state.lock",
    "parallel_lock_tasklist": ".millstone/locks/tasklist.lock",
    "parallel_heartbeat_interval": 30,  # seconds between worker heartbeats
    "parallel_heartbeat_ttl": 300,      # seconds before stale heartbeat triggers cleanup
    # Eval on task: run eval suite after each approved task
    # Options: "none" (disabled), "smoke" (quick tests only), "full" (all tests + coverage),
    # or a path to a custom test suite/script (e.g., "tests/smoke/", "scripts/eval.sh")
    "eval_on_task": "none",
    "review_designs": True,  # Review designs before proceeding to implementation
    "approve_opportunities": True,  # Pause after analyze for human to pick opportunity
    "approve_designs": True,  # Pause after design for human review
    "approve_plans": True,  # Pause after plan for human review
    "min_response_length": 50,  # Minimum response length (chars) before triggering retry
    # Log verbosity control: "minimal" (events only), "normal" (events + summaries), "verbose" (full output)
    "log_verbosity": "normal",
    # Diff logging mode: "full" (complete diffs), "summary" (stats + truncated), "none" (suppress diffs)
    "log_diff_mode": "summary",
    # CLI provider configuration - use "claude", "codex", "gemini", or "opencode"
    # Can be set globally or per-role (builder, reviewer, sanity, analyzer, etc.)
    "cli": "claude",  # Default CLI for all roles
    "profile": "dev_implementation",  # Active profile for role alias resolution
    "cli_builder": None,  # CLI for builder role (None = use default)
    "cli_reviewer": None,  # CLI for reviewer role (None = use default)
    "cli_sanity": None,    # CLI for sanity check role
    "cli_analyzer": None,  # CLI for complexity analysis role
    "cli_release_eng": None, # CLI for release engineering role
    "cli_sre": None,       # CLI for site reliability engineering role
    # Session mode: how sessions persist across tasks
    # "new_each_task" - Fresh session for each task (default, safest)
    # "continue_within_run" - Preserve session for all tasks in single invocation
    # "continue_across_runs" - Preserve session across separate invocations (loads from state)
    "session_mode": "new_each_task",
    # Category weights for composite score (must sum to 1.0)
    "category_weights": {
        "tests": 0.40,
        "typing": 0.15,
        "lint": 0.15,
        "coverage": 0.20,
        "security": 0.05,
        "complexity": 0.05,
    },
    # Thresholds for category scoring (errors beyond threshold = score 0.0)
    "category_thresholds": {
        "typing": 50,     # mypy errors
        "lint": 100,      # ruff errors
        "security": 10,   # bandit issues
        "complexity": 20, # high complexity functions
    },
    # Task atomizer constraints for run_plan()
    "task_constraints": {
        "max_loc": 200,
        "require_tests": True,
        "require_criteria": True,
        "require_risk": True,
        "require_context": True,
        "max_split_attempts": 2,
    },
    # Risk level settings
    "risk_settings": {
        "low": {
            "max_cycles": 2,      # Lower max cycles for low-risk tasks
            "require_full_eval": False,  # Unit tests are sufficient
        },
        "medium": {
            "max_cycles": 3,      # Default max cycles
            "require_full_eval": False,  # Unit + integration tests
        },
        "high": {
            "max_cycles": 5,      # More cycles allowed for complex high-risk tasks
            "require_full_eval": True,   # Full eval suite required
            "require_approval": True,    # Always pause for human approval
        },
    },
    # Model selection: map task characteristics to models
    # Used with --auto-model flag to dynamically select models based on task complexity
    # Each rule has conditions (complexity, risk, keywords) and a model to use
    # Rules are evaluated in order; first matching rule wins
    # Valid models: "haiku" (fast, cheap), "sonnet" (balanced), "opus" (most capable)
    "model_selection": {
        "enabled": False,  # Set to True or use --auto-model to enable dynamic selection
        "default_model": "sonnet",  # Model to use when no rules match
        "rules": [
            # Simple tasks: use faster, cheaper model
            {
                "complexity": "simple",
                "model": "haiku",
            },
            # Medium tasks: use balanced model
            {
                "complexity": "medium",
                "model": "sonnet",
            },
            # Complex tasks: use most capable model
            {
                "complexity": "complex",
                "model": "opus",
            },
            # High-risk tasks always use opus regardless of complexity
            {
                "risk": "high",
                "model": "opus",
            },
        ],
    },
}


def load_config(repo_dir: Path | None = None) -> dict:
    """Load configuration from .millstone/config.toml if it exists.

    Args:
        repo_dir: Repository directory. Defaults to current working directory.

    Returns:
        Dictionary with configuration values. Missing keys use defaults.
        Returns defaults if config file doesn't exist or tomllib is unavailable.
    """
    config = DEFAULT_CONFIG.copy()

    if tomllib is None:
        return config

    repo_path = Path(repo_dir) if repo_dir else Path.cwd()
    config_path = repo_path / WORK_DIR_NAME / CONFIG_FILE_NAME

    if not config_path.exists():
        return config

    try:
        with config_path.open("rb") as f:
            file_config = tomllib.load(f)

        # Only update with known keys to prevent typos from silently being ignored
        for key in DEFAULT_CONFIG:
            if key in file_config:
                config[key] = file_config[key]

        return config
    except Exception:
        # If config file is malformed, use defaults
        return DEFAULT_CONFIG.copy()


def detect_project_type(repo_dir: Path) -> str:
    """Auto-detect project type based on marker files.

    Args:
        repo_dir: Repository directory to scan.

    Returns:
        Language string: "python", "node", "go", or "unknown".
    """
    # Python markers
    if (repo_dir / "pyproject.toml").exists():
        return "python"
    if (repo_dir / "setup.py").exists():
        return "python"
    if (repo_dir / "requirements.txt").exists():
        return "python"
    if list(repo_dir.glob("*.py")):
        return "python"

    # Node/JavaScript markers
    if (repo_dir / "package.json").exists():
        return "node"

    # Go markers
    if (repo_dir / "go.mod").exists():
        return "go"

    return "unknown"


def get_default_commands(language: str, repo_dir: Path) -> dict:
    """Get default commands for a detected language.

    Args:
        language: The detected language ("python", "node", "go", or "unknown").
        repo_dir: Repository directory for marker checks.

    Returns:
        Dict with default test, coverage, lint, and typing commands.
    """
    if language == "python":
        return {
            "tests": {
                "command": "pytest tests/ --tb=short -q",
                "coverage_command": "pytest tests/ --cov=. --cov-report=json --tb=short -q",
            },
            "lint": {
                "command": "ruff check ." if shutil.which("ruff") else "",
            },
            "typing": {
                "command": "mypy ." if shutil.which("mypy") else "",
            },
        }
    elif language == "node":
        return {
            "tests": {
                "command": "npm test",
                "coverage_command": "npm test -- --coverage",
            },
            "lint": {
                "command": "npm run lint" if (repo_dir / "package.json").exists() else "",
            },
            "typing": {
                "command": "npx tsc --noEmit" if (repo_dir / "tsconfig.json").exists() else "",
            },
        }
    elif language == "go":
        return {
            "tests": {
                "command": "go test ./...",
                "coverage_command": "go test -coverprofile=coverage.out ./...",
            },
            "lint": {
                "command": "golangci-lint run" if shutil.which("golangci-lint") else "",
            },
            "typing": {
                "command": "",  # Go is statically typed, no separate type check needed
            },
        }
    else:
        return {
            "tests": {"command": "", "coverage_command": ""},
            "lint": {"command": ""},
            "typing": {"command": ""},
        }


def load_project_config(repo_dir: Path | None = None) -> dict:
    """Load project configuration from .millstone/project.toml if it exists.

    Falls back to auto-detection of project type and default commands.

    Args:
        repo_dir: Repository directory. Defaults to current working directory.

    Returns:
        Dictionary with project configuration including test/lint/typing commands.
    """
    config: dict[str, Any] = copy.deepcopy(DEFAULT_PROJECT_CONFIG)

    repo_path = Path(repo_dir) if repo_dir else Path.cwd()
    project_file = repo_path / WORK_DIR_NAME / PROJECT_FILE_NAME

    # Load from file if it exists and tomllib is available
    if tomllib is not None and project_file.exists():
        try:
            with project_file.open("rb") as f:
                file_config = tomllib.load(f)

            # Deep merge file config into defaults
            for section, default_section in config.items():
                if section in file_config:
                    file_section = file_config[section]
                    if isinstance(default_section, dict) and isinstance(file_section, dict):
                        default_section.update(file_section)
                    else:
                        config[section] = file_section
        except Exception:
            pass  # Use defaults on parse error

    # Auto-detect language if set to "auto" or not specified
    project_config = config.get("project")
    if not isinstance(project_config, dict):
        project_config = {}
        config["project"] = project_config
    language = project_config.get("language", "auto")
    if language == "auto" or not language:
        language = detect_project_type(repo_path)
        project_config["language"] = language

    # Fill in missing commands with auto-detected defaults
    defaults = get_default_commands(language, repo_path)

    for section in ("tests", "lint", "typing"):
        default_section = defaults.get(section)
        if not isinstance(default_section, dict):
            continue
        section_config = config.get(section)
        if not isinstance(section_config, dict):
            section_config = {}
            config[section] = section_config
        for key, value in default_section.items():
            if not section_config.get(key):
                section_config[key] = value

    return config


def load_policy(repo_dir: Path | None = None) -> dict:
    """Load policy configuration from .millstone/policy.toml if it exists.

    The policy defines safety limits and rules for mechanical checks.
    Falls back to DEFAULT_POLICY for any missing sections or values.

    Args:
        repo_dir: Repository directory. Defaults to current working directory.

    Returns:
        Dictionary with policy configuration including limits, sensitive paths,
        dangerous patterns, and eval thresholds.
    """
    policy: dict[str, Any] = copy.deepcopy(DEFAULT_POLICY)

    repo_path = Path(repo_dir) if repo_dir else Path.cwd()
    policy_file = repo_path / WORK_DIR_NAME / POLICY_FILE_NAME

    # Load from file if it exists and tomllib is available
    if tomllib is not None and policy_file.exists():
        try:
            with policy_file.open("rb") as f:
                file_policy = tomllib.load(f)

            # Deep merge file policy into defaults
            for section, default_section in policy.items():
                if section in file_policy:
                    file_section = file_policy[section]
                    if isinstance(default_section, dict) and isinstance(file_section, dict):
                        default_section.update(file_section)
                    else:
                        policy[section] = file_section
        except Exception:
            pass  # Use defaults on parse error

    return policy
