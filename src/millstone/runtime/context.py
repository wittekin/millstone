"""
Cross-task context management for the millstone orchestrator.

This module contains the ContextManager class which handles cross-task context
sharing between related tasks in the same group. The Orchestrator class holds
an instance and delegates via thin wrapper methods.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Avoid circular import - only used for type hints
    pass


class ContextManager:
    """Manages cross-task context sharing for task groups.

    This class handles operations related to accumulating and retrieving
    context information shared between tasks in the same group, including:
    - Context directory management
    - Group context file creation and updates
    - LLM-based context extraction from git diffs
    """

    def __init__(self, work_dir: Path):
        """Initialize the ContextManager.

        Args:
            work_dir: Path to the work directory (.millstone/).
        """
        self.work_dir = work_dir

    def _get_context_dir(self) -> Path:
        """Return the path to the context directory."""
        return self.work_dir / "context"

    def _get_group_context_path(self, group_name: str) -> Path:
        """Return the path to the context file for a specific group.

        Args:
            group_name: The name of the task group.

        Returns:
            Path to the group's context file.
        """
        # Sanitize group name for filesystem (replace spaces/special chars)
        safe_name = re.sub(r"[^\w\-]", "_", group_name.lower())
        return self._get_context_dir() / f"{safe_name}.md"

    def get_group_context(
        self,
        group_name: str | None = None,
        current_task_group: str | None = None,
    ) -> str | None:
        """Load accumulated context for a task group.

        Reads the context file for the specified group (or current_task_group
        if not specified) and returns its contents.

        Args:
            group_name: The group name, or None to use current_task_group.
            current_task_group: The current task's group (from orchestrator state).

        Returns:
            The accumulated context as a string, or None if no context exists.
        """
        target_group = group_name or current_task_group
        if not target_group:
            return None

        context_path = self._get_group_context_path(target_group)
        if not context_path.exists():
            return None

        content = context_path.read_text().strip()
        return content if content else None

    def accumulate_group_context(
        self,
        task_text: str,
        group_name: str | None = None,
        git_diff: str | None = None,
        current_task_group: str | None = None,
        log_callback: Callable[..., None] | None = None,
        extract_context_callback: Callable[[str, str], dict | None] | None = None,
    ) -> bool:
        """Append a task summary to the group's accumulated context.

        Called after a task is approved and committed. Extracts the task title
        and creates a summary entry in the group's context file. If git_diff is
        provided, uses LLM to extract key decisions and patterns.

        Args:
            task_text: The full task description that was completed.
            group_name: The group name, or None to use current_task_group.
            git_diff: Optional git diff of the changes. If provided, LLM
                     will extract key decisions/patterns for richer context.
            current_task_group: The current task's group (from orchestrator state).
            log_callback: Optional callback for logging events.
            extract_context_callback: Optional callback for extracting context
                via LLM. Takes (task_text, git_diff) and returns dict or None.

        Returns:
            True if context was accumulated, False if task is not in a group.
        """
        target_group = group_name or current_task_group
        if not target_group:
            return False

        # Ensure context directory exists
        context_dir = self._get_context_dir()
        context_dir.mkdir(parents=True, exist_ok=True)

        context_path = self._get_group_context_path(target_group)

        # Extract task title (first line, stripping markdown bold markers)
        task_title = task_text.split("\n")[0].strip()
        task_title = re.sub(r"\*\*(.+?)\*\*", r"\1", task_title)  # Remove **bold**

        # Build the context entry
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n## {task_title}\n\n"
        entry += f"_Completed: {timestamp}_\n\n"

        # Try to extract context using LLM if git_diff is available
        extracted_context = None
        if git_diff and git_diff.strip() and extract_context_callback:
            extracted_context = extract_context_callback(task_text, git_diff)

        if extracted_context:
            # Use LLM-extracted context
            if extracted_context.get("summary"):
                entry += f"**Summary:** {extracted_context['summary']}\n\n"
            if extracted_context.get("key_decisions"):
                entry += "**Key decisions:**\n"
                for decision in extracted_context["key_decisions"]:
                    entry += f"- {decision}\n"
                entry += "\n"
        else:
            # Fall back to truncated task description
            task_body = "\n".join(task_text.split("\n")[1:]).strip()
            if task_body:
                # Truncate if too long
                if len(task_body) > 500:
                    task_body = task_body[:497] + "..."
                entry += f"{task_body}\n"

        # Append to existing context or create new file
        if context_path.exists():
            existing = context_path.read_text()
            context_path.write_text(existing + entry)
        else:
            # Create new context file with header
            header = f"# Group Context: {target_group}\n\n"
            header += "_This file tracks completed tasks in this group for context sharing._\n"
            context_path.write_text(header + entry)

        if log_callback:
            log_callback(
                "group_context_accumulated",
                group=target_group,
                task_title=task_title,
                context_path=str(context_path),
                extracted=extracted_context is not None,
            )
        return True

    def extract_context_summary(
        self,
        task_text: str,
        git_diff: str,
        load_prompt_callback: Callable[[str], str],
        run_agent_callback: Callable[..., str],
        log_callback: Callable[..., None] | None = None,
    ) -> dict | None:
        """Extract key decisions and patterns from completed task using LLM.

        Calls the sanity role agent with a context extraction prompt to
        analyze the task and its changes. Returns structured context for
        sharing with subsequent tasks.

        Args:
            task_text: The task description that was completed.
            git_diff: The git diff showing the changes made.
            load_prompt_callback: Callback to load prompt templates.
            run_agent_callback: Callback to invoke the LLM agent.
            log_callback: Optional callback for logging events.

        Returns:
            Dict with 'summary' and 'key_decisions' keys, or None if
            extraction fails or returns empty.
        """
        try:
            # Load and fill the prompt template
            prompt = load_prompt_callback("context_extraction_prompt.md")
            prompt = prompt.replace("{{TASK_TEXT}}", task_text)

            # Truncate diff if too large to avoid overwhelming the model
            max_diff_chars = 10000
            if len(git_diff) > max_diff_chars:
                truncated_diff = git_diff[:max_diff_chars] + "\n... (truncated)"
            else:
                truncated_diff = git_diff
            prompt = prompt.replace("{{GIT_DIFF}}", truncated_diff)

            # Call the sanity role (fast, cheap model) for extraction
            response = run_agent_callback(
                prompt,
                role="sanity",
                output_schema="context_extraction",
            )

            # Parse the JSON response
            json_match = re.search(r"\{[^{}]*\"summary\"[^{}]*\}", response, re.DOTALL)
            if not json_match:
                # Try finding a larger JSON block
                json_match = re.search(r"\{.*?\"key_decisions\".*?\}", response, re.DOTALL)

            if json_match:
                try:
                    result = json.loads(json_match.group())
                    if result.get("summary") or result.get("key_decisions"):
                        if log_callback:
                            log_callback(
                                "context_extracted",
                                summary=result.get("summary", ""),
                                num_decisions=len(result.get("key_decisions", [])),
                            )
                        return result
                except json.JSONDecodeError:
                    pass

            if log_callback:
                log_callback("context_extraction_failed", reason="no_valid_json")
            return None

        except Exception as e:
            if log_callback:
                log_callback("context_extraction_failed", reason=str(e))
            return None
