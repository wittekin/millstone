Review this opportunity set for accuracy, priority, and actionability.

{{OPPORTUNITIES_CONTENT}}

Hard signals:
{{HARD_SIGNALS}}

Project goals:
{{PROJECT_GOALS}}

Check only what matters:
- fidelity to observable repo issues
- correct inclusion and priority of hard signals
- specific, actionable descriptions and locations
- duplicate or overlapping entries
- alignment with stated goals

Return JSON only:

```json
{
  "verdict": "APPROVED" | "NEEDS_REVISION",
  "score": 0,
  "strengths": ["..."],
  "issues": ["Critical: ...", "Major: ...", "Minor: ..."],
  "feedback": "Overall summary and next step."
}
```

Use `NEEDS_REVISION` if any critical or major issue exists.
