# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog and this project follows Semantic Versioning.

## [Unreleased]

## [0.5.9] - 2026-03-29

### Added
- Elapsed-time reporting on agent calls: each builder, reviewer, and sanity check call now prints a "done (Xm Ys)" line on completion, with a heartbeat every 60 seconds during long-running calls.
- Resume hint (`Resume with: millstone --continue`) printed on all halt paths — STOP.md, sanity failures, task failures, and CLI errors.
- Run summary printed at exit showing tasks completed/failed/remaining and total elapsed time.
- Enriched task-completion line showing cycle count, reviewer findings, lines changed, and elapsed time.
- Actionable CLI error guidance: common failure patterns (OOM, rate limit, auth, timeout) now include a one-line suggestion and resume command.
- Compact 2-line startup header enabled by default; full header available via `--verbose-header` or `verbose_header = true` in config.

## [0.5.8] - 2026-03-28

### Changed
- LoC gating now treats `0` as disabled across task execution and merge safety checks, and the default `loc_threshold` is now `0`.

### Fixed
- Startup auto-compaction now skips dirty working trees on fresh runs, avoiding tasklist rewrites after a crash or interrupted task until the operator resumes cleanly.

## [0.5.7] - 2026-03-28

### Fixed
- Builder no longer silently drops unchecked tasks when rewriting `.millstone/tasklist.md`. The builder prompt now explicitly forbids modifying, reorganizing, or removing other task text.
- Added inter-task compaction: the orchestrator compacts completed tasks between cycles when the threshold is met, reducing file verbosity that motivated builders to reorganize.

## [0.5.6] - 2026-03-28

### Added
- Added stdin-safe decision gates for high-risk tasks, eval-regression prompt mode, and critical effect approvals, with explicit resume flags `--approve-high-risk` and `--approve-effects`.

### Changed
- `--continue` now re-surfaces saved decision gates deterministically and resolves them only through explicit approval or eval-regression policy flags.
- Updated the README and operator docs to document decision-gate resumes and the new exit-code contract.

## [0.5.5] - 2026-03-28

### Added
- Added `--on-eval-regression=prompt|rollback|ignore` so supervising automation can choose a deterministic post-eval policy without relying on interactive stdin prompts.

### Fixed
- `--no-approve` now reaches inner-loop high-risk approvals and effect approval hooks instead of only disabling the outer analyze/design/plan gates.
- Kept `--auto-rollback` as a compatibility alias while routing eval regression handling through the new explicit policy model.

## [0.5.4] - 2026-03-28

### Fixed
- Scope local tasklist `-n 1` runs more tightly by carrying the selected task line into builder, reviewer, and fix-cycle prompts.
- Guide reviewers to narrow later-task spillover instead of only rejecting it, so follow-up cycles stay focused on the selected task.
- Fix Codecov coverage upload configuration by enabling branch coverage, supplying the Codecov token, and failing CI on upload errors.

### Changed
- Strengthened planning prompts so generated tasks are explicitly single-concern, low-fanout, fully specified at their boundaries, and independently verifiable.

## [0.5.3] - 2026-03-27

### Fixed
- Preserve explicit `--max-cycles` and configured `max_cycles` values when risk settings are applied, so the inner builder-reviewer loop honors the requested cycle budget.
- Treat `roadmap` as a first-class configured artifact alongside `tasklist`, including config loading, cleanup preservation for `.millstone/roadmap.md`, and CLI/config parity.
- Keep no-code verification test harnesses aligned with the current prompt contracts so read-only task coverage stays stable.

### Changed
- Simplified built-in prompts to be shorter, more general-purpose, and less role-specific.
- Shifted prompt emphasis toward correctness, completeness, verification quality, and concrete output contracts, leaving repo-specific guidance to `AGENTS.md`, user input, and local context.

## [0.5.2] - 2026-03-20

### Fixed
- Fixed plan review context for provider-backed tasklists so planning review sees the correct task provider state.

