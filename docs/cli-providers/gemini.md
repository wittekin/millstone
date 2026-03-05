# Gemini CLI (`gemini`)

[Gemini CLI](https://github.com/google-gemini/gemini-cli) is Google's agentic coding CLI, backed by Gemini models.

## Install

```bash
npm install -g @google/gemini-cli
```

## Authenticate

```bash
gemini auth
```

Or set an API key directly:

```bash
export GEMINI_API_KEY=...
```

## Configure in millstone

```toml
[millstone]
cli = "gemini"
```

Or target a specific role:

```toml
[millstone]
cli          = "claude"
cli_analyzer = "gemini"
```

## Notes

- millstone invokes `gemini -y -o json <prompt>`, which runs Gemini CLI non-interactively (`-y` skips confirmations) and requests JSON-formatted output for safer parsing.
- Gemini CLI has a free tier with per-minute rate limits. millstone automatically retries on `model_capacity_exhausted`, `resource_exhausted` (429), and `service_unavailable` (503) errors, with exponential backoff up to 4 attempts.
- Structured output appends a JSON schema instruction to the prompt, since Gemini CLI does not natively support `--json-schema`.

## Further reading

- [Gemini CLI repository](https://github.com/google-gemini/gemini-cli)
- [MCP server setup](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md)
