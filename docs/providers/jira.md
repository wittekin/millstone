# Jira provider

Maps Jira issues to millstone tasks and opportunities via the `mcp` backend.

---

## Configuration

Configure the `mcp` backend with your Jira MCP server. All reads and writes are delegated to the agent — no Jira credentials are needed in millstone:

```toml
[millstone]
tasklist_provider    = "mcp"
opportunity_provider = "mcp"   # optional

[millstone.artifacts.tasklist_provider_options]
mcp_server = "jira"

[millstone.opportunity_provider_options]
mcp_server = "jira"
```

See [MCP provider](mcp.md) for agent-side setup.

---

## Default scope and narrowing

**Default scope**: All non-Done issues in the configured `project` (JQL: `project = "PROJ" AND statusCategory != Done`).

Use `[millstone.artifacts.tasklist_filter]` in `.millstone/config.toml` to narrow the working set without changing provider options.

### Filter option reference

| Key | Type | Description |
|-----|------|-------------|
| `labels` | list of strings | Return only issues that have **all** listed labels |
| `label` | string | Shortcut — equivalent to `labels = ["<value>"]` |
| `assignees` | list of strings | Return only issues assigned to **any** listed user (account names) |
| `assignee` | string | Shortcut — equivalent to `assignees = ["<value>"]` |
| `statuses` | list of strings | Return only issues in **any** listed Jira status (exact name) |
| `status` | string | Shortcut — equivalent to `statuses = ["<value>"]` |

Single-value shortcuts (`label`, `assignee`, `status`) expand to their list equivalents. If both are set, the list form takes precedence.

Filters are applied as JQL clauses appended to the base query. Values must not contain quote characters.

### Narrowing recipes

**Sprint label:**
```toml
[millstone.artifacts.tasklist_filter]
label = "sprint-1"
```

**Specific assignee:**
```toml
[millstone.artifacts.tasklist_filter]
assignee = "john.doe"
```

**Multiple labels (all must match):**
```toml
[millstone.artifacts.tasklist_filter]
labels = ["backend", "sprint-1"]
```

**Status subset (any match):**
```toml
[millstone.artifacts.tasklist_filter]
statuses = ["In Progress", "In Review"]
```

**Combining filters:**
```toml
[millstone.artifacts.tasklist_filter]
label    = "sprint-1"
assignee = "john.doe"
statuses = ["To Do", "In Progress"]
```

Composing multiple filter keys produces a JQL `AND` across all active clauses.

---

## How artifacts map to issues

**Tasks**

| millstone field | Jira field |
|----------------|-----------|
| `task_id` | Issue key (e.g. `"PROJ-42"`) |
| `title` | Summary |
| `description` | Description (plain text) |
| `status: todo` | Status category ≠ Done |
| `status: done` | Status category = Done |

Completing a task (`update_task_status(id, done)`) triggers the configured `done_transition_id`.

**Opportunities**

Opportunities are issues labelled `millstone-opportunity`. Additional labels drive status:

| millstone status | Jira label |
|-----------------|-----------|
| `identified` | `millstone-opportunity` |
| `adopted` | `adopted` |
| `rejected` | `rejected` |
