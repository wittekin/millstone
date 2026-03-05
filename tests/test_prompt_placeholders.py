"""Tests for the provider placeholder system."""

from __future__ import annotations

import pytest

from millstone.artifact_providers.file import FileTasklistProvider
from millstone.artifact_providers.mcp import MCPDesignProvider, MCPTasklistProvider
from millstone.prompts.utils import apply_provider_placeholders

TASKLIST_KEYS = {
    "TASKLIST_READ_INSTRUCTIONS",
    "TASKLIST_COMPLETE_INSTRUCTIONS",
    "TASKLIST_REWRITE_INSTRUCTIONS",
    "TASKLIST_APPEND_INSTRUCTIONS",
    "TASKLIST_UPDATE_INSTRUCTIONS",
}


# ---------------------------------------------------------------------------
# FileTasklistProvider
# ---------------------------------------------------------------------------


class TestFileTasklistProviderPlaceholders:
    def test_returns_all_five_keys(self, tmp_path):
        provider = FileTasklistProvider(tmp_path / "tasklist.md")
        placeholders = provider.get_prompt_placeholders()
        assert set(placeholders) == TASKLIST_KEYS

    def test_path_embedded_in_every_value(self, tmp_path):
        path = tmp_path / "tasks.md"
        provider = FileTasklistProvider(path)
        placeholders = provider.get_prompt_placeholders()
        path_str = str(path)
        for key, value in placeholders.items():
            assert path_str in value, f"Expected path in {key}: {value!r}"

    def test_all_values_non_empty(self, tmp_path):
        provider = FileTasklistProvider(tmp_path / "tasklist.md")
        for key, value in provider.get_prompt_placeholders().items():
            assert value, f"Value for {key} must not be empty"


# ---------------------------------------------------------------------------
# MCPTasklistProvider
# ---------------------------------------------------------------------------


class TestMCPTasklistProviderPlaceholders:
    def test_returns_all_five_keys(self):
        provider = MCPTasklistProvider("linear")
        assert set(provider.get_prompt_placeholders()) == TASKLIST_KEYS

    def test_without_label_read_has_no_label_text(self):
        provider = MCPTasklistProvider("linear")
        placeholders = provider.get_prompt_placeholders()
        assert "label" not in placeholders["TASKLIST_READ_INSTRUCTIONS"]
        assert "label" not in placeholders["TASKLIST_APPEND_INSTRUCTIONS"]

    def test_with_label_read_and_append_mention_label(self):
        provider = MCPTasklistProvider("linear", labels=["my-label"])
        placeholders = provider.get_prompt_placeholders()
        assert "my-label" in placeholders["TASKLIST_READ_INSTRUCTIONS"]
        assert "my-label" in placeholders["TASKLIST_APPEND_INSTRUCTIONS"]

    def test_with_label_complete_does_not_mention_label(self):
        provider = MCPTasklistProvider("linear", labels=["my-label"])
        placeholders = provider.get_prompt_placeholders()
        assert "my-label" not in placeholders["TASKLIST_COMPLETE_INSTRUCTIONS"]
        assert "my-label" not in placeholders["TASKLIST_REWRITE_INSTRUCTIONS"]
        assert "my-label" not in placeholders["TASKLIST_UPDATE_INSTRUCTIONS"]

    def test_all_values_mention_mcp_server(self):
        provider = MCPTasklistProvider("jira")
        for key, value in provider.get_prompt_placeholders().items():
            assert "jira" in value, f"Expected mcp_server in {key}: {value!r}"

    def test_all_values_non_empty(self):
        provider = MCPTasklistProvider("linear")
        for key, value in provider.get_prompt_placeholders().items():
            assert value, f"Value for {key} must not be empty"


# ---------------------------------------------------------------------------
# MCPDesignProvider
# ---------------------------------------------------------------------------


