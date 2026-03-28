# Sanity Check: Review

Check whether the review feedback is coherent and safe enough to send back to the author.
Do not re-review the underlying work.

Halt only for serious problems:
- incoherent or unrelated feedback
- dangerous instructions
- contradictions that make the feedback impossible to act on
- clear mismatch with the changed work

Do not halt for:
- minor factual inaccuracies such as line lengths, character counts, or other small numeric mistakes
- stylistic disagreement
- incomplete analysis
- abbreviated but still clear review format
- brief reviews for documentation-only or trivial changes

Review output:
{{REVIEW_OUTPUT}}

Return JSON only:

```json
{"status": "OK"}
```

or

```json
{"status": "HALT", "reason": "why human intervention is required"}
```
