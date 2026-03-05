"""millstone init — first-run project scaffolding command."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Project-type detection
# ---------------------------------------------------------------------------

DETECTION_TABLE = [
    # (signal_files, project_type, default_eval_script)
    (["pyproject.toml", "setup.py"], "Python", "pytest -q"),
    # Node: check for jest/vitest in package.json handled separately
    (["package.json"], "Node", None),
    (["go.mod"], "Go", "go test ./..."),
    (["Cargo.toml"], "Rust", "cargo test"),
]


def _detect_project_type(root: Path) -> tuple[str, str]:
    """Return (project_type, default_eval_script) for the given root directory."""
    for signal_files, project_type, default_script in DETECTION_TABLE:
        for signal in signal_files:
            if (root / signal).exists():
                if project_type == "Node":
                    # Peek inside package.json to determine test runner
                    pkg_text = (root / "package.json").read_text(errors="replace")
                    if "vitest" in pkg_text:
                        return "Node/Vitest", "npx vitest run"
                    if "jest" in pkg_text:
                        return "Node/Jest", "npm test"
                    return "Node", "npm test"
                return project_type, default_script or ""
    return "unknown", ""


def _find_git_root(start: Path) -> Path:
    """Walk up from start until a .git directory is found; return that directory.

    Falls back to start if no git root is found.
    """
    current = start.resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            # Reached filesystem root without finding .git
            return start.resolve()
        current = parent


# ---------------------------------------------------------------------------
# Config / tasklist templates
# ---------------------------------------------------------------------------


def _toml_str(value: str) -> str:
    """Return a TOML-safe double-quoted string literal (same escaping as JSON)."""
    return json.dumps(value)


def _build_config(cli: str, eval_script: str) -> str:
    """Build config.toml content with properly escaped string values."""
    lines = [
        f"cli = {_toml_str(cli)}",
        'tasklist_provider = "file"',
        'tasklist = ".millstone/tasklist.md"',
    ]
    if eval_script:
        lines.insert(1, f"eval_scripts = [{_toml_str(eval_script)}]")
    return "\n".join(lines) + "\n"


_TASKLIST_TEMPLATE = """\
# Tasklist

- [ ] Example task: describe what you want millstone to implement
"""


# ---------------------------------------------------------------------------
# Interactive prompt helper
# ---------------------------------------------------------------------------


def _prompt(question: str, default: str) -> str:
    """Print a prompt and read user input; return default on empty input."""
    display_default = f" [{default}]" if default else ""
    try:
        value = input(f"{question}{display_default}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return value if value else default


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_init(
    yes: bool = False,
    force: bool = False,
    repo_dir: Path | None = None,
) -> int:
    """Scaffold a new millstone project.

    Args:
        yes: Non-interactive mode — accept all detected defaults.
        force: Overwrite existing .millstone/config.toml if present.
        repo_dir: Directory to initialise. Defaults to cwd.

    Returns:
        Exit code (0 = success, 1 = error).
    """
    start = Path(repo_dir) if repo_dir else Path.cwd()
    root = _find_git_root(start)

    millstone_dir = root / ".millstone"
    config_path = millstone_dir / "config.toml"
    tasklist_path = millstone_dir / "tasklist.md"

    # Guard: refuse to overwrite existing config without --force
    if config_path.exists() and not force:
        print(
            f"Error: {config_path} already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        return 1

    # --- Detection ---
    project_type, detected_eval = _detect_project_type(root)
    print(f"Detected project type: {project_type}")

    # --- Interactive prompts (skipped with --yes) ---
    if yes:
        eval_script = detected_eval
        cli = "claude"
    else:
        eval_script = _prompt("Test command", detected_eval)
        cli = _prompt("CLI tool", "claude")
        # tasklist provider: only "file" is supported at init time (per design)
        _prompt("Tasklist provider", "file")

    # --- Write config ---
    millstone_dir.mkdir(exist_ok=True)
    config_path.write_text(_build_config(cli, eval_script))
    print(f"Created {config_path.relative_to(root)}")

    # --- Write example tasklist (if it doesn't exist) ---
    if not tasklist_path.exists():
        tasklist_path.write_text(_TASKLIST_TEMPLATE)
        print(f"Created {tasklist_path.relative_to(root)}  (with example task)")

    print("Run `millstone` to start.")
    return 0
