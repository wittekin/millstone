"""Run the high-signal local subset of CI before pushing."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _venv_bin(venv_dir: Path, name: str) -> Path:
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" and not name.endswith(".exe") else ""
    return venv_dir / scripts_dir / f"{name}{suffix}"


def _run_step(
    name: str,
    command: Iterable[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> None:
    command_list = [str(part) for part in command]
    print(f"[local-ci] {name}", flush=True)
    print(f"[local-ci] $ {' '.join(command_list)}", flush=True)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    subprocess.run(command_list, cwd=cwd, env=full_env, check=True)


def run_local_ci(repo_root: Path | None = None, temp_root: Path | None = None) -> int:
    repo_root = repo_root or _repo_root()

    if temp_root is None:
        with tempfile.TemporaryDirectory(prefix="millstone-local-ci-") as temp_dir:
            return run_local_ci(repo_root=repo_root, temp_root=Path(temp_dir))

    dist_dir = temp_root / "dist"
    coverage_xml = temp_root / "coverage.xml"
    site_dir = temp_root / "site"
    smoke_venv = temp_root / "smoke-venv"
    smoke_python = _venv_bin(smoke_venv, "python")
    smoke_millstone = _venv_bin(smoke_venv, "millstone")

    _run_step("ruff lint", ["ruff", "check", "src", "tests"], cwd=repo_root)
    _run_step("ruff format", ["ruff", "format", "--check", "src", "tests", "docs"], cwd=repo_root)
    _run_step("mypy", ["mypy", "src/millstone"], cwd=repo_root)
    _run_step("vulture", ["vulture", "src", "tests", "--min-confidence", "80"], cwd=repo_root)
    pytest_args = ["pytest", "-o", "addopts=--tb=short -q", "-m", "not integration"]
    _run_step("pytest", pytest_args, cwd=repo_root)
    _run_step(
        "pytest coverage",
        [
            *pytest_args,
            "--cov=millstone",
            "--cov-branch",
            f"--cov-report=xml:{coverage_xml}",
            "--cov-report=term-missing",
        ],
        cwd=repo_root,
    )
    _run_step(
        "build distributions",
        [sys.executable, "-m", "build", "--outdir", str(dist_dir)],
        cwd=repo_root,
    )

    dist_files = sorted(path for path in dist_dir.iterdir() if path.is_file())
    if not dist_files:
        raise RuntimeError(f"No distributions were built in {dist_dir}")

    wheels = [path for path in dist_files if path.suffix == ".whl"]
    if not wheels:
        raise RuntimeError(f"No wheel was built in {dist_dir}")

    _run_step("twine check", ["twine", "check", *(str(path) for path in dist_files)], cwd=repo_root)
    _run_step(
        "mkdocs build",
        ["mkdocs", "build", "--strict", "--site-dir", str(site_dir)],
        cwd=repo_root,
        env={"NO_MKDOCS_2_WARNING": "1"},
    )
    _run_step("create smoke venv", [sys.executable, "-m", "venv", str(smoke_venv)], cwd=repo_root)
    _run_step(
        "smoke venv pip upgrade",
        [str(smoke_python), "-m", "pip", "install", "--upgrade", "pip"],
        cwd=repo_root,
    )
    _run_step(
        "smoke install wheel",
        [str(smoke_python), "-m", "pip", "install", str(wheels[0])],
        cwd=repo_root,
    )
    _run_step("smoke millstone", [str(smoke_millstone), "--version"], cwd=repo_root)
    return 0


def main() -> int:
    try:
        return run_local_ci()
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"[local-ci] failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
