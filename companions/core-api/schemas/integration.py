"""Pydantic models for the Integrations registry (sn_config.integrations)."""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


KIND = Literal["system", "app", "external", "mcp-only"]
STATUS = Literal["available", "not-installed", "broken"]
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


class IntegrationBase(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    purpose: str = Field(default="", max_length=60)
    description: str = ""
    kind: KIND = "app"
    status: STATUS = "available"
    install_uri: str = ""
    install_path: str = ""
    # Base URL its `api` tools are called on. Without it a granted api tool is
    # advertised but not callable -- the dispatcher has nowhere to send it and
    # answers "no integration endpoint". The column existed and was readable
    # from the start; nothing could ever WRITE it, so every integration carried
    # an empty one and the whole api path of the catalogue was inert.
    endpoint: str = ""
    companion_slug: str | None = None
    metadata: dict = Field(default_factory=dict)
    # Feature flags declared by this integration (migration 014).
    # Known values: knowledge, memory, browser, orchestrator.
    capabilities: list[str] = Field(default_factory=list)


class IntegrationCreate(IntegrationBase):
    slug: str = Field(min_length=2, max_length=41)
    # Optional inline tool list -- companion installers can ship the whole
    # integration in one POST. Each entry is a partial ToolCreate (kind+title
    # required, name required; tags optional).
    tools: list[dict] | None = None

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("slug must match ^[a-z][a-z0-9-]{1,40}$")
        return v


class IntegrationUpdate(IntegrationBase):
    pass


class Integration(IntegrationBase):
    id: int
    slug: str
    endpoint: str = ""
    tools_count: int = 0
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
