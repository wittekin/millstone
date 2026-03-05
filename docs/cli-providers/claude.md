# Claude Code (`claude`)

[Claude Code](https://code.claude.com) is Anthropic's agentic coding CLI. It is the default CLI provider in millstone.

## Install

```bash
npm install -g @anthropic-ai/claude-code
```

## Authenticate

```bash
claude login
```

Follow the browser prompt to connect your Anthropic account. Claude Code stores credentials in `~/.claude/`.

## Configure in millstone

```toml
[millstone]
cli = "claude"   # this is the default — no change needed
```

Or target a specific role:

```toml
[millstone]
cli_builder  = "claude"
cli_reviewer = "claude"
```

## Notes

- millstone invokes `claude -p <prompt> --dangerously-skip-permissions`, which runs Claude Code non-interactively and bypasses per-tool approval prompts.
- When millstone itself is running inside a Claude Code session, it strips the `CLAUDE_CODE_SSE_PORT` and related environment variables from the subprocess environment so the child agent runs independently rather than re-attaching to the parent session.
- Structured output (reviewer and sanity check roles) uses `--output-format json --json-schema <schema>`.

## Further reading

- [Claude Code docs](https://code.claude.com/docs)
- [MCP server setup](https://code.claude.com/docs/en/mcp)