class TestMCPDesignProviderPlaceholders:
    def test_returns_design_write_key(self):
        provider = MCPDesignProvider("notion")
        placeholders = provider.get_prompt_placeholders()
        assert "DESIGN_WRITE_INSTRUCTIONS" in placeholders

    def test_without_project_no_project_text(self):
        provider = MCPDesignProvider("notion")
        value = provider.get_prompt_placeholders()["DESIGN_WRITE_INSTRUCTIONS"]
        assert "project" not in value

    def test_with_project_value_mentions_project(self):
        provider = MCPDesignProvider("notion", projects=["my-project"])
        value = provider.get_prompt_placeholders()["DESIGN_WRITE_INSTRUCTIONS"]
        assert "my-project" in value

    def test_value_mentions_mcp_server(self):
        provider = MCPDesignProvider("confluence")
        value = provider.get_prompt_placeholders()["DESIGN_WRITE_INSTRUCTIONS"]
        assert "confluence" in value

    def test_value_non_empty(self):
        provider = MCPDesignProvider("notion")
        assert provider.get_prompt_placeholders()["DESIGN_WRITE_INSTRUCTIONS"]


# ---------------------------------------------------------------------------
# apply_provider_placeholders
# ---------------------------------------------------------------------------


class TestApplyProviderPlaceholders:
    def test_replaces_known_keys(self):
        prompt = "Do this: {{TASKLIST_READ_INSTRUCTIONS}} then finish."
        placeholders = {"TASKLIST_READ_INSTRUCTIONS": "Read tasks from file."}
        result = apply_provider_placeholders(prompt, placeholders)
        assert "Read tasks from file." in result
        assert "{{TASKLIST_READ_INSTRUCTIONS}}" not in result

    def test_leaves_other_tokens_untouched(self):
        prompt = "Work in {{WORKING_DIRECTORY}} and {{TASKLIST_READ_INSTRUCTIONS}}."
        placeholders = {"TASKLIST_READ_INSTRUCTIONS": "Read tasks."}
        result = apply_provider_placeholders(prompt, placeholders)
        assert "{{WORKING_DIRECTORY}}" in result
        assert "{{TASKLIST_READ_INSTRUCTIONS}}" not in result

    def test_leaves_tokens_not_in_placeholders_dict_untouched(self):
        prompt = "Hello {{SOME_OTHER_TOKEN}} world."
        placeholders = {"TASKLIST_READ_INSTRUCTIONS": "Read tasks."}
        result = apply_provider_placeholders(prompt, placeholders)
        assert "{{SOME_OTHER_TOKEN}}" in result

    def test_raises_value_error_when_value_empty_and_token_in_prompt(self):
        prompt = "Read: {{TASKLIST_READ_INSTRUCTIONS}}"
        placeholders = {"TASKLIST_READ_INSTRUCTIONS": ""}
        with pytest.raises(ValueError, match="TASKLIST_READ_INSTRUCTIONS"):
            apply_provider_placeholders(prompt, placeholders)

    def test_no_error_when_empty_value_but_token_not_in_prompt(self):
        prompt = "No placeholder here."
        placeholders = {"TASKLIST_READ_INSTRUCTIONS": ""}
        result = apply_provider_placeholders(prompt, placeholders)
        assert result == "No placeholder here."

    def test_replaces_multiple_occurrences(self):
        prompt = "{{TASKLIST_READ_INSTRUCTIONS}} and {{TASKLIST_READ_INSTRUCTIONS}}"
        placeholders = {"TASKLIST_READ_INSTRUCTIONS": "READ"}
        result = apply_provider_placeholders(prompt, placeholders)
        assert result == "READ and READ"

    def test_replaces_multiple_keys(self):
        prompt = "{{TASKLIST_READ_INSTRUCTIONS}} ... {{TASKLIST_COMPLETE_INSTRUCTIONS}}"
        placeholders = {
            "TASKLIST_READ_INSTRUCTIONS": "READ",
            "TASKLIST_COMPLETE_INSTRUCTIONS": "COMPLETE",
        }
        result = apply_provider_placeholders(prompt, placeholders)
        assert result == "READ ... COMPLETE"