## [0.5.1] - 2026-03-14

### Fixed
- MCP sync now raises on missing staging files instead of silently skipping (previously lost pending writes).
- MCP sync now raises on corrupt staging files that parse to zero items instead of silently archiving them.
- `run_eval()` internal commands (pytest fallback, python/pytest mode paths) now use `shell=False` with argument lists.
- `git()` helpers in inner loop and eval manager now raise on failed git operations instead of returning silent empty strings.
- `json.loads` calls in eval comparison, eval summary, and hard signal collection are now guarded against corrupt JSON with safe fallbacks.

### Added
- Unit tests for `summarize_diff()` and `progress()` in utils.py.
- Contract tests for MCP staging file corruption/missing edge cases.
- Unit tests for git helper error handling and JSON parse guards.

## [0.5.0] - 2026-03-14

### Added
- Introduced a composable outer-loop pipeline with typed handoff edges, checkpointed resume, and extensible stage registration.
- Added `--through` to chain `--analyze`, `--design`, or `--plan` forward through `design`, `plan`, or `execute`.
- Added batch outer-loop execution, injected artifact entry points, and pipeline-aware `--continue` resume support.

### Changed
- Replaced duplicated CLI outer-loop chaining logic with a single pipeline dispatcher.
- Preserved `--cycle` triage semantics and `--deliver` backlog protections while routing both through the new pipeline architecture.
- Updated user and maintainer docs to describe the pipeline-based outer loop and `--through` workflows.

## [0.4.2] - 2026-03-07

### Fixed
- Show MCP provider name and labels in startup header instead of local file path (#56).
- `--status` now shows live open task count from remote provider (#57).
- `--research` mode now closes remote tasks via MCP after completing research output (#58).
- `tasklist_prompt.md` now explicitly forbids builder from committing during implementation (#60).
- Orchestrator detects when builder commits early and uses committed diff for review (#60).

## [0.4.1] - 2026-03-07

### Added
- `--worktrees` support for MCP tasklist providers (GitHub Issues, Linear, Jira backends) (#46).

### Fixed
- Show provider info in `--dry-run` and `--status` with remote backends instead of missing file path (#44).
- Populate `{{COMPLETED_TASKS}}` with real content in `--prepare-release` instead of literal placeholder (#48).
- Exclude `build/` and `tests/` from mypy to eliminate false-positive duplicate-module errors in `--eval` (#50).
- Use exit code to determine lint error count in `--eval`, reporting correct `lint: 1.00 (errors=0)` on clean codebase (#51).
- Graceful error for `--split-task` with remote MCP providers instead of crashing (#53).

## [0.4.0] - 2026-03-07

### Added
- `--complete` flag to chain any outer-loop entry point through all remaining stages to implementation (#32).
- Generalized `--continue` to resume any interrupted outer-loop run (analyze/design/plan/cycle) (#32).
- Deferred MCP artifact writes: when `approve_*=True` with MCP backends, agent writes are staged locally until approval and synced on the next run (#38).

### Fixed
- Compact `list_tasks`/`list_designs` responses to avoid 80KB payload crashes with large MCP backends (#36).
- Plan validation for MCP-backed tasklists now uses `get_task()` instead of parsing title-only snapshots (#36).
- Replaced deprecated `datetime.utcnow()` calls with timezone-aware alternatives (#40).

## [0.3.5] - 2026-03-06

- Added `--deliver "OBJECTIVE"` for a discoverable design -> plan -> execute flow that skips analyze.
- Added `--migrate-tasklist PATH` to convert local backlog files into canonical tasklist format.
- Simplified Quick Start documentation around five common user entry points.
- Clarified Quick Start prerequisites: install/authenticate at least one supported coding agent CLI.
- Fixed documentation command references to the default `.millstone/tasklist.md` path.

## [0.3.4] - 2026-03-05

- Updated branding assets and README header rendering.
- Improved docs and release/badge wiring reliability.

## [0.3.3] - 2026-03-03

Initial public release.
