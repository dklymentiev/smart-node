"""Pydantic models for Tools registry entries."""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


KIND = Literal["cli", "mcp", "api", "builtin"]
STATUS = Literal["available", "broken", "stub"]
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")


class ToolBase(BaseModel):
    kind: KIND
    title: str = Field(min_length=1, max_length=120)
    description: str = ""
    install_uri: str = ""
    status: STATUS = "available"
    tags: list[str] = Field(default_factory=list)
    # Authoritative source-of-truth for "which integration owns me". NULL means
    # unmapped (should not happen after the phase 1 backfill, but allowed so
    # FK ON DELETE SET NULL can do its job safely).
    integration_slug: str | None = None
    # Usage contract / how-to, injected into the prompt of every agent granted
    # this tool. Settable at registration so there is no out-of-band SQL step.
    usage_md: str | None = None
    # What a chat shows while the tool runs ("Searching the web"); people read
    # this, never the technical name. Empty: the title is shown.
    doing: str | None = Field(default=None, max_length=80)
    # What makes an `api` row actually CALLABLE. Without these three a row is
    # advertised and does nothing: every runtime that dispatches from the
    # catalogue skips a row with no schema or no path, so it reaches an agent as
    # a capability it cannot use. They were unsettable through this API for the
    # catalogue's whole life, which is why the only callable tools on any node
    # were the ones somebody wrote in by hand.
    input_schema: dict | None = None      # JSON Schema object: what the tool takes
    http_method: str | None = None        # GET / POST / ... ; NULL for non-api kinds
    http_path: str | None = None          # path appended to the integration endpoint


class ToolCreate(ToolBase):
    name: str = Field(min_length=2, max_length=41)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("name must match ^[a-z][a-z0-9_-]{1,40}$")
        return v


class ToolUpdate(ToolBase):
    pass


class Tool(ToolCreate):
    id: int
    created_at: datetime
    updated_at: datetime
    created_by: str
    updated_by: str
