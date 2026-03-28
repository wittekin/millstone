"""Runtime orchestration, execution, and workspace state helpers."""

__all__ = ["Orchestrator", "main"]


def __getattr__(name: str):
    if name in {"Orchestrator", "main"}:
        from millstone.runtime.orchestrator import Orchestrator, main

        exports = {"Orchestrator": Orchestrator, "main": main}
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
