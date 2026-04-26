# Codex CLI (`codex`)

[Codex CLI](https://github.com/openai/codex) is OpenAI's agentic coding CLI.

## Install

```bash
npm install -g @openai/codex
```

## Authenticate

```bash
export OPENAI_API_KEY=sk-...
```

Codex reads `OPENAI_API_KEY` from the environment. Add it to your shell profile or a `.env` file.

## Configure in millstone

```toml
cli = "codex"
codex_yolo = true  # optional: pass --yolo to codex
```

Or target a specific role — for example, using Codex only as the reviewer:

```toml
cli          = "claude"
cli_reviewer = "codex"
```

You can also opt into `--yolo` from the CLI when needed:

```bash
millstone --cli codex --codex-yolo
```

## Notes

- millstone invokes `codex exec -` by default. Add `--codex-yolo` or set `codex_yolo = true` to pass Codex `--yolo` explicitly.
- The prompt is delivered via stdin to avoid OS argument-size limits on large prompts.
- Session resume uses `codex exec resume <session_id>`.
- Structured output (reviewer and sanity check roles) uses `--output-schema <path>` with a JSON schema file written to `.millstone/`.

## Further reading

- [Codex CLI repository](https://github.com/openai/codex)
- [Configuration reference](https://github.com/openai/codex/blob/main/docs/config.md)
- [MCP server setup](https://github.com/openai/codex/blob/main/docs/config.md)
