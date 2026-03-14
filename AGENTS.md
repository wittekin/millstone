# AGENTS.md

If this document is incorrect, fix it. Ensure documentation remains accurate.

## Project Overview

millstone is a Python CLI tool that wraps agentic coding tools (like Claude Code) in a deterministic builder-reviewer workflow. It orchestrates LLM calls to implement tasks from a tasklist, with sanity checks, code review, and automatic commits.

**Key capability**: The system can self-direct through outer loops that discover opportunities, design solutions, and generate tasks - enabling autonomous improvement cycles.

Canonical ontology (roles/artifacts/providers/profiles) for generalized loops:
- `docs/architecture/ontology.md`
- `docs/architecture/scope.md` (task classes, capability tiers, safety boundaries)

## Commands

```bash
# Install in development mode
pip install -e .

# Run tests
pytest

# Run a single test
pytest tests/test_orchestrator.py::test_function_name -v

# Run tests with coverage
pytest --cov=. --cov-report=term-missing

# Run the orchestrator (requires at least one supported CLI installed:
# claude, codex, gemini, or opencode)
millstone                           # Process tasks from .millstone/tasklist.md
millstone --task "description"      # Single direct task
millstone --migrate-tasklist backlog.md  # Convert a local backlog to tasklist format
millstone --deliver "objective"     # Design -> plan -> execute (skip analyze)
millstone --dry-run                 # Preview prompts without invoking agent
millstone --cli codex               # Use Codex CLI instead of Claude

# Outer loop commands
millstone --eval                    # Run tests, capture results to .millstone/evals/
millstone --eval-compare            # Compare two most recent eval runs
millstone --analyze                 # Scan codebase for improvement opportunities
millstone --design "opportunity"    # Create design doc for an opportunity
millstone --plan .millstone/designs/foo.md  # Break design into tasklist tasks
millstone --analyze --through plan  # Analyze -> design -> plan, then stop
millstone --design "objective" --through execute  # Design -> plan -> execute
millstone --cycle                   # Full autonomous loop with triage (pending tasks, roadmap, or fresh analysis)
```

## Practical Note

If you reply back to the user, you are handing control back to them which means you'll lose control until they respond. Do NOT reply to the user until your objectives are complete or you're hard blocked. For example, don't send the user a message like "Continuing on with millstone -n 1 ..." because you won't be able to continue after sending it.

### Inner Loop (Task Execution)

```
Builder → Sanity ✓ → Reviewer → Sanity ✓ → [Fix Loop] → Commit
```

1. Builder agent implements task from tasklist or --task
2. Mechanical checks (LoC threshold, sensitive files)
3. Sanity check on implementation (sanity role/provider)
4. Reviewer agent evaluates changes
5. Sanity check on review (sanity role/provider)
6. If approved: delegate commit to builder; else: loop back with feedback (up to max-cycles)

### Outer Loops (Self-Direction)

```
Analyze → Design → Plan → [Inner Loop]
```

1. **Analyze** (`run_analyze()`): Agent scans codebase for opportunities (default file backend writes `.millstone/opportunities.md`; provider backends may store elsewhere)
2. **Design** (`run_design()`): Agent creates implementation spec → `.millstone/designs/<slug>.md`
3. **Plan** (`run_plan()`): Agent breaks design into atomic tasks → appends to tasklist
4. **Eval** (`run_eval()`): Run tests, capture results → `.millstone/evals/<timestamp>.json`
5. **Cycle** (`run_cycle()`): Resolves a pipeline from pending tasks, roadmap goals, or fresh analysis, then chains forward with approval gates

### Key Methods

**Inner loop**:
- `run()` - Main entry point, handles --continue and task loop
- `run_single_task()` - One task through build-review cycle
- `mechanical_checks()` - LoC threshold, sensitive file detection
- `sanity_check_impl()` / `sanity_check_review()` - LLM-based validation
- `delegate_commit()` - Has builder commit its own changes

**Outer loops**:
- `run_eval()` - Run tests, store JSON results
- `compare_evals()` - Diff two eval results, detect regressions
- `run_analyze()` - Invoke analysis agent, persist opportunities via configured provider (default file: `.millstone/opportunities.md`)
- `run_design()` - Invoke design agent, create design doc
- `review_design()` - Review design for completeness
- `run_plan()` - Invoke planning agent, append tasks to tasklist
- `run_cycle()` - Legacy cycle entry point; CLI outer-loop chaining now goes through the pipeline executor

**State management**:
- `save_state()` / `load_state()` - State persistence for --continue

## Prompts

Built-in templates are in `millstone/prompts/` with `{{PLACEHOLDER}}` substitution and loaded via `load_prompt()`. A custom prompt directory can be supplied with `--prompts-dir`.

| Prompt | Purpose |
|--------|---------|
| `tasklist_prompt.md` | Builder: implement one task from tasklist |
| `task_prompt.md` | Builder: implement direct --task |
| `review_prompt.md` | Reviewer: evaluate changes |
| `sanity_check_impl.md` | Validate implementation isn't gibberish |
| `sanity_check_review.md` | Validate review isn't gibberish |
| `commit_prompt.md` | Builder: commit changes |
| `analyze_prompt.md` | Analysis agent: find opportunities |
| `design_prompt.md` | Design agent: create implementation spec |
| `review_design_prompt.md` | Review design for completeness |
| `plan_prompt.md` | Planning agent: break design into tasks |
| `analyze_review_prompt.md` | Reviewer: evaluate opportunities quality |
| `analyze_fix_prompt.md` | Analyzer: revise opportunities from feedback |
| `design_fix_prompt.md` | Designer: revise design from feedback |
| `plan_review_prompt.md` | Reviewer: evaluate generated plan/tasks |
| `plan_fix_prompt.md` | Planner: revise tasks from feedback |
| `compact_tasklist.md` | Compact completed tasks |

