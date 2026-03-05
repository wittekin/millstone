"""Tests for `millstone init` command (millstone.commands.init)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from millstone.commands.init import _detect_project_type, _find_git_root, run_init
from millstone.config import load_config

# ---------------------------------------------------------------------------
# Detection heuristics
# ---------------------------------------------------------------------------


def test_detect_python_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[build-system]\n")
    project_type, eval_script = _detect_project_type(tmp_path)
    assert project_type == "Python"
    assert eval_script == "pytest -q"


def test_detect_python_setup_py(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    project_type, eval_script = _detect_project_type(tmp_path)
    assert project_type == "Python"
    assert eval_script == "pytest -q"


def test_detect_node_jest(tmp_path):
    (tmp_path / "package.json").write_text('{"devDependencies": {"jest": "^29"}}')
    project_type, eval_script = _detect_project_type(tmp_path)
    assert project_type == "Node/Jest"
    assert eval_script == "npm test"


def test_detect_node_no_known_runner(tmp_path):
    """package.json without jest or vitest should not be labelled Node/Jest."""
    (tmp_path / "package.json").write_text('{"devDependencies": {"mocha": "^10"}}')
    project_type, eval_script = _detect_project_type(tmp_path)
    assert project_type == "Node"
    assert eval_script == "npm test"


def test_detect_node_vitest(tmp_path):
    (tmp_path / "package.json").write_text('{"devDependencies": {"vitest": "^1"}}')
    project_type, eval_script = _detect_project_type(tmp_path)
    assert project_type == "Node/Vitest"
    assert eval_script == "npx vitest run"


def test_detect_go(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/app\n")
    project_type, eval_script = _detect_project_type(tmp_path)
    assert project_type == "Go"
    assert eval_script == "go test ./..."


def test_detect_rust(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "app"\n')
    project_type, eval_script = _detect_project_type(tmp_path)
    assert project_type == "Rust"
    assert eval_script == "cargo test"


def test_detect_unknown(tmp_path):
    project_type, eval_script = _detect_project_type(tmp_path)
    assert project_type == "unknown"
    assert eval_script == ""


# ---------------------------------------------------------------------------
# Git root discovery
# ---------------------------------------------------------------------------


def test_find_git_root_at_root(tmp_path):
    (tmp_path / ".git").mkdir()
    assert _find_git_root(tmp_path) == tmp_path


def test_find_git_root_from_subdirectory(tmp_path):
    (tmp_path / ".git").mkdir()
    subdir = tmp_path / "src" / "pkg"
    subdir.mkdir(parents=True)
    assert _find_git_root(subdir) == tmp_path


def test_find_git_root_no_git_falls_back(tmp_path):
    # No .git anywhere — should fall back to the start directory
    result = _find_git_root(tmp_path)
    assert result == tmp_path.resolve()


# ---------------------------------------------------------------------------
# run_init — non-interactive (--yes) path
# ---------------------------------------------------------------------------


def _make_git_repo(path: Path) -> None:
    """Initialise a minimal git repo at path."""
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)


def test_run_init_creates_config_and_tasklist(tmp_path):
    _make_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[build-system]\n")

    rc = run_init(yes=True, repo_dir=tmp_path)

    assert rc == 0
    config = (tmp_path / ".millstone" / "config.toml").read_text()
    assert 'cli = "claude"' in config
    assert "pytest -q" in config
    assert 'tasklist_provider = "file"' in config

    tasklist = (tmp_path / ".millstone" / "tasklist.md").read_text()
    assert "- [ ]" in tasklist


def test_run_init_no_eval_script_for_unknown(tmp_path):
    _make_git_repo(tmp_path)
    # No project signal files — unknown type

    rc = run_init(yes=True, repo_dir=tmp_path)

    assert rc == 0
    config = (tmp_path / ".millstone" / "config.toml").read_text()
    # eval_scripts line should not appear when eval_script is empty
    assert "eval_scripts" not in config


def test_run_init_refuses_overwrite_without_force(tmp_path):
    _make_git_repo(tmp_path)
    millstone_dir = tmp_path / ".millstone"
    millstone_dir.mkdir()
    (millstone_dir / "config.toml").write_text("[millstone]\ncli = 'claude'\n")

    rc = run_init(yes=True, repo_dir=tmp_path)

    assert rc == 1
    # Existing config must be untouched
    assert "cli = 'claude'" in (millstone_dir / "config.toml").read_text()


def test_run_init_force_overwrites_existing(tmp_path):
    _make_git_repo(tmp_path)
    millstone_dir = tmp_path / ".millstone"
    millstone_dir.mkdir()
    (millstone_dir / "config.toml").write_text("[millstone]\ncli = 'old'\n")
    (tmp_path / "pyproject.toml").write_text("[build-system]\n")

    rc = run_init(yes=True, force=True, repo_dir=tmp_path)

    assert rc == 0
    config = (millstone_dir / "config.toml").read_text()
    assert 'cli = "claude"' in config
    assert "old" not in config


def test_run_init_does_not_overwrite_existing_tasklist(tmp_path):
    _make_git_repo(tmp_path)
    millstone_dir = tmp_path / ".millstone"
    millstone_dir.mkdir()
    existing_tasklist_content = "# My existing tasklist\n- [ ] Do something\n"
    (millstone_dir / "tasklist.md").write_text(existing_tasklist_content)

    rc = run_init(yes=True, repo_dir=tmp_path)

    assert rc == 0
    # Existing tasklist must not be overwritten
    assert (millstone_dir / "tasklist.md").read_text() == existing_tasklist_content


def test_run_init_walks_to_git_root(tmp_path):
    """run_init called from a subdirectory should write config at the git root."""
    _make_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[build-system]\n")
    subdir = tmp_path / "src"
    subdir.mkdir()

    rc = run_init(yes=True, repo_dir=subdir)

    assert rc == 0
    # Config written at git root, not subdirectory
    assert (tmp_path / ".millstone" / "config.toml").exists()
    assert not (subdir / ".millstone").exists()


# ---------------------------------------------------------------------------
# Config loader compatibility (regression guard)
# ---------------------------------------------------------------------------


def test_init_config_consumed_by_load_config(tmp_path):
    """Config written by run_init must be read back correctly by load_config()."""
    _make_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[build-system]\n")

    rc = run_init(yes=True, repo_dir=tmp_path)
    assert rc == 0

    config = load_config(repo_dir=tmp_path)
    assert config["cli"] == "claude"
    assert config["eval_scripts"] == ["pytest -q"]
    assert config["tasklist_provider"] == "file"
    assert config["tasklist"] == ".millstone/tasklist.md"


def test_init_config_no_eval_consumed_by_load_config(tmp_path):
    """Config without eval_scripts (unknown project) is read back by load_config()."""
    _make_git_repo(tmp_path)
    # No signal files — unknown project type, no eval_scripts written

    rc = run_init(yes=True, repo_dir=tmp_path)
    assert rc == 0

    config = load_config(repo_dir=tmp_path)
    assert config["cli"] == "claude"
    assert config["eval_scripts"] == []  # default preserved; not overwritten
    assert config["tasklist_provider"] == "file"


def test_init_config_special_chars_produce_valid_toml(tmp_path):
    """User-provided values with quotes/backslashes must not corrupt the TOML."""
    from millstone.commands.init import _build_config

    # Values containing double quotes and backslashes
    cli = 'my"cli'
    eval_script = 'pytest -k "smoke" --path=C:\\tests'

    content = _build_config(cli, eval_script)

    # Must parse without error — use same fallback as millstone.config
    from millstone.config import _load_toml_library

    _tomllib = _load_toml_library()
    assert _tomllib is not None, "No TOML library available; skip or install tomli"
    parsed = _tomllib.loads(content)
    assert parsed["cli"] == cli
    assert parsed["eval_scripts"] == [eval_script]


def test_run_init_quoted_cli_round_trips(tmp_path):
    """run_init with a cli value containing quotes writes valid, readable TOML."""
    from unittest.mock import patch

    from millstone.config import _load_toml_library

    _tomllib = _load_toml_library()
    assert _tomllib is not None, "No TOML library available; skip or install tomli"

    _make_git_repo(tmp_path)

    tricky_cli = 'claude "experimental"'
    # _prompt is called for eval_script first, then cli — return tricky_cli for cli call
    prompt_returns = iter(["pytest -q", tricky_cli, "file"])
    with patch("millstone.commands.init._prompt", side_effect=prompt_returns):
        rc = run_init(yes=False, repo_dir=tmp_path)

    assert rc == 0
    raw = (tmp_path / ".millstone" / "config.toml").read_text()
    parsed = _tomllib.loads(raw)
    assert parsed["cli"] == tricky_cli
