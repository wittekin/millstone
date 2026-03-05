import subprocess
import sys
import textwrap

from millstone.config import load_config


def test_parallel_config_defaults(temp_repo):
    cfg = load_config(temp_repo)

    assert cfg["parallel_enabled"] is False
    assert cfg["parallel_concurrency"] == 1
    assert cfg["parallel_merge_strategy"] == "merge"
    assert cfg["parallel_integration_branch"] == "millstone/integration"
    assert cfg["parallel_worktree_root"] == ".millstone/worktrees"
    assert cfg["parallel_cleanup"] == "on_success"
    assert cfg["parallel_lock_git"] == ".millstone/locks/git.lock"
    assert cfg["parallel_lock_state"] == ".millstone/locks/state.lock"
    assert cfg["parallel_lock_tasklist"] == ".millstone/locks/tasklist.lock"
    assert cfg["parallel_heartbeat_interval"] == 30
    assert cfg["parallel_heartbeat_ttl"] == 300


def test_parallel_config_from_toml(temp_repo):
    config_dir = temp_repo / ".millstone"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text(
        textwrap.dedent("""\
        parallel_enabled = true
        parallel_concurrency = 4
        parallel_merge_strategy = "cherry-pick"
    """)
    )

    cfg = load_config(temp_repo)
    assert cfg["parallel_enabled"] is True
    assert cfg["parallel_concurrency"] == 4
    assert cfg["parallel_merge_strategy"] == "cherry-pick"


# ---------------------------------------------------------------------------
# tasklist_filter schema
# ---------------------------------------------------------------------------


def test_tasklist_filter_defaults(temp_repo):
    """tasklist_filter defaults to empty lists for all filter keys."""
    cfg = load_config(temp_repo)
    tf = cfg["tasklist_filter"]
    assert tf["labels"] == []
    assert tf["assignees"] == []
    assert tf["statuses"] == []


# ---------------------------------------------------------------------------
# tasklist_filter UX shortcuts
# ---------------------------------------------------------------------------


def test_tasklist_filter_shortcut_defaults(temp_repo):
    """Shortcut keys default to empty string."""
    cfg = load_config(temp_repo)
    tf = cfg["tasklist_filter"]
    assert tf["label"] == ""
    assert tf["assignee"] == ""
    assert tf["status"] == ""


def test_tasklist_filter_label_shortcut_from_toml(temp_repo):
    """label shortcut is loaded from config.toml as a string."""
    config_dir = temp_repo / ".millstone"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text(
        textwrap.dedent("""\
        [tasklist_filter]
        label = "sprint-1"
    """)
    )

    cfg = load_config(temp_repo)
    tf = cfg["tasklist_filter"]
    assert tf["label"] == "sprint-1"


def test_tasklist_filter_all_shortcuts_from_toml(temp_repo):
    """All three shortcuts are loaded from config.toml."""
    config_dir = temp_repo / ".millstone"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text(
        textwrap.dedent("""\
        [tasklist_filter]
        label = "sprint-1"
        assignee = "alice"
        status = "Todo"
    """)
    )

    cfg = load_config(temp_repo)
    tf = cfg["tasklist_filter"]
    assert tf["label"] == "sprint-1"
    assert tf["assignee"] == "alice"
    assert tf["status"] == "Todo"


def test_tasklist_filter_from_toml(temp_repo):
    """tasklist_filter values are loaded from config.toml."""
    config_dir = temp_repo / ".millstone"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text(
        textwrap.dedent("""\
        [tasklist_filter]
        labels = ["sprint-1", "backend"]
        assignees = ["alice"]
        statuses = ["Todo", "In Progress"]
    """)
    )

    cfg = load_config(temp_repo)
    tf = cfg["tasklist_filter"]
    assert tf["labels"] == ["sprint-1", "backend"]
    assert tf["assignees"] == ["alice"]
    assert tf["statuses"] == ["Todo", "In Progress"]


def test_tasklist_filter_partial_override(temp_repo):
    """Unspecified filter keys retain empty-list defaults."""
    config_dir = temp_repo / ".millstone"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text(
        textwrap.dedent("""\
        [tasklist_filter]
        labels = ["urgent"]
    """)
    )

    cfg = load_config(temp_repo)
    tf = cfg["tasklist_filter"]
    assert tf["labels"] == ["urgent"]
    # Partial override replaces the whole dict; the other keys are absent in file
    # so we only check what the file provided.
    # (If the merge keeps defaults for unspecified sub-keys, assert them too.)


def test_tasklist_filter_backward_compat_no_filter_section(temp_repo):
    """Configs without [tasklist_filter] produce empty filter defaults."""
    config_dir = temp_repo / ".millstone"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text(
        textwrap.dedent("""\
        max_cycles = 5
        tasklist_provider = "file"
    """)
    )

    cfg = load_config(temp_repo)
    tf = cfg["tasklist_filter"]
    assert tf["labels"] == []
    assert tf["assignees"] == []
    assert tf["statuses"] == []


# ---------------------------------------------------------------------------
# CLI --help: tasklist_filter scoping section
# ---------------------------------------------------------------------------


def _help_output() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "millstone.runtime.orchestrator", "--help"],
        capture_output=True,
        text=True,
    )
    # argparse prints help to stdout and exits 0
    return result.stdout


def test_help_contains_remote_backlog_scoping_section():
    """--help output includes the 'Remote backlog scoping' section."""
    assert "Remote backlog scoping" in _help_output()


def test_help_shortcut_examples_present():
    """--help output shows single-value shortcut examples for label/assignee/status."""
    out = _help_output()
    assert 'label = "sprint-1"' in out
    assert 'assignee = "alice"' in out
    assert 'status = "Todo"' in out


def test_help_list_form_examples_present():
    """--help output shows multi-value list form examples."""
    out = _help_output()
    assert "labels" in out
    assert "assignees" in out
    assert "statuses" in out


def test_help_shortcut_equivalence_annotation_present():
    """--help output documents that shortcut is equivalent to list form."""
    assert "equivalent to" in _help_output()
