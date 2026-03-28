from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from millstone.devtools import local_ci


def test_run_local_ci_executes_expected_steps(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    temp_root = tmp_path / "temp"
    temp_root.mkdir()

    calls: list[tuple[list[str], Path, dict[str, str] | None]] = []

    def fake_run(command, cwd, env=None, check=None):
        command = [str(part) for part in command]
        calls.append((command, Path(cwd), env))
        if command[:3] == [pytest.__name__, "-m", "not integration"]:
            pass
        if command[:3] == [local_ci.sys.executable, "-m", "build"]:
            dist_dir = Path(command[-1])
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / "millstone-0.5.6-py3-none-any.whl").write_text("wheel")
            (dist_dir / "millstone-0.5.6.tar.gz").write_text("sdist")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(local_ci.subprocess, "run", fake_run)

    assert local_ci.run_local_ci(repo_root=repo_root, temp_root=temp_root) == 0

    commands = [command for command, _, _ in calls]
    assert commands[0] == ["ruff", "check", "src", "tests"]
    assert commands[1] == ["ruff", "format", "--check", "src", "tests", "docs"]
    assert commands[2] == ["mypy", "src/millstone"]
    assert commands[3] == ["vulture", "src", "tests", "--min-confidence", "80"]
    assert commands[4] == ["pytest", "-o", "addopts=--tb=short -q", "-m", "not integration"]
    assert commands[5] == [
        "pytest",
        "-o",
        "addopts=--tb=short -q",
        "-m",
        "not integration",
        "--cov=millstone",
        "--cov-branch",
        f"--cov-report=xml:{temp_root / 'coverage.xml'}",
        "--cov-report=term-missing",
    ]
    assert commands[6] == [
        local_ci.sys.executable,
        "-m",
        "build",
        "--outdir",
        str(temp_root / "dist"),
    ]
    assert commands[7] == [
        "twine",
        "check",
        str(temp_root / "dist" / "millstone-0.5.6-py3-none-any.whl"),
        str(temp_root / "dist" / "millstone-0.5.6.tar.gz"),
    ]
    assert commands[8] == ["mkdocs", "build", "--strict", "--site-dir", str(temp_root / "site")]
    assert commands[9] == [local_ci.sys.executable, "-m", "venv", str(temp_root / "smoke-venv")]

    smoke_python = local_ci._venv_bin(temp_root / "smoke-venv", "python")
    smoke_millstone = local_ci._venv_bin(temp_root / "smoke-venv", "millstone")
    assert commands[10] == [str(smoke_python), "-m", "pip", "install", "--upgrade", "pip"]
    assert commands[11] == [
        str(smoke_python),
        "-m",
        "pip",
        "install",
        str(temp_root / "dist" / "millstone-0.5.6-py3-none-any.whl"),
    ]
    assert commands[12] == [str(smoke_millstone), "--version"]
    assert all(cwd == repo_root for _, cwd, _ in calls)
    assert calls[8][2]["NO_MKDOCS_2_WARNING"] == "1"


def test_run_local_ci_requires_built_wheel(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    temp_root = tmp_path / "temp"
    temp_root.mkdir()

    def fake_run(command, cwd, env=None, check=None):
        command = [str(part) for part in command]
        if command[:3] == [local_ci.sys.executable, "-m", "build"]:
            dist_dir = Path(command[-1])
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / "millstone-0.5.6.tar.gz").write_text("sdist")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(local_ci.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="No wheel was built"):
        local_ci.run_local_ci(repo_root=repo_root, temp_root=temp_root)
