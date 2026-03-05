"""
Tasklist management for the millstone orchestrator.

This module contains the TasklistManager class which handles task parsing
and management from tasklist files. The Orchestrator class holds an instance
and delegates via thin wrapper methods.
"""

import hashlib
import re
from pathlib import Path


class TasklistManager:
    """Manages tasklist tasks: parsing, extraction, and mutation.

    This class handles all operations related to tasklist markdown files,
    including:
    - Task detection and extraction
    - Metadata parsing (risk, context files, groups)
    - Task completion marking
    - Tasklist compaction coordination
    - Task analysis and dependency detection
    """

    def __init__(
        self,
        repo_dir: Path,
        tasklist: str = "docs/tasklist.md",
        compact_threshold: int = 20,
    ):
        """Initialize the TasklistManager.

        Args:
            repo_dir: Path to the repository root.
            tasklist: Path to the tasklist file relative to repo_dir.
            compact_threshold: Number of completed tasks before triggering compaction.
        """
        self.repo_dir = repo_dir
        self.tasklist = tasklist
        self.compact_threshold = compact_threshold
        self.completed_task_count: int = 0

    def _tasklist_path(self) -> Path:
        """Return the full path to the tasklist file."""
        return self.repo_dir / self.tasklist

    def has_remaining_tasks(self) -> bool:
        """Check if there are unchecked tasks in the tasklist.

        Returns:
            True if there's at least one `- [ ]` task remaining.
        """
        tasklist_path = self._tasklist_path()
        if not tasklist_path.exists():
            return False
        content = tasklist_path.read_text()
        return bool(re.search(r"^- \[ \]", content, re.MULTILINE))

    def extract_current_task_title(self) -> str:
        """Extract the title of the first unchecked task from the tasklist.

        Looks for patterns like:
        - `- [ ] **Title**: Description...` -> "Title"
        - `- [ ] **Title**` -> "Title"
        - `- [ ] Title` -> "Title" (first ~50 chars)

        Returns:
            Task title string, or empty string if no unchecked task found.
        """
        tasklist_path = self._tasklist_path()
        if not tasklist_path.exists():
            return ""

        content = tasklist_path.read_text()

        # Find the first unchecked task line
        match = re.search(r"^- \[ \] (.+)$", content, re.MULTILINE)
        if not match:
            return ""

        task_text = match.group(1)

        # Try to extract bold title: **Title**: description or **Title**
        bold_match = re.match(r"\*\*([^*]+)\*\*", task_text)
        if bold_match:
            return bold_match.group(1).strip()

        # Fall back to first ~50 chars of task text
        task_text = task_text.strip()
        if len(task_text) > 50:
            return task_text[:47] + "..."
        return task_text

    def extract_current_task_risk(self) -> str | None:
        """Extract the risk level of the first unchecked task from the tasklist.

        Looks for '- Risk: low|medium|high' in task metadata.

        Returns:
            Risk level string ('low', 'medium', or 'high'), or None if not found.
        """
        tasklist_path = self._tasklist_path()
        if not tasklist_path.exists():
            return None

        content = tasklist_path.read_text()

        # Find the first unchecked task block (task line plus indented metadata)
        match = re.search(r"^- \[ \] (.+(?:\n(?:  .+))*)", content, re.MULTILINE)
        if not match:
            return None

        task_text = match.group(1)
        metadata = self._parse_task_metadata(task_text)
        return metadata.get("risk")

    def extract_current_task_metadata(self) -> dict:
        """Return the full parsed metadata dict for the first unchecked task.

        Operates on the complete raw task block (title line plus indented metadata
        lines), so all fields including ``task_id`` are accessible.

        Returns:
            Metadata dict from ``_parse_task_metadata``, or an empty dict if no
            unchecked task exists.
        """
        tasklist_path = self._tasklist_path()
        if not tasklist_path.exists():
            return {}

        content = tasklist_path.read_text()
        match = re.search(r"^- \[ \] (.+(?:\n(?:  .+))*)", content, re.MULTILINE)
        if not match:
            return {}

        return self._parse_task_metadata(match.group(1))

    def extract_current_task_context_file(self) -> str | None:
        """Extract the context file path for the first unchecked task from the tasklist.

        Looks for HTML comment annotations like: <!-- context: .millstone/context/deprecation.md -->

        Returns:
            Context file path string, or None if not specified.
        """
        tasklist_path = self._tasklist_path()
        if not tasklist_path.exists():
            return None

        content = tasklist_path.read_text()

        # Find the first unchecked task block (task line plus indented metadata)
        match = re.search(r"^- \[ \] (.+(?:\n(?:  .+))*)", content, re.MULTILINE)
        if not match:
            return None

        task_text = match.group(1)
        metadata = self._parse_task_metadata(task_text)
        return metadata.get("context_file")

    def extract_current_task_group(self) -> str | None:
        """Extract the group name for the first unchecked task from the tasklist.

        Looks for `## Group: <name>` section headers. Tasks inherit the group from
        the most recent Group header before them in the file.

        Returns:
            Group name string, or None if task is not in a group.
        """
        tasklist_path = self._tasklist_path()
        if not tasklist_path.exists():
            return None

        content = tasklist_path.read_text()

        # Find the position of the first unchecked task
        task_match = re.search(r"^- \[ \]", content, re.MULTILINE)
        if not task_match:
            return None

        task_pos = task_match.start()

        # Find all ## Group: <name> headers before the task
        # The group header pattern: ## Group: <name> (may have trailing whitespace)
        group_pattern = r"^## Group:\s*(.+?)\s*$"
        current_group = None

        for match in re.finditer(group_pattern, content, re.MULTILINE):
            # Only consider groups that appear before the task
            if match.start() < task_pos:
                current_group = match.group(1).strip()
            else:
                # Stop once we've passed the task position
                break

        return current_group

    def get_task_context_file_content(self, log_callback=None) -> str | None:
        """Get the content of the context file for the current task, if specified.

        Reads the context file referenced by the task's <!-- context: path --> annotation.
        The path is relative to the repository root.

        Args:
            log_callback: Optional callback function for logging events.
                Should accept (event: str, **data) signature.

        Returns:
            Content of the context file, or None if no context file is specified
            or the file doesn't exist.
        """
        context_file_path = self.extract_current_task_context_file()
        if not context_file_path:
            return None

        # Resolve path relative to repo root
        full_path = self.repo_dir / context_file_path
        if not full_path.exists():
            if log_callback:
                log_callback(
                    "context_file_not_found",
                    path=context_file_path,
                    resolved=str(full_path),
                )
            return None

        try:
            content = full_path.read_text()
            if log_callback:
                log_callback(
                    "context_file_loaded",
                    path=context_file_path,
                    length=str(len(content)),
                )
            return content
        except OSError as e:
            if log_callback:
                log_callback(
                    "context_file_read_error",
                    path=context_file_path,
                    error=str(e),
                )
            return None

    def count_completed_tasks(self) -> int:
        """Count completed tasks in the tasklist.

        Returns:
            Number of `- [x]` entries in the tasklist, or 0 if file doesn't exist.
        """
        tasklist_path = self._tasklist_path()
        if not tasklist_path.exists():
            return 0
        content = tasklist_path.read_text()
        return len(re.findall(r"^- \[x\]", content, re.MULTILINE | re.IGNORECASE))

    def mark_task_complete(self, log_callback=None) -> bool:
        """Mark the first unchecked task as complete in the tasklist.

        Replaces the first occurrence of `- [ ]` with `- [x]`.

        Args:
            log_callback: Optional callback function for logging events.
                Should accept (event: str, **data) signature.

        Returns:
            True if a task was marked complete, False if no unchecked tasks found.
        """
        tasklist_path = self._tasklist_path()
        if not tasklist_path.exists():
            return False

        content = tasklist_path.read_text()

        # Replace the first unchecked task with a checked one
        new_content, count = re.subn(
            r"^- \[ \]",
            "- [x]",
            content,
            count=1,
            flags=re.MULTILINE,
        )

        if count == 0:
            return False

        tasklist_path.write_text(new_content)
        if log_callback:
            log_callback(
                "task_marked_complete",
                tasklist=self.tasklist,
            )
        return True

    def should_compact(self) -> bool:
        """Check if tasklist compaction should be triggered.

        Returns:
            True if completed task count >= compact_threshold and threshold > 0.
        """
        if self.compact_threshold <= 0:
            return False
        return self.completed_task_count >= self.compact_threshold

    def _extract_unchecked_tasks(self, content: str) -> list[str]:
        """Extract all unchecked task lines from tasklist content.

        Args:
            content: The tasklist file content.

        Returns:
            List of unchecked task lines (the full line text after `- [ ] `).
        """
        tasks = []
        for line in content.split("\n"):
            match = re.match(r"^- \[ \] (.+)$", line)
            if match:
                tasks.append(match.group(1))
        return tasks

    def _extract_tasks(self, content: str) -> list[tuple[bool, str]]:
        """Extract all tasks from tasklist content.

        Returns:
            List of tuples: (checked, task_text).
        """
        task_pattern = r"^- \[([ x])\] (.+(?:\n(?:  .+))*)"
        matches = re.findall(task_pattern, content, re.MULTILINE | re.IGNORECASE)
        tasks = []
        for checked, task_text in matches:
            tasks.append((checked.lower() == "x", task_text.strip()))
        return tasks

    def validate_single_task_completion(
        self,
        original_content: str,
        new_content: str,
    ) -> tuple[bool, str]:
        """Validate that only the first unchecked task was marked complete.

        Returns:
            Tuple of (is_valid, reason). Reason is empty when valid.
        """
        original_tasks = self._extract_tasks(original_content)
        new_tasks = self._extract_tasks(new_content)

        if not original_tasks or not new_tasks:
            return True, ""

        if len(new_tasks) < len(original_tasks):
            return (
                False,
                f"Task count decreased: {len(original_tasks)} -> {len(new_tasks)}. Deleting tasks is not allowed.",
            )

        checkoffs = []
        for idx, ((orig_checked, _), (new_checked, _)) in enumerate(
            zip(original_tasks, new_tasks, strict=False)
        ):
            if orig_checked and not new_checked:
                return (
                    False,
                    f"Task {idx + 1} was unchecked after completion",
                )
            if not orig_checked and new_checked:
                checkoffs.append(idx)

        if not checkoffs:
            return True, ""

        if len(checkoffs) > 1:
            return (
                False,
                f"Multiple tasks were marked complete ({len(checkoffs)}).",
            )

        first_unchecked = next(
            (i for i, (checked, _) in enumerate(original_tasks) if not checked),
            None,
        )
        if first_unchecked is None:
            return False, "No unchecked tasks remained, but a task was marked complete."

        if checkoffs[0] != first_unchecked:
            return (
                False,
                "A later task was marked complete before the first unchecked task.",
            )

        return True, ""

    def verify_compaction(
        self,
        original_content: str,
        new_content: str,
        original_unchecked: list[str],
    ) -> tuple[bool, str]:
        """Verify compaction sanity checks.

        Checks:
        1. All unchecked tasks are still present
        2. File is shorter than before
        3. No unchecked tasks were modified

        Args:
            original_content: Content before compaction.
            new_content: Content after compaction.
            original_unchecked: List of unchecked task texts from original.

        Returns:
            Tuple of (success, error_message). If success is True, error_message is empty.
        """
        # Check 1 & 3: All unchecked tasks still present and unmodified
        new_unchecked = self._extract_unchecked_tasks(new_content)

        if len(new_unchecked) != len(original_unchecked):
            return (
                False,
                f"Unchecked task count mismatch: {len(original_unchecked)} before, {len(new_unchecked)} after",
            )

        for i, (orig, new) in enumerate(zip(original_unchecked, new_unchecked, strict=False)):
            if orig != new:
                return (
                    False,
                    f"Unchecked task {i + 1} was modified:\n  Before: {orig}\n  After:  {new}",
                )

        # Check 2: File is shorter than before
        if len(new_content) >= len(original_content):
            return (
                False,
                f"Compaction did not reduce file size: {len(original_content)} -> {len(new_content)} bytes",
            )

        return (True, "")

    def run_compaction(
        self,
        run_agent_callback,
        get_prompt_callback,
        log_callback=None,
    ) -> bool:
        """Run tasklist compaction step.

        Invokes an agent to compact the tasklist by summarizing completed tasks
        while preserving unchecked tasks. After compaction, runs sanity checks
        to verify all unchecked tasks are preserved and the file is shorter.

        Args:
            run_agent_callback: Callback to invoke the agent with a prompt.
                Should accept (prompt: str) and return the agent output.
            get_prompt_callback: Callback to get the compaction prompt.
                Should accept no arguments and return the prompt string.
            log_callback: Optional callback function for logging events.
                Should accept (event: str, **data) signature.

        Returns:
            True if compaction succeeded, False if it failed and was rolled back.
        """
        print()
        print("=== Compacting Tasklist ===")
        print(
            f"Completed tasks ({self.completed_task_count}) >= threshold ({self.compact_threshold})"
        )
        print("Running compaction to reduce token usage...")

        tasklist_path = self._tasklist_path()
        original_content = tasklist_path.read_text()
        original_unchecked = self._extract_unchecked_tasks(original_content)

        if log_callback:
            log_callback(
                "compaction_started",
                completed_count=str(self.completed_task_count),
                threshold=str(self.compact_threshold),
                unchecked_task_count=str(len(original_unchecked)),
            )

        compact_prompt = get_prompt_callback()
        output = run_agent_callback(compact_prompt)

        # Read the compacted file
        new_content = tasklist_path.read_text()

        # Verify compaction sanity checks
        success, error_msg = self.verify_compaction(
            original_content, new_content, original_unchecked
        )

        if not success:
            print(f"Compaction sanity check FAILED: {error_msg}")
            print("Restoring original tasklist...")

            # Restore original
            tasklist_path.write_text(original_content)

            if log_callback:
                log_callback(
                    "compaction_failed",
                    error=error_msg,
                    output=output[:2000],
                )
            print("Original tasklist restored.")
            print()
            return False

        # Update completed task count after successful compaction
        new_count = self.count_completed_tasks()

        if log_callback:
            log_callback(
                "compaction_completed",
                old_count=str(self.completed_task_count),
                new_count=str(new_count),
                old_size=str(len(original_content)),
                new_size=str(len(new_content)),
                output=output[:2000],
            )

        print("Compaction sanity check: OK")
        print(f"  File size: {len(original_content)} -> {len(new_content)} bytes")
        print(f"  Completed tasks: {self.completed_task_count} -> {new_count}")
        print(f"  Unchecked tasks: {len(original_unchecked)} (preserved)")
        self.completed_task_count = new_count
        print()
        return True

    def _parse_task_metadata(self, task_text: str) -> dict:
        """Parse metadata from a task description.

        Extracts structured metadata from task text that may include:
        - Est. LoC: estimated lines of code
        - Tests: test file(s) to add/run
        - Risk: low/medium/high
        - Design Ref: design linkage (`design_ref` or `design-ref`)
        - Opportunity Ref: opportunity linkage (`opportunity_ref` or `opportunity-ref`)
        - Criteria/Success/Acceptance: success criteria
        - HTML comment context annotation: <!-- context: path/to/file.md -->

        Args:
            task_text: The full task text including any metadata lines.

        Returns:
            Dict with extracted metadata:
            - title: Task title (bold text)
            - description: Main description text
            - est_loc: Estimated lines of code (or None if not specified)
            - tests: Test specification (or None)
            - risk: Risk level (low/medium/high, or None if not specified)
            - design_ref: Design reference slug (or None)
            - opportunity_ref: Opportunity reference slug (or None)
            - criteria: Success criteria (or None)
            - context_file: Path to context file (or None if not specified)
            - raw: The original task text
        """
        result: dict[str, int | str | None] = {
            "title": "",
            "description": "",
            "task_id": None,
            "est_loc": None,
            "tests": None,
            "risk": None,
            "design_ref": None,
            "opportunity_ref": None,
            "criteria": None,
            "context": None,
            "context_file": None,
            "raw": task_text,
        }

        lines = task_text.strip().split("\n")
        if not lines:
            return result

        # First line contains the title and main description
        first_line = lines[0]

        # Extract bold title: **Title**: description
        title_match = re.match(r"\*\*([^*]+)\*\*(?::\s*(.*))?", first_line)
        if title_match:
            result["title"] = title_match.group(1).strip()
            result["description"] = (title_match.group(2) or "").strip()
        else:
            # No bold title, use first line as description
            result["description"] = first_line.strip()

        # Parse HTML comment id annotation first so explicit "- ID:" metadata
        # (parsed below) remains the authoritative source when both are present.
        id_comment_match = re.search(r"<!--\s*id:\s*([^\s>]+)\s*-->", task_text)
        if id_comment_match:
            candidate = id_comment_match.group(1).strip()
            if re.fullmatch(r"[a-z0-9_-]{1,40}", candidate):
                result["task_id"] = candidate

        # Parse metadata from indented lines
        for line in lines[1:]:
            line = line.strip()
            if not line.startswith("-"):
                continue
            line = line[1:].strip()  # Remove leading dash

            # ID: my-task
            id_match = re.match(r"ID:\s*(\S+)", line, re.IGNORECASE)
            if id_match:
                candidate = id_match.group(1).strip()
                if re.fullmatch(r"[a-z0-9_-]{1,40}", candidate):
                    result["task_id"] = candidate
                continue

            # Est. LoC: 150
            loc_match = re.match(r"Est\.?\s*LoC:\s*(\d+)", line, re.IGNORECASE)
            if loc_match:
                result["est_loc"] = int(loc_match.group(1))
                continue

            # Tests: test_foo.py
            tests_match = re.match(r"Tests?:\s*(.+)", line, re.IGNORECASE)
            if tests_match:
                result["tests"] = tests_match.group(1).strip()
                continue

            # Risk: low/medium/high
            risk_match = re.match(r"Risk:\s*(low|medium|high)", line, re.IGNORECASE)
            if risk_match:
                result["risk"] = risk_match.group(1).lower()
                continue

            # design_ref/design-ref: ...
            design_ref_match = re.match(r"design[-_]ref:\s*(.+)", line, re.IGNORECASE)
            if design_ref_match:
                result["design_ref"] = design_ref_match.group(1).strip()
                continue

            # opportunity_ref/opportunity-ref: ...
            opportunity_ref_match = re.match(r"opportunity[-_]ref:\s*(.+)", line, re.IGNORECASE)
            if opportunity_ref_match:
                result["opportunity_ref"] = opportunity_ref_match.group(1).strip()
                continue

            # Success/Criteria/Acceptance: ...
            criteria_match = re.match(
                r"(?:Success|Criteria|Acceptance):\s*(.+)", line, re.IGNORECASE
            )
            if criteria_match:
                result["criteria"] = criteria_match.group(1).strip()
                continue

            # Context: ...
            context_field_match = re.match(r"Context:\s*(.+)", line, re.IGNORECASE)
            if context_field_match:
                result["context"] = context_field_match.group(1).strip()
                continue

        # Parse HTML comment context annotation: <!-- context: path/to/file.md -->
        # This can appear anywhere in the task text (not just indented lines with -)
        context_match = re.search(r"<!--\s*context:\s*([^\s>]+)\s*-->", task_text)
        if context_match:
            result["context_file"] = context_match.group(1).strip()

        return result

    def generate_task_id(self, task_text: str) -> str:
        """Generate a stable task ID from normalized task text.

        Uses sha256(stripped, whitespace-collapsed text)[:8].
        """
        normalized = re.sub(r"\s+", " ", task_text.strip())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]

    def extract_all_task_ids(self) -> list[dict]:
        """Return all tasks with stable IDs.

        Returns:
            List of dicts: {task_id, checked, title, raw_text, index}.

        Notes:
            This is a pure function: it does NOT persist any taskmap.
            Collision handling appends -1, -2 suffixes to keep IDs unique.
        """
        tasklist_path = self._tasklist_path()
        if not tasklist_path.exists():
            return []

        content = tasklist_path.read_text()
        tasks = self._extract_tasks(content)

        used: set[str] = set()
        results: list[dict] = []
        for index, (checked, raw_text) in enumerate(tasks):
            metadata = self._parse_task_metadata(raw_text)
            base_id = metadata.get("task_id") or self.generate_task_id(raw_text)

            # Ensure uniqueness with deterministic suffixes.
            task_id = base_id
            if task_id in used:
                n = 1
                while True:
                    suffix = f"-{n}"
                    if len(base_id) + len(suffix) > 40:
                        task_id = base_id[: 40 - len(suffix)] + suffix
                    else:
                        task_id = base_id + suffix
                    if task_id not in used:
                        break
                    n += 1

            used.add(task_id)
            results.append(
                {
                    "task_id": task_id,
                    "checked": checked,
                    "title": metadata.get("title") or "",
                    "raw_text": raw_text,
                    "index": index,
                }
            )

        return results

    def mark_task_complete_by_id(self, task_id: str, taskmap: dict, log_callback=None) -> bool:
        """Mark a specific task complete by its ID.

        Finds the task by explicit ID metadata or via taskmap lookup (index-based).
        Returns True if the task was marked complete, False if not found or already complete.
        """
        tasklist_path = self._tasklist_path()
        if not tasklist_path.exists():
            return False

        content = tasklist_path.read_text()
        task_pattern = r"^- \[([ x])\] (.+(?:\n(?:  .+))*)"
        matches = list(re.finditer(task_pattern, content, re.MULTILINE | re.IGNORECASE))
        if not matches:
            return False

        target_index = self._resolve_task_index_by_id(
            task_id=task_id, taskmap=taskmap, matches=matches
        )
        if target_index is None:
            return False

        m = matches[target_index]
        checked = m.group(1).lower() == "x"
        if checked:
            return False

        start = m.start()
        # "- [ ]" / "- [x]" check char is at offset 3 from the start.
        new_content = content[: start + 3] + "x" + content[start + 4 :]
        tasklist_path.write_text(new_content)
        if log_callback:
            log_callback("task_marked_complete_by_id", tasklist=self.tasklist, task_id=task_id)
        return True

    def task_completion_by_id(self, task_id: str, taskmap: dict) -> bool | None:
        """Return completion state for task_id.

        Returns:
            - True if task exists and is checked.
            - False if task exists and is unchecked.
            - None if task could not be resolved.
        """
        tasklist_path = self._tasklist_path()
        if not tasklist_path.exists():
            return None

        content = tasklist_path.read_text()
        task_pattern = r"^- \[([ x])\] (.+(?:\n(?:  .+))*)"
        matches = list(re.finditer(task_pattern, content, re.MULTILINE | re.IGNORECASE))
        if not matches:
            return None

        target_index = self._resolve_task_index_by_id(
            task_id=task_id, taskmap=taskmap, matches=matches
        )
        if target_index is None:
            return None

        return matches[target_index].group(1).lower() == "x"

    def _resolve_task_index_by_id(
        self, task_id: str, taskmap: dict, matches: list[re.Match]
    ) -> int | None:
        """Resolve task index from taskmap first, then by metadata/id hash scan."""
        mapped = taskmap.get(task_id)
        if isinstance(mapped, int) and 0 <= mapped < len(matches):
            return mapped
        if (
            isinstance(mapped, dict)
            and isinstance(mapped.get("index"), int)
            and 0 <= mapped["index"] < len(matches)
        ):
            return mapped["index"]

        for i, m in enumerate(matches):
            raw_text = m.group(2).strip()
            metadata = self._parse_task_metadata(raw_text)
            candidate = metadata.get("task_id") or self.generate_task_id(raw_text)
            if candidate == task_id:
                return i

        return None

    def extract_all_task_groups(self) -> dict[int, str | None]:
        """Return mapping of task index -> group name based on ## Group: headers."""
        tasklist_path = self._tasklist_path()
        if not tasklist_path.exists():
            return {}

        content = tasklist_path.read_text()
        group_pattern = re.compile(r"^## Group:\s*(.+?)\s*$", re.MULTILINE)
        task_pattern = re.compile(r"^- \[[ x]\] ", re.IGNORECASE)

        current_group: str | None = None
        task_index = 0
        mapping: dict[int, str | None] = {}
        for line in content.splitlines():
            gm = group_pattern.match(line)
            if gm:
                current_group = gm.group(1).strip() or None
                continue
            if task_pattern.match(line):
                mapping[task_index] = current_group
                task_index += 1

        return mapping

    def analyze_tasklist(
        self,
        estimate_time_callback=None,
        log_callback=None,
        print_report: bool = True,
    ) -> dict:
        """Analyze the tasklist and report task statistics.

        Scans the tasklist to provide:
        - Task count (pending, completed, total)
        - Estimated complexity per task (simple/medium/complex)
        - Suggested task ordering based on dependencies
        - Potential dependencies between tasks

        Complexity is estimated based on:
        - Number of file references in task description
        - Presence of keywords like "refactor", "migrate", "redesign"
        - Estimated lines of code (if specified with Est. LoC:)

        Args:
            estimate_time_callback: Optional callback to estimate remaining time.
                Should accept (pending_tasks: list[dict]) and return a time estimate dict.
            log_callback: Optional callback function for logging events.
                Should accept (event: str, **data) signature.

        Returns:
            Dict containing analysis results with keys:
            - pending_count: Number of unchecked tasks
            - completed_count: Number of checked tasks
            - total_count: Total tasks
            - tasks: List of task analysis dicts
            - dependencies: List of detected dependencies
            - suggested_order: List of task indices in suggested order
        """
        tasklist_path = self._tasklist_path()
        if not tasklist_path.exists():
            print(f"Tasklist not found: {self.tasklist}")
            return {
                "pending_count": 0,
                "completed_count": 0,
                "total_count": 0,
                "tasks": [],
                "dependencies": [],
                "suggested_order": [],
            }

        content = tasklist_path.read_text()

        # Extract all tasks (both checked and unchecked)
        # Pattern matches task line and any indented continuation lines
        task_pattern = r"^- \[([ x])\] (.+(?:\n(?:  .+))*)"
        matches = re.findall(task_pattern, content, re.MULTILINE | re.IGNORECASE)

        tasks = []
        for i, (checked, task_text) in enumerate(matches):
            is_completed = checked.lower() == "x"
            task_info = self._analyze_task(i, task_text.strip(), is_completed)
            tasks.append(task_info)

        # Count tasks
        pending_count = sum(1 for t in tasks if not t["completed"])
        completed_count = sum(1 for t in tasks if t["completed"])

        # Detect dependencies between pending tasks
        pending_tasks = [t for t in tasks if not t["completed"]]
        dependencies = self._detect_dependencies(pending_tasks)

        # Suggest task ordering based on dependencies and complexity
        suggested_order = self._suggest_task_order(pending_tasks, dependencies)

        # Estimate remaining time for pending tasks
        time_estimate = None
        if estimate_time_callback:
            time_estimate = estimate_time_callback(pending_tasks)

        result = {
            "pending_count": pending_count,
            "completed_count": completed_count,
            "total_count": len(tasks),
            "tasks": tasks,
            "dependencies": dependencies,
            "suggested_order": suggested_order,
            "time_estimate": time_estimate,
        }

        if print_report:
            # Print the report
            self._print_tasklist_analysis(result)

        return result

    def _analyze_task(self, index: int, task_text: str, completed: bool) -> dict:
        """Analyze a single task for complexity and file references.

        Args:
            index: Task index in the tasklist (0-based).
            task_text: The task description text.
            completed: Whether the task is marked complete.

        Returns:
            Dict with task analysis:
            - index: Task index
            - text: First line of task text (title/summary)
            - completed: Whether task is complete
            - complexity: simple/medium/complex
            - file_refs: List of detected file references
            - keywords: List of complexity keywords found
            - est_loc: Estimated LoC if specified
            - group: Group name if task is in a group
        """
        # Parse metadata using existing method
        metadata = self._parse_task_metadata(task_text)

        # Extract first line as summary
        first_line = task_text.split("\n")[0].strip()

        # Detect file references (paths with extensions or directory patterns)
        file_pattern = r'[`"\']?([a-zA-Z0-9_/.-]+\.[a-zA-Z0-9]+)[`"\']?'
        dir_pattern = r'[`"\']?([a-zA-Z0-9_/-]+/)[`"\']?'
        file_refs = list(set(re.findall(file_pattern, task_text)))
        dir_refs = list(set(re.findall(dir_pattern, task_text)))
        all_refs = file_refs + [d for d in dir_refs if d not in file_refs]

        # Detect complexity keywords
        complexity_keywords = {
            "complex": ["refactor", "migrate", "redesign", "rewrite", "overhaul", "rearchitect"],
            "medium": ["add", "implement", "create", "update", "modify", "extend", "integrate"],
            "simple": ["fix", "rename", "remove", "delete", "typo", "comment", "document"],
        }

        found_keywords = []
        task_lower = task_text.lower()
        for level, keywords in complexity_keywords.items():
            for kw in keywords:
                if kw in task_lower:
                    found_keywords.append((kw, level))

        # Calculate referenced code size (only for pending tasks to avoid overhead)
        ref_loc = None
        if not completed and all_refs:
            ref_loc = self._get_referenced_code_size(all_refs)

        # Determine complexity
        complexity = self._estimate_complexity(
            file_refs=all_refs,
            keywords=found_keywords,
            est_loc=metadata.get("est_loc"),
            ref_loc=ref_loc,
        )

        return {
            "index": index,
            "text": first_line[:100] + ("..." if len(first_line) > 100 else ""),
            "full_text": task_text,
            "completed": completed,
            "complexity": complexity,
            "file_refs": all_refs[:10],  # Limit to 10 refs for display
            "keywords": [kw for kw, _ in found_keywords[:5]],  # Limit to 5 keywords
            "est_loc": metadata.get("est_loc"),
            "ref_loc": ref_loc,  # Actual lines in referenced files
            "risk": metadata.get("risk"),
            "title": metadata.get("title") or first_line[:50],
        }

    def _get_referenced_code_size(self, file_refs: list[str]) -> int:
        """Calculate total lines of code in referenced files.

        Looks up each file reference in the repository and counts lines.
        Only counts existing files that appear to be code (not binary, not too large).

        Args:
            file_refs: List of file paths referenced in the task.

        Returns:
            Total lines of code across all existing referenced files.
        """
        total_lines = 0
        max_file_size = 100_000  # Skip files larger than 100KB (likely not code)

        for ref in file_refs:
            # Try to find the file in the repo
            file_path = self.repo_dir / ref

            # Also try common prefixes if not found directly
            if not file_path.exists():
                # Try without leading slash or with common src prefixes
                alt_paths = [
                    self.repo_dir / ref.lstrip("/"),
                    self.repo_dir / "src" / ref,
                    self.repo_dir / "lib" / ref,
                ]
                for alt in alt_paths:
                    if alt.exists():
                        file_path = alt
                        break

            if not file_path.exists() or not file_path.is_file():
                continue

            try:
                # Skip large files (likely binary or generated)
                if file_path.stat().st_size > max_file_size:
                    continue

                # Count lines
                content = file_path.read_text(errors="ignore")
                lines = len(content.splitlines())
                total_lines += lines
            except (OSError, UnicodeDecodeError):
                # Skip files we can't read
                continue

        return total_lines

    def _estimate_complexity(
        self,
        file_refs: list[str],
        keywords: list[tuple[str, str]],
        est_loc: int | None,
        ref_loc: int | None = None,
    ) -> str:
        """Estimate task complexity based on various factors.

        Args:
            file_refs: List of file/directory references found.
            keywords: List of (keyword, level) tuples found.
            est_loc: Estimated lines of code from task metadata, or None.
            ref_loc: Actual lines of code in referenced files, or None.

        Returns:
            "simple", "medium", or "complex"
        """
        score = 0

        # File references contribute to complexity
        ref_count = len(file_refs)
        if ref_count >= 5:
            score += 2
        elif ref_count >= 2:
            score += 1

        # Keywords contribute based on their level
        for _, level in keywords:
            if level == "complex":
                score += 2
            elif level == "medium":
                score += 1
            # simple keywords don't add to score

        # Estimated LoC contributes (explicit metadata takes priority)
        if est_loc is not None:
            if est_loc >= 200:
                score += 2
            elif est_loc >= 50:
                score += 1
        elif ref_loc is not None and ref_loc > 0:
            # Use referenced code size as fallback when no explicit estimate
            # Large files suggest more complex changes
            if ref_loc >= 500:
                score += 2
            elif ref_loc >= 150:
                score += 1

        # Map score to complexity level
        if score >= 4:
            return "complex"
        elif score >= 2:
            return "medium"
        else:
            return "simple"

    def _detect_dependencies(self, tasks: list[dict]) -> list[dict]:
        """Detect potential dependencies between tasks.

        Looks for:
        - Shared file references (tasks touching same files)
        - Explicit ordering hints ("after", "before", "depends on")
        - Component/module overlap

        Args:
            tasks: List of task analysis dicts (pending tasks only).

        Returns:
            List of dependency dicts with:
            - from_index: Index of dependent task
            - to_index: Index of task it depends on
            - reason: Why the dependency was detected
        """
        dependencies = []

        for i, task1 in enumerate(tasks):
            for j, task2 in enumerate(tasks):
                if i >= j:
                    continue  # Only check each pair once

                # Check for shared file references
                refs1 = set(task1.get("file_refs", []))
                refs2 = set(task2.get("file_refs", []))
                shared_refs = refs1 & refs2

                if shared_refs:
                    # Tasks share file references - may have dependency
                    dependencies.append(
                        {
                            "from_index": task1["index"],
                            "to_index": task2["index"],
                            "reason": f"shared files: {', '.join(list(shared_refs)[:3])}",
                            "type": "file_overlap",
                        }
                    )

                # Check for explicit ordering hints in task text
                task1_text = task1.get("full_text", "").lower()
                task2_title = task2.get("title", "").lower()

                # Look for "after X" or "depends on X" patterns
                if task2_title and (
                    f"after {task2_title}" in task1_text
                    or f"depends on {task2_title}" in task1_text
                    or f"requires {task2_title}" in task1_text
                ):
                    dependencies.append(
                        {
                            "from_index": task1["index"],
                            "to_index": task2["index"],
                            "reason": "explicit dependency",
                            "type": "explicit",
                        }
                    )

        return dependencies

    def _suggest_task_order(
        self,
        tasks: list[dict],
        dependencies: list[dict],
    ) -> list[int]:
        """Suggest optimal task ordering based on dependencies and complexity.

        Strategy:
        1. Respect explicit dependencies (topological sort)
        2. Within dependency groups, order by complexity (simple first)
        3. Prefer independent tasks that unblock others

        Args:
            tasks: List of task analysis dicts.
            dependencies: List of dependency dicts.

        Returns:
            List of task indices in suggested order.
        """
        if not tasks:
            return []

        # Build dependency graph
        # dep_graph[i] = set of task indices that task i depends on
        index_to_pos = {t["index"]: i for i, t in enumerate(tasks)}
        dep_graph: dict[int, set[int]] = {t["index"]: set() for t in tasks}

        for dep in dependencies:
            from_idx = dep["from_index"]
            to_idx = dep["to_index"]
            if from_idx in dep_graph and to_idx in index_to_pos:
                dep_graph[from_idx].add(to_idx)

        # Topological sort with complexity as tiebreaker
        complexity_order = {"simple": 0, "medium": 1, "complex": 2}
        result = []
        remaining = {t["index"] for t in tasks}

        while remaining:
            # Find tasks with no unresolved dependencies
            available = [idx for idx in remaining if not (dep_graph[idx] & remaining)]

            if not available:
                # Circular dependency - just pick by complexity
                available = list(remaining)

            # Sort available by complexity (simple first)
            available.sort(
                key=lambda idx: complexity_order.get(tasks[index_to_pos[idx]]["complexity"], 1)
            )

            # Pick the simplest available task
            next_task = available[0]
            result.append(next_task)
            remaining.remove(next_task)

        return result

    def _print_tasklist_analysis(self, result: dict) -> None:
        """Print formatted tasklist analysis report.

        Args:
            result: Analysis result dict from analyze_tasklist().
        """
        print()
        print("=== Tasklist Analysis ===")
        print()

        # Summary counts
        print(
            f"Tasks: {result['total_count']} total, "
            f"{result['pending_count']} pending, "
            f"{result['completed_count']} completed"
        )
        print()

        # Pending tasks by complexity
        pending_tasks = [t for t in result["tasks"] if not t["completed"]]
        if not pending_tasks:
            print("No pending tasks.")
            return

        # Count by complexity
        complexity_counts = {"simple": 0, "medium": 0, "complex": 0}
        for task in pending_tasks:
            complexity_counts[task["complexity"]] = complexity_counts.get(task["complexity"], 0) + 1

        print("Pending tasks by complexity:")
        print(f"  Simple:  {complexity_counts['simple']}")
        print(f"  Medium:  {complexity_counts['medium']}")
        print(f"  Complex: {complexity_counts['complex']}")
        print()

        # Task details
        print("Task details:")
        print("-" * 80)
        for task in pending_tasks:
            complexity_indicator = {
                "simple": "[S]",
                "medium": "[M]",
                "complex": "[C]",
            }.get(task["complexity"], "[?]")

            # Show task with complexity and file refs
            print(f"{task['index'] + 1}. {complexity_indicator} {task['text']}")
            if task["file_refs"]:
                print(f"      Files: {', '.join(task['file_refs'][:5])}")
            if task["est_loc"]:
                print(f"      Est. LoC: {task['est_loc']}")
            if task.get("ref_loc"):
                print(f"      Ref. code size: {task['ref_loc']} lines")
            if task["keywords"]:
                print(f"      Keywords: {', '.join(task['keywords'])}")
        print()

        # Dependencies
        dependencies = result.get("dependencies", [])
        if dependencies:
            print("Potential dependencies:")
            for dep in dependencies[:10]:  # Limit to 10
                print(
                    f"  Task {dep['from_index'] + 1} -> Task {dep['to_index'] + 1}: {dep['reason']}"
                )
            if len(dependencies) > 10:
                print(f"  ... and {len(dependencies) - 10} more")
            print()

        # Suggested order
        suggested = result.get("suggested_order", [])
        if suggested and len(suggested) > 1:
            print("Suggested task order:")
            for i, task_idx in enumerate(suggested, 1):
                task = next((t for t in pending_tasks if t["index"] == task_idx), None)
                if task:
                    print(f"  {i}. Task {task_idx + 1}: {task['text'][:60]}...")
            print()

        # Progress estimation
        time_estimate = result.get("time_estimate")
        if time_estimate:
            print("Estimated remaining time:")
            print(f"  Total: {time_estimate['total_formatted']}")

            # Show breakdown by complexity
            by_complexity = time_estimate.get("by_complexity", {})
            breakdown_parts = []
            for level in ["simple", "medium", "complex"]:
                data = by_complexity.get(level, {})
                count = data.get("count", 0)
                if count > 0:
                    est_sec = data.get("estimated_seconds", 0)
                    time_str = f"{est_sec / 60:.1f}m" if est_sec >= 60 else f"{est_sec:.0f}s"
                    breakdown_parts.append(f"{level}: {count} tasks (~{time_str})")
            if breakdown_parts:
                print(f"  Breakdown: {', '.join(breakdown_parts)}")

            # Show confidence level and data source
            confidence = time_estimate.get("confidence", "low")
            historical = time_estimate.get("historical_tasks", 0)
            if historical > 0:
                print(f"  Based on {historical} historical task(s) (confidence: {confidence})")
            else:
                print("  Based on default estimates (no historical data)")
            print()

    def split_task(
        self,
        task_number: int,
        run_agent_callback,
        load_prompt_callback,
        log_callback=None,
    ) -> dict:
        """Analyze a task and suggest how to split it into subtasks.

        Invokes an agent to analyze the specified task from the tasklist
        and suggest how to break it down into smaller, more atomic subtasks
        based on file/component boundaries.

        Args:
            task_number: 1-indexed task number from the tasklist (matching
                the order shown by --analyze-tasklist).
            run_agent_callback: Callback to invoke the agent with a prompt.
                Should accept (prompt: str) and return the agent output.
            load_prompt_callback: Callback to load a prompt template.
                Should accept (name: str) and return the prompt content.
            log_callback: Optional callback function for logging events.
                Should accept (event: str, **data) signature.

        Returns:
            Dict with split analysis results:
            - success: Boolean indicating if analysis completed
            - task: The original task info dict
            - output: The agent's analysis and suggestions
        """
        from millstone.utils import progress

        progress(f"Analyzing task {task_number} for splitting...")

        # First, analyze the tasklist to get task info
        tasklist_path = self._tasklist_path()
        if not tasklist_path.exists():
            print(f"Error: Tasklist not found: {self.tasklist}")
            return {"success": False, "task": None, "output": "Tasklist not found"}

        content = tasklist_path.read_text()

        # Extract all pending tasks
        task_pattern = r"^- \[ \] (.+(?:\n(?:  .+))*)"
        matches = re.findall(task_pattern, content, re.MULTILINE)

        if not matches:
            print("Error: No pending tasks found in tasklist")
            return {"success": False, "task": None, "output": "No pending tasks found"}

        # Validate task number
        if task_number < 1 or task_number > len(matches):
            print(f"Error: Task number {task_number} out of range (1-{len(matches)})")
            return {
                "success": False,
                "task": None,
                "output": f"Task number out of range. Valid range: 1-{len(matches)}",
            }

        # Get the task text (0-indexed internally)
        task_text = matches[task_number - 1].strip()

        # Analyze this task for additional context
        task_info = self._analyze_task(task_number - 1, task_text, completed=False)

        # Load the split task prompt
        split_prompt = load_prompt_callback("split_task_prompt.md")

        # Substitute placeholders
        split_prompt = split_prompt.replace("{{TASK_NUMBER}}", str(task_number))
        split_prompt = split_prompt.replace("{{TASK_CONTENT}}", task_text)
        split_prompt = split_prompt.replace("{{COMPLEXITY}}", task_info["complexity"])
        split_prompt = split_prompt.replace(
            "{{FILE_REFS}}",
            ", ".join(task_info["file_refs"]) if task_info["file_refs"] else "None detected",
        )
        split_prompt = split_prompt.replace(
            "{{KEYWORDS}}",
            ", ".join(task_info["keywords"]) if task_info["keywords"] else "None detected",
        )

        # Print task info before invoking agent
        print()
        print("=== Task to Split ===")
        print(f"Task #{task_number}: {task_info['text']}")
        print(f"Complexity: {task_info['complexity']}")
        if task_info["file_refs"]:
            print(f"Files: {', '.join(task_info['file_refs'])}")
        if task_info["keywords"]:
            print(f"Keywords: {', '.join(task_info['keywords'])}")
        print()

        # Run the agent to analyze and suggest splits
        output = run_agent_callback(split_prompt)

        # Log the operation
        if log_callback:
            log_callback(
                "split_task",
                task_number=task_number,
                task_text=task_text[:200],
                complexity=task_info["complexity"],
                output_length=len(output),
            )

        result = {
            "success": True,
            "task": task_info,
            "output": output,
        }

        print()
        print("=== Split Analysis Complete ===")
        print("Review the suggestions above. To apply, manually update the tasklist.")

        return result
