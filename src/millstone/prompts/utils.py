"""Shared prompt utility functions for millstone."""

from __future__ import annotations


def apply_provider_placeholders(prompt: str, placeholders: dict[str, str]) -> str:
    """Replace provider-specific placeholder tokens in a prompt.

    Only tokens present in *placeholders* are touched. Raises ``ValueError``
    if a provider key appears in the template but its resolved value is the
    empty string.
    """
    for key, value in placeholders.items():
        token = f"{{{{{key}}}}}"
        if token in prompt:
            if not value:
                raise ValueError(f"Provider placeholder {token} resolved to empty string")
            prompt = prompt.replace(token, value)
    return prompt
