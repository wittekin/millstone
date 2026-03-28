<context>
Work in: {{WORKING_DIRECTORY}}
{{TASKLIST_READ_INSTRUCTIONS}}
</context>

<goal>
Complete exactly one task: the task explicitly selected for this run.
If the selected task is missing, no longer the first unchecked task, or otherwise ambiguous, output exactly `NO_TASKS_REMAIN` and stop.
</goal>

<rules>
- Read the full tasklist before editing.
- Treat the explicitly selected task shown later in this prompt as the only task in scope.
- Do not implement, prepare, reorder, or check off any other task.
- Do not modify, reorganize, summarize, or remove any other task text.
- Run only verification that is appropriate for the selected task and files touched.
- Do not claim success for commands you did not run.
- Do not run `git commit` or `git push`.
</rules>

<process>
1. Read the full tasklist for context, but do not re-select the task yourself.
2. Complete only the explicitly selected task for this run.
3. Verify the work.
{{ACCEPTANCE_CRITERIA}}4. {{TASKLIST_COMPLETE_INSTRUCTIONS}}
5. Stop immediately after that one task is complete.
</process>

<output>
Return only this structure:

<analysis>
- Target Task: exact selected task line from this prompt
- Plan: files/artifacts to change, approach, verification
- Risks/Blockers: brief notes
</analysis>

[do the work]

<summary>
- Action Taken: what you changed for this task
- Verification: commands/checks run and result
- Tasklist Status: updated completed task line
</summary>
</output>
