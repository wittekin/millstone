<h1><img src="docs/assets/logo-wordmark.svg" alt="millstone logo" width="760" /></h1>

[![CI](https://github.com/wittekin/millstone/actions/workflows/ci.yml/badge.svg)](https://github.com/wittekin/millstone/actions/workflows/ci.yml)
[![Quality](https://github.com/wittekin/millstone/actions/workflows/quality.yml/badge.svg)](https://github.com/wittekin/millstone/actions/workflows/quality.yml)
[![Coverage](https://codecov.io/gh/wittekin/millstone/branch/main/graph/badge.svg)](https://codecov.io/gh/wittekin/millstone)
[![Docs](https://github.com/wittekin/millstone/actions/workflows/docs.yml/badge.svg)](https://github.com/wittekin/millstone/actions/workflows/docs.yml)
[![Release](https://github.com/wittekin/millstone/actions/workflows/release.yml/badge.svg)](https://github.com/wittekin/millstone/actions/workflows/release.yml)
[![PyPI version](https://img.shields.io/pypi/v/millstone.svg?cacheSeconds=300)](https://pypi.org/project/millstone/)
[![Python versions](https://img.shields.io/pypi/pyversions/millstone.svg?cacheSeconds=300)](https://pypi.org/project/millstone/)
[![License](https://img.shields.io/pypi/l/millstone.svg?cacheSeconds=300)](https://github.com/wittekin/millstone/blob/main/LICENSE)

Coding agents produce dramatically better results when they plan before they code, and when their output is reviewed by a second agent — ideally from a different model provider. The catch: manually running that cycle (design → review → revise → approve → plan → review → revise → implement → review → revise → commit) across multiple agents is extremely time-consuming.

`millstone` automates that end to end. It wraps any combination of coding CLIs (Claude Code, Codex, Gemini, OpenCode) in a deterministic build-review loop: one agent authors, a second reviews, feedback cycles until the reviewer approves, then the change is committed. The same loop governs designs, plans, and code — with optional autonomous outer loops that discover opportunities, generate designs, and break them into tasks without human prompting.

[Documentation](https://wittekin.github.io/millstone/) | [Getting Started](docs/getting-started.md) | [Meta Invoke](docs/prompts/execute.md) | [Contributing](CONTRIBUTING.md) | [Changelog](CHANGELOG.md)

## Quick Start

```bash
# 1) Install
pipx install millstone

# 2) Move into the repo you want to run on
cd /path/to/your/project

# 3) Recommended: give your coding agent an operator prompt
# @docs/prompts/execute.md  (run a tasklist)
# @docs/prompts/design.md   (design + plan a new feature)
```

Design-first quickstart (recommended):

```bash
# 1) Draft + review a design
millstone --design "Add retry logic to API client"

# 2) Turn that design into tasklist items
DESIGN_DOC=$(ls -t .millstone/designs/*.md | head -n 1)
millstone --plan "$DESIGN_DOC"

# 3) Execute the first planned task
millstone -n 1
```

Fast smoke test (no tasklist required):

```bash
millstone --task "add retry logic to API client"
```

`millstone` / `millstone -n 1` read from the configured tasklist path (default: `.millstone/tasklist.md`).
Use `--task` when you want a one-off run without tasklist setup.

## Highlights

- Deterministic inner loop: `Builder -> Sanity -> Reviewer -> Sanity -> Fix -> Commit`.
- Autonomous outer loops: `analyze`, `design`, `plan`, `cycle` — every authoring step is write/review gated.
- `--max-cycles` governs both inner build-review iterations and outer-loop authoring loops.
- Parallel execution via `git worktree` — run multiple tasks concurrently with isolated checkouts and a serialized merge queue.
- Primary operating mode is coding-agent-invoked execution (`docs/prompts/execute.md`).
- Built-in evaluation flow with result capture and regression comparison.
- Multi-provider CLI routing per role (`claude`, `codex`, `gemini`, `opencode`).
- Stateful runs with logs, evals, and recovery under `.millstone/`.

## Usage Patterns

| Goal | Command |
|---|---|
| Coding agent mediated execution (recommended) | Give your coding agent `docs/prompts/execute.md` |
| Execute next tasks from tasklist | `millstone` |
| Limit to one task | `millstone -n 1` |
| Run custom one-off task | `millstone --task "..."` |
| Claude code as author, codex as reviewer, one task, max of 6 write/review cycles task | `millstone --cli claude --cli-reviewer codex -n 1 --max-cycles 6` |
| Run 4 tasks in parallel (worktree mode) | `millstone --worktrees --concurrency 4` |
| Dry-run prompt flow without invoking agents | `millstone --dry-run` |
| Scan codebase for opportunities | `millstone --analyze` |
| Generate a design doc | `millstone --design "Add caching layer"` |
| Turn design into atomic tasks | `millstone --plan .millstone/designs/add-caching-layer.md` |
| Run autonomous cycle end-to-end | `millstone --cycle` |

## How It Works

Inner loop (delivery):

```text
Builder -> Sanity Check -> Reviewer -> Sanity Check -> Fix Loop -> Commit
```

Outer loop (self-direction):

```text
Analyze -> Design -> Plan -> [Inner Loop] -> Eval -> (repeat)
```

Every authoring step in the outer loop (analyze, design, plan) is write/review gated: a
reviewer agent checks the output and requests revisions until it approves or `--max-cycles`
is exhausted. This is the same iterative loop that governs inner-loop code changes.

> **Supersedes prior behavior**: `--analyze` previously ran the analysis agent once with no
> review step. All outer-loop authoring steps (analyze, design, plan) now run an iterative
> write/review/fix loop identical in structure to the inner build-review loop.

## Installation Options

```bash
# PyPI (recommended when release is available)
pipx install millstone

# GitHub latest
pipx install git+https://github.com/wittekin/millstone.git

# Contributor install
pip install -e .
```

Optional extras:

```bash
pip install -e .[test]      # pytest + coverage
pip install -e .[quality]   # ruff + mypy
pip install -e .[security]  # pip-audit
pip install -e .[release]   # build + twine
```

## Minimal Tasklist Format

```markdown
# Tasklist

- [ ] First task to implement
- [ ] Second task
- [x] Already completed task
```

`millstone` executes the first unchecked `- [ ]` task.

## Configuration Snapshot

Create `.millstone/config.toml` in the target repo:

```toml
max_cycles = 3
max_tasks = 5
tasklist = ".millstone/tasklist.md"

cli = "claude"
cli_builder = "codex"
cli_reviewer = "claude"

eval_on_commit = false
approve_opportunities = true
approve_designs = true
approve_plans = true
```

### Multi-maintainer setup

By default, artifact files (tasklist, designs, opportunities) are written under `.millstone/` and are gitignored — suitable for single-maintainer or local-only workflows.

To commit artifacts to the repo and share them with teammates, opt in per artifact type:

```toml
commit_tasklist = true       # stores at docs/tasklist.md
commit_designs = true        # stores at designs/
commit_opportunities = true  # stores at opportunities.md
```

For full multi-maintainer collaboration, use an external artifact provider (Jira, Linear, or GitHub Issues) instead of file-backed defaults.

### Tasklist filter contract

All tasklist providers (Jira, Linear, GitHub Issues) respect a provider-agnostic `[tasklist_filter]` section in `.millstone/config.toml`:

```toml
[tasklist_filter]
labels    = ["sprint-1"]        # AND – task must carry ALL listed labels
assignees = ["alice", "bob"]    # OR  – task assigned to ANY of these users
statuses  = ["Todo", "In Progress"]  # OR  – task in ANY of these statuses
```

Omit any key (or leave the list empty) to skip filtering on that dimension. The filter is applied when the outer loop fetches the next task from the remote provider. An explicit `filter` key inside `[tasklist_provider_options]` takes precedence over this section.

### Scoping remote backlogs

When using a remote tasklist provider (Jira, Linear, or GitHub Issues), the default scope is the full open-issue set for the configured project/team/repo. Use `[millstone.tasklist_filter]` to restrict millstone to a specific subset without modifying provider options.

**When to use local tasklist vs remote filters**

| Situation | Recommendation |
|---|---|
| Personal project or solo maintainer | Local `.millstone/tasklist.md` |
| Team with shared backlog in Jira/Linear/GitHub | Remote provider + `[millstone.tasklist_filter]` |
| Ad-hoc spike or one-off work | `millstone --task "..."` |
| Sprint-scoped automation on a shared board | Remote provider + label/cycle/milestone filter |

**Quick examples by backend**

Jira — current sprint label:
```toml
[tasklist_provider_options]
type = "jira"
project = "PROJ"

[millstone.tasklist_filter]
label = "sprint-1"
assignee = "john.doe"
```

Linear — active cycle for a team:
```toml
[tasklist_provider_options]
type = "linear"
team_id = "<uuid>"

[millstone.tasklist_filter]
cycles = ["Cycle 5"]
label  = "millstone"
```

GitHub Issues — label + milestone:
```toml
[tasklist_provider_options]
type  = "github"
owner = "myorg"
repo  = "myrepo"

[millstone.tasklist_filter]
label     = "sprint-1"
milestone = "v1.2"
```

See full filter option reference in the per-backend docs under `docs/providers/`.

See full config and CLI options with:

```bash
millstone --help
```

## Project Signals

- Canonical loop ontology: `docs/architecture/ontology.md`
- Scope and safety boundaries: `docs/architecture/scope.md`
- Parallel execution with worktrees: `docs/worktrees.md`
- CLI providers: `docs/cli-providers/`
- Artifact providers: `docs/providers/`
- Release checklist: `docs/maintainer/release_checklist.md`

## Build and Release Workflows

This repository ships with CI, quality, docs, release, security, CodeQL, dependency review, and weekly maintenance workflows in `.github/workflows/`.

Tag release flow:

```bash
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

## Star History

Planned after initial public release and first community adoption.

## Working Directory

Creates `.millstone/` in your repo containing:
- `runs/` - Timestamped logs of each run
- `evals/` - JSON eval results for comparison
- `cycles/` - Logs of autonomous cycle decisions
- `state.json` - Saved state for --continue
- `config.toml` - Per-repo configuration
- `STOP.md` - Created by sanity check to halt

This directory is auto-added to `.gitignore`.

## Safety Checks

**Mechanical:**
- No changes detected -> Warn (proceeds to review)
- Too many lines changed -> Halt for human review
- Sensitive files (`.env`, credentials) -> Halt for human review
- New test failures (with `--eval-on-commit`) -> Halt

**Judgment (via LLM):**
- Builder output is gibberish -> Create `STOP.md` -> Halt
- Reviewer feedback is nonsensical -> Create `STOP.md` -> Halt

## Exit Codes

- `0` - Success
- `1` - Halted (needs human intervention)

## Expected Runtime

Depending on cycles, tasks, and your agent provider / model, millstone can run for minutes or hours.

## Requirements

- Python 3.10+
- `claude` CLI installed and authenticated (default), or
- `codex` CLI installed and authenticated (if using `--cli codex`), or
- `gemini` CLI installed and authenticated (if using `--cli gemini`), or
- `opencode` CLI installed and authenticated (if using `--cli opencode`)

## Open Source Project Files

- License: [LICENSE](LICENSE)
- Contributing guide: [CONTRIBUTING.md](CONTRIBUTING.md)
- Code of conduct: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- Security policy: [SECURITY.md](SECURITY.md)
- Changelog: [CHANGELOG.md](CHANGELOG.md)
