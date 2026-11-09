#!/usr/bin/env python3
"""Reminders as an MCP server (stdio): set, list and cancel the agent's own.

The node keeps the clock (routes/reminders.py): when the time comes it wakes the
agent, which carries out its command and answers in the chat; the person sees every
waiting one in Studio's Calendar. One tool, because weak models choosing
between waking and a plain text lost the work (2026-10-31). A runtime's own task or
schedule list (Flint's add_task, for one) is seen by nobody and fires nowhere
on the node, which is why this exists.

Identity and place are not parameters. The caller is the agent whose OS user
spawned this process, proved by the kernel on core-api's unix socket (as in
mcp_secrets.py). The chat is the one this session serves: the gateway puts it in
SN_MESSAGE_ORIGIN_CONVERSATION ("desk:<id>"); without one, core-api picks the
agent's latest chat.

Registered by companions/core-api.sh under MCP-server slug `reminders`; every
agent gets it (agents.py renders it into each agent's .mcp.json).
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
CORE_API_URL = os.environ.get("SN_CORE_API_URL", "")  # test beds without a socket only
TIMEOUT = float(os.environ.get("SN_REMINDERS_TIMEOUT", "15"))

mcp = _McpServer("smart-node-reminders")


def _desk() -> Optional[str]:
    conv = os.environ.get("SN_MESSAGE_ORIGIN_CONVERSATION", "")
    return conv.split("desk:", 1)[1] if "desk:" in conv else None


async def _call(method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None) -> str:
    if os.path.exists(CORE_API_SOCKET):
        transport, base = httpx.AsyncHTTPTransport(uds=CORE_API_SOCKET), "http://localhost"
    elif CORE_API_URL:
        transport, base = None, CORE_API_URL.rstrip("/")
    else:
        return json.dumps({"error": f"{CORE_API_SOCKET} does not exist"})
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
        r = await client.request(method, f"{base}/api/reminders{path}", params=params, json=body,
                                 headers={"Content-Type": "application/json"})
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:300]
        return json.dumps({"error": f"HTTP {r.status_code}", "detail": detail}, ensure_ascii=False)
    return json.dumps(r.json() if r.content else {}, ensure_ascii=False, default=str)


@mcp.tool()
async def remind_me(command: str, in_minutes: Optional[int] = None, at: Optional[str] = None,
                    repeat: Optional[str] = None) -> str:
    """Wakes you at a later time. When the person asks for anything to happen later,
    call this tool: you cannot act later without it.

    command: the instruction you will carry out when you wake, written to yourself.
        Not the result: you do not have it yet.
    in_minutes: from now. Or at: ISO 8601 ("2026-10-30T09:00"), the person's own local
        time; list_reminders returns their `now`. One of the two, not both.
    repeat: "day", "week" or "month"; omit for once.
    """
    body = {"text": command, "in_minutes": in_minutes, "at": at, "repeat": repeat, "desk_id": _desk(), "to": "agent"}
    return await _call("POST", "", body={k: v for k, v in body.items() if v is not None})


@mcp.tool()
async def list_reminders() -> str:
    """Your reminders still waiting, oldest first, and the person's `now`."""
    return await _call("GET", "", params={"desk": _desk()} if _desk() else None)


@mcp.tool()
async def cancel_reminder(reminder_id: int) -> str:
    """Cancel one of your waiting reminders by its id (from list_reminders)."""
    return await _call("DELETE", f"/{int(reminder_id)}")


if __name__ == "__main__":
    import asyncio
    import inspect
    _r = mcp.run()
    if inspect.isawaitable(_r):
        asyncio.run(_r)
