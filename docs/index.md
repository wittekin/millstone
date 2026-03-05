# millstone

`millstone` is a Python CLI that orchestrates agentic coding tools in a deterministic builder-reviewer loop.

## What it does

- Runs tasks from a tasklist through build, sanity checks, and review.
- Supports autonomous outer loops (`analyze`, `design`, `plan`, `cycle`).
- Captures run logs and evaluation artifacts under `.millstone/`.

## Install

```bash
pipx install millstone
```

## Quick start

```bash
millstone --help
millstone
millstone --task "add input validation"
```

## Operator Prompts

Paste one of these as a system prompt to let an AI agent drive millstone for you:

- [execute.md](prompts/execute.md) — run a tasklist to completion
- [design.md](prompts/design.md) — turn an idea into a reviewed design + plan

→ [Which prompt to use and how to invoke them](prompts/index.md)

## Learn more

- [Getting Started](getting-started.md)
- [Parallel Execution (Worktrees)](worktrees.md)
- [CLI Providers](cli-providers/index.md) — Claude Code, Codex, Gemini, OpenCode
- [Artifact Providers](providers/index.md) — GitHub, Jira, Linear, Confluence, Notion, MCP
- [Loop Ontology](architecture/ontology.md)
- [Scope and Boundaries](architecture/scope.md)
- [Contributing](maintainer/contributing.md)
- [Maintainers](maintainer/maintainers.md)
