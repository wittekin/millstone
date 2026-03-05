# Provider Flow: Config to Prompt to Agent to MCP

This document traces the runtime path from configuration through provider instantiation, prompt preparation, agent invocation, and — for MCP-backed providers — how the coding agent drives remote state operations via its own MCP tools.

Three sequence diagrams are provided in order of increasing complexity.

---

## 1. Initialization

How a provider backend is selected from config and instantiated before any work begins.

```mermaid
sequenceDiagram
    participant CLI as millstone CLI
    participant Orch as Orchestrator
    participant OLM as OuterLoopManager
    participant Reg as Registry
    participant Prov as Provider

    CLI->>Orch: run()
    Orch->>OLM: __init__(provider_config)

    OLM->>Reg: get_tasklist_provider("mcp", options)
    Note over Reg: Lookup registered factory<br/>by backend string
    Reg->>Prov: MCPTasklistProvider.from_config(options)
    Note over Prov: stores mcp_server, labels, projects<br/>(resolved from top-level or tasklist_filter config)<br/>_agent_callback = None (not yet set)
    Prov-->>OLM: provider instance

    Note over OLM,Prov: File and MCP providers share the same<br/>TasklistProvider interface. The callback<br/>is injected later, at loop entry points.
```

**Key points:**

- `config.toml` sets `tasklist_provider = "mcp"`. The backend string is looked up in a module-level registry populated by self-registering imports.
- Label and project filters are read from `options` with a two-level precedence: explicit top-level `labels`/`projects` keys win, then nested `filter.labels`/`filter.projects` (populated from `[millstone.artifacts.tasklist_filter]` config), then `label`/`project` shortcut keys. Key-presence checks (not truthiness) ensure explicit empty lists are respected and not fallen through.
- For MCP providers, `_agent_callback` is intentionally `None` at construction time. It is injected just before the provider is first used (see Diagram 3), not at startup, so the same provider instance can be reused across multiple agent sessions.
- The three provider domains (opportunity, design, tasklist) follow identical patterns.

---

## 2. Inner Loop: Task Execution

How a single task is dispatched to the coding agent, with provider-specific storage instructions embedded in the prompt.

```mermaid
sequenceDiagram
    participant Orch as Orchestrator
    participant Prov as Provider
    participant Utils as prompts/utils.py
    participant Agent as coding agent
    participant FS as File System
    participant MCP as MCP Server

    Orch->>Orch: load_prompt("tasklist_prompt.md")
    Note over Orch: Template contains<br/>{{WORKING_DIRECTORY}}<br/>{{TASKLIST_READ_INSTRUCTIONS}}<br/>{{TASKLIST_COMPLETE_INSTRUCTIONS}}

    rect rgb(240, 248, 255)
        Note over Orch,Utils: Substitution order (get_tasklist_prompt)
        Orch->>Orch: 1. {{WORKING_DIRECTORY}} → str(repo_dir)
        Orch->>Prov: 2. get_prompt_placeholders()
        Prov-->>Orch: {"TASKLIST_READ_INSTRUCTIONS": "...", ...}
        Orch->>Utils: apply_provider_placeholders(prompt, placeholders)
        Utils-->>Orch: prompt with provider tokens resolved
        Orch->>Orch: 3. {{TASKLIST_PATH}} compat fallback<br/>(for custom --prompts-dir templates)
    end

    Orch->>Agent: run_agent(rendered_prompt)

    alt File provider
        Note over Agent: Prompt says:<br/>"Read tasks from `.millstone/tasklist.md`.<br/>Mark exactly this one task - [ ] → - [x]."
        Agent->>FS: read .millstone/tasklist.md
        Agent->>FS: write checkbox update
    else MCP provider
        Note over Agent: Prompt says:<br/>"Use the linear MCP to list all tasks<br/>with label 'sprint-5'. Pick first pending item.<br/>Use the linear MCP to mark it done."
        Agent->>MCP: list_issues(label="sprint-5")
        MCP-->>Agent: [{id, title, status: "todo"}, ...]
        Agent->>MCP: update_issue(id, status="done")
    end

    Agent-->>Orch: output (git diff produced by agent)
```

**Key points:**

- `run_agent()` selects the CLI tool (Claude Code, Codex, Gemini, etc.) per role. The diagram shows a generic "coding agent."
- Provider placeholder substitution runs before the compat `{{TASKLIST_PATH}}` replacement. Provider values contain free-form natural language and must not shadow static tokens; the compat replacement is a string literal and is safe to apply last.
- `apply_provider_placeholders` only touches tokens whose keys appear in the provider dict. All other `{{...}}` tokens pass through untouched.
- **MCP note:** The coding agent is the MCP client. Millstone has no MCP connection or API keys. It writes the instruction; the agent invokes the MCP tool. In practice only Claude Code supports MCP tools; other CLIs will receive the instruction as plain text.
- **`{{WORKING_DIRECTORY}}`** is substituted as the first step in `get_tasklist_prompt()`, before provider placeholders. This matches the behavior of `get_review_prompt()`.

---

