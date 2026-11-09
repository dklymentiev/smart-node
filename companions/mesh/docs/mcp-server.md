# MCP Server

Mesh includes an [MCP](https://modelcontextprotocol.io/) server that lets AI assistants (Claude Desktop, Cursor, etc.) read and write to your document memory.

## Setup with Claude Desktop

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "mesh": {
      "command": "python",
      "args": ["/path/to/mesh/mcp_server.py"],
      "env": {
        "MESH_API_URL": "http://localhost:8000"
      }
    }
  }
}
```

If authentication is enabled on your Mesh instance:

```json
{
  "mcpServers": {
    "mesh": {
      "command": "python",
      "args": ["/path/to/mesh/mcp_server.py"],
      "env": {
        "MESH_API_URL": "http://localhost:8000",
        "MESH_API_KEY": "your-api-key"
      }
    }
  }
}
```

## Setup with Claude Code

Add to `.claude/settings.json`:

```json
{
  "mcpServers": {
    "mesh": {
      "command": "python",
      "args": ["/path/to/mesh/mcp_server.py"],
      "env": {
        "MESH_API_URL": "http://localhost:8000"
      }
    }
  }
}
```

## Running standalone

```bash
# stdio transport (default, for Claude Desktop)
python mcp_server.py

# SSE transport on port 8100
python mcp_server.py --transport sse

# HTTP transport
python mcp_server.py --transport http --port 8100
```

## Available tools

Once connected, your AI assistant gets these tools:

| Tool | Description |
|------|-------------|
| `mesh_search` | Semantic search across all documents |
| `mesh_add` | Save a new document |
| `mesh_update` | Update document content or tags |
| `mesh_delete` | Delete a document |
| `mesh_get` | Retrieve a document by GUID |
| `mesh_bytag` | Find documents by exact tags |
| `mesh_versions` | Find version chain of a document |
| `mesh_projects` | List all projects with document counts |
| `mesh_stats` | Memory statistics |
| `mesh_recent` | Get recent documents |
| `mesh_tags` | List all tags with counts |
| `mesh_schema` | Show tag schema configuration |

## Available resources

| URI | Description |
|-----|-------------|
| `mesh://doc/{guid}` | Read any document as a markdown resource |

## Example conversation

After connecting Mesh to Claude Desktop:

> **You:** What decisions have we made about the database?
>
> **Claude:** *(uses mesh_search)* Found 3 decisions about databases:
> 1. Decided to use PostgreSQL for the project (Feb 21)
> 2. Chose pgvector over Pinecone for embeddings (Feb 18)
> 3. Set connection pool max to 10 (Feb 15)

> **You:** Save a note that we need to add connection timeout config
>
> **Claude:** *(uses mesh_add)* Saved: doc_a1b2c3d4

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MESH_API_URL` | `http://localhost:8000` | Mesh API base URL |
| `MESH_API_KEY` | empty | API key (if AUTH_REQUIRED=true on server) |
