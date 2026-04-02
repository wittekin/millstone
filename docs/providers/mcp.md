# MCP provider

The recommended integration path for interactive sessions. All reads and writes are delegated to your coding agent's configured MCP servers — no service credentials needed in millstone. Auth lives entirely in the agent's MCP configuration.

| Backend | Artifact type(s) | Module |
|---------|-----------------|--------|
| `mcp` | Tasklist, Design | `millstone.artifact_providers.mcp` |

---

## How it works

The MCP provider delegates all artifact operations to the agent via a self-contained prompt. The agent executes reads and writes using its own MCP credentials — millstone never touches the service API directly.

```
millstone run_plan
        │
        ├── list_tasks()          →  agent callback
        │   get_task()            │
        │                         └── "Use your linear MCP server to list
        │                              tasks: ..."
        │                              agent calls MCP tool → Linear API
        │
        └── append_tasks()        →  agent callback
            update_task_status()  │
                                  └── "Use your linear MCP server to create
                                       these issues: ..."
                                       agent calls MCP tool → Linear API
```

All operations — reads included — are handled through the agent callback. millstone constructs a self-contained instruction for each operation and hands it to the agent, which resolves it using its configured MCP server.

---

## Prerequisites

Configure MCP servers in your coding agent before running millstone. Refer to your agent's documentation:

| Agent | MCP setup guide |
|-------|----------------|
| Claude Code (`claude`) | [code.claude.com/docs/en/mcp](https://code.claude.com/docs/en/mcp) |
| Codex (`codex`) | [github.com/openai/codex — docs/config.md](https://github.com/openai/codex/blob/main/docs/config.md) |
| Gemini CLI (`gemini`) | [github.com/google-gemini/gemini-cli — docs/tools/mcp-server.md](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md) |
| OpenCode (`opencode`) | [opencode.ai/docs/mcp-servers](https://opencode.ai/docs/mcp-servers) |

No environment variables are required in millstone for MCP providers — credentials are managed entirely by the agent.

---

## Config

### Linear tasklist

```toml
[millstone]
tasklist_provider = "mcp"

[millstone.artifacts.tasklist_provider_options]
mcp_server = "linear"
```

### Jira tasklist

```toml
[millstone]
tasklist_provider = "mcp"

[millstone.artifacts.tasklist_provider_options]
mcp_server = "jira"
```

### Notion design

```toml
[millstone]
design_provider = "mcp"

[millstone.design_provider_options]
mcp_server = "notion"
```

### Linear tasklist with label filter

Use `[millstone.artifacts.tasklist_filter]` to narrow which tasks the agent fetches:

```toml
[millstone]
tasklist_provider = "mcp"

[millstone.artifacts.tasklist_provider_options]
mcp_server = "linear"

[millstone.artifacts.tasklist_filter]
label = "sprint-1"
```

---

## Option reference

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `mcp_server` | yes | — | Name of the MCP server as registered in your agent (passed verbatim to the operation prompt) |

Additional keys under `tasklist_provider_options` are forwarded verbatim to the agent instruction prompt, allowing you to pass service-specific context (for example `team_id`, `project`, `database_id`).

Task scope narrowing belongs in `[millstone.artifacts.tasklist_filter]`. Currently, only `label`/`labels` and `project`/`projects` are translated into MCP instruction clauses; other keys are accepted in config but have no effect on the generated instruction.

---

## Default scope and narrowing

**Default scope**: Determined by the MCP server's own defaults for the configured account/workspace — typically all open items. Use `[millstone.artifacts.tasklist_filter]` to narrow scope:

```toml
[millstone.artifacts.tasklist_filter]
label = "sprint-1"
```

Only `label`/`labels` and `project`/`projects` are translated into natural-language filter clauses that appear in the MCP instruction sent to the agent:

| Key | Description |
|-----|-------------|
| `label` / `labels` | Restrict to items with these labels |
| `project` / `projects` | Restrict to items in these projects |

Other keys (e.g. `assignee`, `status`, `milestone`) are accepted without error but are not currently included in the generated instruction and have no effect on task scope.

---

## Supported MCP servers

Any MCP server your agent supports works — the `mcp_server` value is passed through verbatim to the operation prompt. Commonly used servers:

| Service | `mcp_server` value | Reference |
|---------|-------------------|-----------|
| Linear | `"linear"` | [@linear/mcp](https://github.com/linear/linear-mcp) |
| Notion | `"notion"` | [@notionhq/notion-mcp-server](https://github.com/makenotion/notion-mcp-server) |
| Jira | `"jira"` | Atlassian MCP server |
| GitHub | `"github"` | [github/github-mcp-server](https://github.com/github/github-mcp-server) |
| Confluence | `"confluence"` | Atlassian MCP server |

---

## Effect policy

Millstone-classified MCP write operations are `EffectClass.transactional` (`C2_remote_bounded`). The default `dev_implementation` profile is `C1_local_write`, so direct provider writes that millstone issues itself are not permitted unless you configure a `C2_remote_bounded` profile with `transactional` effects allowlisted.

In the default profile, prompt-driven MCP actions performed by the coding agent can still run because the agent is executing those tool calls from the rendered prompt, not through millstone's direct provider-effect path. Orchestrator-driven follow-up writes, such as explicit post-commit remote task completion, are skipped unless the active profile allows those transactional effects.

---

## Limitations

- **No atomic rollback** — if an operation fails mid-way, the MCP provider cannot guarantee partial operations are undone. The agent's rollback prompt is best-effort.
- **Interactive sessions only** — MCP operations require an agent callback. millstone wires this automatically when running interactively; it will raise `RuntimeError` if an operation is attempted outside of an interactive session.