## 3. Outer Loop: Planning with MCP Callback Injection

The planning loop has two distinct agent invocations:

1. **Plan generation** — the planner prompt is sent directly via `run_agent`. The instruction `{{TASKLIST_APPEND_INSTRUCTIONS}}` tells the agent to create new tasks via MCP tool calls itself. Millstone does not call `append_tasks()` here.
2. **Snapshot reads and rollback** — the provider's `get_snapshot()` and `restore_snapshot()` methods need to query/mutate remote state. They do this through a stored callback (`_agent_callback`) that routes back through the same `run_agent` function.

```mermaid
sequenceDiagram
    participant Orch as Orchestrator
    participant OLM as OuterLoopManager
    participant Prov as MCPTasklistProvider
    participant Agent as coding agent (Claude Code)
    participant MCP as MCP Server

    Orch->>OLM: run_plan(design_path,<br/>run_agent_callback=self.run_agent)

    OLM->>Prov: set_agent_callback(run_agent)
    Note over Prov: _agent_callback = Orchestrator.run_agent<br/>Provider can now invoke the agent for reads/rollback

    rect rgb(245, 245, 220)
        Note over OLM,MCP: Snapshot: capture current task IDs before planning
        OLM->>Prov: get_snapshot()
        Prov->>Agent: _agent_callback("Use linear MCP to list ALL tasks<br/>in all states. Output JSON array.")
        Agent->>MCP: list_issues(state=["todo","done","in_progress","blocked"])
        MCP-->>Agent: [{id, title, status}, ...]
        Agent-->>Prov: JSON
        Prov->>Prov: parse JSON → store _snapshot_task_ids
        Prov-->>OLM: markdown tasklist
    end

    rect rgb(240, 255, 240)
        Note over OLM,MCP: Planning: planner agent creates tasks via MCP directly
        OLM->>OLM: build plan_prompt<br/>static subs → apply_provider_placeholders<br/>→ {{TASKLIST_PATH}} compat fallback<br/>({{TASKLIST_APPEND_INSTRUCTIONS}} →<br/>"Use the linear MCP to create new tasks…")
        OLM->>Agent: run_agent(plan_prompt)
        Note over Agent: Agent follows the instruction in the prompt<br/>and calls MCP tools directly
        Agent->>MCP: create_issue(title, description, label)
        Agent-->>OLM: plan output
        OLM->>Prov: invalidate_cache()
    end

    alt Plan approved
        OLM->>Prov: get_snapshot() [verify additions]
        Prov->>Agent: _agent_callback("list ALL tasks…")
        Agent->>MCP: list_issues(all_states)
        MCP-->>Agent: updated list
        Agent-->>Prov: JSON
    else Plan rejected → rollback
        OLM->>Prov: restore_snapshot(content)
        Prov->>Prov: list_tasks() → find extra_tasks<br/>(IDs not in _snapshot_task_ids)
        Prov->>Agent: _agent_callback("Use linear MCP to delete<br/>tasks added in error: [ids]")
        Agent->>MCP: delete_issue(id) × n
        Prov->>Prov: invalidate_cache()
    end
```

**Key points:**

- **Callback injection is late-bound.** `_inject_agent_callbacks` is called at the start of `run_analyze`, `run_design`, `run_plan`, `review_design`, and `review_plan` — not at startup. This covers direct CLI invocation of review methods (e.g. `--review-design`) that do not pass through the outer loop.
- **Task creation is prompt-driven, not API-driven.** `run_agent(plan_prompt)` is called with an instruction that tells the agent to create tasks via MCP. The provider's `append_tasks()` method is not called in this path. Millstone's role is prompt construction and snapshot management.
- **Two distinct uses of `run_agent`.** The outer-loop prompt is sent by `OLM → run_agent`. Provider reads and rollback go through `Prov → _agent_callback`, which is the same `run_agent` function stored by reference. Both spawn a coding agent subprocess; neither is "internal."
- **Rollback is scoped.** `restore_snapshot` only deletes tasks whose IDs were not present at snapshot time. It does not restore status changes or content edits to pre-existing tasks — a known and accepted limitation documented on `MCPTasklistProvider.restore_snapshot`.
- **Effect policy gate.** Write operations dispatched by the provider (via `_agent_callback`) call `_apply_write_effect` first, allowing `C2_remote_bounded` enforcement (allowlist, idempotency key, rollback plan) before any remote state changes.

---

## Summary: What Millstone Controls vs. What the Agent Controls

| Concern | Millstone | Coding Agent |
|---|---|---|
| Provider backend selection | ✓ config + registry | — |
| Prompt template rendering | ✓ placeholder substitution | — |
| MCP server configuration | — | ✓ agent-local (e.g. `mcp_servers.json`) |
| MCP tool invocations | — | ✓ `linear:create_issue(...)` etc. |
| Policy gate (write effects) | ✓ `EffectIntent` enforcement | — |
| JSON parsing of agent output | ✓ `list_tasks`, `get_task` | — |
| Rollback (delete added tasks) | ✓ orchestrates via callback | ✓ executes MCP delete call |
