# Sanity Check: Implementation

Check whether the author's work is reasonable enough to send to review.
Do not do a full review.

Halt only for serious problems:
- incoherent or unrelated output
- obvious failure loops or catastrophic errors
- destructive or unsafe changes
- no meaningful work when the task clearly required changes

Do not halt for normal bugs, incompleteness, style issues, or tangential but relevant work.

Author output:
{{AGENT_OUTPUT}}

Git status:
{{GIT_STATUS}}

Git diff:
{{GIT_DIFF}}

Return JSON only:

```json
{"status": "OK"}
```

or

```json
{"status": "HALT", "reason": "why human intervention is required"}
```
