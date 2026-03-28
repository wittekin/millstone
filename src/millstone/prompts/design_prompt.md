Create a concrete design for this opportunity:

{{OPPORTUNITY}}

Read the relevant repo material first. Evaluate at least two viable approaches, choose one, and explain the tradeoff.

{{DESIGN_WRITE_INSTRUCTIONS}}

Do not change `design_id`.
Use this structure:

```markdown
# <Title>

- **design_id**: <kebab-case slug>
- **title**: <Title>
- **status**: draft
- **opportunity_ref**: {{OPPORTUNITY_ID}}
- **created**: <YYYY-MM-DD>

---

## Problem Statement
<why this matters>

## Success Criteria
- [ ] <measurable criterion>

## Approach
<chosen solution, affected files/artifacts, key details>

## Alternatives Considered
### <Alternative>
- Pros: ...
- Cons: ...
- Why not chosen: ...

## Risks and Mitigations
| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| ... | Low/Med/High | Low/Med/High | ... |

## Affected Files
- `path` - <change>
```

Be specific enough that the next agent can execute without guessing.
