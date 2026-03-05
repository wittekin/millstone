# Confluence provider

Stores design artifacts as Confluence pages in a configured space via the `mcp` backend.

---

## Configuration

Configure the `mcp` backend with your Confluence MCP server. All reads and writes are delegated to the agent — no Confluence credentials are needed in millstone:

```toml
[millstone]
design_provider = "mcp"

[millstone.design_provider_options]
mcp_server = "confluence"
```

See [MCP provider](mcp.md) for agent-side setup.

---

## How designs map to pages

Each `Design` is stored as a Confluence page titled `design/{design_id}` in the configured space. The page body contains the full canonical markdown content wrapped in a `<pre>` block to preserve formatting.

| millstone field | Confluence field |
|----------------|----------------|
| `design_id` | Derived from page title (`design/{design_id}`) |
| Full design content | Page body (HTML storage format, markdown in `<pre>`) |
| `status` | Regex-updated metadata line in the page body |

**Creating vs. updating**

- First write: `POST /wiki/rest/api/content` (creates the page).
- Subsequent writes: `PUT /wiki/rest/api/content/{id}` with the version number incremented. The provider fetches the current version automatically before each update.
