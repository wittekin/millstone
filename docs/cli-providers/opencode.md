# OpenCode (`opencode`)

[OpenCode](https://opencode.ai) is an open-source agentic coding CLI by Anomaly that supports multiple model providers (OpenAI, Anthropic, Google, and others) via a unified interface.

## Install

```bash
npm install -g @opencode/cli
```

## Authenticate

OpenCode connects to whichever model provider you configure. Set the relevant API key:

```bash
# OpenAI models
export OPENAI_API_KEY=sk-...

# Anthropic models
export ANTHROPIC_API_KEY=sk-ant-...

# Google models
export GOOGLE_API_KEY=...
```

Or configure a provider in `~/.config/opencode/opencode.jsonc`. See the [OpenCode docs](https://opencode.ai/docs) for the full list of supported providers.

## Configure in millstone

```toml
[millstone]
cli = "opencode"
```

Or target a specific role:

```toml
[millstone]
cli          = "claude"
cli_reviewer = "opencode"
```

## Notes

- millstone invokes `opencode run <prompt> --format json -m <model>`, which runs OpenCode non-interactively and requests newline-delimited JSON event output for parsing.
- The default model is `opencode/trinity-large-preview-free`. Pass `--model` to millstone to override: `millstone --model openai/gpt-5`.
- Structured output appends a JSON schema instruction to the prompt, since OpenCode does not natively support `--json-schema`.
- Session resume uses `opencode run --session <session_id> <follow_up_prompt>`.

## Further reading

- [OpenCode docs](https://opencode.ai/docs)
- [MCP server setup](https://opencode.ai/docs/mcp-servers)
