# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog and this project follows Semantic Versioning.

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
