"""Minimal MCP client for brin -- Phase 2 mcp dispatch.

Talks to the node's MCP servers (mesh / planner / doci / screenbox) using the
official `mcp` SDK. `tools/list` results are cached per server URL for the brin
process lifetime, so the tool catalogue is fetched once -- not per request. Only
the actual `tools/call` hits the server each time (that is the tool doing its
work). This is brin's egress to the tool servers; the agent/chat path stays on
the gateway HTTP API.

TWO TRANSPORTS, CHOSEN BY THE URL. This module spoke only HTTP+SSE, which is the
older of the two, and the node's own bridges are streamable HTTP: they answer an
SSE handshake with "400 Bad Request: Missing session ID". The effect was quiet
and large -- every wake logged `mcp list_tools failed` for mesh and doci, so an
agent answering through brin had neither memory nor documents in front of it,
while the SAME agent woken through the CLI had both (they are in .mcp.json).
Nobody could see it from the agent's side: asked whether memory was reachable,
it checked its own way and truthfully said yes.

A path ending in /sse is the old transport; anything else is streamable HTTP.
If the choice turns out wrong the other one is tried once, because guessing from
a URL is a heuristic and a server that moves should not go dark.
"""

import logging
from typing import Optional
from urllib.parse import urlparse

from mcp import ClientSession
from mcp.client.sse import sse_client

try:  # mcp >= 1.9 names it with underscores; older releases used one word
    from mcp.client.streamable_http import streamable_http_client as _http_client
except ImportError:  # pragma: no cover - depends on the SDK version installed
    try:
        from mcp.client.streamable_http import streamablehttp_client as _http_client
    except ImportError:
        _http_client = None

logger = logging.getLogger("brin.mcp_client")


def _is_sse_url(server_url: str) -> bool:
    """The old transport is identified by its own path, not by the host."""
    return urlparse(server_url).path.rstrip("/").endswith("/sse")


class _Connect:
    """Open `server_url` with the transport its path implies, falling back to
    the other one if that fails.

    The streamable client yields two streams, the SSE client the same two, so
    callers get a uniform (read, write) pair either way.
    """

    def __init__(self, server_url: str):
        self.url = server_url
        self._ctx = None

    def _open(self, use_sse: bool):
        if use_sse or _http_client is None:
            return sse_client(self.url)
        return _http_client(self.url)

    async def __aenter__(self):
        first = _is_sse_url(self.url)
        try:
            self._ctx = self._open(first)
            streams = await self._ctx.__aenter__()
        except Exception as exc:
            logger.info("MCP %s: %s transport failed (%s), trying the other one",
                        self.url, "sse" if first else "streamable-http", exc)
            self._ctx = self._open(not first)
            streams = await self._ctx.__aenter__()
        return streams[0], streams[1]

    async def __aexit__(self, *exc):
        if self._ctx is not None:
            return await self._ctx.__aexit__(*exc)
        return False

# server_url -> [{name, description, input_schema}]
_tools_cache: dict[str, list[dict]] = {}


async def list_tools(server_url: str, force: bool = False) -> list[dict]:
    """List a server's tools (cached). Raises on transport/protocol error."""
    if not force and server_url in _tools_cache:
        return _tools_cache[server_url]
    async with _Connect(server_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resp = await session.list_tools()
    # The SDK renamed Tool.inputSchema to input_schema. Read either, because a
    # node can be on any version of it and the failure mode is not obvious --
    # the transport succeeds, the tools come back, and then an AttributeError
    # lands in the same "list_tools failed" warning as a network error.
    tools = [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": (getattr(t, "input_schema", None)
                             or getattr(t, "inputSchema", None)
                             or {"type": "object", "properties": {}}),
        }
        for t in resp.tools
    ]
    _tools_cache[server_url] = tools
    logger.info("MCP list_tools %s -> %d tools", server_url, len(tools))
    return tools


async def call_tool(server_url: str, name: str, arguments: dict) -> str:
    """Invoke a tool and flatten the result content to text."""
    async with _Connect(server_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments or {})
    parts = []
    for block in (result.content or []):
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    out = "\n".join(p for p in parts if p) or "[no content]"
    if getattr(result, "isError", False):
        return f"[MCP ERROR] {out}"
    return out


def clear_cache() -> None:
    _tools_cache.clear()
