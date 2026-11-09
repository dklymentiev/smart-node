#!/usr/bin/env python3
"""
MCP Server for DOCI — Document Management System

Wraps DOCI HTTP API as MCP tools for Claude Desktop, Claude Code, and other MCP clients.

Usage:
    python mcp_server.py                              # stdio
    python mcp_server.py --transport sse              # SSE on port 8200

Environment:
    DOCI_API_URL   — DOCI API base URL (default: http://localhost/api)
    DOCI_API_KEY   — API key for authentication
"""
import os
import subprocess
import sys

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

# ──────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────


def _detect_doci_url() -> str:
    """Auto-detect DOCI container IP via docker inspect."""
    env_url = os.environ.get("DOCI_API_URL")
    if env_url:
        return env_url.rstrip("/")
    try:
        result = subprocess.run(
            ["docker", "inspect", "doci", "--format",
             "{{(index .NetworkSettings.Networks (index (keys .NetworkSettings.Networks) 0)).IPAddress}}"],
            capture_output=True, text=True, timeout=5
        )
        ip = result.stdout.strip()
        if ip:
            return f"http://{ip}/api"
    except Exception:
        pass
    return "http://localhost/api"


DOCI_URL = _detect_doci_url()


def _refresh_url_if_env_unset():
    """Re-run docker inspect to pick up new container IP after a container restart.

    Called from request wrappers when a connection error or 404 suggests the
    cached IP is stale. No-op if DOCI_API_URL env var is set (explicit override).
    """
    global DOCI_URL
    if os.environ.get("DOCI_API_URL"):
        return
    new_url = _detect_doci_url()
    if new_url and new_url != DOCI_URL:
        DOCI_URL = new_url
DOCI_KEY = os.environ.get("DOCI_API_KEY", "")
if not DOCI_KEY:
    key_file = os.path.join(os.path.dirname(__file__), ".doci-api-key")
    if os.path.exists(key_file):
        DOCI_KEY = open(key_file).read().strip()


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if DOCI_KEY:
        h["X-API-Key"] = DOCI_KEY
    return h


async def _request_with_retry(method: str, path: str, **kwargs) -> dict:
    """Make HTTP request; on connect error or 404, refresh container IP and retry once."""
    async def _do(url_path: str):
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.request(method, f"{DOCI_URL}{url_path}", **kwargs)
            r.raise_for_status()
            return r.json()
    try:
        return await _do(path)
    except (httpx.ConnectError, httpx.ReadError):
        _refresh_url_if_env_unset()
        return await _do(path)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            _refresh_url_if_env_unset()
            return await _do(path)
        raise


async def _get(path: str, params: dict = None) -> dict:
    return await _request_with_retry("GET", path, headers=_headers(), params=params)


async def _post(path: str, body: dict) -> dict:
    return await _request_with_retry("POST", path, headers=_headers(), json=body)


async def _post_form(path: str, data: dict) -> dict:
    h = {}
    if DOCI_KEY:
        h["X-API-Key"] = DOCI_KEY
    return await _request_with_retry("POST", path, headers=h, data=data)


async def _put(path: str, body: dict) -> dict:
    return await _request_with_retry("PUT", path, headers=_headers(), json=body)


async def _delete(path: str) -> dict:
    return await _request_with_retry("DELETE", path, headers=_headers())


# ──────────────────────────────────────────
# MCP Server
# ──────────────────────────────────────────

mcp = FastMCP(
    "DOCI",
    instructions=(
        "DOCI is a document management system with git-backed storage, PostgreSQL metadata, "
        "and AI-powered threads. Use doci_create/doci_get/doci_update/doci_delete for document CRUD. "
        "Use doci_list_folder to enumerate every document under a folder prefix when you do "
        "not know the file names. Use doci_thread to start discussions on documents, "
        "doci_threads to list them, doci_reply to continue conversations. Use doci_inbox "
        "for quick capture. Use doci_versions to see document version history. "
        "Documents are identified by GUIDs. Allowed path extensions: .md, .html, .svg "
        "(configurable via DOCI_ALLOWED_EXTENSIONS on the server). A path with no "
        "extension gets .md auto-appended; an explicit extension is kept as-is if it is "
        "in the allowlist, otherwise the create is rejected. doci_update accepts an "
        "optional path= to rename or change extension without losing the GUID."
    ),
    port=int(os.environ.get("DOCI_MCP_PORT", "8200")),
    transport_security=TransportSecuritySettings(
        allowed_hosts=[h.strip() for h in os.environ.get("DOCI_MCP_ALLOWED_HOSTS", "localhost,localhost:8200").split(",") if h.strip()],
    ),
)


