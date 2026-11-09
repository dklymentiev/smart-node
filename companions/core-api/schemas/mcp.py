"""MCP server registry Pydantic models.

Backed by sn_config.mcp_servers + sn_core.tools (linked via mcp_server_id).
"""

from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field


class MCPServerBase(BaseModel):
    slug: str = Field(
        min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9-]{1,63}$",
        description="Stable identifier, also used as prefix for discovered tool names.",
    )
    name: str = Field(min_length=1, max_length=200)
    transport: Literal["stdio", "http", "sse"] = "stdio"
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Transport-specific: for stdio = {command, args, env}; for http/sse = {url, headers}.",
    )
    auth_secret_ref: str | None = Field(
        default=None,
        description="Path in sops store (e.g. 'github_mcp.token'). Resolved at dispatch-time.",
    )
    enabled: bool = True
    autoinstall: bool = False


class MCPServerCreate(MCPServerBase):
    # In every agent's .mcp.json, granted or not (messaging, reminders). Said by
    # the one who registers the server: the seed that used to mark it ran at
    # core-api's first start, before these rows existed, so a fresh node's agents
    # had neither and said reminders were "not installed".
    default_on: bool = False


class MCPServerUpdate(BaseModel):
    name: str | None = None
    transport: Literal["stdio", "http", "sse"] | None = None
    config: dict[str, Any] | None = None
    auth_secret_ref: str | None = None
    enabled: bool | None = None


class MCPServer(MCPServerBase):
    id: int
    status: Literal["unknown", "available", "broken"]
    last_discovered_at: datetime | None = None
    created_at: datetime


class MCPToolEntry(BaseModel):
    name: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    title: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class MCPToolsBulk(BaseModel):
    tools: list[MCPToolEntry]
