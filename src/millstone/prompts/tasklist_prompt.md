<context>
Work in: {{WORKING_DIRECTORY}}
{{TASKLIST_READ_INSTRUCTIONS}}
</context>

<goal>
Complete exactly one task: the first unchecked task in the tasklist.
If no unchecked task exists, output exactly `NO_TASKS_REMAIN` and stop.
</goal>

<rules>
- Read the full tasklist before editing.
- Work only on the first unchecked task.
- Do not implement, prepare, reorder, or check off any other task.
- You may adjust future task text only when your completed work makes it inaccurate.
- Run only verification that is appropriate for the selected task and files touched.
- Do not claim success for commands you did not run.
- Do not run `git commit` or `git push`.
</rules>

<process>
1. Read the tasklist and select the first unchecked task.
2. Make the smallest repo-consistent changes that complete that task.
3. Verify the work.
{{ACCEPTANCE_CRITERIA}}4. {{TASKLIST_COMPLETE_INSTRUCTIONS}}
5. Stop immediately after that one task is complete.
</process>

<output>
Return only this structure:

<analysis>
- Target Task: exact unchecked task line you selected
- Plan: files/artifacts to change, approach, verification
- Risks/Blockers: brief notes
</analysis>

[do the work]

<summary>
- Action Taken: what you changed for this task
- Verification: commands/checks run and result
- Tasklist Status: updated completed task line
- Coherence Updates: only if you changed future task text
</summary>
</output>
