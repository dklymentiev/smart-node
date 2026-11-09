"""MCP server for project_state, so a Claude CLI agent can call it.

project_state is an `api` tool in the catalog, and api tools run in brin's
function-calling loop. An agent on Claude CLI never goes through that loop: it has
Bash and the MCP servers in its .mcp.json, nothing else. Without this server the
tool's usage text reached the prompt and there was no function behind it, and the
agent answered that it had been told about a tool it was not given.

This server is a thin door to GET /api/project-state/collect on core-api. It adds
no logic: the collector, its window rules, its cache and its `notes` about sources
that were not read all stay in core-api.

Identity is the same as in mcp_messaging.py, for the same reason: the socket, where
SO_PEERCRED names the caller, and never the TCP address, which refuses an agent
identity. The MCP is spawned per agent session as that agent's OS user.

Usage:
    python mcp_project_state.py        # stdio (the only transport)
"""

import json
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

try:  # SDK 2.x
    from mcp.server import MCPServer as _McpServer
except ImportError:  # SDK 1.x
    from mcp.server.fastmcp import FastMCP as _McpServer

CORE_API_SOCKET = os.environ.get("SN_CORE_API_SOCKET", "/run/smart-node/api/core-api.sock")
# For a test bed with no socket, and nothing else. Deliberately NOT a fallback: over
# TCP an agent identity is refused, and the model would read that as "no data".
CORE_API_URL = os.environ.get("SN_CORE_API_URL", "")
# The collector walks the planner, mesh and Doci; a wide window can take a while.
TIMEOUT = float(os.environ.get("SN_PROJECT_STATE_TIMEOUT", "60"))

mcp = _McpServer("smart-node-project-state")


def _me() -> str:
    """The calling agent's slug, from the runtime, never from the model."""
    slug = (os.environ.get("SN_AGENT_SLUG") or "").strip()
    if not slug:
        slug = Path.cwd().name
    return slug


async def _get(path: str) -> str:
    if os.path.exists(CORE_API_SOCKET):
        transport, base = httpx.AsyncHTTPTransport(uds=CORE_API_SOCKET), "http://localhost"
    elif CORE_API_URL:
        transport, base = None, CORE_API_URL.rstrip("/")
    else:
        return json.dumps(
            {"error": "no core-api socket",
             "detail": f"{CORE_API_SOCKET} does not exist, and an agent identity "
                       f"is accepted only there."},
            ensure_ascii=False,
        )
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
        r = await client.get(f"{base}{path}", headers={"Remote-User": _me()})
        if r.status_code >= 400:
            return json.dumps(
                {"error": f"HTTP {r.status_code}", "detail": r.text[:400]},
                ensure_ascii=False,
            )
        return json.dumps(r.json() if r.content else {}, ensure_ascii=False)


@mcp.tool()
async def project_state(slug: str, since: str = "-72h", until: Optional[str] = None,
                        fresh: bool = False) -> str:
    """Collect the facts about one project over one time window, in one call.

    Use it when you are asked for a stand-up, a status or "what happened" on a
    project. The answer is raw facts, not prose: goals, linked tasks, what moved,
    comments, mentions from other projects, mesh memory, Doci documents and owner
    channels, plus waiting_for_owner (anything waiting on the owner, of any age) and
    where_it_stopped (an anchor when the window was quiet). Write the summary
    yourself from these facts and state only what the data shows. The field `notes`
    lists sources that were not read or were cut short; say so instead of passing a
    partial picture off as complete.

    Args:
        slug: the planner project slug, for example 'smart-node' or 'flint'.
        since: where the window starts: an ISO 8601 UTC time, a bare date
            ('2026-10-10') or a relative value ('-72h', '-7d', '-30m').
        until: where it ends; omit for now.
        fresh: skip the 30-second cache; leave it off unless the very latest is
            asked for.
    """
    query = {"slug": slug, "since": since}
    if until:
        query["until"] = until
    if fresh:
        query["fresh"] = "1"
    return await _get("/api/project-state/collect?" + urlencode(query))


if __name__ == "__main__":
    mcp.run()
