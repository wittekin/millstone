"""Minimal loops registry exports."""

from millstone.loops.registry.loops import DEV_REVIEW_LOOP, LOOP_REGISTRY


def get_loop(loop_id: str):
    """Look up a loop definition by ID."""
    return LOOP_REGISTRY.get(loop_id)


__all__ = ["DEV_REVIEW_LOOP", "LOOP_REGISTRY", "get_loop"]
