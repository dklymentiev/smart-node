#!/usr/bin/env python3
"""
MCP Server for Mesh — Semantic Memory API

Wraps Mesh HTTP API as MCP tools for Claude Desktop, Claude Code, and other MCP clients.
Tag schema is loaded from Mesh API (GET /schema) — single source of truth.

Usage:
    python mcp_server.py                              # stdio
    python mcp_server.py --transport sse              # SSE on port 8100

Environment:
    MESH_API_URL   — Mesh API base URL (default: http://localhost:8000)
    MESH_API_KEY   — API key if AUTH_REQUIRED=true
"""
import os
import sys

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

# ──────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────

MESH_URL = os.environ.get("MESH_API_URL", "http://localhost:8000").rstrip("/")
MESH_KEY = os.environ.get("MESH_API_KEY", "")
MESH_WORKSPACE = os.environ.get("MESH_WORKSPACE", "")  # default workspace for MCP

# Schema cache (loaded on first tool call)
_schema_cache: dict | None = None


def _headers(workspace: str | None = None) -> dict:
    h = {"Content-Type": "application/json"}
    if MESH_KEY:
        h["X-API-Key"] = MESH_KEY
    ws = workspace or MESH_WORKSPACE
    if ws:
        h["X-Workspace"] = ws
    return h


