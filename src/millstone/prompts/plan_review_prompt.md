Review only the tasks created or updated for this planning run.

Design:
{{DESIGN_CONTENT}}

Task access:
{{TASKLIST_READ_INSTRUCTIONS}}

Task IDs:
{{PROPOSED_TASK_IDS}}

Planner summary:
{{PROPOSED_PLAN}}

Check for:
- correct ordering and dependency flow
- tasks that are atomic and clear in isolation
- enough context for a stateless builder
- realistic verification in the `Tests`/criteria fields
- appropriate handling of uncertainty or branching decisions

Return JSON only:

```json
{
  "verdict": "APPROVED" | "NEEDS_REVISION",
  "feedback": ["Critical: ...", "Major: ...", "Minor: ..."],
  "score": 0
}
```

Use `NEEDS_REVISION` if any critical or major issue exists.
