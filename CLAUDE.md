# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) and other AI agents when working with code in this repository. You own the tasklist and corresponding execution on behalf of our research team, blocking paper submission to NME and IEEE, and our enterprise customers and investors who need production ready breakthroughs immediately.

## Project Overview

millstone is a Python CLI tool that wraps agentic coding tools (like Claude Code) in a deterministic builder-reviewer workflow. It orchestrates LLM calls to implement tasks from a tasklist, with sanity checks, code review, and automatic commits.

**Key capability**: The system can self-direct through outer loops that discover opportunities, design solutions, and generate tasks - enabling autonomous improvement cycles.

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

# Run the orchestrator (requires claude or codex CLI installed)
millstone                           # Process tasks from .millstone/tasklist.md
millstone --task "description"      # Single direct task
millstone --dry-run                 # Preview prompts without invoking agent
millstone --cli codex               # Use Codex CLI instead of Claude

# Outer loop commands
millstone --eval                    # Run tests, capture results to .millstone/evals/
millstone --eval-compare            # Compare two most recent eval runs
millstone --analyze                 # Scan codebase for improvement opportunities
millstone --design "opportunity"    # Create design doc for an opportunity
millstone --plan .millstone/designs/foo.md  # Break design into tasklist tasks
millstone --cycle                   # Full autonomous loop (analyze → design → plan → build → eval)
```

## Practical Note

If you reply back to the user, you are handing control back to them which means you'll lose control until they respond. Do NOT reply to the user until your objectives are complete or you're hard blocked. For example, don't send the user a message like "Continuing on with millstone -n 1 ..." because you won't be able to continue after sending it.

## Architecture

**Single-file orchestrator**: All orchestration logic lives in `orchestrate.py` (~2600 lines). The `Orchestrator` class manages both inner and outer loops.

### Inner Loop (Task Execution)

```
Builder → Sanity ✓ → Reviewer → Sanity ✓ → [Fix Loop] → Commit
```

1. Builder agent implements task from tasklist or --task
2. Mechanical checks (LoC threshold, sensitive files)
3. Sanity check on implementation (haiku model)
4. Reviewer agent evaluates changes
5. Sanity check on review (haiku model)
6. If approved: delegate commit to builder; else: loop back with feedback (up to max-cycles)

### Outer Loops (Self-Direction)

```
Analyze → Design → Plan → [Inner Loop] → Eval → (loop back)
```

1. **Analyze** (`run_analyze()`): Agent scans codebase for improvement opportunities → `.millstone/opportunities.md`
2. **Design** (`run_design()`): Agent creates implementation spec → `.millstone/designs/<slug>.md`
3. **Plan** (`run_plan()`): Agent breaks design into atomic tasks → appends to tasklist
4. **Eval** (`run_eval()`): Run tests, capture results → `.millstone/evals/<timestamp>.json`
5. **Cycle** (`run_cycle()`): Chains all loops together for autonomous operation

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
- `run_analyze()` - Invoke analysis agent, create `.millstone/opportunities.md`
- `run_design()` - Invoke design agent, create design doc
- `review_design()` - Review design for completeness
- `run_plan()` - Invoke planning agent, append tasks to tasklist
- `run_cycle()` - Full autonomous cycle with approval gates

**State management**:
- `save_state()` / `load_state()` - State persistence for --continue

## Prompts

Templates in `prompts/` with `{{PLACEHOLDER}}` substitution. Loaded via `load_prompt()`.

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
| `compact_tasklist.md` | Compact completed tasks |

## Work Directory

`.millstone/` in target repo contains:
- `runs/` - Timestamped logs of each run
- `evals/` - JSON eval results for trend analysis
- `cycles/` - Logs of autonomous cycle decisions
- `state.json` - Saved state for --continue recovery
- `config.toml` - Per-repo configuration
- `STOP.md` - Created by sanity check to halt on problems

## Configuration

Defaults in `DEFAULT_CONFIG` dict, overridden by `.millstone/config.toml`, then CLI args.

Key config options:
- `max_cycles`, `loc_threshold`, `tasklist`, `max_tasks`
- `eval_on_commit` - Run tests after each commit
- `eval_scripts` - Custom scripts to run during eval
- `approve_opportunities`, `approve_designs`, `approve_plans` - Human-in-loop gates
- `review_designs` - Auto-review designs before planning
- `cli` - Default CLI tool (`claude`, `codex`, or `gemini`)
- `cli_builder`, `cli_reviewer`, `cli_sanity`, `cli_analyzer` - Per-role CLI overrides

## Testing

Tests are in `tests/` using pytest (~300 tests). Key fixtures in `conftest.py`:
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
