"""Abstract contracts for agent execution providers."""

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class CLIResult:
    """Result from a CLI invocation."""

    output: str
    returncode: int
    stdout: str
    stderr: str


class CLIProvider(ABC):
    """Abstract base class for agentic CLI providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name of this CLI provider."""

    @property
    @abstractmethod
    def command(self) -> str:
        """Base command name (e.g., 'claude', 'codex')."""

    @property
    @abstractmethod
    def install_instructions(self) -> str:
        """Instructions for installing this CLI."""

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        *,
        resume: str | None = None,
        model: str | None = None,
        output_schema: str | None = None,
        schema_work_dir: str | None = None,
    ) -> list[str]:
        """Build command-line args for this provider."""

    @abstractmethod
    def version_command(self) -> list[str]:
        """Command to check CLI version (for preflight checks)."""

    def run(
        self,
        prompt: str,
        *,
        resume: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
        output_schema: str | None = None,
        schema_work_dir: str | None = None,
    ) -> CLIResult:
        """Run the CLI with the given parameters."""
        cmd = self.build_command(
            prompt,
            resume=resume,
            model=model,
            output_schema=output_schema,
            schema_work_dir=schema_work_dir,
        )
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
        )

        output = result.stdout
        if result.stdout and result.stderr:
            output += "\n"
        output += result.stderr

        return CLIResult(
            output=output,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def check_available(self) -> tuple[bool, str]:
        """Check if this CLI is available and working."""
        try:
            result = subprocess.run(
                self.version_command(),
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                version = result.stdout.strip() or result.stderr.strip()
                return True, f"{self.name} available: {version}"
            return False, f"{self.name} returned error: {result.stderr}"
        except FileNotFoundError:
            return False, f"{self.name} not found. {self.install_instructions}"
