#!/usr/bin/env python3
"""The configurator's tools for agents, as an MCP server (stdio).

The agents API needs a tool in the agent's hand: curl through run_command is what
a model reaches for otherwise, and Flint's command guard refuses that with no
operator attached, so a configurator on Flint planned a team and created nobody
(2026-10-31).

Identity is not a parameter. The caller is the agent whose OS user spawned this
process, proved by the kernel on core-api's unix socket (as in mcp_secrets.py),
and core-api decides what it may do: agents.create and the rest are the admin
role's. Registered by companions/core-api.sh under MCP-server slug `agents`;
routes/agents.py renders it into the configurator's .mcp.json only.
"""

import json
import os
from typing import Optional

import httpx

try:  # SDK 2.x
    from mcp.server import MCPServer as _McpServer
except ImportError:  # SDK 1.x
    from mcp.server.fastmcp import FastMCP as _McpServer

CORE_API_SOCKET = os.environ.get("SN_CORE_API_SOCKET", "/run/smart-node/api/core-api.sock")
# Creating an agent makes its OS user and folder.
TIMEOUT = float(os.environ.get("SN_AGENTS_TIMEOUT", "120"))

mcp = _McpServer("smart-node-agents")


async def _request(method: str, path: str, body: Optional[dict] = None):
    transport = httpx.AsyncHTTPTransport(uds=CORE_API_SOCKET)
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
        r = await client.request(method, f"http://localhost{path}", json=body,
                                 headers={"Content-Type": "application/json"})
    try:
        return r.status_code, r.json() if r.content else {}
    except ValueError:
        return r.status_code, {"detail": r.text[:300]}


def _render(status: int, data: dict) -> str:
    if status >= 400:
        return json.dumps({"error": f"HTTP {status}", "detail": data.get("detail")}, ensure_ascii=False)
    return json.dumps(data, ensure_ascii=False, default=str)


@mcp.tool()
async def create_agent(slug: str, title: str, instructions: str, role: Optional[str] = None) -> str:
    """Create a new agent on this node. Only after the owner approved it.

    slug: its address, lowercase latin letters, digits and hyphens: "anna".
    title: its name as people see it.
    instructions: what it does and how, written to it: its own job, its rules, who
        it reports to. Only what the owner approved.
    role: its job in a few words: "SMM manager".

    Start it afterwards by sending it its first task as a message.
    """
    # The API's default model is a Claude one; the new agent runs on what this node runs
    # on, and in the same tenant: a node with several refuses one without.
    status, me = await _request("GET", f"/api/agents/{os.environ.get('SN_AGENT_SLUG', '')}")
    if status >= 400:
        return _render(status, me)
    body = {"slug": slug, "title": title, "role": role, "specialist_body": instructions, "model": me.get("model"),
            "tenant_id": me.get("tenant_id")}
    return _render(*await _request("POST", "/api/agents", {k: v for k, v in body.items() if v is not None}))


@mcp.tool()
async def update_agent(slug: str, title: Optional[str] = None, role: Optional[str] = None,
                       instructions: Optional[str] = None, enabled: Optional[bool] = None) -> str:
    """Change an agent: pass only what changes. instructions replaces its whole text."""
    body = {"title": title, "role": role, "specialist_body": instructions, "enabled": enabled}
    return _render(*await _request("PUT", f"/api/agents/{slug}", {k: v for k, v in body.items() if v is not None}))


@mcp.tool()
async def delete_agent(slug: str) -> str:
    """Remove an agent from the node. Only when the owner asked for it."""
    return _render(*await _request("DELETE", f"/api/agents/{slug}"))


if __name__ == "__main__":
    import asyncio
    import inspect
    _r = mcp.run()
    if inspect.isawaitable(_r):
        asyncio.run(_r)
