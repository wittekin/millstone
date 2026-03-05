# Worktrees

Use worktrees mode to run independent tasklist items in parallel with isolated checkouts.

## What it does

- Creates one `git worktree` per in-flight task on branch `millstone/task/<task_id>`.
- Runs the normal inner loop in each worktree (build -> checks -> sanity -> review -> commit).
- Integrates completed task branches through a serialized `millstone/integration` queue.
- Lands passing integration commits to your base branch.

Sequential mode is still the default. Worktrees mode is opt-in.

## Quick start

```bash
millstone --worktrees --concurrency 4
```

Equivalent config (`.millstone/config.toml`):

```toml
parallel_enabled = true
parallel_concurrency = 4
```

## Key flags

- `--worktrees`
- `--concurrency N`
- `--base-branch NAME`
- `--base-ref REF`
- `--integration-branch NAME`
- `--merge-strategy {merge,cherry-pick}`
- `--worktree-root PATH`
- `--worktree-cleanup {always,on_success,never}`
- `--merge-max-retries N`
- `--no-tasklist-edits`
- `--high-risk-concurrency N`

## Operational constraints

- Git worktree branch uniqueness applies: a branch can only be checked out in one worktree.
- For automatic landing, the base branch must not be checked out in a competing worktree.
- In worktrees mode, tasklist writes are control-plane only; workers are blocked from editing `.millstone/tasklist.md`.

## Cleanup behavior

- `on_success` (default): remove landed task worktrees.
- `always`: remove all task worktrees after run.
- `never`: keep all worktrees for inspection.

## Crash recovery

State is persisted under `.millstone/parallel/` (task results, heartbeats, task map, control state).
A subsequent `--worktrees` run reconciles existing `millstone/*` worktrees and resumes where possible.
