"""Compatibility shim for runtime orchestrator entrypoints."""

from millstone.runtime import orchestrator as _orchestrator
from millstone.runtime.orchestrator import *  # noqa: F403


def __getattr__(name: str):
    """Proxy attribute access to the canonical runtime module."""
    return getattr(_orchestrator, name)
