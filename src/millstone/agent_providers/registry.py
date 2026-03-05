"""Registry helpers for agent providers."""

from millstone.agent_providers.base import CLIProvider
from millstone.agent_providers.implementations import (
    ClaudeProvider,
    CodexProvider,
    GeminiProvider,
    OpenCodeProvider,
)

PROVIDERS: dict[str, type[CLIProvider]] = {
    "claude": ClaudeProvider,
    "codex": CodexProvider,
    "gemini": GeminiProvider,
    "opencode": OpenCodeProvider,
}


def get_provider(name: str) -> CLIProvider:
    """Get a CLI provider by name."""
    if name not in PROVIDERS:
        available = ", ".join(PROVIDERS.keys())
        raise ValueError(f"Unknown CLI provider: {name}. Available: {available}")
    return PROVIDERS[name]()


def list_providers() -> list[str]:
    """List all available provider names."""
    return list(PROVIDERS.keys())
