# CLI Providers

millstone delegates all agent work — building, reviewing, sanity-checking, analyzing — to an external coding CLI. Each supported CLI is a drop-in: install it, authenticate it, point millstone at it.

## Supported CLIs

| Provider | Key | Vendor | Guide |
|----------|-----|--------|-------|
| Claude Code | `claude` | Anthropic | [claude.md](claude.md) |
| Codex CLI | `codex` | OpenAI | [codex.md](codex.md) |
| Gemini CLI | `gemini` | Google | [gemini.md](gemini.md) |
| OpenCode | `opencode` | Anomaly | [opencode.md](opencode.md) |

## Configuration

### Default CLI

Set the CLI used for all roles in `.millstone/config.toml`:

```toml
[millstone]
cli = "claude"   # default
```

Or pass it on the command line:

```bash
millstone --cli codex
```

### Per-role overrides

Different roles can use different CLIs. This is useful when one provider is better suited for review than build, or when you want to cross-check with a different model vendor:

```toml
[millstone]
cli          = "claude"    # default for any role not explicitly set
cli_builder  = "claude"    # implements tasks
cli_reviewer = "codex"     # reviews the builder's changes
cli_sanity   = "claude"    # sanity-checks builder and reviewer output
cli_analyzer = "gemini"    # runs --analyze outer loop
```

Or via CLI flags:

```bash
millstone --cli claude --cli-reviewer codex
```

Available role keys: `cli_builder`, `cli_reviewer`, `cli_sanity`, `cli_analyzer`, `cli_release_eng`, `cli_sre`.

### Model selection

Override the model for any invocation:

```bash
millstone --model claude-opus-4-5
```

Per-role model overrides are not yet supported via config — use the default model your CLI authenticates with, or pass `--model` when running millstone.