## Work Directory

`.millstone/` in target repo contains:
- `runs/` - Timestamped logs of each run
- `evals/` - JSON eval results for trend analysis
- `cycles/` - Logs of autonomous cycle decisions
- `tasks/` - Per-task metrics used by eval summary/trend views
- `metrics/` - Review metrics (for `--metrics-report`)
- `research/` - Outputs from `--research` runs
- `locks/` - Lock files used in worktree/parallel execution
- `worktrees/` - Worktree roots in parallel mode
- `state.json` - Saved state for --continue recovery
- `config.toml` - Per-repo configuration
- `policy.toml` - Policy overrides (limits/sensitive/dangerous/tasklist/eval)
- `project.toml` - Project-specific command overrides (tests/lint/typing/etc.)
- `STOP.md` - Created by sanity check to halt on problems

## Configuration

Defaults in `DEFAULT_CONFIG` dict, overridden by `.millstone/config.toml`, then CLI args.

Key config options:
- `max_cycles`, `loc_threshold`, `tasklist`, `max_tasks`
- `eval_on_commit` - Run tests after each commit
- `eval_scripts` - Custom scripts to run during eval
- `approve_opportunities`, `approve_designs`, `approve_plans` - Human-in-loop gates
- `review_designs` - Auto-review designs before planning
- `cli` - Default CLI tool (`claude`, `codex`, `gemini`, or `opencode`)
- `cli_builder`, `cli_reviewer`, `cli_sanity`, `cli_analyzer`, `cli_release_eng`, `cli_sre` - Per-role CLI overrides
- `opportunity_provider`, `design_provider`, `tasklist_provider` - Artifact backend selection (file/MCP-style providers)
- `parallel_*` keys - Worktree/parallel execution controls
- `profile` - Active role/profile registry mapping for loop contracts

## Scoping remote backlogs

When using a remote tasklist provider (Jira, Linear, or GitHub Issues), millstone defaults to
the full open-issue set for the configured project/team/repo. Use `[millstone.artifacts.tasklist_filter]`
in `.millstone/config.toml` to restrict execution without changing provider options.

**When to use local vs remote**:
- **Local `.millstone/tasklist.md`** — solo work, personal projects, explicit ordering.
- **Remote provider + filter** — team boards in Jira/Linear/GitHub where the backlog is shared.

**Quick examples**:
```toml
# Jira: sprint-1 label, specific assignee
[millstone.artifacts.tasklist_filter]
label    = "sprint-1"
assignee = "alice"

# Linear: active cycle
[millstone.artifacts.tasklist_filter]
cycles = ["Cycle 5"]
label  = "millstone"

# GitHub Issues: label + milestone
[millstone.artifacts.tasklist_filter]
label     = "sprint-1"
milestone = "v1.2"
```

Single-value shortcuts (`label`, `assignee`, `status`) expand to their list equivalents.
Full filter reference: `docs/providers/<backend>.md`.

## Testing

Tests are in `tests/` using pytest. Key fixtures in `conftest.py`:
- `temp_repo` - Creates temporary git repo with tasklist
- `mock_claude` - Patches subprocess.run for claude CLI

Integration tests use composable mock factories for response handling.

## Self-Hosting

This project uses itself for development. To run the full autonomous cycle:

```bash
# With human approval gates (default, recommended)
millstone --cycle

# Fully autonomous (use with caution)
millstone --cycle --no-approve
```

## Release and Publishing Workflows

Standard GitHub Actions workflows in this repo:

- `ci.yml` — cross-platform test matrix, coverage artifact, and package build check
- `quality.yml` — lint + dead-code checks, plus advisory format/type checks
- `docs.yml` — MkDocs build on PR; deploy to GitHub Pages on `main`
- `release.yml` — build artifacts, create GitHub Release, publish to PyPI on tag push (`v*`)
- `security.yml` — dependency vulnerability scan (`pip-audit`)
- `codeql.yml` — static security analysis
- `dependency-review.yml` — PR dependency risk review
- `maintenance.yml` — scheduled weekly matrix tests + security audit + dependency health checks

One-time repository setup requirements:

- Enable GitHub Pages with source set to **GitHub Actions**.
- Configure PyPI trusted publishing for project `millstone`:
  - Owner: `wittekin`
  - Repository: `millstone`
  - Workflow: `release.yml`
  - Environment: `pypi`

Tag-based release flow:

```bash
# 1) Update version/changelog and merge to main
git push origin main

# 2) Create annotated release tag
git tag -a vX.Y.Z -m "Release vX.Y.Z"

# 3) Push tag to trigger release + PyPI publish
git push origin vX.Y.Z
```

Post-release verification:

- Confirm `Release` workflow succeeded.
- Confirm GitHub Release exists with wheel/sdist artifacts attached.
- Confirm package is visible on PyPI.
