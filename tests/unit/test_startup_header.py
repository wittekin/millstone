"""Tests for the compact/verbose startup header (T9)."""

from pathlib import Path

from millstone.runtime.orchestrator import Orchestrator


def _make_orchestrator(tmp_path: Path, capsys=None, **kwargs) -> Orchestrator:
    """Return a minimal Orchestrator pointing at a temp repo.

    If *capsys* is passed, the capture buffer is drained after construction
    so callers only see header output on their next ``readouterr()`` call.
    We pre-create the .gitignore to suppress the "Created .gitignore" message.
    """
    ms_dir = tmp_path / ".millstone"
    ms_dir.mkdir(parents=True, exist_ok=True)
    tasklist = ms_dir / "tasklist.md"
    tasklist.write_text("- [ ] Task A\n- [ ] Task B\n")
    # Pre-create .gitignore so _setup_work_dir doesn't print a message
    gitignore = tmp_path / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("/.millstone/\n")
    return Orchestrator(repo_dir=tmp_path, tasklist=str(tasklist), max_tasks=2, **kwargs)


class TestCompactStartupHeader:
    """Default (verbose_header=False) prints a 2-line compact header."""

    def test_compact_header_is_two_lines(self, tmp_path, capsys):
        _make_orchestrator(tmp_path)
        captured = capsys.readouterr().out.strip()
        lines = captured.splitlines()
        assert len(lines) == 2

    def test_compact_header_contains_version(self, tmp_path, capsys):
        orch = _make_orchestrator(tmp_path)
        captured = capsys.readouterr().out
        version = orch._get_version()
        assert f"millstone {version}" in captured

    def test_compact_header_contains_log_path(self, tmp_path, capsys):
        orch = _make_orchestrator(tmp_path)
        captured = capsys.readouterr().out
        assert f"Log: {orch.log_file}" in captured

    def test_compact_header_task_source_file_mode(self, tmp_path, capsys):
        orch = _make_orchestrator(tmp_path)
        captured = capsys.readouterr().out
        assert f"(max {orch.max_tasks})" in captured

    def test_compact_header_task_source_direct_task(self, tmp_path, capsys):
        _make_orchestrator(tmp_path, task="do something")
        captured = capsys.readouterr().out
        assert "1 direct task" in captured

    def test_compact_header_synthetic_task_not_direct(self, tmp_path, capsys):
        """Synthetic tasks used by --review-design/--analyze/--design should NOT show '1 direct task'."""
        for synthetic in ("review-design", "analyze", "design"):
            _make_orchestrator(tmp_path, task=synthetic)
            captured = capsys.readouterr().out
            assert "1 direct task" not in captured, f"synthetic task={synthetic!r} misclassified"

    def test_compact_header_cli_info_single(self, tmp_path, capsys):
        _make_orchestrator(tmp_path, cli="codex")
        captured = capsys.readouterr().out
        assert "codex" in captured

    def test_compact_header_cli_info_overrides(self, tmp_path, capsys):
        _make_orchestrator(tmp_path, cli="claude", cli_reviewer="codex")
        captured = capsys.readouterr().out
        assert "claude + overrides" in captured


class TestVerboseStartupHeader:
    """verbose_header=True prints the full multi-line header."""

    def test_verbose_header_is_multiline(self, tmp_path, capsys):
        _make_orchestrator(tmp_path, verbose_header=True)
        captured = capsys.readouterr().out.strip()
        lines = captured.splitlines()
        assert len(lines) > 2

    def test_verbose_header_starts_with_banner(self, tmp_path, capsys):
        _make_orchestrator(tmp_path, verbose_header=True)
        captured = capsys.readouterr().out
        assert "=== Orchestrator Started ===" in captured

    def test_verbose_header_contains_repo(self, tmp_path, capsys):
        _make_orchestrator(tmp_path, verbose_header=True)
        captured = capsys.readouterr().out
        assert f"Repo: {tmp_path}" in captured

    def test_verbose_header_contains_max_cycles(self, tmp_path, capsys):
        _make_orchestrator(tmp_path, verbose_header=True)
        captured = capsys.readouterr().out
        assert "Max cycles per task:" in captured


class TestQuietModeNoHeader:
    """quiet=True suppresses all header output."""

    def test_quiet_no_output(self, tmp_path, capsys):
        _make_orchestrator(tmp_path, quiet=True)
        captured = capsys.readouterr().out
        assert captured.strip() == ""
