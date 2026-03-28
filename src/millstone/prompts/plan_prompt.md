Break this design into ordered, atomic tasks:

{{DESIGN_CONTENT}}

Current tasklist:

{{TASKLIST_CONTENT}}

Each task must be:
- small enough to fit within `{{MAX_LOC}}` estimated LoC
- single-concern rather than bundling multiple goals
- low-fanout: touch as few subsystems, files, and interfaces as practical
- fully specified at the task boundary so in-scope work and handoffs are unambiguous
- independently verifiable
- understandable in isolation by a stateless builder
- ordered so dependencies come first

Required metadata for every task:
- `Est. LoC`
- `Tests`
- `Risk`
- `Criteria`
- `Context` or `<!-- context: path -->`

Use the `Tests` field for the main verification step even when the task is non-code.
That field may name automated tests, manual checks, artifact review steps, or another concrete validation method.

Risk guidance:
- `low`: localized and easily reversible
- `medium`: user-visible, multi-file, or moderate coordination
- `high`: security/data-sensitive, cross-cutting, migration-like, or hard to reverse

If a task contains uncertainty or a choice, tell the builder how to update future tasks after the decision.
Do not modify existing tasks. Only append new ones.

{{TASKLIST_APPEND_INSTRUCTIONS}}

Output format:

```markdown
### <Design Title> Implementation

- [ ] **Task Title**: Full description...
  - Est. LoC: <must be ≤{{MAX_LOC}}>
  - Tests: <verification step>
  - Risk: <low|medium|high>
  - Criteria: <specific done condition>
  - Context: <essential implementation context>
```
