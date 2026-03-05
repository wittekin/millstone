# Artifact Providers

millstone stores opportunities, designs, and tasklists as **artifacts**. The recommended way to connect a remote backend is via **MCP** — your coding agent already has MCP servers configured and authenticated, so there is zero extra auth plumbing in millstone.

## Integration path

### MCP (recommended)

millstone delegates all artifact operations to your coding agent (Claude Code, Codex, etc.) via a prompt that instructs it to use its own configured MCP server. Both reads and writes go through the agent callback. No credentials are stored in millstone — auth lives entirely in the agent's MCP configuration.

→ [MCP provider setup](mcp.md)

## Provider types

| Artifact | Purpose |
|----------|---------|
| **Opportunity** | Improvement opportunities surfaced by `--analyze` |
| **Design** | Implementation specs created by `--design` |
| **Tasklist** | Task queue consumed by the inner build-review loop |

## Configuration

Set provider backends and their options in `.millstone/config.toml`:

```toml
[millstone]
tasklist_provider    = "mcp"          # recommended for remote backends
design_provider      = "mcp"          # recommended for remote backends
opportunity_provider = "file"         # no remote backend needed for most workflows

[millstone.artifacts.tasklist_provider_options]
mcp_server = "linear"                 # MCP server name as configured in your agent

[millstone.design_provider_options]
mcp_server = "notion"
```

Credentials are managed entirely by the agent's MCP configuration — never stored in millstone.

## Available backends

### Tasklist & Opportunity

| Backend | Description | Guide |
|---------|-------------|-------|
| `mcp` | Agent MCP tools for all reads and writes | [MCP](mcp.md) |
| `file` | Local markdown files | — |

### Design

| Backend | Description | Guide |
|---------|-------------|-------|
| `mcp` | Agent MCP tools for all reads and writes | [MCP](mcp.md) |
| `file` | Local `designs/*.md` files | — |

## Narrowing the working task set

By default the MCP provider instructs the agent to fetch all open items from the configured service. Add a `[millstone.artifacts.tasklist_filter]` section to `.millstone/config.toml` to restrict millstone to a subset — the filter keys are forwarded to the agent as part of the read instruction:

```toml
[millstone.artifacts.tasklist_filter]
label    = "sprint-1"    # single-value shortcut
assignee = "john.doe"    # passed to the agent prompt
```

| Key | Type | Description |
|-----|------|-------------|
| `label` / `labels` | string / list | Restrict to items tagged with these labels |
| `assignee` / `assignees` | string / list | Restrict to items assigned to these users |
| `status` / `statuses` | string / list | Restrict by workflow status name |
| `milestone` | string | Restrict to items in this milestone (when supported) |
| `cycles` | list | Restrict to items in these cycle names (when supported) |
| `projects` | list | Restrict to items in these project names (when supported) |

Single-value shortcuts (`label`, `assignee`, `status`) expand to their list equivalents. Filter keys not supported by the active MCP server may be silently ignored by the agent.

See [MCP provider](mcp.md) for the full filter reference.
