# GitHub provider

millstone integrates with GitHub via the `mcp` backend using two artifact types:

- **Tasklist / Opportunity** — backed by GitHub Issues
- **Design** — backed by GitHub Pages

---

## github-issues

Maps GitHub Issues to millstone tasks and opportunities. Each open issue becomes a `TasklistItem`; labels drive status and opportunity metadata.

### Configuration

Configure the `mcp` backend with the GitHub MCP server. All reads and writes are delegated to the agent — no GitHub credentials are needed in millstone:

```toml
[millstone]
tasklist_provider    = "mcp"
opportunity_provider = "mcp"   # optional

[millstone.artifacts.tasklist_provider_options]
mcp_server = "github"

[millstone.opportunity_provider_options]
mcp_server = "github"
```

See [MCP provider](mcp.md) for agent-side setup.

### Default scope and narrowing

**Default scope**: All open issues in the configured repository.

Use `[millstone.artifacts.tasklist_filter]` in `.millstone/config.toml` to narrow the working set without changing provider options. Labels, assignee, and milestone are forwarded to the GitHub API; status filtering is applied client-side.

#### Filter option reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `labels` | list of strings | `[]` | Return only issues that have **all** listed labels |
| `label` | string | `""` | Shortcut — equivalent to `labels = ["<value>"]` |
| `assignees` | list of strings (max 1) | `[]` | Return only issues assigned to the listed user |
| `assignee` | string | `""` | Shortcut — equivalent to `assignees = ["<value>"]` |
| `statuses` | list of strings | `[]` | Return only issues with matching state: `"open"` or `"closed"` |
| `status` | string | `""` | Shortcut — equivalent to `statuses = ["<value>"]` |
| `milestone` | string | — | Return only issues attached to this milestone name |

Constraints: `assignees` accepts at most one value; `statuses` values must be `"open"` or `"closed"`.

#### Narrowing recipes

**By label:**
```toml
[millstone.artifacts.tasklist_filter]
label = "millstone"
```

**By assignee:**
```toml
[millstone.artifacts.tasklist_filter]
assignee = "octocat"
```

**By milestone:**
```toml
[millstone.artifacts.tasklist_filter]
milestone = "v1.2"
```

**Multiple labels (all must match):**
```toml
[millstone.artifacts.tasklist_filter]
labels = ["backend", "sprint-1"]
```

**Include closed issues:**
```toml
[millstone.artifacts.tasklist_filter]
statuses = ["open", "closed"]
```

**Combining filters:**
```toml
[millstone.artifacts.tasklist_filter]
label     = "sprint-1"
assignee  = "octocat"
milestone = "v1.2"
```

All active filter keys are applied together (AND logic across keys).

### How artifacts map to issues

**Tasks**

| millstone field | GitHub field |
|----------------|-------------|
| `task_id` | Issue number (as string, e.g. `"42"`) |
| `title` | Issue title |
| `description` | Issue body |
| `status: todo` | Issue is open |
| `status: done` | Issue is closed |

**Opportunities**

Opportunities are issues labelled `millstone-opportunity`. Status is tracked via additional labels:

| millstone status | GitHub label |
|-----------------|-------------|
| `identified` | `millstone-opportunity` (no status label) |
| `adopted` | `millstone-adopted` |
| `rejected` | `millstone-rejected` |

Metadata fields (`opportunity_ref`, `roi`, `risk`) are stored as label prefixes on the issue (e.g. `roi:high`, `risk:low`).

---

## github-pages

Publishes design artifacts to a GitHub Pages branch (typically `gh-pages`). Each design becomes a file at `{path_prefix}{design_id}/index.md`.

### Configuration

```toml
[millstone]
design_provider = "mcp"

[millstone.design_provider_options]
mcp_server = "github"
```

See [MCP provider](mcp.md) for agent-side setup.

### How artifacts map to files

Each `Design` is stored at `{path_prefix}{design_id}/index.md` on the target branch, using the same canonical markdown format as the `file` backend — you can switch between them without data loss.

Writes use the GitHub Contents API (`PUT /repos/{owner}/{repo}/contents/{path}`). The SHA of the existing blob is fetched automatically on updates — no manual version tracking required.
