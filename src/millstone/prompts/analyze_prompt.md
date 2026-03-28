Review the repository and identify concrete improvement opportunities.
Consider code, docs, design artifacts, workflows, tests, configuration, and content.
Prefer observable issues over speculation.

{{HARD_SIGNALS}}

{{ROLLBACK_CONTEXT}}

Priority order:
1. Hard signals and regressions
2. Goal-aligned improvements
3. High-impact, low-effort fixes

{{PROJECT_GOALS}}

{{KNOWN_ISSUES}}

Look for:
- missing or incorrect behavior
- verification gaps
- outdated or unclear docs/content
- maintainability or architecture problems
- performance, security, or reliability risks
- workflow or automation gaps

{{OPPORTUNITY_WRITE_INSTRUCTIONS}}

Output checklist entries in this format:

```markdown
# Opportunities

Generated: <timestamp>
Git HEAD: <commit hash>

- [ ] **<Title>**
  - Opportunity ID: <short-kebab-slug>
  - Requires Design: true|false
  - ROI Score: <impact/effort decimal>
  - Impact: <1-5>/5 - <why it matters>
  - Effort: <1-5>/5 - <what is involved>
  - Confidence: <High|Medium|Low>
  - Location: <file:line or area>
  - Description: <specific problem and suggested direction>
```

Sort by ROI Score descending.
Set `Requires Design: true` for cross-cutting, high-risk, or hard-to-reverse work.
