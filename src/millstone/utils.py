"""
Utility functions for the millstone orchestrator.

This module contains standalone utility functions extracted from orchestrate.py
for better modularity. All functions are re-exported from orchestrate.py for
backward compatibility.
"""

import json
import re


def progress(msg: str) -> None:
    """Print a progress message with immediate flush.

    This ensures output is visible immediately for background monitoring,
    rather than being buffered until the process completes.
    """
    try:
        print(msg, flush=True)
    except BrokenPipeError:
        # Downstream consumer (e.g., piped pager/log collector) closed stdout.
        # Progress output is best-effort and must not crash orchestration.
        return


def is_empty_response(
    response: str | None,
    expected_schema: str | None = None,
    min_length: int | None = None,
) -> bool:
    """Check if an agent response is empty or lacks expected structure.

    Detects responses that are:
    - None or empty string
    - Whitespace-only
    - Missing expected JSON structure for known schemas
    - Below minimum length threshold (for unstructured responses only)

    Args:
        response: The agent's response string.
        expected_schema: Optional schema name to check for expected structure.
            Supported values: "sanity_check", "review_decision", "builder_completion",
            "design_review". When provided, checks that the response contains the
            expected JSON block. Schema validation takes priority over min_length -
            a valid structured response is never rejected for being too short.
        min_length: Optional minimum response length in characters. Only applies to
            unstructured responses (when expected_schema is None). Responses shorter
            than this (after stripping whitespace) are considered empty. Set to 0 or
            None to disable this check.

    Returns:
        True if the response is considered empty or malformed, False otherwise.
    """
    # Check for None or empty
    if response is None:
        return True

    # Check for whitespace-only
    stripped = response.strip()
    if not stripped:
        return True

    if stripped == "NO_TASKS_REMAIN":
        return False

    # If schema is specified, validate structure (takes priority over length check)
    # A valid structured response is never rejected for being too short
    if expected_schema is not None:
        if expected_schema == "sanity_check":
            # Expect {"status": "OK"|"HALT", ...}
            if '"status"' not in response:
                return True
            return '"OK"' not in response and '"HALT"' not in response

        elif expected_schema == "review_decision":
            # Expect {"status": "APPROVED"|"REQUEST_CHANGES", "review": "...", "summary": "...", ...}
            if '"status"' not in response:
                return True
            if '"APPROVED"' not in response and '"REQUEST_CHANGES"' not in response:
                return True
            if '"review"' not in response:
                return True
            return '"summary"' not in response

        elif expected_schema == "builder_completion":
            # Expect {"completed": true|false, ...}
            return '"completed"' not in response

        elif expected_schema == "design_review":
            # Expect {"verdict": "APPROVED"|"NEEDS_REVISION", "strengths": [...], "issues": [...], ...}
            if '"verdict"' not in response:
                return True
            if '"APPROVED"' not in response and '"NEEDS_REVISION"' not in response:
                return True
            if '"strengths"' not in response:
                return True
            return '"issues"' not in response

        elif expected_schema == "context_extraction":
            # Expect {"summary": "...", "key_decisions": [...]}
            if '"summary"' not in response:
                return True
            return '"key_decisions"' not in response

        # Unknown schema with content - not empty
        return False

    # No schema specified - apply min_length check for unstructured responses
    return bool(min_length and len(stripped) < min_length)


def extract_claude_result(output: str) -> str:
    """Extract the actual result from Claude Code's JSON wrapper.

    When Claude Code CLI is invoked with --output-format json, it returns
    a JSON wrapper object containing metadata and the actual result:

        {"type":"result","result":"actual content here",...}

    When using --json-schema for structured output, the response may have:
        {"type":"result","result":"","structured_output":{...},...}

    In this case, the actual structured response is in "structured_output",
    not "result". This function handles both cases.

    Args:
        output: Raw output from Claude Code CLI.

    Returns:
        The extracted result content if a JSON wrapper is detected,
        otherwise returns the original output unchanged. For structured
        output, returns the JSON-serialized structured_output.
    """
    if not output:
        return output

    stripped = output.strip()

    # Quick check - does it look like a JSON wrapper?
    # Check for both compact and spaced JSON formats
    if not (stripped.startswith('{"type":"result"') or stripped.startswith('{"type": "result"')):
        return output

    try:
        data = json.loads(stripped)
        if isinstance(data, dict) and data.get("type") == "result":
            # Prefer structured_output when present (used with --json-schema).
            # When Claude Code is invoked with --json-schema, it returns:
            # - A text summary in "result" (human-friendly)
            # - The actual structured data in "structured_output" (canonical)
            # We must prefer structured_output to ensure downstream schema
            # validation (is_empty_response) sees the actual JSON structure.
            structured = data.get("structured_output")
            if structured and isinstance(structured, dict):
                return json.dumps(structured)
            # Fall back to result field if no structured_output
            result = data.get("result", "")
            if result:
                return result
    except (json.JSONDecodeError, TypeError):
        pass

    return output


def summarize_output(text: str, head_chars: int = 500, tail_chars: int = 200) -> str:
    """Summarize long text by keeping head and tail portions.

    Creates a truncated version of text showing the first N characters and
    last M characters, with a marker indicating how much was omitted.

    Args:
        text: The text to summarize.
        head_chars: Number of characters to keep from the start (default: 500).
        tail_chars: Number of characters to keep from the end (default: 200).

    Returns:
        The original text if short enough, or a summarized version with
        head/tail portions and omission marker.
    """
    if not text:
        return text

    total_limit = head_chars + tail_chars
    if len(text) <= total_limit:
        return text

    omitted = len(text) - total_limit
    head = text[:head_chars]
    tail = text[-tail_chars:] if tail_chars > 0 else ""

    return f"{head}\n\n[... {omitted} chars omitted ...]\n\n{tail}"


