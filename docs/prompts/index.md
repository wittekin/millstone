# Operator Prompts

These are system prompts for AI agents (Claude, Codex, etc.) to operate millstone on your behalf. Paste one as your system prompt, or `@`-mention the file in a coding agent conversation.

## Which prompt to use

| I want to… | Use |
|------------|-----|
| Run a tasklist to completion | [execute.md](execute.md) |
| Turn an idea into a design + plan | [design.md](design.md) |

**Typical workflow**: run `design.md` first to produce a tasklist, then hand off to `execute.md` to build it.

## How to invoke

**Claude Code**
```
@docs/prompts/execute.md
```

**Any coding agent (paste as system prompt)**
```bash
cat docs/prompts/execute.md
# paste the output as your system prompt
```

**Direct CLI**
```bash
# Let Claude drive millstone autonomously
claude --system-prompt "$(cat docs/prompts/execute.md)"
```

## Prompts

### [execute.md](execute.md) — Execution Operator
Runs `millstone` against `.millstone/tasklist.md`, verifying each task, intervening on failures, and looping until the list is empty or human input is required.

### [design.md](design.md) — Design Loop Operator
Takes an idea through research → design → review → plan, stopping before any code is built. Produces a reviewed design doc and an atomic tasklist ready for the execution operator.
