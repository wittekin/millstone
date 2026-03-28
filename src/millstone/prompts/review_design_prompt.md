Review this design for correctness, completeness, and execution readiness:

{{DESIGN_CONTENT}}

Check only the essentials:
- success criteria are measurable
- approach is specific enough to execute
- alternatives and tradeoffs are real
- risks and mitigations are useful
- scope is sensible
- verification and rollout concerns are covered when relevant
- the design matches repo patterns instead of inventing unnecessary new ones

Return JSON only:

```json
{
  "verdict": "APPROVED" | "NEEDS_REVISION",
  "strengths": ["..."],
  "issues": ["Concrete edit instruction..."],
  "questions": ["Clarifying question..."]
}
```

If revision is needed, every issue must describe a concrete change to the current document.
