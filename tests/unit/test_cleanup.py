"""Tests for Orchestrator.cleanup() preserving user files.

cleanup() must only remove known transient files — never user-created
config or data files like policy.toml, project.toml, or unknown files.
"""

from __future__ import annotations

from pathlib import Path

from millstone.runtime.orchestrator import Orchestrator


class TestCleanupPreservesUserFiles:
    """cleanup() must not delete user-created files."""

    def test_preserves_policy_toml(self, temp_repo: Path) -> None:
        orch = Orchestrator(max_tasks=1)
        try:
            policy_file = orch.work_dir / "policy.toml"
            policy_file.write_text('[dangerous]\naction = "flag"\n')

            orch.cleanup()

            assert policy_file.exists(), "cleanup() deleted policy.toml"
            assert policy_file.read_text() == '[dangerous]\naction = "flag"\n'
        finally:
            pass  # cleanup already ran

    def test_preserves_project_toml(self, temp_repo: Path) -> None:
        orch = Orchestrator(max_tasks=1)
        try:
            project_file = orch.work_dir / "project.toml"
            project_file.write_text('[project]\nname = "myapp"\n')

            orch.cleanup()

            assert project_file.exists(), "cleanup() deleted project.toml"
        finally:
            pass

    def test_preserves_unknown_user_files(self, temp_repo: Path) -> None:
        """Files the user creates that millstone doesn't know about must survive."""
        orch = Orchestrator(max_tasks=1)
        try:
            custom_file = orch.work_dir / "my_notes.md"
            custom_file.write_text("Important notes")

            orch.cleanup()

            assert custom_file.exists(), "cleanup() deleted unknown user file"
        finally:
            pass

    def test_preserves_unknown_user_directories(self, temp_repo: Path) -> None:
        orch = Orchestrator(max_tasks=1)
        try:
            custom_dir = orch.work_dir / "my_scripts"
            custom_dir.mkdir()
            (custom_dir / "helper.sh").write_text("#!/bin/bash\necho hi")

            orch.cleanup()

            assert custom_dir.exists(), "cleanup() deleted unknown user directory"
            assert (custom_dir / "helper.sh").exists()
        finally:
            pass


class TestCleanupRemovesTransientFiles:
    """cleanup() must still remove known transient artifacts."""

    def test_removes_stop_file(self, temp_repo: Path) -> None:
        orch = Orchestrator(max_tasks=1)
        try:
            stop_file = orch.work_dir / "STOP.md"
            stop_file.write_text("halted")

            orch.cleanup()

            assert not stop_file.exists(), "cleanup() should remove STOP.md"
        finally:
            pass

    def test_preserves_runs_directory(self, temp_repo: Path) -> None:
        orch = Orchestrator(max_tasks=1)
        try:
            runs_dir = orch.work_dir / "runs"
            runs_dir.mkdir(exist_ok=True)
            (runs_dir / "20260408_120000.log").write_text("log content")

            orch.cleanup()

            assert runs_dir.exists()
            assert (runs_dir / "20260408_120000.log").exists()
        finally:
            pass

    def test_preserves_config_toml(self, temp_repo: Path) -> None:
        orch = Orchestrator(max_tasks=1)
        try:
            config_file = orch.work_dir / "config.toml"
            config_file.write_text("cli = 'claude'\n")

            orch.cleanup()

            assert config_file.exists(), "cleanup() deleted config.toml"
        finally:
            pass