async def _get(path: str, params: dict = None, workspace: str | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{MESH_URL}{path}", headers=_headers(workspace), params=params)
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict, workspace: str | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{MESH_URL}{path}", headers=_headers(workspace), json=body)
        r.raise_for_status()
        return r.json()


async def _put(path: str, body: dict, workspace: str | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.put(f"{MESH_URL}{path}", headers=_headers(workspace), json=body)
        r.raise_for_status()
        return r.json()


async def _patch(path: str, body: dict, workspace: str | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.patch(f"{MESH_URL}{path}", headers=_headers(workspace), json=body)
        r.raise_for_status()
        return r.json()


async def _delete(path: str, workspace: str | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.delete(f"{MESH_URL}{path}", headers=_headers(workspace))
        r.raise_for_status()
        return r.json()


async def _get_schema() -> dict:
    """Get tag schema from Mesh API (cached)."""
    global _schema_cache
    if _schema_cache is None:
        try:
            _schema_cache = await _get("/schema")
        except Exception:
            _schema_cache = {"prefixes": {}, "defaults": {}, "auto_infer": {}}
    return _schema_cache


def _project_prefix(schema: dict) -> str | None:
    """Find the project tag prefix from schema."""
    for key, info in schema.get("prefixes", {}).items():
        if info.get("is_project"):
            return info.get("prefix")
    return None


def _schema_summary(schema: dict) -> str:
    """Generate human-readable schema summary for tool instructions."""
    prefixes = schema.get("prefixes", {})
    if not prefixes:
        return "No tag schema. Tags are free-form key:value pairs."
    lines = ["Tag schema:"]
    for key, info in prefixes.items():
        parts = [f"  {info['prefix']}  {info.get('description', key)}"]
        if info.get("values"):
            parts.append(f" [{', '.join(info['values'])}]")
        if info.get("is_project"):
            parts.append(" (PROJECT)")
        if info.get("auto_infer"):
            parts.append(" (auto-inferred)")
        lines.append("".join(parts))
    lines.append("  Auto-tags: date and source added on save. type/project/topic inferred from neighbors.")
    lines.append("  Custom tags always allowed.")
    return "\n".join(lines)


def _parse_tags(tags: list[str] | str | None) -> list[str] | None:
    """Accept tags as list or comma/space-separated string."""
    if tags is None:
        return None
    if isinstance(tags, str):
        return [t.strip() for t in tags.replace(",", " ").split() if t.strip()]
    return tags


# ──────────────────────────────────────────
# MCP Server
# ──────────────────────────────────────────

mcp = FastMCP(
    "Mesh Memory",
    instructions=(
        "Mesh is a semantic memory system for documents with auto-tagging. "
        "Documents get date/source tags automatically on save, and type/project/topic "
        "tags are inferred from similar existing documents after indexing. "
        "Use mesh_search for finding by meaning, mesh_bytag for exact tag filtering, "
        "mesh_versions for document evolution history, mesh_schema to see tag taxonomy."
    ),
    port=int(os.environ.get("MESH_MCP_PORT", "8100")),
    transport_security=TransportSecuritySettings(
        allowed_hosts=["mesh-mcp.local", "127.0.0.1", "127.0.0.1:8100", "localhost", "localhost:8100"],
    ),
)


# ──────────────────────────────────────────
# Tools
# ──────────────────────────────────────────

@mcp.tool()
async def mesh_focus(workspace: str, prefetch: bool = True, limit: int = 5) -> str:
    """Switch active workspace. All subsequent calls use this workspace by default.

    Optionally prefetches recent documents for immediate context.

    Args:
        workspace: Workspace name to switch to (e.g. "architecture", "default")
        prefetch: Load recent documents after switching (default True)
        limit: How many recent docs to prefetch (default 5)
    """
    global MESH_WORKSPACE
    previous = MESH_WORKSPACE or "(none)"
    MESH_WORKSPACE = workspace

    lines = [f"Workspace: {previous} -> {workspace}"]

    if prefetch:
        try:
            body = {"query": workspace, "limit": limit}
            resp = await _post("/search", body, workspace=workspace)
            docs = resp.get("results", []) if isinstance(resp, dict) else resp if isinstance(resp, list) else []
            if docs:
                lines.append(f"\nRecent {len(docs)} documents in '{workspace}':\n")
                for doc in docs:
                    guid = doc.get("guid", "?")
                    tags_str = ", ".join(doc.get("tags", []))
                    preview = (doc.get("content", ""))[:120]
                    created = doc.get("created_at", "")[:10] if doc.get("created_at") else ""
                    lines.append(f"**{guid}** ({created}) [{tags_str}]")
                    lines.append(f"  {preview}")
                    lines.append("")
            else:
                lines.append(f"\nWorkspace '{workspace}' is empty.")
        except Exception as e:
            lines.append(f"\nPrefetch failed: {e}")

    return "\n".join(lines)


@mcp.tool()
async def mesh_search(query: str, limit: int = 10, tags: list[str] | str | None = None,
                      workspaces: dict[str, float] | None = None,
                      workspace: str | None = None) -> str:
    """Semantic search across all documents in Mesh memory.

    Args:
        query: What to search for (natural language)
        limit: Max results (default 10)
        tags: Optional tag filter (AND logic). List or comma-separated string.
        workspaces: Weighted multi-workspace search, e.g. {"sysadmin": 0.7, "security": 0.2}
        workspace: Target workspace (uses default if not set). Ignored if workspaces is set.
    """
    tags = _parse_tags(tags)
    body = {"query": query, "limit": limit}
    if tags:
        body["tags"] = tags
    if workspaces:
        body["workspaces"] = workspaces
    data = await _post("/search", body, workspace=workspace)
    results = data.get("results", [])
    if not results:
        return f"No results found for: {query}"
    lines = [f"Found {len(results)} results for '{query}':\n"]
    for r in results:
        score = r.get("similarity_score", 0)
        guid = r.get("guid", "?")
        tags_str = ", ".join(r.get("tags", []))
        preview = (r.get("content", ""))[:200]
        created = r.get("created_at", "")[:10] if r.get("created_at") else ""
        lines.append(f"**{guid}** (score: {score}, date: {created})")
        lines.append(f"  tags: {tags_str}")
        lines.append(f"  {preview}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def mesh_add(content: str, tags: list[str] | str | None = None, source: str = "mcp",
                   workspace: str | None = None) -> str:
    """Add a new document to Mesh memory.

    Date and source tags are added automatically. After indexing, type/project/topic
    tags will be inferred from similar documents. You can set tags explicitly to
    override auto-inference.

    Args:
        content: Document text (min 10 chars)
        tags: Optional tags as list or comma-separated string. Auto-tags won't override these.
        source: Origin (default: "mcp")
        workspace: Target workspace (uses default if not set)
    """
    tags = _parse_tags(tags)
    body = {"content": content, "source": source}
    if tags:
        body["tags"] = tags
    data = await _put("/", body, workspace=workspace)
    guid = data.get("guid", "?")
    final_tags = data.get("tags", [])
    return (
        f"Document created: {guid}\n"
        f"Tags: {', '.join(final_tags)}\n"
        f"Queued for indexing. Additional tags may be auto-inferred from similar documents."
    )


@mcp.tool()
async def mesh_update(
    guid: str,
    content: str | None = None,
    tags: list[str] | str | None = None,
    add_tags: list[str] | str | None = None,
    remove_tags: list[str] | str | None = None,
    pinned: bool | None = None,
    workspace: str | None = None,
) -> str:
    """Update an existing document's content or tags.

    Args:
        guid: Document GUID (doc_XXXXXXXX)
        content: New content (replaces existing)
        tags: Replace all tags with these. List or comma-separated string.
        add_tags: Add tags to existing. List or comma-separated string.
        remove_tags: Remove tags from existing. List or comma-separated string.
        pinned: Pin document to top of workspace (True/False)
        workspace: Target workspace (uses default if not set)
    """
    tags = _parse_tags(tags)
    add_tags = _parse_tags(add_tags)
    remove_tags = _parse_tags(remove_tags)
    body = {}
    if content is not None:
        body["content"] = content
    if tags is not None:
        body["tags"] = tags
    if add_tags is not None:
        body["add_tags"] = add_tags
    if remove_tags is not None:
        body["remove_tags"] = remove_tags
    if pinned is not None:
        body["pinned"] = pinned
    if not body:
        return "Nothing to update. Provide content, tags, add_tags, remove_tags, or pinned."
    data = await _patch(f"/{guid}", body, workspace=workspace)
    updated = data.get("updated_fields", [])
    final_tags = data.get("tags", [])
    return (
        f"Updated {guid}\n"
        f"Changed: {', '.join(updated) if updated else 'ok'}\n"
        f"Tags: {', '.join(final_tags)}"
    )


@mcp.tool()
async def mesh_delete(guid: str, workspace: str | None = None) -> str:
    """Delete a document from Mesh memory.

    Args:
        guid: Document GUID to delete (doc_XXXXXXXX)
        workspace: Target workspace (uses default if not set)
    """
    data = await _delete(f"/{guid}", workspace=workspace)
    if data.get("deleted"):
        return f"Deleted: {guid}"
    return f"Failed to delete {guid}: {data}"


@mcp.tool()
async def mesh_get(guid: str, workspace: str | None = None) -> str:
    """Get a specific document by its GUID.

    Args:
        guid: Document GUID (format: doc_XXXXXXXX)
        workspace: Target workspace (uses default if not set)
    """
    data = await _get(f"/{guid}", workspace=workspace)
    tags_str = ", ".join(data.get("tags", []))
    created = data.get("created_at", "")[:19] if data.get("created_at") else ""
    return (
        f"**{data.get('guid', guid)}**\n"
        f"Created: {created}\n"
        f"Tags: {tags_str}\n"
        f"Source: {data.get('source', '?')}\n\n"
        f"{data.get('content', '')}"
    )


@mcp.tool()
async def mesh_bytag(tags: list[str] | str, limit: int = 10, offset: int = 0,
                     workspace: str | None = None) -> str:
    """List documents filtered by tags (AND logic).

    Args:
        tags: Tags to filter by. List or comma-separated string.
        limit: Max documents (default 10)
        offset: Skip N documents for pagination
        workspace: Target workspace (uses default if not set)
    """
    tags = _parse_tags(tags) or []
    params = {"limit": limit, "offset": offset}
    for t in tags:
        params.setdefault("tag", [])
        if isinstance(params["tag"], list):
            params["tag"].append(t)
    data = await _get("/", params, workspace=workspace)
    if isinstance(data, dict) and "service" in data:
        return "No documents found"
    if not data:
        return f"No documents found for tags: {', '.join(tags)}"
    lines = [f"Found {len(data)} documents:\n"]
    for doc in data:
        guid = doc.get("guid", "?")
        tags_str = ", ".join(doc.get("tags", []))
        preview = (doc.get("content", ""))[:150]
        created = doc.get("created_at", "")[:10] if doc.get("created_at") else ""
        lines.append(f"**{guid}** ({created})")
        lines.append(f"  tags: {tags_str}")
        lines.append(f"  {preview}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def mesh_versions(
    guid: str,
    threshold: float = 0.85,
    limit: int = 20,
    same_project: bool = True,
    workspace: str | None = None,
) -> str:
    """Find chronological version chain of a document.

    Args:
        guid: Source document GUID (doc_XXXXXXXX)
        threshold: Similarity threshold 0.5-0.99 (default 0.85)
        limit: Max versions (default 20)
        same_project: Only same project docs (default True)
        workspace: Target workspace (uses default if not set)
    """
    params = {
        "threshold": threshold,
        "limit": limit,
        "same_project": str(same_project).lower()
    }
    data = await _get(f"/versions/{guid}", params, workspace=workspace)
    source = data.get("source", {})
    versions = data.get("versions", [])
    count = data.get("count", 0)
    project = data.get("project_tag", "none")

    lines = [
        f"Version chain for {guid} (project: {project})",
        f"Source: {source.get('content_length', 0)} chars, created {source.get('created_at', '?')[:10]}",
        f"Found {count} related versions:\n"
    ]
    for v in versions:
        sim = v.get("similarity", 0)
        delta = v.get("length_delta", 0)
        delta_str = f"+{delta}" if delta > 0 else str(delta)
        created = v.get("created_at", "")[:10] if v.get("created_at") else "?"
        lines.append(f"  {created} | **{v['guid']}** | sim: {sim} | {delta_str} chars")
        if v.get("tags_added"):
            lines.append(f"    + tags: {', '.join(v['tags_added'])}")
        if v.get("tags_removed"):
            lines.append(f"    - tags: {', '.join(v['tags_removed'])}")
        lines.append(f"    {v.get('preview', '')[:120]}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def mesh_projects(limit: int = 30, workspace: str | None = None) -> str:
    """List all projects with document counts and activity dates."""
    schema = await _get_schema()
    prefix = _project_prefix(schema)
    if not prefix:
        return "No project prefix configured in schema."

    data = await _get("/activity", {"prefix": prefix, "limit": limit}, workspace=workspace)
    if not data:
        return "No projects found."

    lines = [f"Projects ({len(data)}):\n"]
    for item in data:
        tag = item.get("tag", "?")
        project_id = tag.replace(prefix, "") if tag.startswith(prefix) else tag
        docs = item.get("docs", 0)
        last = item.get("last", "?")[:10] if item.get("last") else "?"
        first = item.get("first", "?")[:10] if item.get("first") else "?"
        lines.append(f"  {project_id}  ({docs} docs, {first} -> {last})")
    return "\n".join(lines)


@mcp.tool()
async def mesh_stats(workspace: str | None = None) -> str:
    """Get Mesh memory statistics."""
    data = await _get("/stats", workspace=workspace)
    lines = [
        f"Documents: {data.get('documents', 0)}",
        f"Indexed: {data.get('indexed', 0)}",
        f"Pending: {data.get('pending', 0)}",
        f"Projects: {data.get('projects', 0)}",
        f"Tags: {data.get('tags', 0)}",
        f"Queue: {data.get('queue_size', 0)}",
        f"Last update: {data.get('last_update', '?')}",
    ]
    workspaces = data.get("workspaces", [])
    if workspaces:
        lines.append(f"\nWorkspaces ({len(workspaces)}):")
        for ws in workspaces:
            last = (ws.get("last_activity") or "?")[:10]
            lines.append(f"  {ws['workspace']}  ({ws['documents']} docs, last: {last})")
    return "\n".join(lines)


@mcp.tool()
async def mesh_recent(limit: int = 10, type: str | None = None,
                      workspace: str | None = None) -> str:
    """Get recent documents.

    Args:
        limit: Max documents (default 10)
        type: Optional type filter (worklog, note, artifact, etc.)
        workspace: Target workspace (uses default if not set)
    """
    params = {"limit": limit, "all": "true"}
    if type:
        params["tag"] = f"type:{type}"
    data = await _get("/", params, workspace=workspace)
    if isinstance(data, dict) and "service" in data:
        return "No documents found"
    if not data:
        return "No documents found"
    lines = [f"Recent {len(data)} documents:\n"]
    for doc in data:
        guid = doc.get("guid", "?")
        tags_str = ", ".join(doc.get("tags", []))
        preview = (doc.get("content", ""))[:120]
        created = doc.get("created_at", "")[:10] if doc.get("created_at") else ""
        lines.append(f"**{guid}** ({created}) [{tags_str}]")
        lines.append(f"  {preview}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def mesh_tags(prefix: str | None = None, limit: int = 50,
                    workspace: str | None = None) -> str:
    """List all unique tags with document counts.

    Args:
        prefix: Filter by prefix, e.g. "type:", "guid:", "topic:"
        limit: Max tags (default 50)
        workspace: Target workspace (uses default if not set)
    """
    params = {"limit": limit}
    if prefix:
        params["prefix"] = prefix
    data = await _get("/tags", params, workspace=workspace)
    tags = data.get("tags", [])
    if not tags:
        return f"No tags found{' with prefix ' + prefix if prefix else ''}"
    lines = [f"Tags ({len(tags)}):\n"]
    for t in tags:
        lines.append(f"  {t['tag']} ({t['count']} docs)")
    return "\n".join(lines)


@mcp.tool()
async def mesh_schema() -> str:
    """Show the tag schema: configured prefixes, auto-inference rules, defaults.

    The schema defines how documents are tagged. Auto-tags (date, source) are added
    on save. Type, project, and topic tags are inferred from similar documents.
    """
    schema = await _get_schema()
    return _schema_summary(schema)


# ──────────────────────────────────────────
# Resources
# ──────────────────────────────────────────

@mcp.resource("mesh://doc/{guid}")
async def get_document_resource(guid: str) -> str:
    """Read a Mesh document by GUID."""
    data = await _get(f"/{guid}")
    tags_str = ", ".join(data.get("tags", []))
    created = data.get("created_at", "")[:19] if data.get("created_at") else ""
    return (
        f"# {data.get('guid', guid)}\n"
        f"Tags: {tags_str}\n"
        f"Created: {created}\n\n"
        f"{data.get('content', '')}"
    )


# ──────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────

if __name__ == "__main__":
    # SSE heartbeat -- prevent proxy/NAT timeout disconnects
    try:
        sys.path.insert(0, "/srv/scripts")
        from shared.mcp_heartbeat import patch_sse_heartbeat
        patch_sse_heartbeat()
    except ImportError:
        pass

    transport = "stdio"
    port = int(os.environ.get("MESH_MCP_PORT", "8100"))

    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--transport" and i < len(sys.argv) - 1:
            transport = sys.argv[i + 1]
        elif arg == "--port" and i < len(sys.argv) - 1:
            port = int(sys.argv[i + 1])

    if transport in ("sse", "http"):
        mcp.settings.host = os.environ.get("MESH_MCP_HOST", "0.0.0.0")
        mcp.settings.port = port
        actual_transport = "streamable-http" if transport == "http" else "sse"
        mcp.run(transport=actual_transport)
    else:
        mcp.run(transport="stdio")
