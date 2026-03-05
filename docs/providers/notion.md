# Notion provider

Stores design artifacts as pages in a Notion database via the `mcp` backend.

---

## Configuration

Configure the `mcp` backend with your Notion MCP server. All reads and writes are delegated to the agent — no Notion credentials are needed in millstone:

```toml
[millstone]
design_provider = "mcp"

[millstone.design_provider_options]
mcp_server = "notion"
```

See [MCP provider](mcp.md) for agent-side setup.

---

## How designs map to pages

Each `Design` is created as a database page. The full markdown content is stored as a single **code block** in the page body, preserving formatting without requiring Notion block-by-block conversion.

| millstone field | Notion field |
|----------------|-------------|
| `design_id` | `design_id` property (rich text) |
| `title` | `Name` property (title) |
| `status` | `status` property |
| `body` | Page content (code block) |

**API version**: `Notion-Version: 2022-06-28` (set automatically).