# ──────────────────────────────────────────
# Tools — Documents
# ──────────────────────────────────────────

@mcp.tool()
async def doci_create(path: str, content: str, title: str | None = None,
                      tags: str | None = None) -> str:
    """Create a new document in DOCI.

    Args:
        path: Document path. If it has no extension, .md is appended. If it
            has an extension, the extension must be in the server allowlist
            (default: md, html, svg) or the create is rejected with 400.
            Examples: "projects/notes" -> "projects/notes.md",
            "decisions/architecture.html" stays as-is.
        content: Document content. Markdown for .md; raw HTML for .html;
            raw SVG XML for .svg. HTML/SVG is stored verbatim (no link
            rewriting) and rendered in a sandboxed iframe by the frontend.
        title: Optional title. For .md, extracted from the first # heading
            when omitted; for .html/.svg falls back to the filename.
        tags: Optional comma-separated tags.
    """
    body = {"path": path, "content": content}
    if title:
        body["title"] = title
    if tags:
        body["tags"] = tags
    data = await _post("/documents.php", body)
    if data.get("success"):
        return (
            f"Created: {data.get('guid', '?')}\n"
            f"Path: {data.get('path', path)}\n"
            f"Title: {data.get('title', '')}\n"
            f"URL: {data.get('url', '')}"
        )
    return f"Error: {data.get('error', 'Unknown error')}"


@mcp.tool()
async def doci_get(guid: str | None = None, path: str | None = None,
                   content: bool = False) -> str:
    """Get a document by GUID or path.

    Args:
        guid: Document GUID.
        path: Document path (alternative to GUID).
        content: Include document content in response (default False).
    """
    if not guid and not path:
        return "Error: provide either guid or path"
    params = {}
    if guid:
        params["guid"] = guid
    if path:
        params["path"] = path
    if content:
        params["content"] = "1"
    data = await _get("/documents.php", params)
    if not data.get("success"):
        return f"Error: {data.get('error', 'Not found')}"
    doc = data["document"]
    lines = [
        f"**{doc['guid']}**",
        f"Path: {doc['path']}",
        f"Title: {doc.get('title', '')}",
        f"Type: {doc.get('doc_type', '')}",
        f"Created: {doc.get('created_at', '')} by {doc.get('created_by', '')}",
        f"Updated: {doc.get('updated_at', '')} by {doc.get('updated_by', '')}",
        f"Tags: {doc.get('tags', '')}",
        f"URL: {doc.get('url', '')}",
    ]
    if doc.get("content") is not None:
        lines.append(f"\n---\n{doc['content']}")
    return "\n".join(lines)


@mcp.tool()
async def doci_update(guid: str, content: str | None = None, title: str | None = None,
                      summary: str | None = None, tags: str | None = None,
                      path: str | None = None) -> str:
    """Update a document's content, metadata, or path.

    Args:
        guid: Document GUID to update. GUID is stable across renames.
        content: New content (replaces existing).
        title: New title.
        summary: New summary.
        tags: New tags (comma-separated).
        path: New path to rename the document to. The GUID and version
            history are preserved. Extension may change (e.g. .md -> .html)
            as long as the new extension is in the server allowlist. Reject
            with 400 if the extension is missing or not allowed, or 409 if
            another document already lives at the target path.
    """
    body = {"guid": guid}
    if content is not None:
        body["content"] = content
    if title is not None:
        body["title"] = title
    if summary is not None:
        body["summary"] = summary
    if tags is not None:
        body["tags"] = tags
    if path is not None:
        body["path"] = path
    data = await _put("/documents.php", body)
    if data.get("success"):
        renamed = data.get("renamed_from")
        lines = [
            f"Updated: {data.get('guid', guid)}",
            f"Path: {data.get('path', '')}",
        ]
        if renamed:
            lines.append(f"Renamed from: {renamed}")
        lines.append(data.get("message", ""))
        return "\n".join(lines)
    return f"Error: {data.get('error', 'Unknown error')}"


@mcp.tool()
async def doci_delete(guid: str) -> str:
    """Delete a document (soft delete with cascade).

    Args:
        guid: Document GUID to delete.
    """
    data = await _delete(f"/documents.php?guid={guid}")
    if data.get("success"):
        return (
            f"Deleted: {data.get('guid', guid)}\n"
            f"Path: {data.get('path', '')}\n"
            f"Cascade deleted: {data.get('deleted_count', 1)} documents\n"
            f"Redirect to: {data.get('redirect_to', 'none')}"
        )
    return f"Error: {data.get('error', 'Unknown error')}"


