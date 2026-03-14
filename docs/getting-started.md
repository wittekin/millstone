# Getting Started

## Prerequisites

- Python 3.10+
- At least one supported CLI provider installed and authenticated. See [CLI Providers](cli-providers/index.md) for setup instructions for each.

## Installation

```bash
pipx install millstone
```

For local development:

```bash
pip install -e .[dev]
```

For minimal installs by task:

```bash
# Tests only
pip install -e .[test]

# Lint/type checks only
pip install -e .[quality]

# Dependency security audit only
pip install -e .[security]

# Packaging/release checks only
pip install -e .[release]
```

## Basic usage

The most common workflow: write a short list of features and let millstone design, plan, and implement each one.

```markdown
<!-- docs/roadmap.md -->
- [ ] Add a logout button to the header
- [ ] Show toast notifications on form errors
- [ ] Rate-limit the /api/search endpoint
```

```bash
# Local roadmap file
millstone --cycle --roadmap docs/roadmap.md

# Remote issue tracker (GitHub Issues, Linear, Jira) — configure MCP backend
# in .millstone/config.toml, then create issues with your tracker's UI
millstone --cycle
```

For each goal, millstone designs a solution, breaks it into atomic tasks, and implements them through a build-review loop. Approval gates pause between stages; add `--no-approve` for fully autonomous operation.

Other starting points:

```bash
# One task now (no setup required)
millstone --task "add retry logic"

# Design, plan, and execute one objective end-to-end
millstone --deliver "Add retry logic"

# Full autonomous loop — analyze codebase for improvements, then implement
millstone --cycle

# New app / fresh repo
millstone --init
millstone --deliver "Build a CLI app for release note generation"
```

Roadmap-driven flow (no analyze step):

```bash
millstone --cycle --roadmap docs/roadmap.md
```

Partial pipelines — `--through` controls how far `--analyze`, `--design`, or `--plan` chains forward:

```bash
millstone --analyze --through plan              # Analyze → design → plan, stop
millstone --design "Add caching" --through execute  # Design → plan → execute
```

Run from tasklist directly:

```bash
millstone
```

`millstone` and `millstone -n 1` read tasks from the configured tasklist path
(default: `.millstone/tasklist.md`).

Explore all options:

```bash
millstone --help
```

## Scoping remote backlogs

When using a remote tasklist provider millstone delegates reads and writes to the agent's configured MCP server. Add a `[millstone.artifacts.tasklist_filter]` section to `.millstone/config.toml` to restrict execution to a specific subset — filter keys are forwarded to the agent as part of the read instruction.

**Local tasklist vs remote filters**

- **Local `.millstone/tasklist.md`** — best for personal projects, solo maintainers, or when
  you want explicit control over execution order.
- **MCP provider + filter** — best for teams whose backlog lives in a remote service and who
  want millstone to pull work from the shared board automatically.

**Quick example**

MCP with Linear — current cycle, `millstone` label:
```toml
[millstone]
tasklist_provider = "mcp"

[millstone.artifacts.tasklist_provider_options]
mcp_server = "linear"

[millstone.artifacts.tasklist_filter]
cycles = ["Cycle 5"]
label  = "millstone"
```

Omit any key to skip filtering on that dimension. See [docs/providers/mcp.md](providers/mcp.md) for
the full filter reference.

## Working directory

`millstone` creates `.millstone/` in your target repository for state, logs, and evaluations.
