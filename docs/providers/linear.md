# Linear provider

Maps Linear issues to millstone tasks and opportunities via the `mcp` backend.

---

## Configuration

Configure the `mcp` backend with your Linear MCP server. All reads and writes are delegated to the agent — no Linear credentials are needed in millstone:

```toml
[millstone]
tasklist_provider    = "mcp"
opportunity_provider = "mcp"   # optional

[millstone.artifacts.tasklist_provider_options]
mcp_server = "linear"

[millstone.opportunity_provider_options]
mcp_server = "linear"
```

See [MCP provider](mcp.md) for agent-side setup.

---

## Default scope and narrowing

**Default scope**: All non-completed issues in the configured team (GraphQL filter: `state.type != "completed"`).

Use `[millstone.artifacts.tasklist_filter]` in `.millstone/config.toml` to narrow the working set without changing provider options. Filters are applied client-side after fetching and use case-insensitive comparisons.

### Filter option reference

| Key | Type | Description |
|-----|------|-------------|
| `labels` | list of strings | Return only issues that have **all** listed labels (case-insensitive) |
| `label` | string | Shortcut — equivalent to `labels = ["<value>"]` |
| `statuses` | list of strings | Return only issues whose state name matches **any** listed value |
| `status` | string | Shortcut — equivalent to `statuses = ["<value>"]` |
| `cycles` | list of strings | Return only issues belonging to **any** listed cycle name |
| `projects` | list of strings | Return only issues belonging to **any** listed project name |

Single-value shortcuts (`label`, `status`) expand to their list equivalents. If both are set, the list form takes precedence.

### Narrowing recipes

**Current cycle:**
```toml
[millstone.artifacts.tasklist_filter]
cycles = ["Cycle 5"]
```

**Single label:**
```toml
[millstone.artifacts.tasklist_filter]
label = "millstone"
```

**Project subset:**
```toml
[millstone.artifacts.tasklist_filter]
projects = ["Backend", "API"]
```

**In-progress issues only:**
```toml
[millstone.artifacts.tasklist_filter]
status = "In Progress"
```

**Combining filters:**
```toml
[millstone.artifacts.tasklist_filter]
label    = "sprint-1"
cycles   = ["Cycle 5"]
statuses = ["Todo", "In Progress"]
```

All active filter keys are applied together (AND logic across keys; OR logic within a key's list).

---

## How artifacts map to issues

**Tasks**

| millstone field | Linear field |
|----------------|-------------|
| `task_id` | Issue UUID |
| `title` | Issue title |
| `description` | Issue description |
| `status: todo` | State type not `completed` |
| `status: done` | State type `completed` |

Marking a task done updates the issue's state to `done_state_id` via the `issueUpdate` mutation.

**Opportunities**

Opportunities are issues with the label `millstone-opportunity`. Status labels:

| millstone status | Linear label |
|-----------------|-------------|
| `identified` | `millstone-opportunity` |
| `adopted` | _(no extra label; determined by workflow state)_ |
| `rejected` | `millstone-rejected` |

Label IDs are resolved at runtime by name — no manual ID lookup required for labels.

## Pagination

All list operations use cursor-based pagination (`pageInfo.endCursor`) and fetch all pages automatically. Default page size is 50 issues per request.