@mcp.tool()
async def doci_search(path: str) -> str:
    """Find a document by its path.

    Args:
        path: Document path to search for.
    """
    data = await _get("/documents.php", {"path": path})
    if not data.get("success"):
        return f"Not found: {path}"
    doc = data["document"]
    return (
        f"**{doc['guid']}**\n"
        f"Path: {doc['path']}\n"
        f"Title: {doc.get('title', '')}\n"
        f"Type: {doc.get('doc_type', '')}\n"
        f"URL: {doc.get('url', '')}"
    )


@mcp.tool()
async def doci_list_folder(folder: str, limit: int = 500) -> str:
    """List every document under a folder path, recursively.

    Use this when you need to see what documents exist under a folder but
    do not know the file names. Returns each document's `guid`, `path`,
    `title` and `doc_type`, one per line, in path order.

    Args:
        folder: Folder prefix (e.g. "example-project/" or "system/decisions").
                Trailing slash is optional; the API normalises it. The
                match is recursive across sub-folders, so
                folder="example-project" also returns "example-project/notes/x.md".
        limit:  Max rows to return (default 500, 0 or negative = no cap).
                The server-side query itself is unbounded on this route;
                the cap here is only to keep MCP responses short.
    """
    params = {"folder": folder}
    data = await _get("/list.php", params)
    if not data.get("success"):
        return f"Error: {data.get('error', 'Unknown error')}"
    docs = data.get("documents") or []
    total = len(docs)
    if not docs:
        return f"No documents found under {folder!r}."
    truncated = False
    if limit is not None and limit > 0 and total > limit:
        docs = docs[:limit]
        truncated = True
    lines = [f"Folder: {folder} ({total} document{'s' if total != 1 else ''}"]
    if truncated:
        lines[0] += f", showing first {len(docs)}"
    lines[0] += "):\n"
    for d in docs:
        guid = d.get("guid", "?")
        path = d.get("path", "")
        title = d.get("title") or ""
        doc_type = d.get("doc_type") or ""
        lines.append(f"  **{guid}** — {path}")
        if title:
            lines.append(f"    Title: {title}")
        if doc_type and doc_type != "document":
            lines.append(f"    Type: {doc_type}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ──────────────────────────────────────────
# Tools — Threads
# ──────────────────────────────────────────

@mcp.tool()
async def doci_thread(document_guid: str, comment: str,
                      ai_response: bool = False, ai_model: str = "sonnet") -> str:
    """Create a new discussion thread on a document.

    Args:
        document_guid: GUID of the document to discuss.
        comment: Your comment/question about the document.
        ai_response: Request AI response in the thread (default False).
        ai_model: AI model to use: "sonnet" or "opus" (default "sonnet").
    """
    body = {
        "documentGuid": document_guid,
        "comment": comment,
    }
    if ai_response:
        body["requestAiResponse"] = True
        body["aiModel"] = ai_model
    data = await _post("/thread.php", body)
    if not data.get("success"):
        return f"Error: {data.get('error', 'Unknown error')}"
    thread = data.get("thread", {})
    version = data.get("version", {})
    lines = [
        f"Thread created: {thread.get('guid', '?')}",
        f"Title: {thread.get('title', '')}",
        f"URL: {thread.get('url', '')}",
        f"Version: {version.get('guid', '?')} ({version.get('url', '')})",
    ]
    if data.get("ai", {}).get("pending"):
        lines.append(f"AI response pending (model: {data['ai']['model']})")
    return "\n".join(lines)


@mcp.tool()
async def doci_threads(document_guid: str) -> str:
    """List all threads for a document.

    Args:
        document_guid: GUID of the document.
    """
    data = await _get("/thread.php", {"document": document_guid})
    if not data.get("success"):
        return f"Error: {data.get('error', 'Unknown error')}"
    threads = data.get("threads", [])
    if not threads:
        return "No threads found for this document."
    lines = [f"Threads ({len(threads)}):\n"]
    for t in threads:
        created = t.get("created_at", "")[:16] if t.get("created_at") else ""
        lines.append(f"  **{t['guid']}** — {t.get('title', 'Untitled')}")
        lines.append(f"    by {t.get('created_by', '?')} at {created}")
        if t.get("version_guid"):
            lines.append(f"    version: {t['version_guid']}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def doci_reply(thread_guid: str, message: str) -> str:
    """Reply to an existing thread.

    Args:
        thread_guid: GUID of the thread to reply to.
        message: Your reply message.
    """
    body = {
        "threadGuid": thread_guid,
        "message": message,
    }
    data = await _post("/thread-reply.php", body)
    if data.get("success"):
        status = "AI response pending" if data.get("pending") else "Reply added"
        return f"{status} in thread {thread_guid}"
    return f"Error: {data.get('error', 'Unknown error')}"


# ──────────────────────────────────────────
# Tools — Inbox
# ──────────────────────────────────────────

@mcp.tool()
async def doci_inbox(text: str, tags: str = "") -> str:
    """Quick capture — add a note to the inbox.

    Args:
        text: Content of the inbox item.
        tags: Optional comma-separated tags.
    """
    data = await _post_form("/inbox.php?action=add", {"content": text, "tags": tags})
    if data.get("success"):
        result = data.get("data", data)
        guid = result.get("guid", "?")
        path = result.get("path", "")
        return f"Inbox item created: {guid}\nPath: {path}"
    return f"Error: {data.get('error', 'Unknown error')}"


@mcp.tool()
async def doci_inbox_list(limit: int = 20) -> str:
    """List recent inbox items.

    Args:
        limit: Max items to return (default 20, max 100).
    """
    data = await _get("/inbox.php", {"action": "list", "limit": limit})
    if not data.get("success"):
        return f"Error: {data.get('error', 'Unknown error')}"
    items_data = data.get("data", data)
    items = items_data.get("items", []) if isinstance(items_data, dict) else []
    if not items:
        return "Inbox is empty."
    lines = [f"Inbox ({len(items)} items):\n"]
    for item in items:
        created = item.get("created_at", "")[:16] if item.get("created_at") else ""
        tags_list = item.get("tags", [])
        tags_str = ", ".join(tags_list) if isinstance(tags_list, list) else str(tags_list)
        title = (item.get("title", "") or "")[:80]
        lines.append(f"  **{item['guid']}** ({created})")
        if tags_str:
            lines.append(f"    tags: {tags_str}")
        lines.append(f"    {title}")
        lines.append("")
    return "\n".join(lines)


# ──────────────────────────────────────────
# Tools — Versions
# ──────────────────────────────────────────

@mcp.tool()
async def doci_versions(document_guid: str) -> str:
    """List all versions of a document.

    Args:
        document_guid: GUID of the document.
    """
    data = await _get("/versions.php", {"document": document_guid})
    if not data.get("success"):
        return f"Error: {data.get('error', 'Unknown error')}"
    original = data.get("original", {})
    versions = data.get("versions", [])
    lines = [
        f"Document: {original.get('guid', '?')} — {original.get('title', '')}",
        f"Path: {original.get('path', '')}",
        f"Total versions: {data.get('total_versions', 0)}\n",
    ]
    if not versions:
        lines.append("No versions yet.")
    else:
        for v in versions:
            created = v.get("created_at", "")[:16] if v.get("created_at") else ""
            lines.append(f"  **{v['guid']}** ({created})")
            lines.append(f"    by {v.get('created_by', '?')} — {v.get('thread_count', 0)} threads")
            lines.append(f"    URL: {v.get('url', '')}")
            lines.append("")
    return "\n".join(lines)


# ──────────────────────────────────────────
# Tools — Health
# ──────────────────────────────────────────

@mcp.tool()
async def doci_health() -> str:
    """Check DOCI system health."""
    try:
        data = await _get("/health.php")
        lines = []
        for key, value in data.items():
            lines.append(f"{key}: {value}")
        return "\n".join(lines) if lines else "OK"
    except Exception as e:
        return f"Health check failed: {e}"


# ──────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────

if __name__ == "__main__":
    transport = "stdio"
    port = int(os.environ.get("DOCI_MCP_PORT", "8200"))

    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--transport" and i < len(sys.argv) - 1:
            transport = sys.argv[i + 1]
        elif arg == "--port" and i < len(sys.argv) - 1:
            port = int(sys.argv[i + 1])

    if transport in ("sse", "http"):
        host = os.environ.get("DOCI_MCP_HOST")
        if not host:
            sys.stderr.write(
                "DOCI_MCP_HOST is required for sse/http transport "
                "(e.g. 127.0.0.1 for local-only, 0.0.0.0 to bind all interfaces)\n"
            )
            sys.exit(2)
        mcp.settings.host = host
        mcp.settings.port = port
        actual_transport = "streamable-http" if transport == "http" else "sse"
        mcp.run(transport=actual_transport)
    else:
        mcp.run(transport="stdio")
