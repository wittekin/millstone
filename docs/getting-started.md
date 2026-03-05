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

Run tasks from `.millstone/tasklist.md`:

```bash
millstone
```

Run a one-off task:

```bash
millstone --task "add retry logic"
```

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
