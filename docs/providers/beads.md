# Beads provider

Backs millstone's tasklist and opportunity artifacts with the [beads](https://github.com/gastownhall/beads) graph issue tracker. Unlike the MCP-based providers (Jira, Linear, GitHub), beads is invoked **directly via subprocess** — no LLM/MCP round-trip, no token cost, deterministic latency.

---

## Requirements

- The `bd` CLI on `$PATH` (or set `bd_path` explicitly — see below).
- A beads-initialized working tree (`bd init`).

---

## Configuration

```toml
[millstone]
tasklist_provider    = "beads"
opportunity_provider = "beads"

[millstone.tasklist_provider_options]
label   = "millstone-task"          # optional; recommended to namespace artifacts
bd_path = "/usr/local/bin/bd"        # optional; default uses $PATH lookup

[millstone.opportunity_provider_options]
label   = "millstone-opportunity"    # default if omitted
```

The `label` option scopes each provider to its own slice of the beads repo. Tasks and opportunities can therefore coexist in a single beads database without overlapping reads.

---

## Identity model

Beads assigns hash-based IDs (`bd-a1b2`) at creation time. When you call `append_tasks(...)` or `write_opportunity(...)`, the provider mutates the artifact's id field in place to the canonical bd id. Subsequent reads, updates, and links reference that id.

---

## Status mapping

| millstone `TaskStatus` | beads status |
|---|---|
| `todo` | `open` |
| `in_progress` | `in_progress` |
| `done` | `closed` (set via `bd close`) |
| `blocked` | `blocked` |

| millstone `OpportunityStatus` | beads status |
|---|---|
| `identified` | `open` |
| `adopted` | `in_progress` |
| `rejected` | `closed` (set via `bd close`) |

---

## Dependency linking

Both providers implement the optional `DependencyLinker` capability protocol:

```python
provider.link(from_id, to_id, DependencyKind.blocks)
```

Dependency kinds map to `bd dep add ... --type <kind>`:

| `DependencyKind` | bd type |
|---|---|
| `blocks` | `blocks` |
| `related` | `related` |
| `parent_child` | `parent-child` |
| `discovered_from` | `discovered-from` |

When `append_tasks(...)` receives a task with `opportunity_ref` set, the provider automatically runs:

```
bd dep add <new-task-id> <opportunity-id> --type discovered-from
```

so the build-from-opportunity relationship is preserved in the beads graph. Link failures are logged but don't fail the append.

---

## Ready-task discovery

`BeadsTasklistProvider` implements `ReadyAwareTasklistProvider`:

```python
provider.list_ready_tasks()  # → bd ready --json
```

This returns only unblocked tasks (the dependency graph is consulted server-side by beads). Callers can feature-detect with `isinstance(provider, ReadyAwareTasklistProvider)` and fall back to `list_tasks()` for providers that don't implement it.

---

## Snapshot / restore

Snapshots store the set of current task ids; restore deletes any ids added since (`bd delete <id>`). Status changes and content edits made to pre-existing tasks are not reverted — beads' own history is the source of truth for those. This mirrors the scoped-rollback semantics used by the MCP provider.