def filter_reasoning_traces(text: str) -> str:
    """Filter Codex reasoning traces from output text.

    Codex CLI emits "thinking" blocks that contain internal reasoning.
    These blocks start with a line containing just "thinking" and end
    with a line containing just "codex". This function strips these
    blocks to reduce log verbosity.

    Args:
        text: The output text that may contain reasoning traces.

    Returns:
        The text with reasoning trace blocks removed. If no blocks
        are found, returns the original text unchanged.
    """
    if not text:
        return text

    # Pattern matches: newline + "thinking" + content + "codex" + newline
    # Using DOTALL so . matches newlines in the content
    # The pattern is non-greedy to handle multiple thinking blocks
    pattern = r'\nthinking\n.*?\ncodex\n'

    # Also handle case where thinking block is at the start of text
    start_pattern = r'^thinking\n.*?\ncodex\n'

    # First replace blocks at the start
    result = re.sub(start_pattern, '', text, flags=re.DOTALL)
    # Then replace blocks in the middle
    result = re.sub(pattern, '\n', result, flags=re.DOTALL)

    return result


def summarize_diff(diff_text: str, lines_per_file: int = 20) -> str:
    """Summarize a git diff by showing stats and truncated content.

    For 'summary' mode of log_diff_mode, this provides a compact view showing:
    - File count and total lines changed
    - List of modified files with +/- counts
    - First N lines of each file's diff

    Args:
        diff_text: The full git diff output.
        lines_per_file: Number of diff lines to show per file (default: 20).

    Returns:
        A summarized version of the diff with stats header and truncated content.
    """
    if not diff_text or not diff_text.strip():
        return "(empty diff)"

    lines = diff_text.split('\n')

    # Parse diff to extract file stats
    files: list[dict] = []
    current_file: dict | None = None
    total_added = 0
    total_removed = 0

    for line in lines:
        if line.startswith('diff --git'):
            # Start of new file diff
            if current_file:
                files.append(current_file)
            # Extract filename from "diff --git a/path b/path"
            parts = line.split(' ')
            if len(parts) >= 4:
                filename = parts[3][2:] if parts[3].startswith('b/') else parts[3]
            else:
                filename = "unknown"
            current_file = {
                'name': filename,
                'added': 0,
                'removed': 0,
                'lines': [line]
            }
        elif current_file is not None:
            current_file['lines'].append(line)
            if line.startswith('+') and not line.startswith('+++'):
                current_file['added'] += 1
                total_added += 1
            elif line.startswith('-') and not line.startswith('---'):
                current_file['removed'] += 1
                total_removed += 1

    if current_file:
        files.append(current_file)

    # Build summary
    summary_parts = []

    # Stats header
    file_count = len(files)
    total_changes = total_added + total_removed
    summary_parts.append(f"=== Diff Summary: {file_count} file(s), +{total_added}/-{total_removed} ({total_changes} total) ===\n")

    # File list with stats
    summary_parts.append("Files changed:")
    for f in files:
        summary_parts.append(f"  {f['name']}: +{f['added']}/-{f['removed']}")
    summary_parts.append("")

    # Truncated diff content per file
    summary_parts.append("Preview (first 20 lines per file):")
    summary_parts.append("-" * 40)

    for f in files:
        # Show first N lines of this file's diff
        file_lines = f['lines'][:lines_per_file]
        summary_parts.extend(file_lines)
        if len(f['lines']) > lines_per_file:
            omitted = len(f['lines']) - lines_per_file
            summary_parts.append(f"  ... [{omitted} more lines in {f['name']}]")
        summary_parts.append("")

    return '\n'.join(summary_parts)


def is_whitespace_or_comment_only_change(before_diff: str, after_diff: str) -> bool:
    """Check if the difference between two diffs is only whitespace or comments.

    This is used to detect potential false positive reviews: if a task was
    approved on retry without meaningful code changes (only whitespace or
    comment changes), the original REQUEST_CHANGES may have been a false positive.

    Args:
        before_diff: Git diff from before REQUEST_CHANGES verdict.
        after_diff: Git diff from when APPROVED verdict was given.

    Returns:
        True if the only differences between the diffs are whitespace or comments.
        Returns False if diffs are identical (no changes made) or if there are
        meaningful code changes.
    """
    # If diffs are identical, no changes were made at all - not a whitespace change
    if before_diff == after_diff:
        return False

    # Parse both diffs to extract meaningful lines
    def extract_code_changes(diff_text: str) -> set[str]:
        """Extract meaningful code changes from a diff, ignoring whitespace and comments."""
        meaningful_lines = set()
        for line in diff_text.split('\n'):
            # Only look at added/removed lines
            if not line.startswith(('+', '-')):
                continue
            # Skip diff headers
            if line.startswith(('+++', '---')):
                continue
            # Get the actual content (without +/- prefix)
            content = line[1:].strip()
            # Skip empty lines (pure whitespace changes)
            if not content:
                continue
            # Skip comment-only lines (Python, JS, C-style)
            if content.startswith('#'):
                continue
            if content.startswith('//'):
                continue
            if content.startswith('/*') or content.startswith('*') or content.endswith('*/'):
                continue
            # Skip docstrings (triple quotes)
            if content.startswith('"""') or content.startswith("'''"):
                continue
            # This is a meaningful change
            meaningful_lines.add(content)
        return meaningful_lines

    before_meaningful = extract_code_changes(before_diff)
    after_meaningful = extract_code_changes(after_diff)

    # If the meaningful code is the same, only whitespace/comments changed
    # AND we know diffs are different (checked above), so something changed
    return before_meaningful == after_meaningful
