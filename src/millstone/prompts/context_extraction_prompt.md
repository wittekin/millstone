# Context Extraction

You are analyzing a completed task to extract key decisions and patterns for sharing with subsequent tasks in the same group.

## Task Description

{{TASK_TEXT}}

## Code Changes (Git Diff)

```diff
{{GIT_DIFF}}
```

## Your Goal

Extract the key decisions, patterns, and context from this completed task that would be useful for someone working on related tasks. Focus on:

1. **Architectural decisions**: What patterns, libraries, or approaches were chosen?
2. **File locations**: Where are the key files and components?
3. **API patterns**: What interfaces, function signatures, or conventions were established?
4. **Implementation notes**: Any non-obvious choices or gotchas?

Keep your response concise (2-5 bullet points). Focus on information that would help avoid repeated exploration of the codebase.

## Response Format

Respond with a JSON object:

```json
{
  "summary": "One-line summary of what was done",
  "key_decisions": [
    "First key decision or pattern...",
    "Second key decision or pattern..."
  ]
}
```
