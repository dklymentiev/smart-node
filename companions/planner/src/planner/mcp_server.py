"""MCP server -- stdio transport.

Wraps the same repo layer that REST and CLI use. Tools are defined in
``planner.mcp_tools``. On startup the server opens an asyncpg pool from
``DATABASE_URL`` and keeps it alive for the duration of the process.

Run as::

    python -m planner.mcp_server

This is the developer-facing default. Production deployments wire stdio to
their MCP client of choice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server
from mcp.server.lowlevel import NotificationOptions
from mcp.server.models import InitializationOptions

from planner import __version__
from planner.db import create_pool
from planner.mcp_tools import dispatch, tool_catalogue

logger = logging.getLogger(__name__)


def build_server() -> Server:
    """Construct an MCP Server with all planner tools registered.

    The server is stateless except for the asyncpg pool, which is attached as
    ``server._pool`` after :func:`run_stdio` opens it. Tool dispatch reads
    the pool off the server.
    """
    server: Server = Server("planner")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in tool_catalogue()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        pool = getattr(server, "_pool", None)
        if pool is None:
            raise RuntimeError("MCP server has no DB pool attached")
        try:
            result = await dispatch(
                pool, name, arguments or {},
                actor_id=os.environ.get("SN_AGENT_SLUG") or "system",
            )
        except Exception as exc:  # noqa: BLE001 -- surface as tool error to client
            return [types.TextContent(type="text", text=json.dumps({"error": str(exc)}))]
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    return server


async def run_stdio() -> None:
    logging.basicConfig(
        level=os.environ.get("PLANNER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = build_server()
    pool = await create_pool()
    server._pool = pool  # type: ignore[attr-defined]
    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="planner",
                    server_version=__version__,
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        await pool.close()


def main() -> None:
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
